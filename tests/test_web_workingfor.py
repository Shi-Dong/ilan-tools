"""Pytest entry point for the WORKING duration shown beside the status.

Assertions live in ``tests/js/workingfor_test.mjs``. Skips where no JS runtime
is installed.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

HARNESS = Path(__file__).parent / "js" / "workingfor_test.mjs"
STATIC = Path(__file__).parent.parent / "src" / "ilan" / "web" / "static"


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_working_tasks_report_how_long_they_have_been_working() -> None:
    result = subprocess.run(
        ["node", str(HARNESS)], capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, f"\n{result.stdout}{result.stderr}"


def test_the_duration_is_measured_from_when_the_task_started_working() -> None:
    """created_at would count time spent queued or in an earlier status."""
    js = (STATIC / "app.js").read_text()
    assert "secondsSince(task.status_changed_at)" in js
