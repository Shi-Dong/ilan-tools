"""Generate a one-line summary of a task's most recent exchange.

Called from :mod:`ilan.runner` when an agent finishes a turn (status
transitioning from ``WORKING`` to ``NEEDS_ATTENTION`` or ``AGENT_FINISHED``).

The summary is produced by sending the last user message + the new
assistant message to Anthropic's newest Haiku model. The backend depends
on the ``api-key`` config:

* When ``api-key`` is set, the summary is produced by a direct HTTPS call
  to Anthropic's Messages API (pay-per-token).
* When ``api-key`` is empty, we fall back to the local ``claude`` CLI in
  print mode (``claude -p``), which authenticates with the machine's
  Claude Code subscription. This needs ``claude`` on ``PATH`` and a
  logged-in session.

If neither backend can produce a summary the call returns ``None`` so
callers can fall back gracefully.
"""

from __future__ import annotations

import json
import subprocess
import urllib.error
import urllib.request

from ilan import config as cfg

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_API_VERSION = "2023-06-01"

# Newest Haiku model snapshot at time of writing (Claude Haiku 4.5).
HAIKU_MODEL = "claude-haiku-4-5-20251001"

# Model alias passed to `claude -p` to generate the one-liner; resolves to the
# latest Haiku snapshot.
CLAUDE_ONELINER_MODEL = "haiku"

_MAX_WORDS = 20
_MAX_INPUT_CHARS = 4000  # truncate very long messages before sending
_REQUEST_TIMEOUT_SECONDS = 30
# The CLI cold-starts a Node process, so it needs a longer leash than the
# raw HTTP call.
_CLAUDE_CLI_TIMEOUT_SECONDS = 60


SYSTEM_PROMPT = (
    "You write one-line status summaries for a developer tool. "
    f"Given the last user message and the assistant's new reply from a coding "
    f"agent's conversation, write ONE concise sentence (strictly at most "
    f"{_MAX_WORDS} words) describing what the assistant just did or is "
    "blocked on. Output the sentence only — no quotes, no preamble, no "
    "trailing punctuation beyond a single period."
)


def _truncate(text: str, limit: int = _MAX_INPUT_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "\n...[truncated]"


def _build_user_prompt(last_user: str, last_assistant: str) -> str:
    return (
        f"## Last user message\n\n{_truncate(last_user)}\n\n"
        f"## Latest assistant reply\n\n{_truncate(last_assistant)}\n\n"
        f"Write the one-line summary now."
    )


def _trim_to_words(text: str, max_words: int = _MAX_WORDS) -> str:
    """Clip *text* to the first *max_words* whitespace-separated tokens."""
    words = text.split()
    if len(words) <= max_words:
        return text.strip()
    return " ".join(words[:max_words]).rstrip(",;:") + "…"


def _call_haiku(api_key: str, prompt: str) -> str:
    """POST the prompt to the Anthropic Messages API and return the text."""
    body = json.dumps({
        "model": HAIKU_MODEL,
        "max_tokens": 80,
        "system": SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": prompt}],
    }).encode()

    req = urllib.request.Request(
        ANTHROPIC_API_URL,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": ANTHROPIC_API_VERSION,
        },
    )
    with urllib.request.urlopen(req, timeout=_REQUEST_TIMEOUT_SECONDS) as resp:
        payload = json.loads(resp.read().decode())

    blocks = payload.get("content", [])
    parts = [b.get("text", "") for b in blocks if b.get("type") == "text"]
    return "".join(parts).strip()


def _call_claude_cli(prompt: str) -> str:
    """Generate the summary with the local ``claude`` CLI (``claude -p``).

    Used when no ``api-key`` is configured: the CLI authenticates with the
    machine's Claude Code subscription. Requires ``claude`` on ``PATH`` and
    a logged-in session.
    """
    result = subprocess.run(
        [
            "claude",
            "-p",
            "--model",
            CLAUDE_ONELINER_MODEL,
            "--system-prompt",
            SYSTEM_PROMPT,
        ],
        input=prompt,
        capture_output=True,
        text=True,
        timeout=_CLAUDE_CLI_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"claude CLI exited {result.returncode}: {result.stderr.strip()}"
        )
    return result.stdout.strip()


def generate_one_liner(last_user: str, last_assistant: str) -> str | None:
    """Produce a one-line summary, or ``None`` if it cannot be generated.

    Picks the backend by config: a non-empty ``api-key`` uses Anthropic's
    Messages API, otherwise it falls back to the local ``claude`` CLI.
    Returns ``None`` when the assistant text is empty or whichever backend
    fails. Never raises — a failure here must not break the scheduler's
    reap path.
    """
    if not last_assistant.strip():
        return None
    api_key = str(cfg.load().get("api-key", "")).strip()

    prompt = _build_user_prompt(last_user, last_assistant)
    try:
        if api_key:
            text = _call_haiku(api_key, prompt)
        else:
            text = _call_claude_cli(prompt)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError):
        return None
    except (OSError, subprocess.SubprocessError):
        return None
    except Exception:
        return None

    if not text:
        return None

    # Collapse whitespace + clip to the 20-word limit so a chatty model
    # can't blow out the status cell.
    text = " ".join(text.split())
    return _trim_to_words(text)
