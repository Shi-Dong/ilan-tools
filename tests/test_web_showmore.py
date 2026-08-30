"""Pytest entry point for the conversation's Show More assertions.

The behaviour is JavaScript, so the assertions are too — they live in
``tests/js/showmore_test.mjs``, which drives the real render and click handler
against a DOM stub and a fetch that reproduces the server's ``?n=`` slice.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

HARNESS = Path(__file__).parent / "js" / "showmore_test.mjs"
STATIC = Path(__file__).parent.parent / "src" / "ilan" / "web" / "static"


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_show_more_reveals_one_exchange_at_a_time() -> None:
    result = subprocess.run(
        ["node", str(HARNESS)], capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, f"\n{result.stdout}{result.stderr}"


def test_the_prompt_and_full_log_views_are_gone() -> None:
    """Both were segmented-control modes; nothing should still reference them."""
    js = (STATIC / "app.js").read_text()
    assert "detailView" not in js
    assert "data-view" not in js
    assert "Full log" not in js


def test_the_conversation_asks_the_server_for_a_bounded_tail() -> None:
    """The reveal rule is the server's, not a second implementation here."""
    js = (STATIC / "app.js").read_text()
    assert "/tail?n=${state.detailShown}" in js
