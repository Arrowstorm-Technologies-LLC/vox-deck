"""`vox-deck` command line client for the running app."""
import argparse
import json
import sys

from . import ipc


def main(argv=None):
    ap = argparse.ArgumentParser(prog="vox-deck", description="Control a running Vox Deck.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("play", help="play a soundboard clip (name or index)")
    p.add_argument("clip")
    p = sub.add_parser("preset", help="switch voice preset (id, or 'next'/'prev')")
    p.add_argument("preset")
    p = sub.add_parser("toggle", help="toggle the voice effects on/off (bypass)")
    p.add_argument("state", nargs="?", choices=["on", "off"])
    p = sub.add_parser("mute", help="mute/unmute the microphone path")
    p.add_argument("state", nargs="?", choices=["on", "off"])
    p = sub.add_parser("mic", help="select the microphone (source node name)")
    p.add_argument("source")
    sub.add_parser("stop", help="stop all playing clips")
    sub.add_parser("status", help="print status as JSON")
    sub.add_parser("presets", help="list presets")
    sub.add_parser("clips", help="list clips")
    sub.add_parser("mics", help="list microphones")
    sub.add_parser("subscribe", help="stream status changes as JSON lines")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "subscribe":
            for ev in ipc.subscribe():
                print(json.dumps(ev), flush=True)
            return 0
        msg = {"cmd": a.cmd}
        if a.cmd == "play":
            msg["clip"] = a.clip
        elif a.cmd == "preset":
            msg["preset"] = a.preset
        elif a.cmd in ("toggle", "mute") and a.state:
            msg["on"] = a.state == "on"
        elif a.cmd == "mic":
            msg["source"] = a.source
        r = ipc.request(msg)
    except (OSError, KeyboardInterrupt) as e:
        print(f"vox-deck: app not running ({e})", file=sys.stderr)
        return 2
    print(json.dumps(r, indent=None if a.cmd in ("play", "preset", "toggle", "mute", "stop", "mic") else 1))
    return 0 if r.get("ok", True) else 1


if __name__ == "__main__":
    sys.exit(main())
