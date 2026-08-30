"""Pytest entry point for the task-card assertions.

The behaviour is JavaScript, so the assertions are too — they live in
``tests/js/card_test.mjs``, which clicks the real handlers against a DOM stub
rather than inspecting the source. Where no JS runtime is installed the test
skips rather than fails.
"""

from __future__ import annotations

import re
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


def test_a_collapsed_card_hides_the_summary_and_the_metadata() -> None:
    """The collapsed view is defined by CSS, so assert the rules exist.

    A collapsed card shows the pin, alias, name, unread marker and status. The
    summary, the engine and the age are what it drops, hidden by class rather
    than by a second rendering path — so losing one of these selectors would
    quietly put the detail back.
    """
    css = (STATIC / "app.css").read_text()

    for selector in (".card.collapsed .row-sum", ".card.collapsed .meta-detail"):
        assert selector in css, f"{selector} is no longer hidden when collapsed"

    # The status must NOT be hidden: it is one of the things that stay.
    assert ".card.collapsed .status" not in css


def test_a_collapsed_card_still_shows_that_a_task_is_asleep() -> None:
    """This deliberately departs from `ls -c`, which omits the suffix.

    Most of the list is only ever seen collapsed on a phone, and whether an
    agent is asleep is worth knowing without expanding the card first.
    """
    css = (STATIC / "app.css").read_text()
    assert ".card.collapsed .sleep" not in css, (
        "the sleep suffix is hidden again when collapsed"
    )


def test_the_summary_is_not_truncated_on_an_expanded_card() -> None:
    """The one-liner may already have been shortened upstream.

    Clamping it here truncated a second time, cutting the tail off a sentence
    that was meant to be complete. The summary only renders on an expanded
    card, so there is nothing for a clamp to buy.
    """
    css = (STATIC / "app.css").read_text()
    rule = re.search(r"\.row-sum \{(.*?)\}", css, re.S)
    assert rule, ".row-sum is not styled"
    body = rule.group(1)
    assert "line-clamp" not in body, "the summary is clamped again"
    assert "-webkit-box" not in body, "the summary is clamped again"


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
