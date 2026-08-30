"""Cross-check the web app's sleep suffix against the CLI's.

``_build_name_cell`` appends ``(sleeping for Xm)`` after a task's name, but only
while the task is WORKING — the value lingers on the task after the agent
stops, so showing it on a finished task would claim something is asleep when
nothing is running. Both halves of that (the text and the status rule) are
asserted here against the CLI's own helper rather than against a hard-coded
string, so the two cannot drift apart.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from ilan.cli import _format_sleep_suffix

HARNESS = Path(__file__).parent / "js" / "sleep_dump.mjs"

# (label, status, sleep_seconds)
CASES = [
    ("working_sleeping", "WORKING", 300),
    ("working_long_sleep", "WORKING", 5400),
    ("working_min_sleep", "WORKING", 1),
    ("working_not_sleeping", "WORKING", None),
    ("working_zero_sleep", "WORKING", 0),
    ("finished_with_stale_sleep", "AGENT_FINISHED", 300),
    ("needs_attention_with_stale_sleep", "NEEDS_ATTENTION", 300),
    ("error_with_stale_sleep", "ERROR", 300),
    ("done_with_stale_sleep", "DONE", 300),
]


def _render() -> dict:
    result = subprocess.run(
        ["node", str(HARNESS), json.dumps(CASES)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, f"\n{result.stdout}{result.stderr}"
    return json.loads(result.stdout)


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_sleep_suffix_matches_the_cli() -> None:
    rendered = _render()

    for label, status, seconds in CASES:
        cli = _format_sleep_suffix(seconds)
        # The CLI's suffix carries a leading space for the table cell; the web
        # app renders the same parenthesised text as its own element.
        expected = cli.strip() if (cli and status == "WORKING") else None
        assert rendered[label] == expected, (
            f"{label}: rendered {rendered[label]!r}, expected {expected!r}"
        )


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_a_stale_sleep_is_not_shown_on_a_stopped_task() -> None:
    """The status rule is the easy half to get wrong, so assert it directly."""
    rendered = _render()
    assert rendered["working_sleeping"] == "(sleeping for 5m)"
    for label in (
        "finished_with_stale_sleep",
        "needs_attention_with_stale_sleep",
        "error_with_stale_sleep",
        "done_with_stale_sleep",
    ):
        assert rendered[label] is None, f"{label} still advertises a sleep"
