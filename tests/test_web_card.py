"""Pytest entry point for the task-card assertions.

The behaviour is JavaScript, so the assertions are too — they live in
``tests/js/card_test.mjs``, which clicks the real handlers against a DOM stub
rather than inspecting the source. Where no JS runtime is installed the test
skips rather than fails.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

HARNESS = Path(__file__).parent / "js" / "card_test.mjs"
STATIC = Path(__file__).parent.parent / "src" / "ilan" / "web" / "static"


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_card_collapses_and_acts() -> None:
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
    css = (STATIC / "app.css").read_text()

    for selector in (".card.collapsed .row-sum",
                     ".card.collapsed .meta-detail",
                     ".card.collapsed .sleep"):
        assert selector in css, f"{selector} is no longer hidden when collapsed"

    # The status must NOT be hidden: it is one of the three things that stay.
    assert ".card.collapsed .status" not in css


def test_card_actions_are_hidden_while_collapsed() -> None:
    """Two buttons on every collapsed card would undo the density that
    collapsing by default exists to provide."""
    css = (STATIC / "app.css").read_text()
    assert ".card.collapsed .row-actions { display: none; }" in css


def test_the_card_body_is_the_toggle() -> None:
    """The chevron is gone; the row itself carries the toggle now.

    A leftover ``.disclose`` rule or handler would mean a control that is
    styled but never rendered, or rendered but never wired.
    """
    js = (STATIC / "app.js").read_text()
    css = (STATIC / "app.css").read_text()
    assert "disclose" not in js
    assert "disclose" not in css
    assert 'data-toggle="${esc(task.name)}"' in js
