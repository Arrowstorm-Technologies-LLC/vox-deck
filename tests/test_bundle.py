import io
import json
import os
import sys
import tempfile
import wave
import zipfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from voxdeck import bundle, effects  # noqa: E402


class FakeBoard:
    def __init__(self, clips=None):
        self.clips = list(clips or [])
        self.saved = 0

    def save(self):
        self.saved += 1


def _env():
    root = Path(tempfile.mkdtemp(prefix="vd-bundle-"))
    os.environ["XDG_CONFIG_HOME"] = str(root / "cfg")
    os.environ["XDG_DATA_HOME"] = str(root / "data")
    return root


def _wav(path, n=4800):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(48000)
        w.writeframes(b"\x01\x00" * n)
    return str(path)


def _zip(path, manifest, members=()):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("manifest.json", manifest if isinstance(manifest, str) else json.dumps(manifest))
        for name, data in members:
            z.writestr(name, data)
    return path


def _wav_bytes():
    b = io.BytesIO()
    with wave.open(b, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(48000)
        w.writeframes(b"\x00\x00" * 100)
    return b.getvalue()


def _fails(path):
    try:
        bundle.read_bundle(path)
    except bundle.BundleError:
        return True
    return False


def _board(clips):
    return {"format": "vox-deck", "version": 1, "board": {"clips": clips}}


def test_round_trip():
    root = _env()
    presets = effects.load_presets()
    mine = effects.save_user_preset(effects.apply_tuning(presets["deep"], {"pitch": 0.6}), "Gravel", set(presets))
    clips = [{"name": "Boom", "path": _wav(root / "boom.wav"), "color": "#112233", "gain": 0.5},
             {"name": "Gone", "path": str(root / "missing.wav")},
             {"name": "Text", "path": str(root / "notes.txt")}]
    (root / "notes.txt").write_text("hello")
    out = root / "x.voxdeck"
    rep = bundle.export_bundle(out, [mine], clips, 0.6)
    assert rep == {"presets": 1, "clips": 1, "skipped": ["Gone", "Text"]}
    b = bundle.read_bundle(out)
    assert [p["id"] for p in b["presets"]] == ["gravel"]
    assert b["board"]["clips"][0]["name"] == "Boom" and b["board"]["board_volume"] == 0.6
    # import on another "machine": new config and data dirs
    _env()
    board = FakeBoard([{"name": "Old", "path": "/x.wav"}])
    rep = bundle.apply_import(b, effects.load_presets(), board)
    assert rep["presets_added"] == ["gravel"] and rep["clips"] == 1 and board.saved == 1
    assert [c["name"] for c in board.clips] == ["Old", "Boom"]
    new = Path(board.clips[1]["path"])
    assert new.is_file() and new.parent.parent == Path(os.environ["XDG_DATA_HOME"]) / "vox-deck" / "clips"
    assert new.read_bytes() == Path(clips[0]["path"]).read_bytes()
    assert board.clips[1]["color"] == "#112233" and board.clips[1]["gain"] == 0.5
    loaded = effects.load_presets()["gravel"]
    assert loaded["user"] and loaded["chain"][0]["control"]["Pitch co-efficient"] == 0.6


def test_conflict_modes():
    root = _env()
    presets = effects.load_presets()
    mine = effects.save_user_preset(presets["deep"], "Mine", set(presets))
    out = root / "p.voxdeck"
    bundle.export_bundle(out, [dict(mine, name="Theirs"), presets["robot"]], None)
    b = bundle.read_bundle(out)
    assert bundle.conflicts(b, effects.load_presets()) == ["mine"]
    assert bundle.summary(b, effects.load_presets())["builtin_ids"] == ["robot"]
    rep = bundle.apply_import(b, effects.load_presets(), FakeBoard(), "skip", "skip")
    assert rep["presets_skipped"] == ["mine"] and effects.load_presets()["mine"]["name"] == "Mine"
    assert rep["presets_added"] == ["robot-2"]                      # a built-in id is never replaced
    rep = bundle.apply_import(b, effects.load_presets(), FakeBoard(), "rename", "skip")
    assert "theirs" in rep["presets_added"] and effects.load_presets()["mine"]["name"] == "Mine"
    assert effects.load_presets()["theirs"]["name"] == "Theirs (imported)"
    assert effects.load_presets()["robot-2"]["name"] == "Robot (imported)"
    rep = bundle.apply_import(b, effects.load_presets(), FakeBoard(), "replace", "skip")
    assert rep["presets_replaced"] == ["mine"] and effects.load_presets()["mine"]["name"] == "Theirs"
    assert effects.load_presets()["robot"]["name"] == "Robot" and not effects.load_presets()["robot"]["user"]


def test_board_replace_and_skip():
    root = _env()
    out = bundle.export_bundle(root / "b.voxdeck", [], [{"name": "A", "path": _wav(root / "a.wav")}], 0.3)
    assert out["clips"] == 1
    b = bundle.read_bundle(root / "b.voxdeck")
    board = FakeBoard([{"name": "Old", "path": "/x.wav"}])
    assert bundle.apply_import(b, effects.load_presets(), board, board_mode="skip")["clips"] == 0
    assert [c["name"] for c in board.clips] == ["Old"] and board.saved == 0
    rep = bundle.apply_import(b, effects.load_presets(), board, board_mode="replace")
    assert [c["name"] for c in board.clips] == ["A"] and rep["board_volume"] == 0.3


def test_rejects_hostile_archives():
    root = _env()
    w = _wav_bytes()
    z = lambda name, m, mem=(): _zip(root / name, m, mem)  # noqa: E731
    assert _fails(z("slip1.voxdeck", _board([{"name": "x", "file": "../evil.wav"}]), [("../evil.wav", w)]))
    assert _fails(z("slip2.voxdeck", _board([{"name": "x", "file": "clips/../../evil.wav"}]), [("clips/../../evil.wav", w)]))
    assert _fails(z("slip3.voxdeck", _board([{"name": "x", "file": "/etc/evil.wav"}]), [("/etc/evil.wav", w)]))
    assert _fails(z("slip4.voxdeck", _board([{"name": "x", "file": "clips\\..\\evil.wav"}])))
    assert _fails(z("missing.voxdeck", _board([{"name": "x", "file": "clips/a.wav"}])))
    assert _fails(z("ext.voxdeck", _board([{"name": "x", "file": "clips/a.sh"}]), [("clips/a.sh", b"#!/bin/sh")]))
    nm = root / "nomanifest.voxdeck"
    with zipfile.ZipFile(nm, "w") as zf:
        zf.writestr("clips/a.wav", w)
    assert _fails(nm)
    assert _fails(z("badjson.voxdeck", "{not json"))
    assert _fails(z("format.voxdeck", {"format": "other", "version": 1}))
    assert _fails(z("version.voxdeck", {"format": "vox-deck", "version": 2}))
    assert _fails(z("ladspa.voxdeck", {"format": "vox-deck", "version": 1, "presets": [
        {"id": "x", "chain": [{"type": "ladspa", "plugin": "../../home/u/evil", "label": "a"}]}]}))
    assert _fails(z("inject.voxdeck", {"format": "vox-deck", "version": 1, "presets": [
        {"id": "x", "chain": [{"type": "builtin", "label": 'copy } ] context.exec = [ {'}]}]}))
    not_zip = root / "plain.voxdeck"
    not_zip.write_text("hello")
    assert _fails(not_zip)
    assert _fails(root / "does-not-exist.voxdeck")
    # zip bomb: a tiny compressed member that claims to expand enormously
    bomb = root / "bomb.voxdeck"
    with zipfile.ZipFile(bomb, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(_board([{"name": "x", "file": "clips/a.wav"}])))
        zf.writestr("clips/a.wav", w + b"\0" * (20 << 20))
    try:
        bundle.read_bundle(bomb)
        raise AssertionError("bomb accepted")
    except bundle.BundleError as e:
        assert "compression" in str(e)


def test_fake_audio_is_refused_and_nothing_changes():
    root = _env()
    p = _zip(root / "fake.voxdeck", {"format": "vox-deck", "version": 1,
                                     "presets": [{"id": "fresh", "chain": []}],
                                     "board": {"clips": [{"name": "x", "file": "clips/a.wav"}]}},
             [("clips/a.wav", b"#!/bin/sh\necho owned\n")])
    b = bundle.read_bundle(p)           # structure is fine; content is checked while copying
    board = FakeBoard()
    try:
        bundle.apply_import(b, effects.load_presets(), board)
        raise AssertionError("fake audio accepted")
    except bundle.BundleError:
        pass
    assert board.clips == [] and "fresh" not in effects.load_presets()
    clips_root = Path(os.environ["XDG_DATA_HOME"]) / "vox-deck" / "clips"
    assert not clips_root.exists() or not any(clips_root.iterdir())


def test_unreferenced_members_are_never_written():
    root = _env()
    w = _wav_bytes()
    p = _zip(root / "extra.voxdeck", _board([{"name": "ok", "file": "clips/ok.wav"}]),
             [("clips/ok.wav", w), ("../../outside.txt", b"x"), ("clips/other.wav", w)])
    board = FakeBoard()
    bundle.apply_import(bundle.read_bundle(p), effects.load_presets(), board)
    assert len(board.clips) == 1 and not (root.parent / "outside.txt").exists()
    written = list((Path(os.environ["XDG_DATA_HOME"]) / "vox-deck" / "clips").rglob("*"))
    assert sorted(f.name for f in written if f.is_file()) == ["001-ok.wav"]


def test_audio_kind():
    assert bundle.audio_kind(_wav_bytes()[:16]) == "wav"
    assert bundle.audio_kind(b"fLaC\0\0\0\0") == "flac"
    assert bundle.audio_kind(b"OggS\0\2") == "ogg"
    assert bundle.audio_kind(b"ID3\4\0") == "mp3"
    assert bundle.audio_kind(b"\x7fELF\2\1\1") is None
