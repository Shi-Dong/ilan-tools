"""Cross-check the web app's duration rendering against the CLI's.

The requirement is that a ``reply -t`` interval reads the same on a phone as it
does in ``ilan dashboard`` — which means the two implementations have to agree,
not merely that each looks sensible alone. So this runs the JavaScript and
compares its output to the Python for the same inputs.

The input list is chosen around the places the two could plausibly diverge: the
1800s minutes/hours threshold, and 1799 vs 1800, where an implementation that
rounded instead of truncating would print 30m and disagree.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from ilan.cli import (
    _format_compact_duration,
    _format_reply_every_suffix,
    _format_sleep_suffix,
)

HARNESS = Path(__file__).parent / "js" / "duration_dump.mjs"

SECONDS = [
    -5, 0,                      # no cycle: both sides must render nothing
    1, 5, 30, 59,               # sub-minute, clamped to a nonzero tenth
    60, 90, 119, 300,
    1199, 1200,                 # 1200 is REPLY_EVERY_MIN_SECONDS, the real floor
    1799, 1800, 1801,           # the minutes/hours threshold
    2160, 3600, 5400, 7199, 86400,
]


def _render() -> dict:
    result = subprocess.run(
        ["node", str(HARNESS), json.dumps(SECONDS)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, f"\n{result.stdout}{result.stderr}"
    return json.loads(result.stdout)


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_web_duration_matches_the_cli() -> None:
    rendered = _render()

    for secs in SECONDS:
        got = rendered[str(secs)]

        # The CLI wraps its suffix in " (...)" for the table cell; the web app
        # uses the same phrase without the parentheses.
        cli_reply = _format_reply_every_suffix(secs)
        cli_sleep = _format_sleep_suffix(secs)
        assert got["replyEvery"] == (cli_reply.strip(" ()") if cli_reply else ""), secs
        assert got["sleep"] == (cli_sleep.strip(" ()") if cli_sleep else ""), secs

        # The loop marker must appear exactly when the CLI would print the
        # reply-every suffix.
        assert got["looping"] is bool(cli_reply), secs

        if secs > 0:
            assert got["compact"] == _format_compact_duration(secs), secs


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_the_reported_interval_is_not_shown_in_seconds() -> None:
    """The bug this fixes: 2160s used to render as the literal `every 2160s`."""
    got = _render()["2160"]
    assert got["compact"] == "0.6h"
    assert got["replyEvery"] == "responding every 0.6h"
    assert "2160" not in got["replyEvery"]
    assert not got["replyEvery"].endswith("s")
