"""Generate a one-line summary of a task's most recent exchange.

Called from :mod:`ilan.runner` when an agent finishes a turn (status
transitioning from ``WORKING`` to ``NEEDS_ATTENTION`` or ``AGENT_FINISHED``).

The summary is produced by sending the last user message + the new
assistant message to Anthropic's newest Haiku model. The Anthropic API
key is read from the ``api-key`` config; if it is unset, summarization
is skipped and ``None`` is returned so callers can fall back gracefully.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from ilan import config as cfg

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_API_VERSION = "2023-06-01"

# Newest Haiku model snapshot at time of writing (Claude Haiku 4.5).
HAIKU_MODEL = "claude-haiku-4-5-20251001"

_MAX_WORDS = 20
_MAX_INPUT_CHARS = 4000  # truncate very long messages before sending
_REQUEST_TIMEOUT_SECONDS = 30


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


def generate_one_liner(last_user: str, last_assistant: str) -> str | None:
    """Produce a one-line summary, or ``None`` if it cannot be generated.

    Returns ``None`` when the ``api-key`` config is empty, when the API
    call fails, or when the assistant text is empty. Never raises — a
    failure here must not break the scheduler's reap path.
    """
    if not last_assistant.strip():
        return None
    api_key = str(cfg.load().get("api-key", "")).strip()
    if not api_key:
        return None

    prompt = _build_user_prompt(last_user, last_assistant)
    try:
        text = _call_haiku(api_key, prompt)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError):
        return None
    except Exception:
        return None

    if not text:
        return None

    # Collapse whitespace + clip to the 20-word limit so a chatty model
    # can't blow out the status cell.
    text = " ".join(text.split())
    return _trim_to_words(text)
