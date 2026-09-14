"""Local server discovery shared by the CLI, HTTP client and server."""

from __future__ import annotations

import json
import os
from pathlib import Path

from ilan import config as cfg


# ── PID file helpers ─────────────────────────

def pid_file_path() -> Path:
    return cfg.get_workdir() / "server.pid"


def read_server_info() -> dict | None:
    """Return ``{"pid": int, "port": int}`` if the server is alive, else *None*."""
    pf = pid_file_path()
    if not pf.exists():
        return None
    try:
        with open(pf) as f:
            info = json.load(f)
    except (json.JSONDecodeError, PermissionError):
        pf.unlink(missing_ok=True)
        return None
    try:
        os.kill(info["pid"], 0)
    except PermissionError:
        # EPERM means the pid exists but belongs to another user (e.g. a
        # client account probing a server started by a different account).
        # The server is alive — don't delete its pid file.
        pass
    except (ProcessLookupError, KeyError):
        pf.unlink(missing_ok=True)
        return None
    return info


# ── server owner pinning ─────────────────────

def server_owner_path() -> Path:
    return cfg.get_workdir() / "server.owner"


def read_server_owner() -> str | None:
    """Return the account server startup is pinned to, or *None*.

    A ``server.owner`` file in the workdir (containing a username) pins
    server startup to that account.  This matters when several accounts
    share one workdir (e.g. on a ``noowners`` volume): a server started
    under another account spawns agents the pinned owner cannot signal,
    so every kill/reply on those tasks fails with EPERM.
    """
    path = server_owner_path()
    if not path.exists():
        return None
    return path.read_text().strip() or None
