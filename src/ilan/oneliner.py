"""Generate a one-line summary of a task's most recent exchange.

Called from :mod:`ilan.runner` when an agent finishes a turn (status
transitioning from ``WORKING`` to ``NEEDS_ATTENTION`` or ``AGENT_FINISHED``).

The summary is produced by sending the last user message + the new
assistant message to OpenAI's GPT-5.6 Luna. The backend depends
on the ``api-key-codex`` config:

* When ``api-key-codex`` is set, the summary is produced by a direct HTTPS call
  to OpenAI's Chat Completions API (pay-per-token).
* When ``api-key-codex`` is empty, we fall back to the local ``codex`` CLI in
  non-interactive mode (``codex exec``), which authenticates with the machine's
  ``codex login`` session. This needs ``codex`` on ``PATH`` and a
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

OPENAI_API_URL = "https://api.openai.com/v1/chat/completions"

# Model used for the one-liner, on both the API and the CLI path. Luna is the
# small/fast member of the GPT-5.6 family, which suits a 20-word summary.
ONELINER_MODEL = "gpt-5.6-luna"

# Luna is a reasoning model: at its default effort it spends more tokens
# thinking than a 20-word summary needs. "none" is the cheapest setting it
# accepts (it rejects "minimal").
_REASONING_EFFORT = "none"

_MAX_WORDS = 20
_MAX_INPUT_CHARS = 4000  # truncate very long messages before sending
_MAX_OUTPUT_TOKENS = 200
_REQUEST_TIMEOUT_SECONDS = 30
# The CLI cold-starts a Node process, so it needs a longer leash than the
# raw HTTP call.
_CODEX_CLI_TIMEOUT_SECONDS = 60


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


def _call_luna(api_key: str, prompt: str) -> str:
    """POST the prompt to OpenAI's Chat Completions API and return the text."""
    body = json.dumps({
        "model": ONELINER_MODEL,
        # Luna rejects the legacy ``max_tokens`` parameter outright.
        "max_completion_tokens": _MAX_OUTPUT_TOKENS,
        "reasoning_effort": _REASONING_EFFORT,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
    }).encode()

    req = urllib.request.Request(
        OPENAI_API_URL,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
    )
    with urllib.request.urlopen(req, timeout=_REQUEST_TIMEOUT_SECONDS) as resp:
        payload = json.loads(resp.read().decode())

    choices = payload.get("choices") or []
    if not choices:
        return ""
    message = choices[0].get("message") or {}
    return (message.get("content") or "").strip()


def _parse_codex_events(stdout: str) -> str:
    """Pull the agent's message text out of a ``codex exec --json`` stream."""
    text = ""
    for raw in stdout.splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") != "item.completed":
            continue
        item = event.get("item") or {}
        if item.get("type") == "agent_message":
            message = item.get("text")
            if isinstance(message, str):
                text = message
    return text.strip()


def _call_codex_cli(prompt: str) -> str:
    """Generate the summary with the local ``codex`` CLI (``codex exec``).

    Used when no ``api-key-codex`` is configured: the CLI authenticates with the
    machine's ``codex login`` session. Requires ``codex`` on ``PATH`` and
    a logged-in session.
    """
    result = subprocess.run(
        [
            "codex",
            "exec",
            "--model",
            ONELINER_MODEL,
            "--json",
            # The server's cwd is not necessarily a git repo. Note there is no
            # approval/sandbox bypass here: summarising text needs no tools, and
            # the prompt embeds agent output we should not hand a shell to.
            "--skip-git-repo-check",
            "-",
        ],
        # codex exec has no --system-prompt flag, so the instructions ride along
        # at the top of the stdin prompt.
        input=f"{SYSTEM_PROMPT}\n\n{prompt}",
        capture_output=True,
        text=True,
        timeout=_CODEX_CLI_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"codex CLI exited {result.returncode}: {result.stderr.strip()}"
        )
    return _parse_codex_events(result.stdout)


def generate_one_liner(last_user: str, last_assistant: str) -> str | None:
    """Produce a one-line summary, or ``None`` if it cannot be generated.

    Picks the backend by config: a non-empty ``api-key-codex`` uses OpenAI's
    Chat Completions API, otherwise it falls back to the local ``codex`` CLI.
    Returns ``None`` when the assistant text is empty or whichever backend
    fails. Never raises — a failure here must not break the reaper's
    reap path.
    """
    if not last_assistant.strip():
        return None
    api_key = str(cfg.load().get("api-key-codex", "")).strip()

    prompt = _build_user_prompt(last_user, last_assistant)
    try:
        text = _call_luna(api_key, prompt) if api_key else _call_codex_cli(prompt)
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
