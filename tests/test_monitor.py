import json
import os
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from voxdeck import audio  # noqa: E402


class _Child:
    def __init__(self, pid):
        self.pid, self.stopped = pid, False

    def poll(self):
        return 0 if self.stopped else None

    def terminate(self):
        self.stopped = True

    def wait(self, *a):
        return 0


def _engine():
    e = audio.Engine.__new__(audio.Engine)
    e.voice, e.voice_key, e.voice_ctl, e.pending = _Child(10), None, {}, {}
    e.voice_node, e.inflight, e.live_updates, e.last_error = None, None, 0, ""
    e.mon, e.mon_key, e.mon_vol, e.mon_sent, e.mon_linked, e.mon_node, e.mon_inflight = \
        None, None, 0.0, 0.0, None, None, None
    e.spawned, e.cmds = [], []
    n = [100]

    def spawn(name, text):
        n[0] += 1
        e.spawned.append((name, text))
        return _Child(n[0])
    e._spawn = spawn
    e._ports = lambda flag, prefix: [prefix + (":output_FL" if flag == "-o" else ":input_MONO")]
    e.find_node = lambda name, proc: 55
    return e


class _Run:
    def __init__(self, cmds):
        self.cmds = cmds

    def __call__(self, args, **kw):
        self.cmds.append(args)

        class R:
            returncode, stderr, stdout = 0, "", ""
        return R()


def _patched(e):
    runs = []
    audio.subprocess.run = _Run(runs)

    class P:
        def __init__(self, args, **kw):
            runs.append(args)

        def poll(self):
            return 0

        def wait(self):
            return 0
    audio.subprocess.Popen = P
    return runs


def test_monitor_conf_is_low_latency_and_isolated():
    c = audio.monitor_conf("", 0.5)
    assert "node.autoconnect = false" in c            # never grabs a microphone on its own
    assert "target.object" not in c                   # empty target = default output
    assert "resample" not in c and "delay" not in c.lower()
    assert c.count("node.latency = 512/48000") == 2 and "audio.rate = 48000" in c
    assert '"Mult" = 0.5' in c
    t = audio.monitor_conf('evil" } x', 1.0)
    assert 'target.object = "evil\\" } x"' in t


def test_monitor_links_voice_output_only_and_relinks_on_respawn():
    e = _engine()
    real_run, real_popen = audio.subprocess.run, audio.subprocess.Popen
    try:
        runs = _patched(e)
        e.set_monitor(True, "", 0.7)
        links = [r for r in runs if r[0] == "pw-link"]
        assert links == [["pw-link", f"{audio.VOICE_OUT}:output_FL", f"{audio.MON_IN}:input_MONO"]]
        assert not e.busy()
        runs.clear()
        e.set_monitor(True, "", 0.7)                 # nothing changed: no new process, no new link
        assert not runs and len(e.spawned) == 1
        e.voice = _Child(11)                         # voice chain replaced: link must be made again
        assert e.busy()
        e.flush()
        assert [r for r in runs if r[0] == "pw-link"] and not e.busy()
    finally:
        audio.subprocess.run, audio.subprocess.Popen = real_run, real_popen


def test_monitor_volume_is_live_and_target_change_respawns():
    e = _engine()
    real_run, real_popen = audio.subprocess.run, audio.subprocess.Popen
    try:
        runs = _patched(e)
        e.set_monitor(True, "", 0.7)
        runs.clear()
        e.set_monitor(True, "", 0.3)
        sp = [r for r in runs if r[0] == "pw-cli"]
        assert len(e.spawned) == 1 and sp and sp[0][:5] == ["pw-cli", "set-param", "55", "Props", sp[0][4]]
        assert json.dumps(audio.MON_GAIN) + " 0.3" in sp[0][4]
        e.set_monitor(True, "some_sink", 0.3)        # new target: new monitor child, voice untouched
        assert len(e.spawned) == 2 and e.voice.poll() is None
        assert 'target.object = "some_sink"' in e.spawned[1][1]
        e.set_monitor(False)
        assert e.mon is None
    finally:
        audio.subprocess.run, audio.subprocess.Popen = real_run, real_popen


def test_monitor_silent_when_voice_stopped():
    e = _engine()
    real_run, real_popen = audio.subprocess.run, audio.subprocess.Popen
    try:
        runs = _patched(e)
        e.voice = None                               # mic muted: no voice chain, nothing to link
        e.set_monitor(True, "", 0.7)
        assert not [r for r in runs if r[0] == "pw-link"] and not e.busy()
    finally:
        audio.subprocess.run, audio.subprocess.Popen = real_run, real_popen


def test_controller_settings_and_command():
    from PySide6.QtCore import QCoreApplication
    app = QCoreApplication.instance() or QCoreApplication([])
    from voxdeck import controller
    assert controller.DEFAULTS["voice_monitor"] is False
    assert 0 < controller.DEFAULTS["voice_monitor_volume"] <= 1
    c = controller.Controller.__new__(controller.Controller)
    c.s = json.loads(json.dumps(controller.DEFAULTS))
    calls = []
    c.apply_monitor = lambda *a, **k: calls.append(dict(c.s))
    c.save_timer = type("T", (), {"start": lambda self, ms: None})()
    c.set_voice_monitor(True)
    assert c.s["voice_monitor"] is True
    c.set_voice_monitor()                            # toggle
    assert c.s["voice_monitor"] is False
    c.set_voice_monitor_volume(5)
    assert c.s["voice_monitor_volume"] == 1.0
    c.set_voice_monitor_volume(-1)
    assert c.s["voice_monitor_volume"] == 0.0
    for bad in ("loud", None, True, float("nan")):
        try:
            c.set_voice_monitor_volume(bad)
        except ValueError:
            continue
        raise AssertionError(bad)
    del app
