"""Soundboard: clip list and playback through pw-play streams."""
import json
import subprocess
import wave
from pathlib import Path

import numpy as np

from .audio import MIX_SINK
from .paths import config_dir, data_dir

COLORS = ["#c0392b", "#d68910", "#27ae60", "#2980b9", "#8e44ad", "#16a085", "#7f8c8d", "#d35400"]


def make_samples() -> list:
    """Synthesize a few starter sounds so the board is not empty on first run."""
    d = data_dir() / "samples"
    d.mkdir(exist_ok=True)
    sr = 48000
    t = lambda s: np.linspace(0, s, int(sr * s), endpoint=False)  # noqa: E731
    rng = np.random.default_rng(1)
    x = t(0.9)
    sounds = {
        "Chime": (np.sin(2 * np.pi * 880 * x) + 0.5 * np.sin(2 * np.pi * 1320 * x)) * np.exp(-4 * x),
        "Buzzer": np.sign(np.sin(2 * np.pi * 110 * t(0.7))) * 0.5,
        "Whoosh": np.convolve(rng.standard_normal(sr), np.ones(40) / 40, "same")[: int(sr * 0.8)]
                  * np.sin(np.pi * np.linspace(0, 1, int(sr * 0.8))),
        "Thump": np.sin(2 * np.pi * (120 * np.exp(-9 * t(0.5))).cumsum() / sr) * np.exp(-7 * t(0.5)),
        "Rise": np.sin(2 * np.pi * np.cumsum(np.linspace(200, 1200, int(sr * 0.8))) / sr) * 0.6,
        "Laser": np.sin(2 * np.pi * np.cumsum(np.linspace(2200, 300, int(sr * 0.35))) / sr) * 0.6,
    }
    out = []
    for name, sig in sounds.items():
        sig = sig / max(1e-9, np.abs(sig).max()) * 0.7
        p = d / f"{name.lower()}.wav"
        with wave.open(str(p), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(sr)
            w.writeframes((sig * 32767).astype("<i2").tobytes())
        out.append({"name": name, "path": str(p)})
    return out


GAIN_MAX = 4.0   # same ceiling the .voxdeck import accepts


def clamp_gain(value) -> float:
    """Per-clip gain as a float in 0..GAIN_MAX (1.0 = unchanged); junk becomes 1.0."""
    try:
        g = float(value)
    except (TypeError, ValueError):
        return 1.0
    if g != g:
        return 1.0
    return round(max(0.0, min(GAIN_MAX, g)), 3)


class Board:
    def __init__(self):
        self.file = config_dir() / "board.json"
        self.clips = []
        self.procs = []
        self.load()

    def load(self):
        try:
            self.clips = json.loads(self.file.read_text())
        except (OSError, ValueError):
            self.clips = make_samples()
            for i, c in enumerate(self.clips):
                c["color"] = COLORS[i % len(COLORS)]
            self.save()

    def save(self):
        self.file.write_text(json.dumps(self.clips, indent=1))

    def find(self, ref):
        """Clip by index (int / digit string) or case-insensitive name."""
        if isinstance(ref, int) or (isinstance(ref, str) and ref.isdigit()):
            i = int(ref)
            return self.clips[i] if 0 <= i < len(self.clips) else None
        for c in self.clips:
            if c["name"].lower() == str(ref).lower():
                return c
        return None

    def play(self, clip, volume=1.0, monitor=False, monitor_target=None):
        if not Path(clip["path"]).exists():
            return False
        vol = f"{self.level(clip, volume):.3f}"
        targets = [MIX_SINK] + ([monitor_target or ""] if monitor else [])
        for t in targets:
            cmd = ["pw-play", "--volume", vol, "--media-role", "Game"]
            if t:
                cmd += ["--target", t]
            p = subprocess.Popen(cmd + [clip["path"]], stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
            p.clip, p.level = clip, float(vol)
            self.procs.append(p)
        return True

    @staticmethod
    def level(clip, volume=1.0) -> float:
        """Stream volume pw-play gets for this clip: board volume x clip gain, capped at 1."""
        return max(0.0, min(1.0, volume * clamp_gain(clip.get("gain", 1.0))))

    def set_gain(self, clip, gain) -> float:
        clip["gain"] = clamp_gain(gain)
        return clip["gain"]

    def apply_live(self, volume=1.0) -> int:
        """Push the current level to clips that are playing right now (one `pw-dump`, then one
        `pw-cli set-param` per changed stream). Returns how many streams were updated."""
        todo = [p for p in self.procs if p.poll() is None and getattr(p, "clip", None) is not None
                and abs(self.level(p.clip, volume) - p.level) > 5e-4]
        if not todo:
            return 0
        try:
            objs = json.loads(subprocess.run(["pw-dump"], capture_output=True, text=True, timeout=5).stdout)
        except (OSError, ValueError, subprocess.SubprocessError):
            return 0
        clients = {o["id"]: str(o["info"]["props"].get("application.process.id"))
                   for o in objs if o.get("type", "").endswith("Client") and o.get("info")}
        nodes = {}
        for o in objs:
            if o.get("type", "").endswith("Node") and o.get("info"):
                pr = o["info"].get("props", {})
                if pr.get("media.class") == "Stream/Output/Audio":
                    nodes[clients.get(pr.get("client.id"))] = o["id"]
        n = 0
        for p in todo:
            nid, lvl = nodes.get(str(p.pid)), self.level(p.clip, volume)
            if nid is None:
                continue
            try:
                subprocess.run(["pw-cli", "set-param", str(nid), "Props", f"{{ volume = {lvl:.4f} }}"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
            except (OSError, subprocess.SubprocessError):
                continue
            p.level = lvl
            n += 1
        return n

    def reap(self) -> int:
        self.procs = [p for p in self.procs if p.poll() is None]
        return len(self.procs)

    def stop_all(self):
        for p in self.procs:
            if p.poll() is None:
                p.terminate()
        self.procs = []
