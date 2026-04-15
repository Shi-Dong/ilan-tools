"""Tmux session helpers for ilan task tracking."""

from __future__ import annotations

import subprocess


def kill_tmux_sessions_by_prefix(prefix: str) -> list[str]:
    """Kill all tmux sessions whose names start with *prefix*.

    Returns the list of session names that were killed.
    """
    try:
        result = subprocess.run(
            ["tmux", "list-sessions", "-F", "#{session_name}"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []

    if result.returncode != 0:
        return []

    killed: list[str] = []
    for name in result.stdout.strip().splitlines():
        if name.startswith(prefix):
            try:
                subprocess.run(
                    ["tmux", "kill-session", "-t", name],
                    capture_output=True,
                    timeout=5,
                )
                killed.append(name)
            except (FileNotFoundError, subprocess.TimeoutExpired):
                pass
    return killed
