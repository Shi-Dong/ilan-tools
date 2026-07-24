"""Mirror each task's conversation to a private GitHub Gist.

Every task gets one secret Gist. Each user/assistant message is posted as a
separate Gist *comment* so GitHub renders the two roles as distinct Markdown
bubbles, giving a clean web view of the whole conversation.

The work happens on a background thread (:class:`GistSyncer`) so it never
blocks the reaper or a client reply: as soon as the agent finishes and the
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
import time
import urllib.error
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

# Mirroring a long conversation means hundreds of sequential POSTs, so a single
# transient network blip (TLS handshake timeout, dropped connection) or a
# secondary rate-limit must not abort the whole backfill. Retry those with
# exponential backoff; a non-retryable status (e.g. 401/404) still fails fast.
_MAX_RETRIES = 6
_INITIAL_BACKOFF_SECONDS = 1.0
_MAX_BACKOFF_SECONDS = 30.0
# GitHub's secondary (abuse) rate limit for creating content needs a much
# longer cooldown than an ordinary transient error — it typically wants a full
# minute or more — and it may not send a Retry-After header. Back those 403/429
# responses off hard, and always honor an explicit Retry-After in full (uncapped).
_SECONDARY_LIMIT_BACKOFF_SECONDS = 60.0
_RATE_LIMIT_STATUS = frozenset({403, 429})
_RETRYABLE_STATUS = frozenset({403, 429, 500, 502, 503, 504})
# Pace comment posting so a large backfill stays under GitHub's burst limit.
_COMMENT_THROTTLE_SECONDS = 1.0

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


_LANDING_BODY = (
    "This secret Gist mirrors the conversation between the user and the "
    "agent for this task. Each message is posted below as its own comment "
    "so the two roles render as separate Markdown bubbles.\n"
)


def _title_line(task_name: str) -> str:
    """The Gist's Markdown H1, with the task name as inline code.

    The name is refreshed on rename (see :meth:`GistSyncer.sync_task`), so it
    stays current instead of freezing at whatever the task was first called.
    """
    return f"# ilan task `{task_name}`"


def landing_content(task_name: str) -> str:
    """The full Markdown body of the Gist's landing-page file."""
    return f"{_title_line(task_name)}\n\n{_LANDING_BODY}"


def initial_file(task_name: str) -> tuple[str, str]:
    """Return ``(filename, content)`` for the Gist's landing-page file.

    The file is only a title card — the conversation itself lives in the
    comments, one message per comment.
    """
    return _safe_filename(task_name), landing_content(task_name)


# Fenced code blocks and inline code spans, captured so ``re.split`` keeps
# them as odd-indexed segments that the math conversion leaves untouched.
_CODE_SEGMENT = re.compile(r"(```.*?```|~~~.*?~~~|`[^`\n]+`)", re.DOTALL)
_DISPLAY_MATH = re.compile(r"\\\[(.+?)\\\]", re.DOTALL)
_INLINE_MATH = re.compile(r"\\\((.+?)\\\)", re.DOTALL)


def _convert_math_delimiters(text: str) -> str:
    r"""Rewrite LaTeX math delimiters into GitHub-flavored math syntax.

    The agents emit math as ``\(...\)`` / ``\[...\]``, which GitHub Markdown
    does not understand (the backslashes are simply swallowed). GitHub only
    renders ``$`...`$`` / ``$...$`` inline and ``$$...$$`` / ```` ```math ````
    blocks — verified empirically against rendered gist-comment HTML. The
    backtick-inline and math-fence forms are used because they also shield the
    math from Markdown itself (e.g. ``_`` would otherwise start italics).
    Code blocks and inline code spans are left untouched.
    """
    segments = _CODE_SEGMENT.split(text)
    for i, segment in enumerate(segments):
        if i % 2 == 1:  # a code block or inline code span
            continue
        segment = _DISPLAY_MATH.sub(
            lambda m: f"\n```math\n{m.group(1).strip()}\n```\n", segment
        )
        segment = _INLINE_MATH.sub(lambda m: f"$`{m.group(1).strip()}`$", segment)
        segments[i] = segment
    return "".join(segments)


def format_comment(entry: LogEntry) -> str:
    """Render a log entry as Markdown for a gist comment.

    The stored content is already Markdown, so we post it as-is under a short
    bold role header (all comments come from the same GitHub account, so the
    header is what tells User and Assistant bubbles apart).
    """
    label = _ROLE_LABELS.get(entry.role.strip().lower(), entry.role or "Message")
    body = _convert_math_delimiters(entry.content or "")
    if len(body) > _MAX_COMMENT_CHARS:
        body = body[:_MAX_COMMENT_CHARS] + "\n\n…[truncated]"
    rendered = f"**{label}**\n\n{body}"
    if entry.role.strip().lower() == "assistant" and entry.model:
        attribution = entry.model
        if entry.effort:
            attribution += f", effort: {entry.effort}"
        rendered += f"\n\n*(message is generated by {attribution})*"
    return rendered


def _retry_after_seconds(err: urllib.error.HTTPError) -> float | None:
    """Return GitHub's ``Retry-After`` value in seconds, or ``None`` if absent."""
    header = err.headers.get("Retry-After") if err.headers else None
    if header and header.strip().isdigit():
        return float(header.strip())
    return None


def _http_error_wait(err: urllib.error.HTTPError, backoff: float) -> float:
    """Pick how long to wait before retrying a retryable HTTP error.

    An explicit ``Retry-After`` is honored in full (never capped). A secondary
    rate-limit (403/429) with no header backs off hard; other retryable errors
    use the ordinary exponential backoff capped at ``_MAX_BACKOFF_SECONDS``.
    """
    retry_after = _retry_after_seconds(err)
    if retry_after is not None:
        return retry_after
    if err.code in _RATE_LIMIT_STATUS:
        return max(_SECONDARY_LIMIT_BACKOFF_SECONDS, min(backoff, _MAX_BACKOFF_SECONDS))
    return min(backoff, _MAX_BACKOFF_SECONDS)


def _api_request(method: str, path: str, token: str, payload: dict | None = None) -> dict:
    """Make a GitHub REST API request and return the parsed JSON response.

    Transient failures (network errors, TLS timeouts, 5xx, secondary rate
    limits) are retried with exponential backoff so a single blip mid-backfill
    doesn't lose the rest of the conversation.
    """
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
    backoff = _INITIAL_BACKOFF_SECONDS
    for attempt in range(_MAX_RETRIES):
        last = attempt == _MAX_RETRIES - 1
        try:
            with urllib.request.urlopen(req, timeout=_REQUEST_TIMEOUT_SECONDS) as resp:
                raw = resp.read().decode()
            return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as err:
            if err.code not in _RETRYABLE_STATUS or last:
                raise
            wait = _http_error_wait(err, backoff)
        except (urllib.error.URLError, TimeoutError):
            if last:
                raise
            wait = min(backoff, _MAX_BACKOFF_SECONDS)
        time.sleep(wait)
        backoff *= 2
    raise RuntimeError("unreachable: retry loop exhausted without returning")


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


def last_comment_url(token: str, gist_id: str, html_url: str) -> str:
    """Return a deep link to the most recent comment on the task's Gist.

    GitHub's comment permalink format is
    ``<gist-url>?permalink_comment_id=<id>#gistcomment-<id>``. The comments
    endpoint is paginated oldest-first, so the last comment is fetched
    directly with ``per_page=1&page=<comment count>``. Falls back to the
    plain Gist page when the Gist has no comments yet.
    """
    meta = _api_request("GET", f"/gists/{gist_id}", token)
    count = int(meta.get("comments") or 0)
    if count <= 0:
        return html_url
    comments = _api_request(
        "GET", f"/gists/{gist_id}/comments?per_page=1&page={count}", token
    )
    # The comments endpoint returns a JSON array (not the dict most other
    # endpoints return), so guard the shape before indexing.
    if not isinstance(comments, list) or not comments:
        return html_url
    comment_id = comments[-1]["id"]
    return f"{html_url}?permalink_comment_id={comment_id}#gistcomment-{comment_id}"


def fetch_gist_filename(token: str, gist_id: str) -> str | None:
    """Return the name of the Gist's landing file (its only file), or ``None``.

    A Gist file is addressed by its filename, so a title rewrite has to look up
    the current name rather than re-deriving it (the task may have been renamed
    since the file was created).
    """
    resp = _api_request("GET", f"/gists/{gist_id}", token)
    files = resp.get("files") or {}
    return next(iter(files), None)


def update_gist_title(token: str, gist_id: str, task_name: str) -> None:
    """Rewrite the landing file so its title line tracks *task_name*.

    The file is edited in place (same filename, new content); only the title
    line changes. A no-op if the Gist has somehow lost its file.
    """
    filename = fetch_gist_filename(token, gist_id)
    if filename is None:
        return
    _api_request(
        "PATCH",
        f"/gists/{gist_id}",
        token,
        {"files": {filename: {"content": landing_content(task_name)}}},
    )


class GistSyncer:
    """Background worker that mirrors task conversations to secret Gists.

    Enqueued task names are processed on a single daemon thread. The GitHub
    calls (slow) run outside the server lock; only the short task-state reads
    and writes take the lock, so the reaper and client requests are never
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
            title_name = task.gist_title_name

        entries: Sequence[LogEntry] = self.store.read_logs(name)
        if gist_id is None and not entries:
            return  # nothing to mirror yet

        if gist_id is None:
            filename, content = initial_file(display_name)
            gist_id, html_url = create_gist(token, filename, content, "ilan task")
            with self.lock:
                task = self.store.get_task(name)
                if task is None:
                    return
                task.gist_id = gist_id
                task.gist_url = html_url
                task.gist_title_name = display_name
                self.store.put_task(task)
        elif title_name != display_name:
            # The task was renamed (or predates title-name tracking): rewrite
            # the Gist's title line so it shows the current name.
            update_gist_title(token, gist_id, display_name)
            with self.lock:
                task = self.store.get_task(name)
                if task is not None:
                    task.gist_title_name = display_name
                    self.store.put_task(task)

        pending = list(entries[already:])
        posted = 0
        try:
            for i, entry in enumerate(pending):
                if i > 0:
                    # Pace multi-message backfills so a burst of hundreds of
                    # comments stays under GitHub's secondary rate limit. A
                    # single new message (the common live case) never waits.
                    time.sleep(_COMMENT_THROTTLE_SECONDS)
                post_comment(token, gist_id, format_comment(entry))
                posted += 1
        finally:
            if posted:
                with self.lock:
                    task = self.store.get_task(name)
                    if task is not None:
                        task.gist_synced_count = already + posted
                        self.store.put_task(task)
