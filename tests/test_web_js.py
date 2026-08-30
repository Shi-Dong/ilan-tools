"""Run the web app's behavioural assertions, which are themselves JavaScript.

The app is a browser script, so asserting on its behaviour means running it:
each ``tests/js/*_test.mjs`` evaluates the shipped ``app.js`` against a DOM
stub and drives its real handlers. This module is the pytest entry point for
all of them.

There was one wrapper module per harness, each an identical ``subprocess.run``
around a different path. Discovering them instead means a new harness is picked
up by dropping the file in, and there is no second place to remember to edit.

The ``*_dump.mjs`` files in the same directory are deliberately not collected:
they print JSON for a Python test to compare against the CLI's own formatters,
so they are run by the test that owns the comparison, not by this one.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

JS_DIR = Path(__file__).parent / "js"
HARNESSES = sorted(JS_DIR.glob("*_test.mjs"))

needs_node = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is not installed",
)


def test_the_harnesses_were_found() -> None:
    """Discovery failing silently would look exactly like everything passing."""
    assert HARNESSES, f"no *_test.mjs harnesses under {JS_DIR}"


@needs_node
@pytest.mark.parametrize("harness", HARNESSES, ids=lambda p: p.stem)
def test_web_behaviour(harness: Path) -> None:
    result = subprocess.run(
        ["node", str(harness)], capture_output=True, text=True, timeout=120,
    )
    # Each harness prints one line per failing assertion; surface all of it,
    # since a bare exit code says nothing about which behaviour regressed.
    assert result.returncode == 0, f"\n{result.stdout}{result.stderr}"
