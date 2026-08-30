"""Pytest entry point for the revive control on a closed task.

The behavioural assertions live in ``tests/js/revive_test.mjs``; this module
runs them, and separately ties the button to the routes it posts to.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from ilan.models import TaskStatus
from ilan.server import ROUTES

HARNESS = Path(__file__).parent / "js" / "revive_test.mjs"
STATIC = Path(__file__).parent.parent / "src" / "ilan" / "web" / "static"


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_a_closed_task_can_be_reopened_from_its_detail_page() -> None:
    result = subprocess.run(
        ["node", str(HARNESS)], capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, f"\n{result.stdout}{result.stderr}"


def _revive_choices() -> dict[str, str]:
    """The status → endpoint pairs the web app's revive button is built from."""
    js = (STATIC / "app.js").read_text()
    body = re.search(r"function reviveAction\(task\) \{(.*?)\n\}", js, re.S)
    assert body, "reviveAction is gone; the revive button no longer has a source"
    return dict(re.findall(
        r"task\.status === '([A-Z_]+)'\) return \{ choice: '([a-z]+)'", body.group(1),
    ))


def test_the_button_posts_to_routes_the_server_actually_serves() -> None:
    """A renamed route would leave the button posting into a 404.

    Nothing else would notice: the button still renders, the tap still fires,
    and the failure only shows up as a toast on a phone.
    """
    served = {
        pattern for method, pattern, _ in ROUTES if method == "POST"
    }
    for status, choice in _revive_choices().items():
        assert rf"^/tasks/([^/]+)/{choice}$" in served, (
            f"the web app posts /{choice} for a {status} task, "
            "which the server does not route"
        )


def test_each_closed_status_is_reopened_by_its_own_endpoint() -> None:
    """The server refuses a mismatch, so the label must not blur the two.

    ``undone`` only accepts a DONE task and ``undiscard`` only a DISCARDED one.
    Offering one button that posts to a single endpoint would work for half the
    closed tasks and return 409 for the other half.
    """
    assert _revive_choices() == {"DONE": "undone", "DISCARDED": "undiscard"}


def test_every_closed_status_has_a_way_back() -> None:
    """Derived from the enum, so a new terminal status has to be considered.

    The detail page hides the composer for these statuses, which would leave a
    task with no action at all on screen if it also had no revive button.
    """
    closed = {
        s.value for s in TaskStatus if s in (TaskStatus.DONE, TaskStatus.DISCARDED)
    }
    js = (STATIC / "app.js").read_text()
    terminal = re.search(r"TERMINAL_STATUSES = new Set\(\[(.*?)\]\)", js)
    assert terminal, "the web app no longer marks any status terminal"
    assert set(re.findall(r"'([A-Z_]+)'", terminal.group(1))) == closed
    assert set(_revive_choices()) == closed


def test_the_revive_button_fills_the_bar_it_sits_in() -> None:
    """It is alone in a flex row, so without this it shrinks to its label.

    A short button floating at one end of the bottom bar is both harder to hit
    on a phone and easy to miss, which is the opposite of the point.
    """
    css = (STATIC / "app.css").read_text()
    rule = re.search(r"\.composer \.btn-revive \{(.*?)\}", css, re.S)
    assert rule, "the revive button has no layout rule in the composer bar"
    assert "flex: 1" in rule.group(1)
