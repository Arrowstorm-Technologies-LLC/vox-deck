#!/usr/bin/env python3
"""Fake Stream Deck host: launches the plugin like OpenDeck does, presses keys, prints what the plugin sends.

Needs a running vox-deck app. Usage: python tests/mock_streamdeck_host.py
"""
import base64
import hashlib
import json
import socket
import struct
import subprocess
import sys
import time
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[1] / "streamdeck/xyz.arrowstorm.voxdeck.sdPlugin/plugin.py"
U = "xyz.arrowstorm.voxdeck"

srv = socket.socket()
srv.bind(("127.0.0.1", 0))
srv.listen(1)
port = srv.getsockname()[1]
proc = subprocess.Popen([sys.executable, str(PLUGIN), "-port", str(port), "-pluginUUID", "PLUGINUUID",
                         "-registerEvent", "registerPlugin", "-info", "{}"])
conn, _ = srv.accept()
buf = b""
while b"\r\n\r\n" not in buf:
    buf += conn.recv(4096)
key = [l.split(b": ")[1] for l in buf.split(b"\r\n") if l.lower().startswith(b"sec-websocket-key")][0]
acc = base64.b64encode(hashlib.sha1(key + b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11").digest())
conn.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
             b"Sec-WebSocket-Accept: " + acc + b"\r\n\r\n")
rest = buf.split(b"\r\n\r\n", 1)[1]


def need(n):
    global rest
    while len(rest) < n:
        rest += conn.recv(65536)
    o, rest = rest[:n], rest[n:]
    return o


def recv():
    b1, b2 = need(2)
    n = b2 & 0x7F
    if n == 126:
        n = struct.unpack(">H", need(2))[0]
    elif n == 127:
        n = struct.unpack(">Q", need(8))[0]
    mask = need(4)
    d = bytes(b ^ mask[i % 4] for i, b in enumerate(need(n)))
    return json.loads(d)


def send(o):
    d = json.dumps(o).encode()
    h = bytes([0x81]) + (bytes([len(d)]) if len(d) < 126 else bytes([126]) + struct.pack(">H", len(d)))
    conn.sendall(h + d)


conn.settimeout(1.0)
log = []


def pump(sec):
    end = time.time() + sec
    while time.time() < end:
        try:
            m = recv()
            log.append(m)
        except socket.timeout:
            pass


print("register:", recv())
keys = {"c_play": (f"{U}.play", {"clip": "Chime"}), "c_deep": (f"{U}.preset", {"preset": "deep"}),
        "c_radio": (f"{U}.preset", {"preset": "radio"}), "c_tog": (f"{U}.toggle", {}),
        "c_mute": (f"{U}.mute", {}), "c_stop": (f"{U}.stop", {})}
for ctx, (a, s) in keys.items():
    send({"event": "willAppear", "action": a, "context": ctx, "payload": {"settings": s}})
pump(1.5)
for ctx in ("c_deep", "c_tog", "c_play", "c_mute", "c_mute", "c_tog", "c_radio"):
    send({"event": "keyDown", "action": keys[ctx][0], "context": ctx, "payload": {"settings": keys[ctx][1]}})
    pump(1.2)
for m in log:
    p = m.get("payload", {})
    if "image" in p:
        p = {"image": p["image"][:30] + "..."}
    print(m["event"], m.get("context"), p)
proc.terminate()
