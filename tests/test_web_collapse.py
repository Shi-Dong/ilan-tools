"""Pytest entry point for the collapsible-card assertions.

The behaviour is JavaScript, so the assertions are too — they live in
``tests/js/collapse_test.mjs``, which clicks the real disclosure handlers
against a DOM stub rather than inspecting the source. Where no JS runtime is
installed the test skips rather than fails.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

HARNESS = Path(__file__).parent / "js" / "collapse_test.mjs"


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_cards_collapse_and_remember() -> None:
    result = subprocess.run(
        ["node", str(HARNESS)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, f"\n{result.stdout}{result.stderr}"


def test_collapsed_view_hides_exactly_what_ls_c_omits() -> None:
    """The collapsed view is defined by CSS, so assert the rules exist.

    ``ilan ls -c`` prints the pin, alias, name, unread marker and status. The
    summary, the engine, the age and the sleep suffix are what it leaves out,
    and those are hidden by class rather than by a second rendering path — so
    losing one of these selectors would quietly put the detail back.
    """
    css = (
        Path(__file__).parent.parent
        / "src" / "ilan" / "web" / "static" / "app.css"
    ).read_text()

    for selector in (".card.collapsed .row-sum",
                     ".card.collapsed .meta-detail",
                     ".card.collapsed .sleep"):
        assert selector in css, f"{selector} is no longer hidden when collapsed"

    # The status must NOT be hidden: it is one of the three things that stay.
    assert ".card.collapsed .status" not in css
