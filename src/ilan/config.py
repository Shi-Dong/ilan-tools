from __future__ import annotations

import json
import re
from pathlib import Path


DEFAULTS: dict[str, str | int | bool] = {
    "workdir": "~/.ilan",
    "model": "opus",
    "effort": "xhigh",
    "time-zone": "US/Pacific",
    "editor": "emacs",
    "default-backend": "claude",
    "api-key-claude": "",
    "api-key-codex": "",
    "github-token": "",
    "dashboard-interval": 1,
    "line-number": False,
    "markdown": False,
    "one-line-summary": True,
}

VALID_KEYS = set(DEFAULTS)

# The intersection of the effort levels supported by both backends
# (claude --effort additionally knows "max", codex additionally knows
# "minimal"; we only allow values that mean the same thing everywhere).
VALID_EFFORTS = ("low", "medium", "high", "xhigh")

INT_KEYS = {"dashboard-interval"}
BOOL_KEYS = {"line-number", "markdown", "one-line-summary"}

# Values that should never be printed in full by `ilan config show`.
# ``ilan config show`` renders these as ``**<last-5-chars>`` so the user can
# confirm a key is set (and which one) without leaking it to the terminal.
SECRET_KEYS = {"api-key-claude", "api-key-codex", "github-token"}

# Keys whose effect is purely on the CLI running on the user's machine
# (rendering, input rewriting, etc.).  ``ilan config set`` writes these to
# the local config file instead of routing them through the server, so the
# toggle works the same way whether the server is local or remote.
CLIENT_SIDE_KEYS = {"line-number", "markdown", "time-zone", "one-line-summary"}

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
        stored = json.load(f)
    # Drop keys the current version no longer knows about, so settings
    # removed from DEFAULTS don't linger in old config files forever
    # (they disappear from `ilan config show` immediately and from the
    # file itself on the next save).
    return {**DEFAULTS, **{k: v for k, v in stored.items() if k in VALID_KEYS}}


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


# ── time-zone aliases ──────────────────────────────────────────────
# Friendly names for the handful of zones we actually work in, so users can
# write ``ilan config set time-zone tokyo`` instead of remembering ``Asia/Tokyo``.
# Note: ``atlantic`` maps to US/Eastern by request — the alias names the
# region, the value names the clock.
TIMEZONE_ALIASES: dict[str, str] = {
    "china": "Asia/Shanghai",
    "beijing": "Asia/Shanghai",
    "shanghai": "Asia/Shanghai",
    "wuhan": "Asia/Shanghai",
    "japan": "Asia/Tokyo",
    "tokyo": "Asia/Tokyo",
    "korea": "Asia/Seoul",
    "seoul": "Asia/Seoul",
    "uk": "Europe/London",
    "london": "Europe/London",
    "pacific": "US/Pacific",
    "west": "US/Pacific",
    "western": "US/Pacific",
    "atlantic": "US/Eastern",
    "east": "US/Eastern",
    "eastern": "US/Eastern",
}


def resolve_time_zone(value: str) -> str:
    """Resolve a friendly time-zone alias (case-insensitive) to an IANA name.

    Unknown values pass through unchanged (whitespace-trimmed) so a raw IANA
    name like ``Asia/Tokyo`` still works.
    """
    stripped = value.strip()
    return TIMEZONE_ALIASES.get(stripped.lower(), stripped)


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
