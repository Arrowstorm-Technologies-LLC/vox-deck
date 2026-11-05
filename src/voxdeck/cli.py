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
    p = sub.add_parser("params", help="list the tunable controls of a preset (default: current)")
    p.add_argument("preset", nargs="?")
    p = sub.add_parser("param", help="set a tunable control live, e.g. `param pitch 0.8`; `param --reset`")
    p.add_argument("id", nargs="?")
    p.add_argument("value", nargs="?", type=float)
    p.add_argument("--preset", help="preset id (default: current)")
    p.add_argument("--reset", action="store_true", help="restore the preset's own values")
    p = sub.add_parser("save", help="save the current preset with its tuning as a new preset")
    p.add_argument("name")
    p.add_argument("--preset", help="save this preset instead of the current one")
    p = sub.add_parser("rename", help="rename one of your presets")
    p.add_argument("preset")
    p.add_argument("name")
    p = sub.add_parser("delete", help="delete one of your presets (built-ins are read-only)")
    p.add_argument("preset")
    sub.add_parser("levels", help="print mic and Voice Mix peak levels (dBFS)")
    p = sub.add_parser("subscribe", help="stream status changes as JSON lines")
    p.add_argument("--levels", action="store_true", help="also stream level meter events")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "subscribe":
            for ev in ipc.subscribe(levels=a.levels):
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
        elif a.cmd == "save":
            msg = {"cmd": "save-preset", "name": a.name, "preset": a.preset}
        elif a.cmd == "rename":
            msg = {"cmd": "rename-preset", "preset": a.preset, "name": a.name}
        elif a.cmd == "delete":
            msg = {"cmd": "delete-preset", "preset": a.preset}
        elif a.cmd == "params" and a.preset:
            msg["preset"] = a.preset
        elif a.cmd == "param":
            if a.preset:
                msg["preset"] = a.preset
            if a.reset:
                msg["reset"] = True
            elif a.id is None or a.value is None:
                ap.error("param needs an id and a value (or --reset)")
            else:
                msg["id"], msg["value"] = a.id, a.value
        r = ipc.request(msg)
    except (OSError, KeyboardInterrupt) as e:
        print(f"vox-deck: app not running ({e})", file=sys.stderr)
        return 2
    print(json.dumps(r, indent=None if a.cmd in ("play", "preset", "toggle", "mute", "stop", "mic", "param", "save", "rename", "delete") else 1))
    return 0 if r.get("ok", True) else 1


if __name__ == "__main__":
    sys.exit(main())
