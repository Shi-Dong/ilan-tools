"""Mirror each task's conversation to a private GitHub Gist.

Every task gets one secret Gist. Each user/assistant message is posted as a
separate Gist *comment* so GitHub renders the two roles as distinct Markdown
bubbles, giving a clean web view of the whole conversation.

The work happens on a background thread (:class:`GistSyncer`) so it never
blocks the scheduler or a client reply: as soon as the agent finishes and the
task transitions status, the message is enqueued and mirrored asynchronously.

The feature is enabled purely by setting a ``github-token`` config value. When
no token is set, :func:`gist_enabled` is ``False`` and the syncer does nothing.
GitHub access uses the REST API directly over ``urllib`` (no ``gh`` dependency),
so it works the same on a laptop or a remote server as long as the token is set.
"""

from __future__ import annotations

import json
import queue
import re
import threading
import urllib.request
from collections.abc import Sequence

from ilan import config as cfg
from ilan.models import LogEntry
from ilan.store import Store

GITHUB_API_URL = "https://api.github.com"
GITHUB_API_VERSION = "2022-11-28"
_USER_AGENT = "ilan-cli"
_REQUEST_TIMEOUT_SECONDS = 30
# GitHub rejects gist comments larger than 65536 bytes; keep a safety margin.
_MAX_COMMENT_CHARS = 60000

_ROLE_LABELS = {"user": "User", "assistant": "Assistant"}


def github_token() -> str:
    """Return the configured GitHub token (empty string if unset)."""
    return str(cfg.load().get("github-token", "")).strip()


def gist_enabled() -> bool:
    """True when Gist mirroring is configured (a ``github-token`` is set)."""
    return bool(github_token())


def _safe_filename(task_name: str) -> str:
    """Turn a task name into a safe ``.md`` gist filename."""
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", task_name).strip("_") or "conversation"
    return f"{safe}.md"


def initial_file(task_name: str) -> tuple[str, str]:
    """Return ``(filename, content)`` for the Gist's landing-page file.

    The file is only a title card — the conversation itself lives in the
    comments, one message per comment.
    """
    filename = _safe_filename(task_name)
    content = (
        f"# ilan task: {task_name}\n\n"
        "This secret Gist mirrors the conversation between the user and the "
        "agent for this task. Each message is posted below as its own comment "
        "so the two roles render as separate Markdown bubbles.\n"
    )
    return filename, content


def format_comment(entry: LogEntry) -> str:
    """Render a log entry as Markdown for a gist comment.

    The stored content is already Markdown, so we post it as-is under a short
    bold role header (all comments come from the same GitHub account, so the
    header is what tells User and Assistant bubbles apart).
    """
    label = _ROLE_LABELS.get(entry.role.strip().lower(), entry.role or "Message")
    body = entry.content or ""
    if len(body) > _MAX_COMMENT_CHARS:
        body = body[:_MAX_COMMENT_CHARS] + "\n\n…[truncated]"
    return f"**{label}**\n\n{body}"


def _api_request(method: str, path: str, token: str, payload: dict | None = None) -> dict:
    """Make a GitHub REST API request and return the parsed JSON response."""
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        f"{GITHUB_API_URL}{path}",
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": GITHUB_API_VERSION,
            "Content-Type": "application/json",
            "User-Agent": _USER_AGENT,
        },
    )
    with urllib.request.urlopen(req, timeout=_REQUEST_TIMEOUT_SECONDS) as resp:
        raw = resp.read().decode()
    return json.loads(raw) if raw else {}


def create_gist(token: str, filename: str, content: str, description: str) -> tuple[str, str]:
    """Create a secret Gist and return ``(gist_id, html_url)``."""
    payload = {
        "description": description,
        "public": False,
        "files": {filename: {"content": content}},
    }
    resp = _api_request("POST", "/gists", token, payload)
    return resp["id"], resp["html_url"]


def post_comment(token: str, gist_id: str, body: str) -> None:
    """Post a single comment to a Gist."""
    _api_request("POST", f"/gists/{gist_id}/comments", token, {"body": body})


class GistSyncer:
    """Background worker that mirrors task conversations to secret Gists.

    Enqueued task names are processed on a single daemon thread. The GitHub
    calls (slow) run outside the server lock; only the short task-state reads
    and writes take the lock, so the scheduler and client requests are never
    blocked by network I/O.
    """

    def __init__(self, store: Store, lock: threading.Lock) -> None:
        self.store = store
        self.lock = lock
        self._queue: queue.Queue[str] = queue.Queue()
        self._pending: set[str] = set()
        self._pending_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="gist-syncer", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def enqueue(self, task_name: str) -> None:
        """Schedule *task_name* for a (deduplicated) Gist sync. Never blocks."""
        if not gist_enabled():
            return
        with self._pending_lock:
            if task_name in self._pending:
                return
            self._pending.add(task_name)
        self._queue.put(task_name)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                name = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            with self._pending_lock:
                self._pending.discard(name)
            try:
                self.sync_task(name)
            except Exception:
                # A sync failure (network, auth, deleted task) must never kill
                # the worker; the next message re-enqueues and retries.
                pass

    def sync_task(self, name: str) -> None:
        """Create the task's Gist if needed and post any unposted messages."""
        token = github_token()
        if not token:
            return

        with self.lock:
            task = self.store.get_task(name)
            if task is None:
                return
            gist_id = task.gist_id
            already = task.gist_synced_count
            display_name = task.name

        entries: Sequence[LogEntry] = self.store.read_logs(name)
        if gist_id is None and not entries:
            return  # nothing to mirror yet

        if gist_id is None:
            filename, content = initial_file(display_name)
            gist_id, html_url = create_gist(
                token, filename, content, f"ilan task: {display_name}"
            )
            with self.lock:
                task = self.store.get_task(name)
                if task is None:
                    return
                task.gist_id = gist_id
                task.gist_url = html_url
                self.store.put_task(task)

        pending = list(entries[already:])
        posted = 0
        try:
            for entry in pending:
                post_comment(token, gist_id, format_comment(entry))
                posted += 1
        finally:
            if posted:
                with self.lock:
                    task = self.store.get_task(name)
                    if task is not None:
                        task.gist_synced_count = already + posted
                        self.store.put_task(task)
