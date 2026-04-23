from __future__ import annotations

import json
import re
from pathlib import Path


DEFAULTS: dict[str, str | int | bool] = {
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
    "line-number": False,
}

VALID_KEYS = set(DEFAULTS)

INT_KEYS = {"num-agents", "dashboard-interval"}
BOOL_KEYS = {"line-number"}

_CONFIG_DIR = Path("~/.config/ilan").expanduser()
_CONFIG_FILE = _CONFIG_DIR / "config.json"


def _ensure_config_file() -> None:
    """Create ``~/.config/ilan/config.json`` with defaults if it doesn't exist."""
    if not _CONFIG_FILE.exists():
        _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        with open(_CONFIG_FILE, "w") as f:
            json.dump(DEFAULTS, f, indent=2)


def load() -> dict[str, str | int | bool]:
    _ensure_config_file()
    with open(_CONFIG_FILE) as f:
        return {**DEFAULTS, **json.load(f)}


def save(config: dict[str, str | int | bool]) -> None:
    _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(_CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=2)


def get_workdir() -> Path:
    return Path(str(load()["workdir"])).expanduser()


def parse_bool(value) -> bool:
    """Coerce a config value to bool. Accepts true/false/1/0/yes/no/on/off."""
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"true", "1", "yes", "on"}


# ── last-tail cache ────────────────────────────────────────────────
# Stores the numbered assistant lines from the most recent tail of a task
# so that ``ilan reply`` can expand ``@N`` references against them.


def _last_tail_dir() -> Path:
    return _CONFIG_DIR / "last-tail"


def last_tail_path(task_name: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", task_name)
    return _last_tail_dir() / f"{safe}.json"


def save_last_tail(task_name: str, lines: list[str]) -> None:
    d = _last_tail_dir()
    d.mkdir(parents=True, exist_ok=True)
    with open(last_tail_path(task_name), "w") as f:
        json.dump({"lines": lines}, f)


def load_last_tail(task_name: str) -> list[str]:
    p = last_tail_path(task_name)
    if not p.exists():
        return []
    with open(p) as f:
        data = json.load(f)
    return list(data.get("lines", []))
