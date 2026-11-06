import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from voxdeck import audio, effects  # noqa: E402

BUILTIN = {p["id"]: p for p in json.loads(effects.DATA.read_text())}


def test_builtins_validate_and_have_gain():
    for pid, p in BUILTIN.items():
        effects.validate_preset(p)
        ids = [t["id"] for t in effects.tunables(p)]
        assert ids[-1] == "gain", pid
        assert ("mix" in ids) == bool(p.get("drywet")), pid


def test_defaults_inside_ranges():
    for pid, p in BUILTIN.items():
        for t in effects.tunables(p):
            assert t["min"] <= round(t["value"], 6) <= t["max"], (pid, t["id"], t["value"])


def test_ranges_inside_ladspa_bounds():
    checked = 0
    for pid, p in BUILTIN.items():
        for t in p.get("tune", []):
            n = p["chain"][t["node"]]
            if n["type"] != "ladspa":
                continue
            bounds = effects.ladspa_control_ranges(n["plugin"], n["label"])
            if t["control"] not in bounds:
                continue   # plugin not installed here, or it declares no bounds
            lo, hi = bounds[t["control"]]
            for v in (t["min"], t["max"]):
                raw = effects.to_raw(t, v)
                assert lo - 1e-6 <= raw <= hi + 1e-6, (pid, t["id"], raw, lo, hi)
            checked += 1
    assert checked or not effects.find_ladspa("pitch_scale_1194")


def test_apply_tuning_writes_and_clamps():
    deep = BUILTIN["deep"]
    t = effects.apply_tuning(deep, {"pitch": 0.6, "gain": -6, "nope": 3})
    assert t["chain"][0]["control"]["Pitch co-efficient"] == 0.6
    assert abs(t["gain"] - 10 ** (-6 / 20)) < 1e-9
    assert deep["chain"][0]["control"]["Pitch co-efficient"] == 0.75   # original untouched
    t = effects.apply_tuning(deep, {"pitch": 99})
    assert t["chain"][0]["control"]["Pitch co-efficient"] == 1.0       # clamped to the range
    r = effects.apply_tuning(BUILTIN["robot"], {"mix": 25})
    cm = effects.control_map(r)
    assert cm["mix:Gain 1"] == 0.25 and cm["mix:Gain 2"] == 0.75


def test_tunables_round_trip():
    for p in BUILTIN.values():
        vals = {t["id"]: t["value"] for t in effects.tunables(p)}
        again = {t["id"]: t["value"] for t in effects.tunables(effects.apply_tuning(p, vals))}
        for k, v in vals.items():
            assert abs(again[k] - v) < 1e-3, (p["id"], k)


def test_structure_decides_live_vs_respawn():
    s = effects.structure
    assert s(BUILTIN["deep"]) == s(BUILTIN["chipmunk"])                 # only values differ
    assert s(BUILTIN["deep"]) == s(effects.apply_tuning(BUILTIN["deep"], {"pitch": 0.6, "gain": 3}))
    assert s(BUILTIN["deep"]) != s(BUILTIN["robot"])
    assert s(BUILTIN["robot"]) != s(dict(BUILTIN["robot"], drywet=False))


def test_graph_layout():
    g = effects.build_graph(BUILTIN["clean"])
    assert 'inputs = [ "out:In" ]' in g and 'outputs = [ "out:Out" ]' in g
    if effects.find_ladspa("ringmod_1188") and effects.find_ladspa("flanger_1191"):
        g = effects.build_graph(BUILTIN["robot"])
        assert 'label = "mixer"' in g and '"dry:Out"  input = "mix:In 2"' in g
        assert 'inputs = [ "dry:In" ]' in g
    for k in effects.control_map(BUILTIN["robot"]):
        assert k.split(":", 1)[0] in ("n0", "n1", "mix", "out")


class _Running:
    pid = 4242

    def poll(self):
        return None


def test_engine_live_update_without_respawn():
    e = audio.Engine.__new__(audio.Engine)
    e.voice, e.voice_key, e.voice_ctl, e.pending = _Running(), None, {}, {}
    e.voice_node, e.inflight, e.live_updates, e.last_error = None, None, 0, ""
    e.mon, e.mon_key, e.mon_vol, e.mon_sent, e.mon_linked, e.mon_node, e.mon_inflight = \
        None, None, 0.0, 0.0, None, None, None
    deep = BUILTIN["deep"]
    e.voice_key = (effects.structure(deep), "mic")
    e.voice_ctl = effects.control_map(deep)
    spawned, launched = [], []
    e._spawn = lambda *a: spawned.append(a)
    e.find_voice_node = lambda: 77

    class P:
        def __init__(self, args, **kw):
            launched.append(args)
            self.returncode = 0

        def poll(self):
            return 0

    real = audio.subprocess.Popen
    audio.subprocess.Popen = P
    try:
        e.set_voice(effects.apply_tuning(deep, {"pitch": 0.6}), "mic")
        assert not spawned
        assert launched and launched[0][:4] == ["pw-cli", "set-param", "77", "Props"]
        assert '"n0:Pitch co-efficient" 0.6' in launched[0][4] and "out:Mult" not in launched[0][4]
        assert e.flush() and not e.busy()
        launched.clear()
        e.set_voice(effects.apply_tuning(deep, {"pitch": 0.6}), "mic")   # nothing changed
        assert not launched and not spawned
    finally:
        audio.subprocess.Popen = real


def test_voice_conf_quotes_mic():
    conf = audio.voice_conf(BUILTIN["clean"], 'evil" } context.exec = [ { path = "/bin/sh" } ] {')
    assert 'target.object = "evil\\" }' in conf
