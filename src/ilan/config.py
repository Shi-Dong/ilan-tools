from __future__ import annotations

import json
from pathlib import Path


DEFAULTS: dict[str, str | int] = {
    "workdir": "~/.ilan",
    "num-agents": 5,
    "model": "opus",
    "effort": "high",
    "summarize-model": "sonnet",
    "summarize-effort": "medium",
    "time-zone": "US/Pacific",
    "editor": "emacs",
    "api-key": "",
    "dashboard-interval": 1,
}

VALID_KEYS = set(DEFAULTS)

INT_KEYS = {"num-agents", "dashboard-interval"}

_CONFIG_DIR = Path("~/.config/ilan").expanduser()
_CONFIG_FILE = _CONFIG_DIR / "config.json"


def _ensure_config_file() -> None:
    """Create ``~/.config/ilan/config.json`` with defaults if it doesn't exist."""
    if not _CONFIG_FILE.exists():
        _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        with open(_CONFIG_FILE, "w") as f:
            json.dump(DEFAULTS, f, indent=2)


def load() -> dict[str, str | int]:
    _ensure_config_file()
    with open(_CONFIG_FILE) as f:
        return {**DEFAULTS, **json.load(f)}


def save(config: dict[str, str | int]) -> None:
    _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(_CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=2)


def get_workdir() -> Path:
    return Path(str(load()["workdir"])).expanduser()
