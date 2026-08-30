"""Pytest entry point for the web app's Markdown renderer assertions.

The renderer is JavaScript, so its assertions are too — they live in
``tests/js/markdown_test.mjs`` and this module runs them. Where no JS runtime
is installed the test skips rather than fails, so the Python suite still runs
on a machine without one.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

HARNESS = Path(__file__).parent / "js" / "markdown_test.mjs"


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_markdown_renderer() -> None:
    result = subprocess.run(
        ["node", str(HARNESS)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    # The harness prints one line per failing case; surface all of it, since a
    # bare exit code says nothing about which case regressed.
    assert result.returncode == 0, f"\n{result.stdout}{result.stderr}"


def test_markdown_harness_is_wired_to_the_shipped_renderer() -> None:
    """Guard against the harness silently testing a file that moved.

    ``markdown_test.mjs`` reaches for the renderer by relative path. If the
    asset were renamed, the harness would throw rather than pass vacuously —
    but only where node runs, so assert the path exists from Python too.
    """
    renderer = (
        Path(__file__).parent.parent
        / "src" / "ilan" / "web" / "static" / "markdown.js"
    )
    assert renderer.is_file()
    assert "markdown.js" in HARNESS.read_text()
