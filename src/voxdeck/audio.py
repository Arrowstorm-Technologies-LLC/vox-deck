"""Runtime audio graph. Everything is owned by this process.

Two child `pipewire -c <conf>` processes act as clients of the running
PipeWire daemon (the same way the stock filter-chain service does):

  mix   sink "vox_mix" -> source "Voice Mix"      (the virtual microphone)
  voice mic -> effect chain -> vox_mix

Nothing here touches the daemon, the session manager or the default devices.
Children die with the app (PDEATHSIG) and are cleaned up on exit.
"""
import ctypes
import json
import os
import signal
import subprocess
import time
from pathlib import Path

from . import effects
from .paths import runtime_dir

# Node names. VOXDECK_NODE_PREFIX lets a test instance run beside a real one without sharing nodes.
PREFIX = os.environ.get("VOXDECK_NODE_PREFIX", "vox")
MIX_SINK = f"{PREFIX}_mix"
MIX_SOURCE = "voice_mix" if PREFIX == "vox" else f"{PREFIX}_voice_mix"
MIX_DESC = "Voice Mix"
VOICE_IN = f"{PREFIX}_voice_in"
VOICE_OUT = f"{PREFIX}_voice_out"
MON_IN = f"{PREFIX}_monitor_in"
MON_OUT = f"{PREFIX}_monitor_out"
MON_GAIN = "monvol:Mult"       # control name of the monitor's volume node

HEADER = """context.properties = { log.level = 1 }
context.spa-libs = {
    audio.convert.* = audioconvert/libspa-audioconvert
    support.*       = support/libspa-support
}
context.modules = [
    { name = libpipewire-module-rt  args = { }  flags = [ ifexists nofail ] }
    { name = libpipewire-module-protocol-native }
    { name = libpipewire-module-client-node }
    { name = libpipewire-module-adapter }
"""


def _pdeathsig():
    try:
        ctypes.CDLL("libc.so.6").prctl(1, signal.SIGTERM)  # PR_SET_PDEATHSIG
    except OSError:
        pass


def mix_conf() -> str:
    return HEADER + f"""    {{ name = libpipewire-module-filter-chain
      args = {{
        node.description = "{MIX_DESC}"
        filter.graph = {{
          nodes = [
            {{ type = builtin name = cl label = copy }}
            {{ type = builtin name = cr label = copy }}
          ]
          inputs  = [ "cl:In" "cr:In" ]
          outputs = [ "cl:Out" "cr:Out" ]
        }}
        capture.props = {{
          node.name = "{MIX_SINK}"
          node.description = "Voice Mix input (Vox Deck)"
          media.class = Audio/Sink
          audio.rate = 48000
          audio.channels = 2
          audio.position = [ FL FR ]
          priority.session = 1
          priority.driver = 1
        }}
        playback.props = {{
          node.name = "{MIX_SOURCE}"
          node.description = "{MIX_DESC}"
          media.class = Audio/Source
          audio.rate = 48000
          audio.channels = 2
          audio.position = [ FL FR ]
          priority.session = 1
          node.latency = 512/48000
        }}
      }}
    }}
]
"""


def voice_conf(preset: dict, mic: str) -> str:
    props = ""
    if mic.endswith(".monitor"):
        mic, props = mic[:-len(".monitor")], "stream.capture.sink = true"
    graph = effects.build_graph(preset)
    return HEADER + f"""    {{ name = libpipewire-module-filter-chain
      args = {{
        node.description = "Vox Deck voice effects"
        filter.graph = {graph}
        capture.props = {{
          node.name = "{VOICE_IN}"
          target.object = {json.dumps(mic)}
          node.autoconnect = true
          node.dont-fallback = true
          node.dont-reconnect = true
          state.restore-target = false
          audio.rate = 48000
          audio.channels = 1
          audio.position = [ MONO ]
          node.latency = 512/48000
          {props}
        }}
        playback.props = {{
          node.name = "{VOICE_OUT}"
          target.object = "{MIX_SINK}"
          node.autoconnect = true
          node.dont-fallback = true
          node.dont-reconnect = true
          state.restore-target = false
          audio.rate = 48000
          audio.channels = 1
          audio.position = [ MONO ]
          node.latency = 512/48000
        }}
      }}
    }}
]
"""


def monitor_conf(target: str, volume: float) -> str:
    """Small child that plays the processed voice to `target` (empty = default output).

    Same 48 kHz mono and 512-sample quantum as the voice chain, no resampling, no delay line.
    Its input is linked to the voice chain's output by Engine (autoconnect is off so it never
    grabs a microphone). One `linear` node carries the monitor volume.
    """
    tgt = (f"target.object = {json.dumps(target)}\n          node.dont-fallback = true"
           if target else "")
    return HEADER + f"""    {{ name = libpipewire-module-filter-chain
      args = {{
        node.description = "Vox Deck voice monitor"
        filter.graph = {{
          nodes = [
            {{ type = builtin name = monvol label = linear control = {{ "Mult" = {float(volume)!r} "Add" = 0.0 }} }}
          ]
          inputs  = [ "monvol:In" ]
          outputs = [ "monvol:Out" ]
        }}
        capture.props = {{
          node.name = "{MON_IN}"
          node.autoconnect = false
          node.passive = true
          audio.rate = 48000
          audio.channels = 1
          audio.position = [ MONO ]
          node.latency = 512/48000
        }}
        playback.props = {{
          node.name = "{MON_OUT}"
          node.autoconnect = true
          node.dont-reconnect = true
          state.restore-target = false
          audio.rate = 48000
          audio.channels = 1
          audio.position = [ MONO ]
          node.latency = 512/48000
          {tgt}
        }}
      }}
    }}
]
"""


def list_mics() -> list:
    """Physical-looking capture sources: [(node_name, description)]. Our own are excluded."""
    try:
        out = subprocess.run(["pactl", "-f", "json", "list", "sources"], capture_output=True,
                             text=True, timeout=5).stdout
        items = json.loads(out)
    except (OSError, ValueError, subprocess.SubprocessError):
        return []
    res = []
    for s in items:
        n = s.get("name", "")
        if n.endswith(".monitor") or n == MIX_SOURCE:
            continue
        res.append((n, s.get("description", n)))
    return res


def default_source() -> str:
    try:
        return subprocess.run(["pactl", "get-default-source"], capture_output=True, text=True,
                              timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


class Engine:
    def __init__(self):
        self.dir = runtime_dir() / "vox-deck"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.mix = None
        self.voice = None
        self.voice_key = None       # (graph structure, mic): a change here needs a new voice child
        self.voice_ctl = {}         # control values the running chain has
        self.pending = {}           # control values still to send
        self.voice_node = None      # PipeWire id of vox_voice_in (holds the filter controls)
        self.inflight = None        # (pw-cli process, values) of the update being sent
        self.live_updates = 0       # count of live control updates (for tests / status)
        self.last_error = ""
        self.mon = None             # voice monitor child (separate from the voice child)
        self.mon_key = None         # target the monitor child was started for
        self.mon_vol = 0.0          # volume the child has / wants
        self.mon_sent = 0.0
        self.mon_linked = None      # (voice pid, monitor pid) the link was made for
        self.mon_node = None
        self.mon_inflight = None
        self.kill_stale()

    # -- process helpers
    def _spawn(self, conf_name: str, text: str) -> subprocess.Popen:
        conf = self.dir / conf_name
        conf.write_text(text)
        log = open(self.dir / (conf_name + ".log"), "w")
        return subprocess.Popen(["pipewire", "-c", str(conf)], stdout=log, stderr=log,
                                preexec_fn=_pdeathsig, start_new_session=True)

    @staticmethod
    def _stop(p):
        if p and p.poll() is None:
            p.terminate()
            try:
                p.wait(2)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait()

    def kill_stale(self):
        """Stop leftovers of an earlier crashed run (matched by our config path)."""
        for pid_dir in Path("/proc").iterdir():
            if not pid_dir.name.isdigit() or int(pid_dir.name) == os.getpid():
                continue
            try:
                cmd = (pid_dir / "cmdline").read_bytes().split(b"\0")
            except OSError:
                continue
            if len(cmd) > 2 and cmd[0].endswith(b"pipewire") and cmd[1] == b"-c" \
                    and cmd[2].startswith(str(self.dir).encode()):
                try:
                    os.kill(int(pid_dir.name), signal.SIGTERM)
                except OSError:
                    pass

    # -- public API
    def start_mix(self):
        if not self.running(self.mix):
            self.mix = self._spawn("mix.conf", mix_conf())

    @staticmethod
    def running(p) -> bool:
        return p is not None and p.poll() is None

    def set_voice(self, preset: dict | None, mic: str):
        """Run the voice chain for `preset`; preset None = microphone muted (chain stopped).

        If the running chain has the same structure (same plugins and control names) and the
        same mic, only control values differ: they are sent live with set-param, without a new
        child and without an audio gap. Anything else starts a new voice child.
        """
        if preset is None or not mic:
            self._stop_voice()
            return
        key = (effects.structure(preset), mic)
        ctl = effects.control_map(preset)
        if self.running(self.voice) and key == self.voice_key:
            want = {k: v for k, v in ctl.items() if self.voice_ctl.get(k) != v}
            self.pending.update(want)
            for k in list(self.pending):           # dropped back to what is already running
                if self.voice_ctl.get(k) == ctl.get(k):
                    del self.pending[k]
            self.flush()
            return
        self._stop_voice()
        try:
            self.voice = self._spawn("voice.conf", voice_conf(preset, mic))
            self.voice_key, self.voice_ctl = key, ctl
            self.last_error = ""
        except Exception as e:  # noqa: BLE001
            self.voice, self.voice_key = None, None
            self.last_error = str(e)

    def _stop_voice(self):
        self._stop(self.voice)
        self.voice, self.voice_key, self.voice_ctl, self.pending, self.voice_node = None, None, {}, {}, None
        if self.inflight:
            self.inflight[0].wait()
            self.inflight = None

    def find_voice_node(self):
        """Id of our voice-in node (owned by our voice child), or None if not registered yet."""
        return self.find_node(VOICE_IN, self.voice)

    @staticmethod
    def find_node(name, proc):
        """Id of node `name` owned by our child `proc`, or None if it is not registered yet."""
        if not Engine.running(proc):
            return None
        try:
            objs = json.loads(subprocess.run(["pw-dump"], capture_output=True, text=True, timeout=5).stdout)
        except (OSError, ValueError, subprocess.SubprocessError):
            return None
        clients = {o["id"]: o["info"]["props"].get("application.process.id")
                   for o in objs if o.get("type", "").endswith("Client") and o.get("info")}
        for o in objs:
            if o.get("type", "").endswith("Node") and o.get("info"):
                p = o["info"].get("props", {})
                if p.get("node.name") == name and \
                        str(clients.get(p.get("client.id"))) == str(proc.pid):
                    return o["id"]
        return None

    def flush(self) -> bool:
        """Push pending live updates (voice controls and monitor link/volume). False = call again."""
        a = self._flush_voice()
        return self.sync_monitor() and a

    def _flush_voice(self) -> bool:
        """Send pending control values to the running chain. False = not done yet (call again).

        One short-lived `pw-cli set-param` per batch, at most one in flight; values that arrive
        meanwhile are merged and sent next, so slider drags coalesce instead of queueing up.
        """
        if self.inflight:
            proc, sent = self.inflight
            if proc.poll() is None:
                return False
            self.inflight = None
            if proc.returncode != 0:          # node vanished or id stale: look it up again
                self.voice_node = None
                for k, v in sent.items():
                    self.pending.setdefault(k, v)
                    self.voice_ctl.pop(k, None)
        if not self.pending:
            return True
        if not self.running(self.voice):
            return False
        if self.voice_node is None:
            self.voice_node = self.find_voice_node()
            if self.voice_node is None:
                return False
        sent, self.pending = self.pending, {}
        params = " ".join(f"{json.dumps(k)} {float(v)!r}" for k, v in sent.items())
        try:
            proc = subprocess.Popen(["pw-cli", "set-param", str(self.voice_node), "Props",
                                     f"{{ params = [ {params} ] }}"], stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, preexec_fn=_pdeathsig)
        except OSError as e:
            self.last_error = f"pw-cli: {e}"
            self.pending = {**sent, **self.pending}
            return False
        self.inflight = (proc, sent)
        self.voice_ctl.update(sent)
        self.live_updates += 1
        return False

    # -- voice monitor: processed voice -> headset, own child so the voice chain is never touched
    def set_monitor(self, on: bool, target: str = "", volume: float = 0.7):
        """Hear the processed voice. The child only restarts when the target changes; volume is live."""
        volume = max(0.0, min(1.0, float(volume)))
        if not on:
            self._stop_monitor()
            return
        if self.running(self.mon) and self.mon_key == target:
            self.mon_vol = volume
        else:
            self._stop_monitor()
            try:
                self.mon = self._spawn("monitor.conf", monitor_conf(target, volume))
                self.mon_key, self.mon_vol, self.mon_sent = target, volume, volume
            except Exception as e:  # noqa: BLE001
                self.mon = None
                self.last_error = str(e)
        self.sync_monitor()

    def _stop_monitor(self):
        self._stop(self.mon)
        self.mon, self.mon_key, self.mon_linked, self.mon_node = None, None, None, None
        if self.mon_inflight:
            self.mon_inflight.wait()
            self.mon_inflight = None

    @staticmethod
    def _ports(flag, prefix):
        try:
            out = subprocess.run(["pw-link", flag], capture_output=True, text=True, timeout=5).stdout
        except (OSError, subprocess.SubprocessError):
            return []
        return [ln.strip() for ln in out.splitlines() if ln.strip().startswith(prefix + ":")]

    def monitor_wanted(self) -> bool:
        return self.running(self.mon) and self.running(self.voice)

    def sync_monitor(self) -> bool:
        """Link the voice output to the monitor input and push a changed volume. False = call again.

        The link belongs to the voice child, so it is made again whenever either child is new.
        """
        if self.mon_inflight and self.mon_inflight.poll() is None:
            return False
        self.mon_inflight = None
        if not self.monitor_wanted():
            return True
        pair = (self.voice.pid, self.mon.pid)
        if self.mon_linked != pair:
            src, dst = self._ports("-o", VOICE_OUT), self._ports("-i", MON_IN)
            if not src or not dst:
                return False
            r = subprocess.run(["pw-link", src[0], dst[0]], capture_output=True, text=True)
            if r.returncode != 0 and "exists" not in r.stderr:
                return False
            self.mon_linked = pair
        if abs(self.mon_vol - self.mon_sent) > 1e-9:
            if self.mon_node is None:
                self.mon_node = self.find_node(MON_IN, self.mon)
                if self.mon_node is None:
                    return False
            try:
                self.mon_inflight = subprocess.Popen(
                    ["pw-cli", "set-param", str(self.mon_node), "Props",
                     f"{{ params = [ {json.dumps(MON_GAIN)} {float(self.mon_vol)!r} ] }}"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, preexec_fn=_pdeathsig)
            except OSError:
                return False
            self.mon_sent = self.mon_vol
            return False
        return True

    def monitor_busy(self) -> bool:
        return self.monitor_wanted() and (self.mon_linked != (self.voice.pid, self.mon.pid)
                                          or abs(self.mon_vol - self.mon_sent) > 1e-9
                                          or self.mon_inflight is not None)

    def busy(self) -> bool:
        return bool(self.pending or self.inflight) or self.monitor_busy()

    def healthy(self) -> dict:
        return {"mix": self.running(self.mix), "voice": self.running(self.voice)}

    def stop(self):
        self._stop_monitor()
        self._stop_voice()
        self._stop(self.mix)
        self.mix = None
        for f in self.dir.glob("*"):
            try:
                f.unlink()
            except OSError:
                pass
