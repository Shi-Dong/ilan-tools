from __future__ import annotations

import json
from pathlib import Path


DEFAULTS: dict[str, str | int] = {
    "workdir": "~/.ilan",
    "num-agents": 5,
    "time-zone": "US/Pacific",
    "editor": "emacs",
}

VALID_KEYS = set(DEFAULTS)

INT_KEYS = {"num-agents"}

_CONFIG_DIR = Path("~/.ilan").expanduser()
_CONFIG_FILE = _CONFIG_DIR / "config.json"


def load() -> dict[str, str | int]:
    if _CONFIG_FILE.exists():
        with open(_CONFIG_FILE) as f:
            return {**DEFAULTS, **json.load(f)}
    return dict(DEFAULTS)


def save(config: dict[str, str | int]) -> None:
    _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(_CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=2)


def get_workdir() -> Path:
    return Path(str(load()["workdir"])).expanduser()
