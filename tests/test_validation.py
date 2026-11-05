import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from voxdeck import effects  # noqa: E402

GOOD = {"id": "my-voice", "name": "My Voice", "gain": 1.0,
        "chain": [{"type": "ladspa", "plugin": "pitch_scale_1194", "label": "pitchScaleHQ",
                   "control": {"Pitch co-efficient": 0.9}}],
        "tune": [{"id": "pitch", "name": "Pitch", "node": 0, "control": "Pitch co-efficient",
                  "min": 0.5, "max": 2, "step": 0.01}]}


def rejects(p):
    try:
        effects.validate_preset(p)
    except ValueError:
        return True
    return False


def bad_node(**kw):
    return dict(GOOD, chain=[dict(GOOD["chain"][0], **kw)], tune=[])


def test_accepts_good():
    out = effects.validate_preset(GOOD)
    assert out["id"] == "my-voice" and out["tune"][0]["scale"] == "lin"


def test_rejects_paths_and_injection():
    assert rejects(bad_node(plugin="../../tmp/evil"))
    assert rejects(bad_node(plugin="/tmp/evil"))
    assert rejects(bad_node(plugin=".."))
    assert rejects(bad_node(label='x } context.exec = [ { path = "/bin/sh" } ] {'))
    assert rejects(bad_node(control={'a" = 1 } { "b': 1.0}))
    assert rejects(bad_node(control={"Pitch co-efficient": "1; rm"}))
    assert rejects(bad_node(control={"x": float("nan")}))
    assert rejects(bad_node(control={"x": True}))
    assert rejects(bad_node(**{"in": 'in" }'}))
    assert rejects(dict(GOOD, chain=[{"type": "lv2", "plugin": 'http://x/y" }'}], tune=[]))
    assert rejects(dict(GOOD, chain=[{"type": "lv2", "plugin": "file:///etc/passwd"}], tune=[]))
    assert rejects(dict(GOOD, chain=[{"type": "builtin", "label": "convolver"}], tune=[]))


def test_rejects_bad_shapes():
    assert rejects(dict(GOOD, id="../x"))
    assert rejects(dict(GOOD, id="UPPER"))
    assert rejects(dict(GOOD, name="two\nlines"))
    assert rejects(dict(GOOD, surprise=1))
    assert rejects(dict(GOOD, chain="nope"))
    assert rejects(dict(GOOD, chain=[{"type": "builtin", "label": "copy"}] * 17))
    assert rejects(dict(GOOD, gain=1e9))
    assert rejects([GOOD])
    tune = dict(GOOD["tune"][0])
    assert rejects(dict(GOOD, tune=[dict(tune, control="Missing")]))
    assert rejects(dict(GOOD, tune=[dict(tune, node=3)]))
    assert rejects(dict(GOOD, tune=[dict(tune, min=2, max=1)]))
    assert rejects(dict(GOOD, tune=[dict(tune, scale="exp")]))
    assert rejects(dict(GOOD, tune=[dict(tune, id="gain")]))      # reserved
    assert rejects(dict(GOOD, tune=[tune, tune]))                  # duplicate


def test_drops_runtime_keys():
    out = effects.validate_preset(dict(GOOD, user=True, file="/x", missing=[], available=True))
    assert not {"user", "file", "missing", "available"} & set(out)
