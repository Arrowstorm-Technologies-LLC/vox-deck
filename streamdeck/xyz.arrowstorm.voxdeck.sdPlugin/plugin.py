#!/usr/bin/env python3
"""Vox Deck plugin for OpenDeck / Stream Deck (Elgato plugin protocol).

Pure standard library. Talks to the Stream Deck host over its websocket and to the
running Vox Deck app over its Unix socket ($XDG_RUNTIME_DIR/vox-deck.sock).
"""
import base64
import hashlib
import json
import os
import socket
import struct
import sys
import threading
import time
from urllib.parse import quote

UUID = "xyz.arrowstorm.voxdeck"
SOCK = os.path.join(os.environ.get("XDG_RUNTIME_DIR") or f"/tmp/vox-deck-{os.getuid()}", "vox-deck.sock")


def log(*a):
    print("[voxdeck]", *a, file=sys.stderr, flush=True)


# ---------------------------------------------------------------- minimal websocket client
class WS:
    def __init__(self, port):
        self.s = socket.create_connection(("127.0.0.1", int(port)))
        key = base64.b64encode(os.urandom(16)).decode()
        self.s.sendall((f"GET / HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nUpgrade: websocket\r\n"
                        f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
                        "Sec-WebSocket-Version: 13\r\n\r\n").encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            c = self.s.recv(4096)
            if not c:
                raise ConnectionError("handshake failed")
            buf += c
        head, self.buf = buf.split(b"\r\n\r\n", 1)
        if b" 101 " not in head.split(b"\r\n")[0]:
            raise ConnectionError(head.decode(errors="replace"))
        self.lock = threading.Lock()

    def _need(self, n):
        while len(self.buf) < n:
            c = self.s.recv(65536)
            if not c:
                raise ConnectionError("closed")
            self.buf += c
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def send(self, obj):
        data = json.dumps(obj).encode()
        hdr = bytearray([0x81])
        n = len(data)
        if n < 126:
            hdr.append(0x80 | n)
        elif n < 65536:
            hdr += bytes([0x80 | 126]) + struct.pack(">H", n)
        else:
            hdr += bytes([0x80 | 127]) + struct.pack(">Q", n)
        mask = os.urandom(4)
        body = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        with self.lock:
            self.s.sendall(bytes(hdr) + mask + body)

    def recv(self):
        msg = b""
        while True:
            b1, b2 = self._need(2)
            op, n = b1 & 0x0F, b2 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._need(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._need(8))[0]
            mask = self._need(4) if b2 & 0x80 else None
            data = self._need(n)
            if mask:
                data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
            if op == 8:
                raise ConnectionError("closed by host")
            if op == 9:  # ping -> pong
                with self.lock:
                    self.s.sendall(b"\x8a" + bytes([0x80 | len(data)]) + b"\0\0\0\0" + data)
                continue
            msg += data
            if b1 & 0x80:
                return json.loads(msg)


# ---------------------------------------------------------------- vox-deck socket
def vd_request(msg):
    s = socket.socket(socket.AF_UNIX)
    s.settimeout(3)
    s.connect(SOCK)
    s.sendall((json.dumps(msg) + "\n").encode())
    buf = b""
    while not buf.endswith(b"\n"):
        c = s.recv(65536)
        if not c:
            break
        buf += c
    s.close()
    return json.loads(buf)


def svg_key(title, bg, fg="#ffffff", sub=""):
    words = title.split()[:2] or [""]
    texts = "".join(f'<text x="72" y="{70 + 30 * i - (15 if len(words) > 1 else 0)}" font-size="26" '
                    f'font-weight="bold" font-family="sans-serif" text-anchor="middle" fill="{fg}">{w[:10]}</text>'
                    for i, w in enumerate(words))
    s = (f'<svg xmlns="http://www.w3.org/2000/svg" width="144" height="144"><rect width="144" height="144" '
         f'rx="18" fill="{bg}"/>{texts}'
         f'<text x="72" y="126" font-size="16" font-family="sans-serif" text-anchor="middle" fill="{fg}" '
         f'opacity="0.8">{sub}</text></svg>')
    return "data:image/svg+xml;charset=utf8," + quote(s)


# ---------------------------------------------------------------- plugin
class Plugin:
    def __init__(self, ws):
        self.ws = ws
        self.ctx = {}  # context -> (action uuid, settings)
        self.status = {}

    def send(self, event, context, payload=None):
        m = {"event": event, "context": context}
        if payload is not None:
            m["payload"] = payload
        self.ws.send(m)

    def render(self, context):
        action, st = self.ctx[context]
        s = self.status
        short = action.rsplit(".", 1)[-1]
        if short == "toggle":
            self.send("setState", context, {"state": 0 if s.get("enabled", False) else 1})
            self.send("setTitle", context, {"title": "FX " + ("ON" if s.get("enabled") else "OFF")})
        elif short == "mute":
            self.send("setState", context, {"state": 1 if s.get("muted") else 0})
            self.send("setTitle", context, {"title": "MIC MUTED" if s.get("muted") else "MIC LIVE"})
        elif short == "preset":
            pid = st.get("preset", "")
            name = st.get("title") or pid or "Preset"
            active = bool(s) and s.get("preset") == pid and s.get("enabled", True)
            self.send("setState", context, {"state": 1 if active else 0})
            self.send("setImage", context, {"image": svg_key(name, "#5865f2" if active else "#2a2e3a",
                                                             sub="ACTIVE" if active else ""), "target": 0})
        elif short == "play":
            self.send("setImage", context, {"image": svg_key(st.get("clip", "Clip") or "Clip",
                                                             "#2980b9"),
                                            "target": 0})
        elif short == "stop":
            self.send("setImage", context, {"image": svg_key("Stop all", "#8a2a2a"), "target": 0})

    def render_all(self):
        for c in list(self.ctx):
            self.render(c)

    def key_down(self, context):
        action, st = self.ctx[context]
        short = action.rsplit(".", 1)[-1]
        msg = {"toggle": {"cmd": "toggle"}, "mute": {"cmd": "mute"}, "stop": {"cmd": "stop"},
               "preset": {"cmd": "preset", "preset": st.get("preset", "")},
               "play": {"cmd": "play", "clip": st.get("clip", "")}}.get(short)
        try:
            r = vd_request(msg)
            if not r.get("ok", True):
                self.send("showAlert", context)
        except (OSError, ValueError) as e:
            log("request failed:", e)
            self.send("showAlert", context)

    def handle(self, m):
        ev, c = m.get("event"), m.get("context")
        if ev == "willAppear":
            self.ctx[c] = (m["action"], m.get("payload", {}).get("settings", {}) or {})
            self.render(c)
        elif ev == "willDisappear":
            self.ctx.pop(c, None)
        elif ev == "didReceiveSettings" and c in self.ctx:
            self.ctx[c] = (self.ctx[c][0], m.get("payload", {}).get("settings", {}) or {})
            self.render(c)
        elif ev == "keyDown":
            if c in self.ctx:
                self.key_down(c)

    def watch(self):
        """Follow status events from the Vox Deck app; reconnect forever."""
        while True:
            try:
                s = socket.socket(socket.AF_UNIX)
                s.connect(SOCK)
                s.sendall(b'{"cmd":"subscribe"}\n')
                for line in s.makefile("r"):
                    if line.strip():
                        self.status = json.loads(line)
                        self.render_all()
            except (OSError, ValueError):
                pass
            if self.status:
                self.status = {}
                self.render_all()
            time.sleep(2)


def main():
    args = dict(zip(sys.argv[1::2], sys.argv[2::2]))
    ws = WS(args["-port"])
    ws.send({"event": args["-registerEvent"], "uuid": args["-pluginUUID"]})
    p = Plugin(ws)
    threading.Thread(target=p.watch, daemon=True).start()
    log("registered")
    while True:
        p.handle(ws.recv())


if __name__ == "__main__":
    try:
        main()
    except (ConnectionError, OSError) as e:
        log("exiting:", e)
        sys.exit(1)
