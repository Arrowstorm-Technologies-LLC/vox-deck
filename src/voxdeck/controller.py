"""Application core: state, preset switching, board and the control socket server."""
import json
import os
import time

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtNetwork import QLocalServer

from . import audio, bundle, effects
from .board import Board, clamp_gain
from .meter import PeakMeter
from .paths import config_dir, socket_path

DEFAULTS = {"mic": "", "preset": "clean", "enabled": True, "muted": False,
            "board_volume": 0.8, "monitor": False, "monitor_target": "", "tuning": {},
            "voice_monitor": False, "voice_monitor_volume": 0.7}


class Controller(QObject):
    changed = Signal()
    presets_changed = Signal()
    board_changed = Signal()
    clip_gain_changed = Signal(int, float)   # pad index, new gain (keeps sliders in step)

    def __init__(self):
        super().__init__()
        self.sfile = config_dir() / "settings.json"
        self.s = json.loads(json.dumps(DEFAULTS))
        try:
            self.s.update(json.loads(self.sfile.read_text()))
        except (OSError, ValueError):
            pass
        if not isinstance(self.s.get("tuning"), dict):
            self.s["tuning"] = {}
        self.presets = effects.load_presets()
        if self.s["preset"] not in self.presets:
            self.s["preset"] = "clean"
        if not self.s["mic"]:
            self.s["mic"] = audio.default_source()
        self.board = Board()
        self.engine = audio.Engine()
        self.subs = []
        self.server = QLocalServer(self)
        QLocalServer.removeServer(socket_path())
        self.server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
        self.server.newConnection.connect(self._accept)
        self.server.listen(socket_path())
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(1000)
        self._last = None
        self.save_timer = QTimer(self)          # slider drags save once they settle
        self.save_timer.setSingleShot(True)
        self.save_timer.timeout.connect(self.save)
        self.board_save_timer = QTimer(self)    # pad slider drags write board.json once they settle
        self.board_save_timer.setSingleShot(True)
        self.board_save_timer.setInterval(400)
        self.board_save_timer.timeout.connect(self.board.save)
        self.live_gain_timer = QTimer(self)     # and reach clips that are playing, at most ~12 times a second
        self.live_gain_timer.setSingleShot(True)
        self.live_gain_timer.setInterval(80)
        self.live_gain_timer.timeout.connect(lambda: self.board.apply_live(self.s["board_volume"]))
        self.flush_timer = QTimer(self)         # retries live updates while a new chain registers
        self.flush_timer.timeout.connect(self._flush)
        # level meters run only while someone looks: the window, a levels subscriber or a recent query
        self.meter = PeakMeter()
        self.meter_window = False
        self.meter_lease = 0.0
        self.level_subs = []
        self.level_timer = QTimer(self)
        self.level_timer.timeout.connect(self._push_levels)

    # -- state
    def start(self):
        self.engine.start_mix()
        self.apply_voice()

    def save(self):
        self.sfile.write_text(json.dumps(self.s, indent=1))

    def effective(self, pid: str) -> dict:
        """Preset `pid` with the user's live tuning applied."""
        return effects.apply_tuning(self.presets[pid], self.s["tuning"].get(pid, {}))

    def apply_voice(self, save=True):
        pid = self.s["preset"] if self.s["enabled"] else "clean"
        self.engine.set_voice(None if self.s["muted"] else self.effective(pid), self.s["mic"])
        self.apply_monitor(save=False, emit=False)
        if self.engine.busy() and not self.flush_timer.isActive():
            self.flush_timer.start(30)
        self.update_meters()
        if save:
            self.save()
        else:
            self.save_timer.start(800)
        self._emit()

    def apply_monitor(self, save=True, emit=True):
        """Start/stop/update the voice monitor child from settings (it never touches the voice child)."""
        self.engine.set_monitor(self.s["voice_monitor"], self.s["monitor_target"], self.s["voice_monitor_volume"])
        if self.engine.busy() and not self.flush_timer.isActive():
            self.flush_timer.start(30)
        if save:
            self.save()
        if emit:
            self._emit()

    def set_voice_monitor(self, on=None):
        self.s["voice_monitor"] = (not self.s["voice_monitor"]) if on is None else bool(on)
        self.apply_monitor()

    def set_voice_monitor_volume(self, v):
        if isinstance(v, bool) or not isinstance(v, (int, float)) or v != v:
            raise ValueError("volume must be a number from 0 to 1")
        self.s["voice_monitor_volume"] = max(0.0, min(1.0, float(v)))
        self.apply_monitor(save=False)
        self.save_timer.start(800)

    def _flush(self):
        self.engine.flush()
        if not self.engine.busy() or not self.engine.running(self.engine.voice):
            self.flush_timer.stop()

    # -- live tuning
    def params(self, pid=None) -> list:
        pid = pid or self.s["preset"]
        return effects.tunables(self.effective(pid))

    def set_param(self, tid: str, value, pid=None):
        """Set tunable `tid` of preset `pid` (default: current) live. Returns the clamped value."""
        pid = pid or self.s["preset"]
        if pid not in self.presets:
            raise ValueError(f"unknown preset {pid!r}")
        spec = {t["id"]: t for t in effects.tunables(self.presets[pid])}.get(tid)
        if spec is None:
            raise ValueError(f"preset {pid!r} has no tunable {tid!r}")
        v = effects.clamp(spec, value)
        self.s["tuning"].setdefault(pid, {})[tid] = v
        self.apply_voice(save=False)
        return v

    # -- user presets
    def reload_presets(self):
        self.presets = effects.load_presets()
        if self.s["preset"] not in self.presets:
            self.s["preset"] = "clean"
        for pid in list(self.s["tuning"]):
            if pid not in self.presets:
                del self.s["tuning"][pid]
        self.presets_changed.emit()

    def save_preset(self, name: str, pid=None) -> str:
        """Save preset `pid` (default: current) with its live tuning as a new user preset and
        select it. Same plugins, so the switch is live. Returns the new id."""
        pid = pid or self.s["preset"]
        rec = effects.save_user_preset(self.effective(pid), name, set(self.presets))
        self.s["tuning"].pop(pid, None)          # the values now live in the new preset
        self.reload_presets()
        if pid == self.s["preset"]:
            self.s["preset"] = rec["id"]
        self.apply_voice()
        return rec["id"]

    def rename_preset(self, pid: str, name: str):
        if pid not in self.presets:
            raise ValueError(f"unknown preset {pid!r}")
        effects.rename_user_preset(self.presets[pid], name)
        self.reload_presets()
        self._emit()

    def delete_preset(self, pid: str):
        if pid not in self.presets:
            raise ValueError(f"unknown preset {pid!r}")
        p = self.presets[pid]
        effects.delete_user_preset(p)
        if self.s["preset"] == pid:
            base = p.get("base")
            self.s["preset"] = base if base in self.presets and base != pid else "clean"
        self.s["tuning"].pop(pid, None)
        self.reload_presets()
        self.apply_voice()

    # -- import / export
    def export(self, path, board=True, presets="user") -> dict:
        """presets: "user" (all of yours), "none", or a list of preset ids."""
        if presets == "user":
            sel = [p for p in self.presets.values() if p["user"]]
        elif presets in ("none", None):
            sel = []
        else:
            missing = [i for i in presets if i not in self.presets]
            if missing:
                raise ValueError(f"unknown presets: {', '.join(missing)}")
            sel = [self.presets[i] for i in presets]
        if not sel and not board:
            raise ValueError("nothing to export")
        return bundle.export_bundle(path, sel, self.board.clips if board else None,
                                    self.s["board_volume"] if board else None)

    def inspect(self, path) -> dict:
        return bundle.summary(bundle.read_bundle(path), self.presets)

    def import_bundle(self, path, preset_mode="rename", board_mode="append") -> dict:
        b = bundle.read_bundle(path)
        rep = bundle.apply_import(b, self.presets, self.board, preset_mode, board_mode)
        if "board_volume" in rep:
            self.s["board_volume"] = rep["board_volume"]
        self.reload_presets()
        self.board_changed.emit()
        self.apply_voice()
        return rep

    def reset_params(self, pid=None):
        self.s["tuning"].pop(pid or self.s["preset"], None)
        self.apply_voice()

    def set_preset(self, ref) -> bool:
        ids = [k for k, p in self.presets.items() if p["available"]]
        if ref in ("next", "prev"):
            cur = ids.index(self.s["preset"]) if self.s["preset"] in ids else 0
            ref = ids[(cur + (1 if ref == "next" else -1)) % len(ids)]
        if ref not in self.presets or not self.presets[ref]["available"]:
            return False
        self.s["preset"] = ref
        self.apply_voice()
        return True

    def set_enabled(self, on=None):
        self.s["enabled"] = (not self.s["enabled"]) if on is None else bool(on)
        self.apply_voice()

    def set_muted(self, on=None):
        self.s["muted"] = (not self.s["muted"]) if on is None else bool(on)
        self.apply_voice()

    def set_mic(self, source: str):
        self.s["mic"] = source
        self.apply_voice()

    def set_opt(self, key, value):
        self.s[key] = value
        self.save()
        if key == "monitor_target":
            self.apply_monitor()
        self._emit()

    def play(self, ref) -> bool:
        clip = self.board.find(ref)
        if not clip:
            return False
        ok = self.board.play(clip, self.s["board_volume"], self.s["monitor"],
                             self.s["monitor_target"] or None)
        self._emit()
        return ok

    def set_clip_gain(self, ref, gain, from_ui=False):
        """Set one pad's volume (gain, 1.0 = as recorded). Next play uses it, clips already
        playing follow within about 80 ms, board.json is written 400 ms after the last change."""
        clip = self.board.find(ref)
        if not clip:
            return None
        g = self.board.set_gain(clip, gain)
        self.board_save_timer.start()
        if self.board.reap():
            self.live_gain_timer.start()
        if not from_ui:
            self.clip_gain_changed.emit(self.board.clips.index(clip), g)
        return g

    def stop_all(self):
        self.board.stop_all()
        self._emit()

    def status(self) -> dict:
        p = self.presets[self.s["preset"]]
        return {"ok": True, "enabled": self.s["enabled"], "muted": self.s["muted"],
                "preset": p["id"], "preset_name": p["name"], "mic": self.s["mic"],
                "monitor": self.s["monitor"], "voice_monitor": self.s["voice_monitor"],
                "voice_monitor_volume": self.s["voice_monitor_volume"],
                "board_volume": self.s["board_volume"],
                "playing": self.board.reap(), "engine": self.engine.healthy(),
                "source": audio.MIX_DESC, "error": self.engine.last_error,
                "tuned": bool(self.s["tuning"].get(p["id"]))}

    # -- level meters
    def meters_wanted(self) -> bool:
        return self.meter_window or bool(self.level_subs) or time.monotonic() < self.meter_lease

    def update_meters(self):
        if self.meters_wanted():
            self.meter.watch("mic", self.s["mic"])
            self.meter.watch("out", audio.MIX_SOURCE if self.engine.running(self.engine.mix) else "")
        elif self.meter.want:
            self.meter.unwatch_all()

    def set_meter_window(self, on: bool):
        self.meter_window = bool(on)
        self.update_meters()

    def levels(self) -> dict:
        """Peak levels in dBFS: mic = selected microphone, out = Voice Mix. None = not metering."""
        return {"mic": self.meter.level("mic"), "out": self.meter.level("out")}

    def _push_levels(self):
        self.level_subs = [c for c in self.level_subs
                           if c.state() == c.LocalSocketState.ConnectedState]
        if not self.level_subs:
            self.level_timer.stop()
            self.update_meters()
            return
        line = (json.dumps({"event": "levels", **self.levels()}) + "\n").encode()
        for c in self.level_subs:
            c.write(line)

    # -- housekeeping
    def _tick(self):
        h = self.engine.healthy()
        if not h["mix"]:
            self.engine.start_mix()
        if not h["voice"] and not self.s["muted"]:
            self.apply_voice()
        elif self.s["voice_monitor"] and not self.engine.running(self.engine.mon):
            self.apply_monitor()
        self.update_meters()
        self._emit(only_if_changed=True)

    def _emit(self, only_if_changed=False):
        st = self.status()
        if only_if_changed and st == self._last:
            return
        self._last = st
        self.changed.emit()
        line = (json.dumps({"event": "status", **st}) + "\n").encode()
        for c in list(self.subs):
            if c.state() != c.LocalSocketState.ConnectedState:
                self.subs.remove(c)
            else:
                c.write(line)

    def shutdown(self):
        if self.save_timer.isActive():
            self.save()
        if self.board_save_timer.isActive():
            self.board.save()
        self.meter.unwatch_all()
        self.board.stop_all()
        self.engine.stop()
        self.server.close()
        try:
            os.unlink(socket_path())
        except OSError:
            pass

    @staticmethod
    def _path(m) -> str:
        p = m.get("path")
        if not isinstance(p, str) or not os.path.isabs(p):
            raise ValueError("path must be an absolute file path")
        return p

    def _lease_meters(self, seconds=10.0):
        """A one-shot level query keeps the meters running for a while so the next one has data."""
        self.meter_lease = time.monotonic() + seconds
        self.update_meters()

    # -- socket
    def _accept(self):
        while (c := self.server.nextPendingConnection()):
            c.readyRead.connect(lambda c=c: self._read(c))

    def _read(self, c):
        while c.canReadLine():
            try:
                msg = json.loads(bytes(c.readLine()).decode())
                reply = self.handle(msg, c)
            except Exception as e:  # noqa: BLE001
                reply = {"ok": False, "error": str(e)}
            c.write((json.dumps(reply) + "\n").encode())
            c.flush()

    def handle(self, m: dict, conn=None) -> dict:
        cmd = m.get("cmd")
        if cmd == "play":
            return {"ok": self.play(m.get("clip", "")), "cmd": cmd}
        if cmd == "preset":
            return {"ok": self.set_preset(m.get("preset", "")), "cmd": cmd}
        if cmd == "toggle":
            self.set_enabled(m.get("on"))
        elif cmd == "mute":
            self.set_muted(m.get("on"))
        elif cmd == "voice-monitor":
            if "volume" in m:
                self.set_voice_monitor_volume(m["volume"])
            if "on" in m:
                self.set_voice_monitor(m["on"])
            elif m.get("toggle"):
                self.set_voice_monitor()
        elif cmd == "mic":
            self.set_mic(m.get("source", ""))
        elif cmd == "stop":
            self.stop_all()
        elif cmd == "status":
            self._lease_meters()
            return {**self.status(), "levels": self.levels()}
        elif cmd == "levels":
            self._lease_meters()
            return {"ok": True, **self.levels()}
        elif cmd == "presets":
            return {"ok": True, "presets": [
                {"id": k, "name": p["name"], "available": p["available"], "missing": p["missing"],
                 "description": p.get("description", ""), "user": p["user"]}
                for k, p in self.presets.items()]}
        elif cmd == "save-preset":
            pid = self.save_preset(m.get("name", ""), m.get("preset"))
            return {"ok": True, "cmd": cmd, "id": pid}
        elif cmd == "export":
            rep = self.export(self._path(m), bool(m.get("board", True)), m.get("presets", "user"))
            return {"ok": True, "cmd": cmd, **rep}
        elif cmd == "inspect":
            return {"ok": True, "cmd": cmd, **self.inspect(self._path(m))}
        elif cmd == "import":
            rep = self.import_bundle(self._path(m), m.get("presets", "rename"), m.get("board", "append"))
            return {"ok": True, "cmd": cmd, **rep}
        elif cmd == "rename-preset":
            self.rename_preset(str(m.get("preset", "")), m.get("name", ""))
        elif cmd == "delete-preset":
            self.delete_preset(str(m.get("preset", "")))
        elif cmd == "params":
            pid = m.get("preset") or self.s["preset"]
            if pid not in self.presets:
                return {"ok": False, "error": f"unknown preset {pid!r}"}
            return {"ok": True, "preset": pid, "params": self.params(pid)}
        elif cmd == "param":
            if m.get("reset"):
                self.reset_params(m.get("preset"))
                return {"ok": True, "cmd": cmd, "params": self.params(m.get("preset"))}
            v = self.set_param(str(m.get("id", "")), m.get("value"), m.get("preset"))
            return {"ok": True, "cmd": cmd, "id": m.get("id"), "value": v}
        elif cmd == "clips":
            return {"ok": True, "clips": [c["name"] for c in self.board.clips],
                    "gains": [clamp_gain(c.get("gain", 1.0)) for c in self.board.clips]}
        elif cmd == "clip-volume":
            clip = self.board.find(m.get("clip", ""))
            if not clip:
                return {"ok": False, "error": f"no such clip {m.get('clip')!r}"}
            v = m.get("volume")
            if v is not None:
                if isinstance(v, bool) or not isinstance(v, (int, float)):
                    return {"ok": False, "error": "volume must be a number (percent, 100 = as recorded)"}
                self.set_clip_gain(m["clip"], v / 100)
            return {"ok": True, "cmd": cmd, "clip": clip["name"],
                    "volume": round(clamp_gain(clip.get("gain", 1.0)) * 100, 1)}
        elif cmd == "mics":
            return {"ok": True, "mics": [{"name": n, "description": d} for n, d in audio.list_mics()]}
        elif cmd == "subscribe":
            if conn is not None:
                self.subs.append(conn)
                if m.get("levels"):
                    self.level_subs.append(conn)
                    self.update_meters()
                    if not self.level_timer.isActive():
                        self.level_timer.start(100)
            return {"event": "status", **self.status()}
        else:
            return {"ok": False, "error": f"unknown command {cmd!r}"}
        return {"ok": True, "cmd": cmd, **self.status()}
