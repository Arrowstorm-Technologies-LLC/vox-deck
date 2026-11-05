import sys
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from voxdeck import meter  # noqa: E402


def test_to_db():
    assert meter.to_db(1.0) == 0.0
    assert abs(meter.to_db(0.1) + 20.0) < 1e-9
    assert meter.to_db(0.0) == meter.FLOOR
    assert meter.to_db(None) == meter.FLOOR
    assert meter.to_db(1e-12) == meter.FLOOR


def _meter_with(stream):
    m = meter.PeakMeter.__new__(meter.PeakMeter)   # no libpulse needed
    m.want, m.streams = {"mic": stream.source}, {"mic": stream}
    return m


def test_level_states():
    st = meter._Stream("mic_source")
    m = _meter_with(st)
    assert m.level("mic") is None                # not connected yet
    st.ready = True
    assert m.level("mic") == meter.FLOOR          # connected, no data: silence
    st.peak, st.t = 0.5, time.monotonic()
    assert m.level("mic") == round(meter.to_db(0.5), 1)
    st.t -= meter.STALE + 0.1
    assert m.level("mic") == meter.FLOOR          # stale data counts as silence
    st.bad = True
    assert m.level("mic") is None                 # server put us on another device: hide it
    assert m.level("other") is None
    assert set(m.levels()) == {"mic"}


def test_without_libpulse():
    m = meter.PeakMeter.__new__(meter.PeakMeter)
    m.pa, m.ml, m.ctx, m.want, m.streams, m.ok = None, None, None, {}, {}, False
    m.watch("mic", "x")
    assert m.level("mic") is None
    m.unwatch_all()
