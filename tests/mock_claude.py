#!/usr/bin/env python3
"""Mock ``claude`` CLI that mimics Claude Code's ``-p`` (print) mode.

This script is used by the test suite as a drop-in replacement for the real
``claude`` binary.  It parses the same flags that ilan passes and writes a
JSON result to stdout (which ilan redirects to an output file).

Behaviour is controlled via environment variables so that individual tests
can configure different outcomes:

    MOCK_CLAUDE_STATUS
        One of "DONE", "NEEDS_ATTENTION", "ERROR", "EMPTY".
        Controls which ``[STATUS: …]`` marker appears in the response
        (or whether the output is an error / empty).  Default: "DONE".

    MOCK_CLAUDE_SESSION_ID
        The session ID to include in the JSON output.
        Default: "mock-session-0001".

    MOCK_CLAUDE_DELAY
        Seconds to sleep before writing output (simulates work).
        Default: "0".

    MOCK_CLAUDE_RESPONSE
        Override the entire assistant response text.
        When set, MOCK_CLAUDE_STATUS still controls is_error but the
        response body is taken verbatim from this variable.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time


def main() -> None:
    parser = argparse.ArgumentParser(description="Mock Claude CLI")
    parser.add_argument("-p", dest="prompt", required=True, help="Prompt text")
    parser.add_argument("--output-format", dest="output_format", default="json")
    parser.add_argument("--model", default="opus")
    parser.add_argument("--effort", default="high")
    parser.add_argument("--resume", default=None)
    parser.add_argument("--dangerously-skip-permissions", action="store_true")

    args = parser.parse_args()

    status = os.environ.get("MOCK_CLAUDE_STATUS", "DONE")
    session_id = os.environ.get("MOCK_CLAUDE_SESSION_ID", "mock-session-0001")
    delay = float(os.environ.get("MOCK_CLAUDE_DELAY", "0"))
    custom_response = os.environ.get("MOCK_CLAUDE_RESPONSE")

    if delay > 0:
        time.sleep(delay)

    cost = float(os.environ.get("MOCK_CLAUDE_COST", "0.05"))

    if status == "ERROR":
        result = {
            "session_id": session_id,
            "result": "Something went wrong.",
            "is_error": True,
            "total_cost_usd": cost,
        }
    elif status == "EMPTY":
        # Simulate a crash — write nothing
        return
    else:
        if custom_response is not None:
            response = custom_response
        elif status == "NEEDS_ATTENTION":
            response = (
                "I need your help with something.\n\n"
                "[STATUS: NEEDS_ATTENTION]"
            )
        else:
            # Default: DONE
            response = (
                f"I completed the task based on prompt: {args.prompt[:80]}\n\n"
                "[STATUS: DONE]"
            )

        result = {
            "session_id": session_id,
            "result": response,
            "is_error": False,
            "total_cost_usd": cost,
        }

    json.dump(result, sys.stdout)


if __name__ == "__main__":
    main()
