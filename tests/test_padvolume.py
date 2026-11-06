import json
import os
import sys
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from voxdeck import board as boardmod, bundle  # noqa: E402
from voxdeck.board import Board, clamp_gain  # noqa: E402
from voxdeck.paths import config_dir  # noqa: E402


def test_clamp_gain():
    assert clamp_gain(0.5) == 0.5 and clamp_gain("1.25") == 1.25
    assert clamp_gain(-3) == 0.0 and clamp_gain(99) == 4.0
    assert clamp_gain(None) == 1.0 and clamp_gain("x") == 1.0 and clamp_gain(float("nan")) == 1.0


def test_level_keeps_semantics():
    assert Board.level({"gain": 0.5}, 0.8) == 0.4
    assert Board.level({}, 0.8) == 0.8                  # old boards without gain
    assert Board.level({"gain": 2.0}, 0.8) == 1.0       # capped at full scale like before
    assert Board.level({"gain": 0}, 1.0) == 0.0


def test_play_uses_gain_and_tracks_proc(monkeypatch=None):
    cmds = []

    class P:
        pid = 1

        def __init__(self, cmd, **kw):
            cmds.append(cmd)

        def poll(self):
            return None
    real = boardmod.subprocess.Popen
    boardmod.subprocess.Popen = P
    try:
        b = Board.__new__(Board)
        b.procs = []
        wav = config_dir() / "x.wav"
        wav.write_bytes(b"RIFF")
        clip = {"name": "a", "path": str(wav), "gain": 0.25}
        assert b.play(clip, 0.8)
        assert cmds[0][cmds[0].index("--volume") + 1] == "0.200"
        clip["gain"] = 0.5
        assert b.play(clip, 0.8)
        assert cmds[1][cmds[1].index("--volume") + 1] == "0.400"   # next play picks up the change
        assert b.procs[0].level == 0.2 and b.procs[0].clip is clip
    finally:
        boardmod.subprocess.Popen = real


def test_apply_live_only_touches_changed_streams():
    class P:
        def __init__(self, clip, level, pid):
            self.clip, self.level, self.pid = clip, level, pid

        def poll(self):
            return None
    calls = []
    dump = [{"type": "PipeWire:Interface:Client", "id": 7, "info": {"props": {"application.process.id": 41}}},
            {"type": "PipeWire:Interface:Node", "id": 99,
             "info": {"props": {"client.id": 7, "media.class": "Stream/Output/Audio"}}}]

    class R:
        stdout = json.dumps(dump)
    real_run = boardmod.subprocess.run

    def fake_run(cmd, **kw):
        if cmd[0] == "pw-dump":
            return R()
        calls.append(cmd)
    boardmod.subprocess.run = fake_run
    try:
        b = Board.__new__(Board)
        c = {"name": "a", "gain": 0.5}
        same = {"name": "b", "gain": 1.0}
        b.procs = [P(c, 1.0, 41), P(same, 1.0, 42)]
        assert b.apply_live(1.0) == 1
        assert calls[0][:4] == ["pw-cli", "set-param", "99", "Props"] and "0.5000" in calls[0][4]
        assert b.procs[0].level == 0.5
        assert b.apply_live(1.0) == 0 and len(calls) == 1      # nothing changed, no pw-dump work
    finally:
        boardmod.subprocess.run = real_run


def test_controller_debounced_save_and_roundtrip():
    from PySide6.QtCore import QCoreApplication
    from voxdeck.controller import Controller
    app = QCoreApplication.instance() or QCoreApplication([])
    import tempfile
    os.environ["XDG_RUNTIME_DIR"] = tempfile.mkdtemp(prefix="vdrun")   # never touch a real instance's socket
    ctl = Controller()
    try:
        ctl.board.clips = [{"name": "Boom", "path": "/x.wav", "color": "#112233"},
                           {"name": "Zip", "path": "/y.wav", "gain": 0.9}]
        ctl.board.save()
        mtime = ctl.board.file.stat().st_mtime_ns
        seen = []
        ctl.clip_gain_changed.connect(lambda i, g: seen.append((i, g)))
        for pct in range(100, 40, -1):                    # a slider drag: many changes
            ctl.set_clip_gain(0, pct / 100, from_ui=True)
        assert ctl.board.file.stat().st_mtime_ns == mtime  # nothing written mid-drag
        assert ctl.set_clip_gain("Zip", 0.37) == 0.37 and seen == [(1, 0.37)]
        end = time.time() + 1.5
        while time.time() < end and ctl.board.file.stat().st_mtime_ns == mtime:
            app.processEvents()
            time.sleep(0.01)
        saved = json.loads(ctl.board.file.read_text())
        assert saved[0]["gain"] == 0.41 and saved[1]["gain"] == 0.37
        assert ctl.set_clip_gain("nope", 1) is None
        # socket command and range handling
        r = ctl.handle({"cmd": "clip-volume", "clip": "Zip", "volume": 250})
        assert r["ok"] and r["volume"] == 250.0 and ctl.board.clips[1]["gain"] == 2.5
        assert ctl.handle({"cmd": "clip-volume", "clip": "Zip", "volume": 9999})["volume"] == 400.0
        assert ctl.handle({"cmd": "clip-volume", "clip": "Zip", "volume": "loud"})["ok"] is False
        assert ctl.handle({"cmd": "clip-volume", "clip": "Zip"})["volume"] == 400.0   # read only
        assert ctl.handle({"cmd": "clip-volume", "clip": "ghost", "volume": 5})["ok"] is False
        assert ctl.handle({"cmd": "clips"})["gains"] == [0.41, 4.0]
        # export / import keeps the value
        ctl.set_clip_gain("Zip", 0.37)
        wav = Path(config_dir()) / "zip.wav"
        wav.write_bytes(b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\x10\x00\x00\x00\x01\x00\x01\x00\x80\xbb\x00\x00\x00\x77\x01\x00\x02\x00\x10\x00data\x00\x00\x00\x00")
        out = Path(config_dir()) / "g.voxdeck"
        bundle.export_bundle(out, [], [dict(ctl.board.clips[1], path=str(wav))], 0.5)
        b = bundle.read_bundle(out)
        assert b["board"]["clips"][0]["gain"] == 0.37
        ctl.board.clips = []
        bundle.apply_import(b, ctl.presets, ctl.board)
        assert ctl.board.clips[0]["gain"] == 0.37
    finally:
        ctl.server.close()


def test_old_board_without_gain_loads():
    b = Board.__new__(Board)
    clip = {"name": "old", "path": "/nope.wav"}
    assert b.level(clip, 0.6) == 0.6 and "gain" not in clip
