import os
from pathlib import Path


def runtime_dir() -> Path:
    return Path(os.environ.get("XDG_RUNTIME_DIR") or f"/tmp/vox-deck-{os.getuid()}")


def socket_path() -> str:
    return str(runtime_dir() / "vox-deck.sock")


def config_dir() -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    d = base / "vox-deck"
    d.mkdir(parents=True, exist_ok=True)
    return d


def data_dir() -> Path:
    base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share")
    d = base / "vox-deck"
    d.mkdir(parents=True, exist_ok=True)
    return d
