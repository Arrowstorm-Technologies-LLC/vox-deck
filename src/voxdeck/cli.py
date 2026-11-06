"""`vox-deck` command line client for the running app."""
import argparse
import json
import os
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
    p = sub.add_parser("hear", help="hear your own processed voice in the headset (no argument = show state)")
    p.add_argument("state", nargs="?", choices=["on", "off", "toggle"])
    p.add_argument("--volume", type=float, metavar="PERCENT", help="monitor volume, 0 to 100")
    sub.add_parser("stop", help="stop all playing clips")
    sub.add_parser("status", help="print status as JSON")
    sub.add_parser("presets", help="list presets")
    sub.add_parser("clips", help="list clips")
    p = sub.add_parser("clip-volume", help="show or set a pad's volume in percent (100 = as recorded, up to 400)")
    p.add_argument("clip")
    p.add_argument("percent", nargs="?", type=float)
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
    p = sub.add_parser("export", help="export your presets and/or the soundboard (with its sounds) to a .voxdeck file")
    p.add_argument("file")
    p.add_argument("--no-board", action="store_true", help="leave the soundboard out")
    p.add_argument("--presets", default="user", help="'user' (all yours, default), 'none', or ids separated by commas")
    p = sub.add_parser("import", help="import a .voxdeck file (never overwrites unless told to)")
    p.add_argument("file")
    p.add_argument("--presets", choices=["rename", "replace", "skip"], default="rename",
                   help="presets whose id you already have: keep both (default), replace yours, or skip")
    p.add_argument("--board", choices=["append", "replace", "skip"], default="append",
                   help="add the pads to your board (default), replace your board, or skip it")
    p.add_argument("--dry-run", action="store_true", help="only show what is inside")
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
        elif a.cmd == "clip-volume":
            msg["clip"] = a.clip
            if a.percent is not None:
                msg["volume"] = a.percent
        elif a.cmd in ("toggle", "mute") and a.state:
            msg["on"] = a.state == "on"
        elif a.cmd == "mic":
            msg["source"] = a.source
        elif a.cmd == "hear":
            msg = {"cmd": "voice-monitor"}
            if a.state == "toggle":
                msg["toggle"] = True
            elif a.state:
                msg["on"] = a.state == "on"
            if a.volume is not None:
                msg["volume"] = a.volume / 100
        elif a.cmd == "export":
            pr = a.presets if a.presets in ("user", "none") else [x for x in a.presets.split(",") if x]
            msg = {"cmd": "export", "path": os.path.abspath(a.file), "board": not a.no_board, "presets": pr}
        elif a.cmd == "import":
            msg = {"cmd": "inspect" if a.dry_run else "import", "path": os.path.abspath(a.file),
                   "presets": a.presets, "board": a.board}
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
        r = ipc.request(msg, timeout=300 if msg["cmd"] in ("import", "export") else 5)
    except (OSError, KeyboardInterrupt) as e:
        print(f"vox-deck: app not running ({e})", file=sys.stderr)
        return 2
    print(json.dumps(r, indent=None if a.cmd in ("play", "preset", "toggle", "mute", "stop", "mic", "hear", "param", "clip-volume", "save", "rename", "delete") else 1))
    return 0 if r.get("ok", True) else 1


if __name__ == "__main__":
    sys.exit(main())
