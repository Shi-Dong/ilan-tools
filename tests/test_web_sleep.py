"""Cross-check the web app's SLEEPING progress against the terminal helpers."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from ilan.time_format import _format_progress_duration

HARNESS = Path(__file__).parent / "js" / "sleep_dump.mjs"

# (label, status, sleep_seconds, elapsed_seconds)
CASES = [
    ("sleeping_half", "SLEEPING", 300, 150),
    ("sleeping_overdue", "SLEEPING", 300, 600),
    ("sleeping_hours", "SLEEPING", 4 * 3600, 2 * 3600 + 38 * 60),
    ("sleeping_no_total", "SLEEPING", None, 150),
    ("working_with_stale_sleep", "WORKING", 300, 150),
    ("finished_with_stale_sleep", "AGENT_FINISHED", 300, 150),
]


def _render() -> dict:
    result = subprocess.run(
        ["node", str(HARNESS), json.dumps(CASES)],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, f"\n{result.stdout}{result.stderr}"
    return json.loads(result.stdout)


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_sleep_progress_matches_the_terminal_duration_format() -> None:
    rendered = _render()

    for label, status, total, elapsed in CASES:
        progress = rendered[label]
        if status != "SLEEPING" or not total:
            assert progress is None, f"{label} unexpectedly rendered {progress}"
            continue
        expected = (
            f"{_format_progress_duration(elapsed)} / "
            f"{_format_progress_duration(total)}"
        )
        assert progress["time"] == expected
        assert progress["now"] == min(total, elapsed)
        assert progress["max"] == total


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_sleep_progress_bar_has_the_right_fraction_and_accessible_label() -> None:
    rendered = _render()
    half = rendered["sleeping_half"]
    assert half["now"] / half["max"] == 0.5
    assert half["label"] == "2m30s of 5m slept"

    overdue = rendered["sleeping_overdue"]
    assert overdue["now"] / overdue["max"] == 1.0
    assert overdue["time"] == "10m / 5m"
    assert overdue["label"] == "10m of 5m slept"


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_sleep_duration_is_not_repeated_beside_the_task_name() -> None:
    rendered = _render()
    assert not rendered["_old_suffix_present"]
