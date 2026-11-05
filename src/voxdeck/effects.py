"""Effect layer: plugin discovery and filter-chain graph generation.

A preset is a serial chain of nodes. Each node is a PipeWire builtin, a LADSPA
plugin or an LV2 plugin. Presets list what they need under "requires"; presets
whose plugins are missing are reported as unavailable instead of failing.
"""
import ctypes
import json
import os
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


class _Desc(ctypes.Structure):
    _fields_ = [("UniqueID", ctypes.c_ulong), ("Label", ctypes.c_char_p),
                ("Properties", ctypes.c_int), ("Name", ctypes.c_char_p),
                ("Maker", ctypes.c_char_p), ("Copyright", ctypes.c_char_p),
                ("PortCount", ctypes.c_ulong), ("PortDescriptors", ctypes.POINTER(ctypes.c_int)),
                ("PortNames", ctypes.POINTER(ctypes.c_char_p)), ("Hints", ctypes.c_void_p)]


def find_ladspa(name: str):
    for d in LADSPA_DIRS:
        p = d / (name if name.endswith(".so") else name + ".so")
        if p.exists():
            return p
    return None


@lru_cache(maxsize=None)
def ladspa_labels(name: str) -> dict:
    """label -> (first audio input port, first audio output port)."""
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
        ins, outs = [], []
        for n in range(d.PortCount):
            f = d.PortDescriptors[n]
            if f & 8:  # audio
                (ins if f & 1 else outs).append(d.PortNames[n].decode())
        out[d.Label.decode()] = (ins[0] if ins else None, outs[0] if outs else None)
        i += 1
    return out


@lru_cache(maxsize=None)
def lv2_available(uri: str) -> bool:
    for d in LV2_DIRS:
        if d.is_dir():
            for m in d.glob("*/manifest.ttl"):
                try:
                    if uri in m.read_text(errors="ignore"):
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


def load_presets() -> dict:
    """Builtin presets plus user presets from the config directory (id -> preset)."""
    presets = {}
    sources = [json.loads(DATA.read_text())]
    udir = config_dir() / "presets"
    if udir.is_dir():
        for f in sorted(udir.glob("*.json")):
            try:
                sources.append(json.loads(f.read_text()))
            except (OSError, ValueError):
                continue
    for src in sources:
        for p in (src if isinstance(src, list) else [src]):
            if "id" in p and "chain" in p:
                presets[p["id"]] = p
    for p in presets.values():
        p["missing"] = missing_requirements(p)
        p["available"] = not p["missing"]
    return presets


def _fmt(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(float(v)) if isinstance(v, float) else str(v)
    return json.dumps(str(v))


def build_graph(preset: dict) -> str:
    """Render the filter.graph body (a SPA-JSON string) for a preset."""
    chain = list(preset.get("chain", []))
    if preset.get("gain") is not None:
        chain.append({"type": "builtin", "label": "linear", "control": {"Mult": preset["gain"]}})
    if not chain:
        chain = [{"type": "builtin", "label": "copy"}]
    nodes, ports = [], []
    for i, n in enumerate(chain):
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
        ports.append((f"n{i}", pin, pout))
        line = f'{{ type = {t}  name = n{i}'
        if plugin:
            line += f'  plugin = {json.dumps(plugin)}'
        if t != "lv2":
            line += f'  label = {n["label"]}'
        ctl = n.get("control")
        if ctl:
            line += "  control = { " + " ".join(f'{json.dumps(k)} = {_fmt(v)}' for k, v in ctl.items()) + " }"
        nodes.append(line + " }")
    links = [f'{{ output = "{a[0]}:{a[2]}"  input = "{b[0]}:{b[1]}" }}'
             for a, b in zip(ports, ports[1:])]
    return ("{\n  nodes = [\n    " + "\n    ".join(nodes) + "\n  ]\n  links = [\n    "
            + "\n    ".join(links) + "\n  ]\n  inputs = [ \"%s:%s\" ]\n  outputs = [ \"%s:%s\" ]\n}"
            % (ports[0][0], ports[0][1], ports[-1][0], ports[-1][2]))
