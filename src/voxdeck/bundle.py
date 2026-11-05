"""Import / export of presets and soundboards as one `.voxdeck` file.

A bundle is a zip archive:

    manifest.json        {"format": "vox-deck", "version": 1, "presets": [...],
                          "board": {"name": ..., "board_volume": 0.8,
                                    "clips": [{"name", "color", "gain", "file": "clips/001-chime.wav"}]}}
    clips/001-chime.wav  the sound files the board uses (bundled so a board moves between machines)

Imported bundles are untrusted. Nothing in them is executed, members are never extracted by
their own names (only the manifest's references, checked against a strict pattern, are read and
written to files we name ourselves), sizes are capped while reading (not just from the headers),
presets go through effects.validate_preset, and audio files must look like audio.
"""
import json
import re
import shutil
import time
import zipfile
from pathlib import Path

from . import __version__, effects
from .board import COLORS
from .paths import data_dir

FORMAT, VERSION = "vox-deck", 1
SUFFIX = ".voxdeck"
AUDIO_EXT = {".wav", ".flac", ".ogg", ".oga", ".opus", ".mp3"}
MEMBER_RE = re.compile(r"^clips/[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
MAX_ARCHIVE = 1 << 30          # 1 GiB on disk
MAX_MANIFEST = 2 << 20         # 2 MiB
MAX_CLIP = 200 << 20           # 200 MiB per sound
MAX_TOTAL = 1 << 30            # 1 GiB of sounds
MAX_MEMBERS, MAX_CLIPS, MAX_PRESETS = 2000, 500, 200
MAX_RATIO = 1000               # uncompressed / compressed, zip-bomb guard
PRESET_MODES = ("rename", "replace", "skip")
BOARD_MODES = ("append", "replace", "skip")


class BundleError(ValueError):
    pass


def audio_kind(head: bytes) -> str | None:
    """Audio family from the first bytes, or None."""
    if head[:4] in (b"RIFF", b"RF64") and head[8:12] == b"WAVE":
        return "wav"
    if head[:4] == b"fLaC":
        return "flac"
    if head[:4] == b"OggS":
        return "ogg"
    if head[:3] == b"ID3" or (len(head) > 1 and head[0] == 0xFF and head[1] & 0xE0 == 0xE0):
        return "mp3"
    return None


EXT_KIND = {".wav": "wav", ".flac": "flac", ".ogg": "ogg", ".oga": "ogg", ".opus": "ogg", ".mp3": "mp3"}


# ---------------------------------------------------------------- export
def export_bundle(path, presets=(), clips=None, board_volume=None, name="Vox Deck board") -> dict:
    """Write a bundle. `presets`: preset dicts; `clips`: board clip list or None for no board.
    Returns {"presets": n, "clips": n, "skipped": [clip names whose file is missing or not audio]}."""
    path = Path(path)
    recs = [effects.preset_record(p) for p in presets]
    manifest = {"format": FORMAT, "version": VERSION, "app": __version__,
                "created": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "presets": recs}
    files, skipped = [], []
    if clips is not None:
        out = []
        for c in clips:
            src = Path(c.get("path", ""))
            ext = src.suffix.lower()
            try:
                ok = ext in AUDIO_EXT and src.is_file() and src.stat().st_size <= MAX_CLIP
                if ok:
                    with open(src, "rb") as f:
                        ok = audio_kind(f.read(16)) == EXT_KIND[ext]
            except OSError:
                ok = False
            if not ok:
                skipped.append(c.get("name", src.name))
                continue
            member = f"clips/{len(out) + 1:03d}-{effects.slug(c.get('name', 'clip'))}{ext}"
            files.append((src, member))
            entry = {"name": str(c.get("name", src.stem))[:40], "file": member}
            if c.get("color"):
                entry["color"] = c["color"]
            if c.get("gain") is not None:
                entry["gain"] = c["gain"]
            out.append(entry)
        manifest["board"] = {"name": name, "clips": out}
        if board_volume is not None:
            manifest["board"]["board_volume"] = board_volume
    tmp = path.with_name(path.name + ".part")
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("manifest.json", json.dumps(manifest, indent=1))
            for src, member in files:
                z.write(src, member)
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)
    return {"presets": len(recs), "clips": len(files), "skipped": skipped}


# ---------------------------------------------------------------- import: reading
def _read_capped(z, info, limit) -> bytes:
    """Read a member, refusing more than `limit` bytes whatever the header says."""
    with z.open(info) as f:
        data = f.read(limit + 1)
    if len(data) > limit:
        raise BundleError(f"{info.filename} is too large")
    return data


def read_bundle(path) -> dict:
    """Open and fully validate a bundle without extracting anything.

    Returns {"path", "presets": [validated], "board": None | {"name", "board_volume",
    "clips": [{"name", "color", "gain", "member", "ext"}]}}. Raises BundleError."""
    path = Path(path)
    try:
        if path.stat().st_size > MAX_ARCHIVE:
            raise BundleError("file is larger than 1 GiB")
        if not zipfile.is_zipfile(path):
            raise BundleError("not a Vox Deck file (not a zip archive)")
        z = zipfile.ZipFile(path)
    except OSError as e:
        raise BundleError(f"can not read the file: {e}") from e
    with z:
        infos = z.infolist()
        if len(infos) > MAX_MEMBERS:
            raise BundleError("too many files inside")
        by_name = {}
        for i in infos:
            if i.flag_bits & 0x1:
                raise BundleError("encrypted archives are not supported")
            by_name.setdefault(i.filename, i)
        mi = by_name.get("manifest.json")
        if mi is None:
            raise BundleError("no manifest.json: not a Vox Deck file")
        try:
            manifest = json.loads(_read_capped(z, mi, MAX_MANIFEST).decode("utf-8"))
        except (UnicodeDecodeError, ValueError, zipfile.BadZipFile, RuntimeError) as e:
            raise BundleError(f"manifest.json is not valid: {e}") from e
        if not isinstance(manifest, dict) or manifest.get("format") != FORMAT:
            raise BundleError("not a Vox Deck file")
        v = manifest.get("version")
        if not isinstance(v, int) or isinstance(v, bool) or v < 1 or v > VERSION:
            raise BundleError(f"unsupported bundle version {v!r} (this app reads version {VERSION})")

        presets = manifest.get("presets", [])
        if not isinstance(presets, list) or len(presets) > MAX_PRESETS:
            raise BundleError("presets must be a list")
        out_presets, seen = [], set()
        for j, p in enumerate(presets):
            try:
                rec = effects.validate_preset(p)
            except ValueError as e:
                raise BundleError(f"preset {j + 1}: {e}") from e
            if rec["id"] in seen:
                raise BundleError(f"preset id {rec['id']!r} appears twice")
            seen.add(rec["id"])
            out_presets.append(rec)

        board = manifest.get("board")
        out_board = None
        if board is not None:
            if not isinstance(board, dict) or not isinstance(board.get("clips", []), list):
                raise BundleError("board must be an object with a clips list")
            clips = board.get("clips", [])
            if len(clips) > MAX_CLIPS:
                raise BundleError("too many clips")
            out_clips, total, used = [], 0, set()
            for j, c in enumerate(clips):
                if not isinstance(c, dict):
                    raise BundleError(f"clip {j + 1}: bad entry")
                member = c.get("file")
                if not isinstance(member, str) or not MEMBER_RE.match(member) or ".." in member:
                    raise BundleError(f"clip {j + 1}: bad file reference")
                if member in used:
                    raise BundleError(f"clip {j + 1}: file used twice")
                used.add(member)
                ext = Path(member).suffix.lower()
                if ext not in AUDIO_EXT:
                    raise BundleError(f"clip {j + 1}: not an audio file type")
                info = by_name.get(member)
                if info is None or info.is_dir():
                    raise BundleError(f"clip {j + 1}: {member} is missing from the archive")
                if info.file_size > MAX_CLIP:
                    raise BundleError(f"clip {j + 1}: sound is larger than 200 MiB")
                if info.compress_size and info.file_size / info.compress_size > MAX_RATIO:
                    raise BundleError(f"clip {j + 1}: suspicious compression ratio")
                total += info.file_size
                if total > MAX_TOTAL:
                    raise BundleError("sounds add up to more than 1 GiB")
                name = c.get("name", f"Clip {j + 1}")
                try:
                    name = effects._text(name, "clip name", 200).strip()[:40] or f"Clip {j + 1}"
                except ValueError as e:
                    raise BundleError(f"clip {j + 1}: {e}") from e
                color = c.get("color")
                color = color if isinstance(color, str) and COLOR_RE.match(color) else COLORS[j % len(COLORS)]
                gain = c.get("gain", 1.0)
                if isinstance(gain, bool) or not isinstance(gain, (int, float)) or not 0 <= gain <= 4:
                    gain = 1.0
                out_clips.append({"name": name, "color": color, "gain": float(gain),
                                  "member": member, "ext": ext})
            bname = board.get("name", "Imported board")
            bname = bname.strip()[:60] if isinstance(bname, str) and bname.isprintable() else "Imported board"
            bv = board.get("board_volume")
            bv = float(bv) if isinstance(bv, (int, float)) and not isinstance(bv, bool) and 0 <= bv <= 1 else None
            out_board = {"name": bname or "Imported board", "board_volume": bv, "clips": out_clips}
    return {"path": str(path), "presets": out_presets, "board": out_board}


def conflicts(bundle: dict, presets: dict) -> list:
    """Imported preset ids that already exist as user presets (built-in ids are always renamed)."""
    return [p["id"] for p in bundle["presets"]
            if p["id"] in presets and presets[p["id"]].get("user")]


def summary(bundle: dict, presets: dict) -> dict:
    b = bundle["board"]
    return {"presets": [{"id": p["id"], "name": p["name"]} for p in bundle["presets"]],
            "conflicts": conflicts(bundle, presets),
            "builtin_ids": [p["id"] for p in bundle["presets"] if p["id"] in effects.BUILTIN_IDS],
            "board": None if b is None else {"name": b["name"], "clips": len(b["clips"])}}


# ---------------------------------------------------------------- import: applying
def _extract_clips(path, board, dest: Path):
    """Copy the board's sounds to `dest` under names we choose; check sizes and audio headers."""
    dest.mkdir(parents=True)
    out = []
    with zipfile.ZipFile(path) as z:
        for i, c in enumerate(board["clips"]):
            target = dest / f"{i + 1:03d}-{effects.slug(c['name'])}{c['ext']}"
            n = 0
            with z.open(c["member"]) as src, open(target, "xb") as dst:
                head = src.read(16)
                if audio_kind(head) != EXT_KIND[c["ext"]]:
                    raise BundleError(f"{c['name']}: the file is not {c['ext'][1:]} audio")
                dst.write(head)
                n = len(head)
                while chunk := src.read(1 << 16):
                    n += len(chunk)
                    if n > MAX_CLIP:
                        raise BundleError(f"{c['name']}: sound is larger than 200 MiB")
                    dst.write(chunk)
            out.append({"name": c["name"], "path": str(target), "color": c["color"], "gain": c["gain"]})
    return out


def _free_file(udir: Path, pid: str) -> Path:
    target, k = udir / f"{pid}.json", 2
    while target.exists():
        target, k = udir / f"{pid}-{k}.json", k + 1
    return target


def apply_import(bundle: dict, presets: dict, board, preset_mode="rename", board_mode="append") -> dict:
    """Bring a validated bundle in.

    preset_mode for ids that clash with your presets: rename (keep both), replace, skip.
    board_mode: append (add pads), replace (swap the whole board; sound files are not deleted), skip.
    `board` is the Board object (its clips list is changed and saved). Returns a report.
    """
    if preset_mode not in PRESET_MODES or board_mode not in BOARD_MODES:
        raise BundleError("bad import mode")
    report = {"presets_added": [], "presets_replaced": [], "presets_skipped": [], "clips": 0,
              "board": "skipped"}
    new_clips, clip_dir = None, None
    b = bundle["board"]
    if b is not None and board_mode != "skip" and b["clips"]:
        root = data_dir() / "clips"
        stamp = time.strftime("%Y%m%d-%H%M%S")
        clip_dir = root / f"{effects.slug(b['name'])}-{stamp}"
        n = 2
        while clip_dir.exists():
            clip_dir, n = root / f"{effects.slug(b['name'])}-{stamp}-{n}", n + 1
        tmp = root / f".import-{stamp}-{id(bundle)}"
        try:
            extracted = _extract_clips(bundle["path"], b, tmp)
            tmp.rename(clip_dir)
        except (BundleError, OSError, zipfile.BadZipFile, RuntimeError) as e:
            shutil.rmtree(tmp, ignore_errors=True)
            raise e if isinstance(e, BundleError) else BundleError(str(e)) from e
        new_clips = [dict(c, path=str(clip_dir / Path(c["path"]).name)) for c in extracted]

    taken = set(presets)
    udir = effects.user_preset_dir()
    for rec in bundle["presets"]:
        rec = dict(rec)
        pid = rec["id"]
        if pid in presets and presets[pid].get("user"):
            if preset_mode == "skip":
                report["presets_skipped"].append(pid)
                continue
            if preset_mode == "replace":
                effects.delete_user_preset(presets[pid])
                effects._write_json(_free_file(udir, pid), rec)
                report["presets_replaced"].append(pid)
                continue
        if pid in taken or pid in effects.BUILTIN_IDS:
            rec["id"] = effects.unique_id(rec["name"], taken | {pid})
            rec["name"] = (rec["name"][:29] + " (imported)") if not rec["name"].endswith("(imported)") else rec["name"]
        taken.add(rec["id"])
        effects._write_json(_free_file(udir, rec["id"]), rec)
        report["presets_added"].append(rec["id"])

    if new_clips is not None:
        if board_mode == "replace":
            board.clips = new_clips
        else:
            board.clips = board.clips + new_clips
        board.save()
        report["clips"], report["board"] = len(new_clips), board_mode
        report["clip_dir"] = str(clip_dir)
        if board_mode == "replace" and b.get("board_volume") is not None:
            report["board_volume"] = b["board_volume"]
    return report
