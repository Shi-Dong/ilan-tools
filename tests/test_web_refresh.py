"""Pytest entry point for the list header's Refresh button.

Assertions live in ``tests/js/refresh_test.mjs`` and drive the real handler
against a DOM stub. Skips where no JS runtime is installed.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

HARNESS = Path(__file__).parent / "js" / "refresh_test.mjs"
STATIC = Path(__file__).parent.parent / "src" / "ilan" / "web" / "static"


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_refresh_replaces_the_all_toggle() -> None:
    result = subprocess.run(
        ["node", str(HARNESS)], capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, f"\n{result.stdout}{result.stderr}"


def test_the_show_all_state_is_gone_entirely() -> None:
    """Leaving `showAll` behind would be state nothing can ever set."""
    js = (STATIC / "app.js").read_text()
    assert "showAll" not in js
    assert "toggle-all" not in js
