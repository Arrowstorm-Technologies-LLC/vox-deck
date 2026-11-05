"""Client side of the control socket (stdlib only; used by the CLI and the Stream Deck plugin)."""
import json
import socket

from .paths import socket_path


def request(msg: dict, timeout: float = 5.0) -> dict:
    s = socket.socket(socket.AF_UNIX)
    s.settimeout(timeout)
    s.connect(socket_path())
    s.sendall((json.dumps(msg) + "\n").encode())
    buf = b""
    while not buf.endswith(b"\n"):
        chunk = s.recv(65536)
        if not chunk:
            break
        buf += chunk
    s.close()
    return json.loads(buf) if buf else {"ok": False, "error": "no reply"}


def subscribe(timeout=None, levels=False):
    """Generator of status events; yields the current status first.

    With levels=True, {"event": "levels", "mic": dB, "out": dB} lines arrive about 10 times a second too.
    """
    s = socket.socket(socket.AF_UNIX)
    s.connect(socket_path())
    s.sendall((json.dumps({"cmd": "subscribe", "levels": bool(levels)}) + "\n").encode())
    s.settimeout(timeout)
    f = s.makefile("r")
    for line in f:
        if line.strip():
            yield json.loads(line)
