"""Cheap input/output level meters.

Uses libpulse (talking to pipewire-pulse) with PA_STREAM_PEAK_DETECT: the server
computes the peaks and sends about 25 floats per second per meter, so the app
never pulls full-rate audio. Runs on libpulse's own thread; the GUI just reads
the latest value. If libpulse is missing, meters report None.
"""
import ctypes
import ctypes.util
import math
import time

RATE = 25                       # peak values per second
FLOAT32LE = 5
CTX_READY, CTX_FAILED, CTX_TERMINATED = 4, 5, 6
ST_CREATING, ST_READY, ST_FAILED, ST_TERMINATED = 1, 2, 3, 4
DONT_MOVE, PEAK_DETECT, ADJUST_LATENCY = 0x200, 0x800, 0x2000
STALE = 0.6                     # seconds without data = silence
FLOOR = -90.0


class _Spec(ctypes.Structure):
    _fields_ = [("format", ctypes.c_int), ("rate", ctypes.c_uint32), ("channels", ctypes.c_uint8)]


class _Attr(ctypes.Structure):
    _fields_ = [("maxlength", ctypes.c_uint32), ("tlength", ctypes.c_uint32),
                ("prebuf", ctypes.c_uint32), ("minreq", ctypes.c_uint32),
                ("fragsize", ctypes.c_uint32)]


_CTX_CB = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p)
_ST_CB = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p)
_READ_CB = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p)


def _lib():
    name = ctypes.util.find_library("pulse") or "libpulse.so.0"
    pa = ctypes.CDLL(name)
    V, P, I, S = ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_char_p
    sig = {
        "pa_threaded_mainloop_new": (P, []), "pa_threaded_mainloop_get_api": (P, [V]),
        "pa_threaded_mainloop_start": (I, [V]), "pa_threaded_mainloop_stop": (None, [V]),
        "pa_threaded_mainloop_free": (None, [V]), "pa_threaded_mainloop_lock": (None, [V]),
        "pa_threaded_mainloop_unlock": (None, [V]),
        "pa_proplist_new": (P, []), "pa_proplist_sets": (I, [V, S, S]), "pa_proplist_free": (None, [V]),
        "pa_context_new_with_proplist": (P, [V, S, V]), "pa_context_set_state_callback": (None, [V, _CTX_CB, V]),
        "pa_context_connect": (I, [V, S, I, V]), "pa_context_get_state": (I, [V]),
        "pa_context_disconnect": (None, [V]), "pa_context_unref": (None, [V]),
        "pa_stream_new_with_proplist": (P, [V, S, ctypes.POINTER(_Spec), V, V]),
        "pa_stream_set_read_callback": (None, [V, _READ_CB, V]),
        "pa_stream_set_state_callback": (None, [V, _ST_CB, V]),
        "pa_stream_connect_record": (I, [V, S, ctypes.POINTER(_Attr), I]),
        "pa_stream_peek": (I, [V, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_size_t)]),
        "pa_stream_drop": (I, [V]), "pa_stream_get_state": (I, [V]),
        "pa_stream_get_device_name": (S, [V]),
        "pa_stream_disconnect": (I, [V]), "pa_stream_unref": (None, [V]),
    }
    for fn, (res, args) in sig.items():
        f = getattr(pa, fn)
        f.restype, f.argtypes = res, args
    return pa


def to_db(peak):
    return FLOOR if peak is None or peak <= 1e-9 else max(FLOOR, 20 * math.log10(peak))


class _Stream:
    def __init__(self, source):
        self.source = source
        self.ptr = None
        self.peak = 0.0
        self.t = 0.0
        self.bad = False          # server moved us to another device: do not trust
        self.ready = False
        self.cbs = ()


class PeakMeter:
    """meter.watch("mic", "<source name>"); meter.level("mic") -> dBFS or None."""

    def __init__(self):
        self.pa = None
        self.ml = self.ctx = None
        self.want = {}
        self.streams = {}
        self.ok = False
        self._ctx_cb = _CTX_CB(lambda c, u: None)
        try:
            self.pa = _lib()
        except (OSError, AttributeError):
            self.pa = None

    # -- lifecycle (call from the GUI thread)
    def _start(self):
        pa = self.pa
        self.ml = pa.pa_threaded_mainloop_new()
        props = pa.pa_proplist_new()
        pa.pa_proplist_sets(props, b"application.name", b"Vox Deck level meter")
        pa.pa_proplist_sets(props, b"application.id", b"xyz.arrowstorm.voxdeck.meter")
        self.ctx = pa.pa_context_new_with_proplist(pa.pa_threaded_mainloop_get_api(self.ml),
                                                   b"Vox Deck level meter", props)
        pa.pa_proplist_free(props)
        pa.pa_context_set_state_callback(self.ctx, self._ctx_cb, None)
        if pa.pa_context_connect(self.ctx, None, 1, None) < 0:  # 1 = NOAUTOSPAWN
            pa.pa_context_unref(self.ctx)
            pa.pa_threaded_mainloop_free(self.ml)
            self.ml = self.ctx = None
            return
        pa.pa_threaded_mainloop_start(self.ml)

    def close(self):
        if not self.ml:
            return
        pa = self.pa
        pa.pa_threaded_mainloop_lock(self.ml)
        for st in self.streams.values():
            self._drop_stream(st)
        self.streams = {}
        pa.pa_context_disconnect(self.ctx)
        pa.pa_context_unref(self.ctx)
        pa.pa_threaded_mainloop_unlock(self.ml)
        pa.pa_threaded_mainloop_stop(self.ml)
        pa.pa_threaded_mainloop_free(self.ml)
        self.ml = self.ctx = None
        self.ok = False

    def watch(self, key, source):
        """Meter `source` under `key`; None/"" stops that meter."""
        if source:
            self.want[key] = source
        else:
            self.want.pop(key, None)
        self.ensure()

    def unwatch_all(self):
        self.want = {}
        self.close()

    def ensure(self):
        """(Re)connect whatever should be running. Cheap; call about once a second."""
        if not self.pa:
            return
        if not self.want:
            if self.ml:
                self.close()
            return
        if not self.ml:
            self._start()
            if not self.ml:
                return
        pa = self.pa
        pa.pa_threaded_mainloop_lock(self.ml)
        state = pa.pa_context_get_state(self.ctx)
        if state in (CTX_FAILED, CTX_TERMINATED):   # server went away: start over next time
            pa.pa_threaded_mainloop_unlock(self.ml)
            self.close()
            return
        try:
            self.ok = state == CTX_READY
            if not self.ok:
                return
            for key in list(self.streams):
                st = self.streams[key]
                s = pa.pa_stream_get_state(st.ptr)
                if self.want.get(key) != st.source or s in (ST_FAILED, ST_TERMINATED) or st.bad:
                    self._drop_stream(st)
                    del self.streams[key]
            for key, src in self.want.items():
                if key not in self.streams:
                    st = self._new_stream(src)
                    if st:
                        self.streams[key] = st
        finally:
            pa.pa_threaded_mainloop_unlock(self.ml)

    # -- streams (mainloop lock held)
    def _new_stream(self, source):
        pa = self.pa
        st = _Stream(source)
        spec = _Spec(FLOAT32LE, RATE, 1)
        props = pa.pa_proplist_new()
        pa.pa_proplist_sets(props, b"media.name", b"level meter")
        pa.pa_proplist_sets(props, b"application.id", b"xyz.arrowstorm.voxdeck.meter")
        ptr = pa.pa_stream_new_with_proplist(self.ctx, b"level meter", ctypes.byref(spec), None, props)
        pa.pa_proplist_free(props)
        if not ptr:
            return None
        st.ptr = ptr

        def on_read(s, nbytes, _u, st=st):
            data, n = ctypes.c_void_p(), ctypes.c_size_t()
            while pa.pa_stream_peek(s, ctypes.byref(data), ctypes.byref(n)) == 0 and n.value:
                if data.value:
                    k = n.value // 4
                    if k:
                        arr = (ctypes.c_float * k).from_address(data.value)
                        st.peak = max(abs(v) for v in arr)
                        st.t = time.monotonic()
                pa.pa_stream_drop(s)

        def on_state(s, _u, st=st):
            if pa.pa_stream_get_state(s) == ST_READY:
                st.ready = True
                dev = pa.pa_stream_get_device_name(s)
                if dev is not None and dev.decode(errors="replace") != st.source:
                    st.bad = True     # fell back to another device; ensure() drops it

        st.cbs = (_READ_CB(on_read), _ST_CB(on_state))
        pa.pa_stream_set_read_callback(ptr, st.cbs[0], None)
        pa.pa_stream_set_state_callback(ptr, st.cbs[1], None)
        attr = _Attr(0xFFFFFFFF, 0xFFFFFFFF, 0xFFFFFFFF, 0xFFFFFFFF, 4)
        flags = DONT_MOVE | PEAK_DETECT | ADJUST_LATENCY
        if pa.pa_stream_connect_record(ptr, source.encode(), ctypes.byref(attr), flags) < 0:
            pa.pa_stream_unref(ptr)
            return None
        return st

    def _drop_stream(self, st):
        pa = self.pa
        pa.pa_stream_set_read_callback(st.ptr, _READ_CB(0), None)
        pa.pa_stream_set_state_callback(st.ptr, _ST_CB(0), None)
        if pa.pa_stream_get_state(st.ptr) in (ST_CREATING, ST_READY):
            pa.pa_stream_disconnect(st.ptr)
        pa.pa_stream_unref(st.ptr)

    # -- readings (any thread)
    def level(self, key):
        """Latest peak in dBFS, FLOOR when silent/stale, None when the meter is not running."""
        st = self.streams.get(key)
        if st is None or st.bad or not st.ready:
            return None
        if time.monotonic() - st.t > STALE:
            return FLOOR
        return round(to_db(st.peak), 1)

    def levels(self) -> dict:
        return {k: self.level(k) for k in self.want}

