"""Application core: state, preset switching, board and the control socket server."""
import json
import os

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtNetwork import QLocalServer

from . import audio, effects
from .board import Board
from .paths import config_dir, socket_path

DEFAULTS = {"mic": "", "preset": "clean", "enabled": True, "muted": False,
            "board_volume": 0.8, "monitor": False, "monitor_target": ""}


class Controller(QObject):
    changed = Signal()

    def __init__(self):
        super().__init__()
        self.sfile = config_dir() / "settings.json"
        self.s = dict(DEFAULTS)
        try:
            self.s.update(json.loads(self.sfile.read_text()))
        except (OSError, ValueError):
            pass
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

    # -- state
    def start(self):
        self.engine.start_mix()
        self.apply_voice()

    def save(self):
        self.sfile.write_text(json.dumps(self.s, indent=1))

    def apply_voice(self):
        pr = self.presets[self.s["preset"] if self.s["enabled"] else "clean"]
        self.engine.set_voice(None if self.s["muted"] else pr, self.s["mic"])
        self.save()
        self._emit()

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
        self._emit()

    def play(self, ref) -> bool:
        clip = self.board.find(ref)
        if not clip:
            return False
        ok = self.board.play(clip, self.s["board_volume"], self.s["monitor"],
                             self.s["monitor_target"] or None)
        self._emit()
        return ok

    def stop_all(self):
        self.board.stop_all()
        self._emit()

    def status(self) -> dict:
        p = self.presets[self.s["preset"]]
        return {"ok": True, "enabled": self.s["enabled"], "muted": self.s["muted"],
                "preset": p["id"], "preset_name": p["name"], "mic": self.s["mic"],
                "monitor": self.s["monitor"], "board_volume": self.s["board_volume"],
                "playing": self.board.reap(), "engine": self.engine.healthy(),
                "source": audio.MIX_DESC, "error": self.engine.last_error}

    # -- housekeeping
    def _tick(self):
        h = self.engine.healthy()
        if not h["mix"]:
            self.engine.start_mix()
        if not h["voice"] and not self.s["muted"]:
            self.apply_voice()
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
        self.board.stop_all()
        self.engine.stop()
        self.server.close()
        try:
            os.unlink(socket_path())
        except OSError:
            pass

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
        elif cmd == "mic":
            self.set_mic(m.get("source", ""))
        elif cmd == "stop":
            self.stop_all()
        elif cmd == "status":
            return self.status()
        elif cmd == "presets":
            return {"ok": True, "presets": [
                {"id": k, "name": p["name"], "available": p["available"], "missing": p["missing"],
                 "description": p.get("description", "")} for k, p in self.presets.items()]}
        elif cmd == "clips":
            return {"ok": True, "clips": [c["name"] for c in self.board.clips]}
        elif cmd == "mics":
            return {"ok": True, "mics": [{"name": n, "description": d} for n, d in audio.list_mics()]}
        elif cmd == "subscribe":
            if conn is not None:
                self.subs.append(conn)
            return {"event": "status", **self.status()}
        else:
            return {"ok": False, "error": f"unknown command {cmd!r}"}
        return {"ok": True, "cmd": cmd, **self.status()}
