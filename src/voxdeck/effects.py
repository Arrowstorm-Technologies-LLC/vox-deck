"""Effect layer: plugin discovery and filter-chain graph generation.

A preset is a serial chain of nodes. Each node is a PipeWire builtin, a LADSPA
plugin or an LV2 plugin. Presets list what they need under "requires"; presets
whose plugins are missing are reported as unavailable instead of failing.
"""
import copy
import ctypes
import json
import math
import os
import re
from functools import lru_cache
from pathlib import Path

from .paths import config_dir

DATA = Path(__file__).with_name("builtin_presets.json")

LADSPA_DIRS = [Path(p) for p in os.environ.get("LADSPA_PATH", "").split(":") if p] + [
    Path("/usr/lib/ladspa"), Path("/usr/local/lib/ladspa"), Path.home() / ".ladspa"]
LV2_DIRS = [Path(p) for p in os.environ.get("LV2_PATH", "").split(":") if p] + [
    Path("/usr/lib/lv2"), Path("/usr/local/lib/lv2"), Path.home() / ".lv2"]

# builtin plugins shipped with PipeWire: label -> (input port, output port)
BUILTIN_PORTS = {"copy": ("In", "Out"), "linear": ("In", "Out")}
for _l in ("bq_lowpass", "bq_highpass", "bq_bandpass", "bq_lowshelf", "bq_highshelf",
           "bq_peaking", "bq_notch", "bq_allpass"):
    BUILTIN_PORTS[_l] = ("In", "Out")


class _Hint(ctypes.Structure):
    _fields_ = [("Descriptor", ctypes.c_int), ("Lower", ctypes.c_float), ("Upper", ctypes.c_float)]


class _Desc(ctypes.Structure):
    _fields_ = [("UniqueID", ctypes.c_ulong), ("Label", ctypes.c_char_p),
                ("Properties", ctypes.c_int), ("Name", ctypes.c_char_p),
                ("Maker", ctypes.c_char_p), ("Copyright", ctypes.c_char_p),
                ("PortCount", ctypes.c_ulong), ("PortDescriptors", ctypes.POINTER(ctypes.c_int)),
                ("PortNames", ctypes.POINTER(ctypes.c_char_p)), ("Hints", ctypes.POINTER(_Hint))]


def find_ladspa(name: str):
    for d in LADSPA_DIRS:
        p = d / (name if name.endswith(".so") else name + ".so")
        if p.exists():
            return p
    return None


@lru_cache(maxsize=None)
def ladspa_labels(name: str) -> dict:
    """label -> (first audio input port, first audio output port)."""
    return {k: v[0] for k, v in _ladspa_info(name).items()}


def ladspa_control_ranges(name: str, label: str) -> dict:
    """Control input port -> (lower, upper) bound, where the plugin declares both."""
    return _ladspa_info(name).get(label, (None, {}))[1]


@lru_cache(maxsize=None)
def _ladspa_info(name: str) -> dict:
    p = find_ladspa(name)
    if not p:
        return {}
    try:
        lib = ctypes.CDLL(str(p))
        lib.ladspa_descriptor.restype = ctypes.POINTER(_Desc)
    except (OSError, AttributeError):
        return {}
    out, i = {}, 0
    while True:
        d = lib.ladspa_descriptor(i)
        if not d:
            break
        d = d[0]
        ins, outs, ranges = [], [], {}
        for n in range(d.PortCount):
            f = d.PortDescriptors[n]
            if f & 8:  # audio
                (ins if f & 1 else outs).append(d.PortNames[n].decode())
            elif f & 4 and f & 1 and d.Hints:  # control input
                h = d.Hints[n]
                if h.Descriptor & 1 and h.Descriptor & 2:  # BOUNDED_BELOW | BOUNDED_ABOVE
                    ranges[d.PortNames[n].decode()] = (h.Lower, h.Upper)
        out[d.Label.decode()] = ((ins[0] if ins else None, outs[0] if outs else None), ranges)
        i += 1
    return out


_PREFIX_RE = re.compile(r"(?:@prefix|PREFIX)\s+([\w.-]*):\s*<([^>]*)>", re.I)


def manifest_declares(text: str, uri: str) -> bool:
    """True if a Turtle manifest names `uri`, as <full-uri> or as prefix:local."""
    if f"<{uri}>" in text:
        return True
    for pfx, ns in _PREFIX_RE.findall(text):
        if ns and uri.startswith(ns) and "/" not in uri[len(ns):]:
            name = re.escape(f"{pfx}:{uri[len(ns):]}")
            if re.search(rf"(?<![\w:.-]){name}(?![\w:.-])", text):
                return True
    return False


@lru_cache(maxsize=None)
def lv2_available(uri: str) -> bool:
    for d in LV2_DIRS:
        if d.is_dir():
            for m in d.glob("*/manifest.ttl"):
                try:
                    if manifest_declares(m.read_text(errors="ignore"), uri):
                        return True
                except OSError:
                    pass
    return False


def missing_requirements(preset: dict) -> list:
    miss = []
    for node in preset.get("chain", []):
        t = node.get("type")
        if t == "ladspa":
            if node.get("label") not in ladspa_labels(node["plugin"]):
                miss.append(node["plugin"])
        elif t == "lv2":
            if not lv2_available(node["plugin"]):
                miss.append(node.get("package") or node["plugin"])
    return sorted(set(miss))


BUILTIN_IDS = frozenset(p["id"] for p in json.loads(DATA.read_text()))


def user_preset_dir() -> Path:
    d = config_dir() / "presets"
    d.mkdir(exist_ok=True)
    return d


def load_presets() -> dict:
    """Builtin presets plus user presets from the config directory (id -> preset).

    User presets get "user": True and "file" (their path). Invalid user files are skipped;
    a user file can not replace a built-in (built-ins are read-only).
    """
    presets = {}
    for p in json.loads(DATA.read_text()):
        presets[p["id"]] = dict(p, user=False)
    udir = config_dir() / "presets"
    if udir.is_dir():
        for f in sorted(udir.glob("*.json")):
            try:
                src = json.loads(f.read_text())
            except (OSError, ValueError):
                continue
            for p in (src if isinstance(src, list) else [src]):
                try:
                    p = validate_preset(p)
                except ValueError:
                    continue
                if p["id"] in BUILTIN_IDS:
                    continue
                presets[p["id"]] = dict(p, user=True, file=str(f))
    for p in presets.values():
        p["missing"] = missing_requirements(p)
        p["available"] = not p["missing"]
    return presets


# ---------------------------------------------------------------- validation
# Preset files can come from imports, so they are untrusted: every string that ends up in a
# PipeWire config or a filesystem lookup is checked against a strict pattern.
ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,47}$")
PLUGIN_RE = re.compile(r"^[A-Za-z0-9_.+-]{1,64}$")          # LADSPA library basename, never a path
LABEL_RE = re.compile(r"^[A-Za-z0-9_.+-]{1,64}$")
URI_RE = re.compile(r"^https?://[A-Za-z0-9._~:/?#@!$&'()*+,;=%-]{1,200}$")
PORT_RE = re.compile(r"^[A-Za-z0-9 _.,:%()/=+*#&<>'-]{1,80}$")  # control / port names
PRESET_KEYS = {"id", "name", "description", "chain", "gain", "mix", "drywet", "tune", "extra", "base"}
NODE_KEYS = {"type", "plugin", "label", "control", "in", "out", "package"}
TUNE_KEYS = {"id", "name", "node", "control", "min", "max", "step", "unit", "scale"}
SCALES = ("lin", "db", "pct")
MAX_NODES = 16


def _num(v, what):
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v != v or v in (float("inf"), float("-inf")):
        raise ValueError(f"{what} must be a finite number")
    return float(v) if isinstance(v, float) else v


def _text(v, what, maxlen):
    if not isinstance(v, str) or len(v) > maxlen or any(ord(c) < 32 for c in v):
        raise ValueError(f"{what} must be a short single-line string")
    return v


def validate_preset(p) -> dict:
    """Return a clean copy of preset `p` or raise ValueError. No unknown keys survive."""
    if not isinstance(p, dict):
        raise ValueError("preset must be an object")
    extra = set(p) - PRESET_KEYS - {"user", "file", "missing", "available"}
    if extra:
        raise ValueError(f"unknown preset keys: {sorted(extra)}")
    if not isinstance(p.get("id"), str) or not ID_RE.match(p["id"]):
        raise ValueError("preset id must be lowercase letters, digits, - or _ (max 48)")
    out = {"id": p["id"], "name": _text(p.get("name", p["id"]), "name", 40) or p["id"],
           "description": _text(p.get("description", ""), "description", 300)}
    chain = p.get("chain", [])
    if not isinstance(chain, list) or len(chain) > MAX_NODES:
        raise ValueError("chain must be a list of at most 16 nodes")
    out["chain"] = []
    for i, n in enumerate(chain):
        if not isinstance(n, dict) or set(n) - NODE_KEYS:
            raise ValueError(f"node {i}: bad node")
        t = n.get("type")
        node = {"type": t}
        if t == "builtin":
            if n.get("label") not in BUILTIN_PORTS:
                raise ValueError(f"node {i}: unknown builtin")
            node["label"] = n["label"]
        elif t == "ladspa":
            if not isinstance(n.get("plugin"), str) or not PLUGIN_RE.match(n["plugin"]) or ".." in n["plugin"]:
                raise ValueError(f"node {i}: LADSPA plugin must be a plain library name")
            if not isinstance(n.get("label"), str) or not LABEL_RE.match(n["label"]):
                raise ValueError(f"node {i}: bad label")
            node["plugin"], node["label"] = n["plugin"], n["label"]
        elif t == "lv2":
            if not isinstance(n.get("plugin"), str) or not URI_RE.match(n["plugin"]):
                raise ValueError(f"node {i}: LV2 plugin must be an http(s) URI")
            node["plugin"] = n["plugin"]
            if "package" in n:
                node["package"] = _text(n["package"], "package", 60)
        else:
            raise ValueError(f"node {i}: type must be builtin, ladspa or lv2")
        for k in ("in", "out"):
            if k in n:
                if not isinstance(n[k], str) or not PORT_RE.match(n[k]):
                    raise ValueError(f"node {i}: bad port name")
                node[k] = n[k]
        ctl = n.get("control", {})
        if not isinstance(ctl, dict) or len(ctl) > 64:
            raise ValueError(f"node {i}: control must be an object")
        if ctl:
            node["control"] = {}
            for k, v in ctl.items():
                if not PORT_RE.match(k):
                    raise ValueError(f"node {i}: bad control name")
                node["control"][k] = _num(v, f"node {i} control")
        out["chain"].append(node)
    if "gain" in p:
        out["gain"] = _num(p["gain"], "gain")
        if not 0 <= out["gain"] <= 16:
            raise ValueError("gain out of range")
    if p.get("drywet"):
        out["drywet"] = True
    if "mix" in p:
        out["mix"] = min(1.0, max(0.0, float(_num(p["mix"], "mix"))))
    for k in ("extra",):
        if p.get(k):
            out[k] = True
    if "base" in p:
        out["base"] = _text(p["base"], "base", 48)
    tunes = p.get("tune", [])
    if not isinstance(tunes, list) or len(tunes) > 12:
        raise ValueError("tune must be a list of at most 12 controls")
    out["tune"], seen = [], {"gain", "mix"}
    for j, t in enumerate(tunes):
        if not isinstance(t, dict) or set(t) - TUNE_KEYS:
            raise ValueError(f"tune {j}: bad entry")
        if not isinstance(t.get("id"), str) or not ID_RE.match(t["id"]) or t["id"] in seen:
            raise ValueError(f"tune {j}: bad or duplicate id")
        seen.add(t["id"])
        node = t.get("node")
        if isinstance(node, bool) or not isinstance(node, int) or not 0 <= node < len(out["chain"]):
            raise ValueError(f"tune {j}: node must index the chain")
        if t.get("control") not in out["chain"][node].get("control", {}):
            raise ValueError(f"tune {j}: control must be set on that node")
        spec = {"id": t["id"], "name": _text(t.get("name", t["id"]), "tune name", 30), "node": node,
                "control": t["control"], "min": _num(t.get("min"), "min"), "max": _num(t.get("max"), "max"),
                "step": _num(t.get("step", 0.01), "step"), "unit": _text(t.get("unit", ""), "unit", 8),
                "scale": t.get("scale", "lin")}
        if spec["scale"] not in SCALES or not spec["min"] < spec["max"] or spec["step"] <= 0:
            raise ValueError(f"tune {j}: bad range or scale")
        out["tune"].append(spec)
    if not out["tune"]:
        del out["tune"]
    return out


# ---------------------------------------------------------------- tuning
GAIN_TUNE = {"id": "gain", "name": "Output gain", "node": "out", "min": -24.0, "max": 12.0,
             "step": 0.5, "unit": "dB", "scale": "db"}
MIX_TUNE = {"id": "mix", "name": "Effect mix", "node": "mix", "min": 0.0, "max": 100.0,
            "step": 1.0, "unit": "%", "scale": "pct"}


def to_raw(spec, v):
    s = spec.get("scale", "lin")
    return 10 ** (v / 20) if s == "db" else (v / 100 if s == "pct" else v)


def from_raw(spec, raw):
    s = spec.get("scale", "lin")
    if s == "db":
        return 20 * math.log10(raw) if raw > 1e-6 else -120.0
    return raw * 100 if s == "pct" else raw


def _raw_of(preset, spec):
    if spec["node"] == "out":
        return float(preset.get("gain", 1.0))
    if spec["node"] == "mix":
        return float(preset.get("mix", 1.0))
    return float(preset["chain"][spec["node"]]["control"][spec["control"]])


def tunables(preset) -> list:
    """Tunable controls of a preset with their current values (display units)."""
    specs = list(preset.get("tune", []))
    if preset.get("drywet"):
        specs.append(MIX_TUNE)
    specs.append(GAIN_TUNE)
    out = []
    for sp in specs:
        v = from_raw(sp, _raw_of(preset, sp))
        out.append(dict(sp, value=round(v, 4)))
    return out


def clamp(spec, v) -> float:
    v = _num(v, "value")
    return float(min(spec["max"], max(spec["min"], v)))


def apply_tuning(preset, values: dict) -> dict:
    """Copy of `preset` with tune values (id -> display value) written into it. Unknown ids ignored."""
    p = copy.deepcopy(preset)
    specs = {t["id"]: t for t in tunables(preset)}
    for tid, v in (values or {}).items():
        sp = specs.get(tid)
        if sp is None:
            continue
        raw = to_raw(sp, clamp(sp, v))
        if sp["node"] == "out":
            p["gain"] = raw
        elif sp["node"] == "mix":
            p["mix"] = raw
        else:
            p["chain"][sp["node"]]["control"][sp["control"]] = raw
    return p


def control_map(preset) -> dict:
    """Every control the graph sets, as "<node>:<control>" -> value (the keys of Props params)."""
    m = {}
    for i, n in enumerate(preset.get("chain", [])):
        for k, v in (n.get("control") or {}).items():
            m[f"n{i}:{k}"] = float(v)
    if preset.get("drywet"):
        mix = float(preset.get("mix", 1.0))
        m["mix:Gain 1"], m["mix:Gain 2"] = mix, 1.0 - mix
    m["out:Mult"] = float(preset.get("gain", 1.0))
    return m


def structure(preset) -> str:
    """Signature of everything that needs a new graph: plugins, ports and which controls exist.
    Two presets with the same signature differ only in control values, which can change live."""
    nodes = [(n["type"], n.get("plugin"), n.get("label"), n.get("in"), n.get("out"),
              sorted((n.get("control") or {}).keys())) for n in preset.get("chain", [])]
    return json.dumps([nodes, bool(preset.get("drywet"))])


def _fmt(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(float(v)) if isinstance(v, float) else str(v)
    return json.dumps(str(v))


def build_graph(preset: dict) -> str:
    """Render the filter.graph body (a SPA-JSON string) for a preset.

    Layout:  [dry copy] -> n0 -> n1 ... -> [mixer In 1 (wet) + In 2 (dry)] -> out (linear gain)
    The dry copy and mixer only exist for "drywet" presets; "out" always exists so the output
    gain can change live.
    """
    nodes, chain_ports = [], []
    for i, n in enumerate(preset.get("chain", [])):
        t = n["type"]
        if t == "builtin":
            pin, pout = BUILTIN_PORTS[n["label"]]
            plugin = None
        elif t == "ladspa":
            pin, pout = ladspa_labels(n["plugin"])[n["label"]]
            plugin = str(find_ladspa(n["plugin"]))
        elif t == "lv2":
            pin, pout = n.get("in", "in"), n.get("out", "out")
            plugin = n["plugin"]
        else:
            raise ValueError(f"unknown node type {t}")
        pin, pout = n.get("in", pin), n.get("out", pout)
        chain_ports.append((f"n{i}", pin, pout))
        nodes.append(_node(t, f"n{i}", n.get("label") if t != "lv2" else None, plugin, n.get("control")))
    drywet = bool(preset.get("drywet")) and chain_ports
    links = [(f"{a[0]}:{a[2]}", f"{b[0]}:{b[1]}") for a, b in zip(chain_ports, chain_ports[1:])]
    if drywet:
        mix = float(preset.get("mix", 1.0))
        nodes.insert(0, _node("builtin", "dry", "copy"))
        nodes.append(_node("builtin", "mix", "mixer", None, {"Gain 1": mix, "Gain 2": 1.0 - mix}))
        links += [("dry:Out", f"{chain_ports[0][0]}:{chain_ports[0][1]}"),
                  (f"{chain_ports[-1][0]}:{chain_ports[-1][2]}", "mix:In 1"),
                  ("dry:Out", "mix:In 2"), ("mix:Out", "out:In")]
        first = "dry:In"
    elif chain_ports:
        links.append((f"{chain_ports[-1][0]}:{chain_ports[-1][2]}", "out:In"))
        first = f"{chain_ports[0][0]}:{chain_ports[0][1]}"
    else:
        first = "out:In"
    nodes.append(_node("builtin", "out", "linear", None, {"Mult": float(preset.get("gain", 1.0)), "Add": 0.0}))
    link_txt = [f"{{ output = {json.dumps(a)}  input = {json.dumps(b)} }}" for a, b in links]
    return ("{\n  nodes = [\n    " + "\n    ".join(nodes) + "\n  ]\n  links = [\n    "
            + "\n    ".join(link_txt) + "\n  ]\n  inputs = [ %s ]\n  outputs = [ \"out:Out\" ]\n}"
            % json.dumps(first))


def _node(t, name, label=None, plugin=None, ctl=None) -> str:
    line = f"{{ type = {t}  name = {name}"
    if plugin:
        line += f"  plugin = {json.dumps(plugin)}"
    if label:
        line += f"  label = {json.dumps(label)}"
    if ctl:
        line += "  control = { " + " ".join(f"{json.dumps(k)} = {_fmt(v)}" for k, v in ctl.items()) + " }"
    return line + " }"
