"""Pytest entry point for the web app's deferred-search assertions.

The behaviour is JavaScript, so the assertions are too — they live in
``tests/js/search_test.mjs``, which drives the real handlers against a DOM stub
rather than inspecting the source. Where no JS runtime is installed the test
skips rather than fails.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

HARNESS = Path(__file__).parent / "js" / "search_test.mjs"


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_search_only_runs_when_submitted() -> None:
    result = subprocess.run(
        ["node", str(HARNESS)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    # The harness prints one line per failing assertion; surface all of it,
    # since a bare exit code says nothing about which behaviour regressed.
    assert result.returncode == 0, f"\n{result.stdout}{result.stderr}"


def test_search_harness_points_at_the_shipped_app() -> None:
    """Guard against the harness silently testing a file that moved."""
    app = (
        Path(__file__).parent.parent
        / "src" / "ilan" / "web" / "static" / "app.js"
    )
    assert app.is_file()
    assert "app.js" in HARNESS.read_text()
