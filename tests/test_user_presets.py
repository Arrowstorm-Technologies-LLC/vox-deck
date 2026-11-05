import json
import os
import sys
import tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from voxdeck import effects  # noqa: E402


def _fresh_config():
    os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="vd-test-")
    return effects.user_preset_dir()


def test_slug_and_unique_ids():
    assert effects.slug("My Deep Voice!") == "my-deep-voice"
    assert effects.slug("***") == "preset"
    assert effects.unique_id("Deep", set()) == "deep-2"           # built-in ids are taken
    assert effects.unique_id("x", {"x", "x-2"}) == "x-3"
    assert effects.ID_RE.match(effects.unique_id("Ünïcödé " * 20, set()))


def test_save_load_rename_delete():
    d = _fresh_config()
    presets = effects.load_presets()
    tuned = effects.apply_tuning(presets["deep"], {"pitch": 0.6, "gain": -3})
    rec = effects.save_user_preset(tuned, "  Gravel   Voice ", set(presets))
    assert rec["id"] == "gravel-voice" and rec["name"] == "Gravel Voice" and rec["base"] == "deep"
    assert (d / "gravel-voice.json").exists()
    presets = effects.load_presets()
    p = presets["gravel-voice"]
    assert p["user"] and not presets["deep"]["user"]
    assert {t["id"]: t["value"] for t in effects.tunables(p)} == {"pitch": 0.6, "gain": -3.0}
    assert effects.structure(p) == effects.structure(presets["deep"])   # switching to it is live
    effects.rename_user_preset(p, "Gravel")
    assert effects.load_presets()["gravel-voice"]["name"] == "Gravel"
    effects.delete_user_preset(effects.load_presets()["gravel-voice"])
    assert "gravel-voice" not in effects.load_presets() and not list(d.glob("*.json"))


def test_builtins_read_only():
    _fresh_config()
    deep = effects.load_presets()["deep"]
    for fn in (lambda: effects.rename_user_preset(deep, "X"), lambda: effects.delete_user_preset(deep)):
        try:
            fn()
        except ValueError:
            continue
        raise AssertionError("built-in was editable")


def test_user_file_cannot_shadow_builtin_and_bad_files_skipped():
    d = _fresh_config()
    (d / "shadow.json").write_text(json.dumps({"id": "deep", "name": "Evil", "chain": []}))
    (d / "broken.json").write_text("{nope")
    (d / "evil.json").write_text(json.dumps({"id": "evil", "chain": [
        {"type": "ladspa", "plugin": "../../tmp/x", "label": "y"}]}))
    (d / "multi.json").write_text(json.dumps([{"id": "a1", "chain": []}, {"id": "a2", "chain": []}]))
    presets = effects.load_presets()
    assert presets["deep"]["name"] == "Deep" and "evil" not in presets
    assert presets["a1"]["user"] and presets["a2"]["user"]
    effects.delete_user_preset(presets["a1"])                # one entry out of a list file
    left = json.loads((d / "multi.json").read_text())
    assert [p["id"] for p in left] == ["a2"]


def test_bad_names():
    _fresh_config()
    deep = effects.load_presets()["deep"]
    for bad in ("", "   ", "x" * 41):
        try:
            effects.save_user_preset(deep, bad, set())
        except ValueError:
            continue
        raise AssertionError(f"accepted {bad!r}")
