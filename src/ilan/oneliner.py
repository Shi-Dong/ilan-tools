"""Generate a one-line summary of a task's most recent exchange.

Produced when an agent finishes a turn (status transitioning from
``WORKING`` to ``NEEDS_ATTENTION`` or ``AGENT_FINISHED``). The reaper in
:mod:`ilan.runner` records the finish; :class:`Summarizer` below then writes
the summary on its own thread, off the server lock, because generating one
is a model call that can take several seconds.

The summary is produced by sending the last user message + the new
assistant message to OpenAI's GPT-6 Luna. The backend depends
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

import contextlib
import json
import queue
import subprocess
import threading
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass

from ilan import config as cfg
from ilan.models import LogEntry, Task, TaskStatus
from ilan.store import Store

OPENAI_API_URL = "https://api.openai.com/v1/chat/completions"

# The finishes that get a summary. An ERROR finish has no reply worth
# summarising, and a running task has not finished its turn yet.
SUMMARIZED_STATUSES = frozenset({TaskStatus.NEEDS_ATTENTION, TaskStatus.AGENT_FINISHED})

# Model used for the one-liner, on both the API and the CLI path. Luna is the
# small/fast member of the GPT-6 family, which suits a 20-word summary.
ONELINER_MODEL = "gpt-6-luna"

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


# ── background summarizer ────────────────────────────────────────────────

def _turn(task: Task) -> tuple[str, str]:
    """Identify the turn a task is on: its status and when it got there.

    ``set_status`` stamps a fresh time on every change, so two turns never
    compare equal even when they land on the same status.
    """
    return task.status.value, task.status_changed_at


def _last_exchange(entries: list[LogEntry]) -> tuple[str, str]:
    """The last user message and the assistant reply that closes the log.

    The reaper appends a finished turn's reply as the log's last entry, so
    that entry is what the summary describes. Anything else last (an empty
    reply is never logged) means there is nothing to summarise, and the empty
    assistant text makes :func:`generate_one_liner` answer ``None``.
    """
    if not entries or entries[-1].role != "assistant":
        return "", ""
    response = entries[-1].content
    last_user = next(
        (entry.content for entry in reversed(entries[:-1]) if entry.role == "user"),
        "",
    )
    return last_user, response


@dataclass(frozen=True)
class _SummaryJob:
    name: str
    turn: tuple[str, str]
    then: Callable[[Task], object] | None


class Summarizer:
    """Write each finished turn's one-line summary on a background thread.

    A summary is a model call: several seconds through the ``codex`` CLI, a
    second or so over the API. The reaper used to make that call while it
    held the server lock, so every ``ilan ls``, tail and reply that arrived
    in those seconds waited for it. Now the reaper only enqueues, which never
    blocks; this thread makes the call off the lock and writes the summary
    back under it. Same shape as the Gist syncer and the push notifier.

    A job names the turn it summarises — the task's status and when it
    changed — and is dropped if the task has moved on by the time it runs: a
    reply that landed meanwhile has already made the summary stale, and stale
    text must not overwrite whatever the new turn produces.
    """

    def __init__(self, store: Store, lock: threading.Lock) -> None:
        self.store = store
        self.lock = lock
        self._queue: queue.Queue[_SummaryJob] = queue.Queue()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="summarizer", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def enqueue(self, task: Task, then: Callable[[Task], object] | None = None) -> None:
        """Schedule a summary of *task*'s latest turn. Never blocks.

        *then* runs afterwards — off the lock, with the task re-read from the
        store so it carries the summary — or straight away for a turn that
        takes no summary, such as an error. It is how a phone notification
        gets to include the summary: the reaper passes ``notify_finished``.
        Like the summary itself, it is skipped when the turn was superseded.
        """
        self._queue.put(_SummaryJob(task.name, _turn(task), then))

    def summarize(self, task: Task, then: Callable[[Task], object] | None = None) -> None:
        """Do now, on the calling thread, what :meth:`enqueue` defers."""
        self._run(_SummaryJob(task.name, _turn(task), then))

    def enqueue_missing(self) -> list[str]:
        """Schedule a summary for every finished task that has none.

        For the server's startup. A finish recovered from disk was reaped
        without a summary, and a finish whose summary was still being
        written when the last server stopped never got one; both are
        finished tasks with the field empty. Nothing is chained after them:
        these finishes happened while the server was down, so no phone is
        told. Returns the names scheduled.
        """
        with self.lock:
            tasks = self.store.load_tasks()
        missing = [
            task for task in tasks.values()
            if task.status in SUMMARIZED_STATUSES and task.summary_one_liner is None
        ]
        for task in missing:
            self.enqueue(task)
        return [task.name for task in missing]

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                job = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            # A failure on one turn must not kill the thread; the next
            # finish still gets its summary.
            with contextlib.suppress(Exception):
                self._run(job)

    def _run(self, job: _SummaryJob) -> None:
        with self.lock:
            task = self._current(job)
            if task is None:
                return
            exchange = (
                _last_exchange(self.store.read_logs(task.name))
                if task.status in SUMMARIZED_STATUSES
                else None
            )
        if exchange is not None:
            # The slow part, and the reason this class exists: no lock held.
            summary = generate_one_liner(*exchange)
            with self.lock:
                task = self._current(job)
                if task is None:
                    return
                task.summary_one_liner = summary
                self.store.put_task(task)
        if job.then is not None:
            job.then(task)

    def _current(self, job: _SummaryJob) -> Task | None:
        """The task, if it is still on the turn *job* was made for."""
        task = self.store.get_task(job.name)
        if task is None or _turn(task) != job.turn:
            return None
        return task
