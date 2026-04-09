"""Ilan CLI - manage a swarm of Claude Code agents."""

from __future__ import annotations

import subprocess
from pathlib import Path

__version__ = "0.1.0"


def get_git_commit() -> str | None:
    """Return the short git commit hash of the ilan source tree, or *None*."""
    src_dir = Path(__file__).resolve().parent
    try:
        return (
            subprocess.check_output(
                ["git", "rev-parse", "--short", "HEAD"],
                cwd=src_dir,
                stderr=subprocess.DEVNULL,
            )
            .decode()
            .strip()
        )
    except Exception:
        return None
