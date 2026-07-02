"""Tests for ilan.gist — Gist mirroring helpers and the async GistSyncer.

No test here touches the real GitHub API: the network calls
(``create_gist`` / ``post_comment``) are monkeypatched, so the suite exercises
the syncer's bookkeeping (gist creation, incremental comment posting, dedup,
error resilience) entirely offline.
"""

from __future__ import annotations

import email.message
import threading
import urllib.error
from pathlib import Path

import pytest

import ilan.gist as gist_mod
from ilan.gist import (
    GistSyncer,
    format_comment,
    gist_enabled,
    github_token,
    initial_file,
)
from ilan.models import LogEntry, Task
from ilan.store import Store


# ── pure helpers ────────────────────────────────────────────────────────


class TestHelpers:
    def test_safe_filename(self) -> None:
        assert gist_mod._safe_filename("my task/1") == "my_task_1.md"
        assert gist_mod._safe_filename("clean-name_1.2") == "clean-name_1.2.md"

    def test_safe_filename_empty_fallback(self) -> None:
        assert gist_mod._safe_filename("///") == "conversation.md"

    def test_initial_file(self) -> None:
        filename, content = initial_file("task-abc")
        assert filename == "task-abc.md"
        assert "task-abc" in content
        assert content.startswith("# ilan task: task-abc")

    def test_format_comment_user(self) -> None:
        entry = LogEntry(role="user", content="hello **world**", timestamp="")
        out = format_comment(entry)
        assert out == "**User**\n\nhello **world**"

    def test_format_comment_assistant(self) -> None:
        entry = LogEntry(role="assistant", content="done", timestamp="")
        out = format_comment(entry)
        assert out == "**Assistant**\n\ndone"

    def test_format_comment_unknown_role(self) -> None:
        entry = LogEntry(role="system", content="x", timestamp="")
        out = format_comment(entry)
        assert out.startswith("**system**")

    def test_format_comment_truncates(self) -> None:
        big = "x" * (gist_mod._MAX_COMMENT_CHARS + 500)
        entry = LogEntry(role="user", content=big, timestamp="")
        out = format_comment(entry)
        assert "…[truncated]" in out
        assert len(out) < len(big) + 200


# ── _api_request retry / backoff ────────────────────────────────────────


class _FakeResp:
    """Minimal context-manager stand-in for an ``http.client.HTTPResponse``."""

    def __init__(self, body: bytes = b"{}") -> None:
        self._body = body

    def __enter__(self) -> "_FakeResp":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def read(self) -> bytes:
        return self._body


def _http_error(code: int, retry_after: str | None = None) -> urllib.error.HTTPError:
    hdrs = email.message.Message()
    if retry_after is not None:
        hdrs["Retry-After"] = retry_after
    return urllib.error.HTTPError("https://x", code, "err", hdrs, None)


class TestApiRequestRetry:
    def test_retries_transient_urlerror_then_succeeds(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = {"n": 0}

        def fake_urlopen(req, timeout=None):  # noqa: ANN001
            calls["n"] += 1
            if calls["n"] < 3:
                raise urllib.error.URLError("handshake timed out")
            return _FakeResp(b'{"ok": true}')

        sleeps: list[float] = []
        monkeypatch.setattr(gist_mod.urllib.request, "urlopen", fake_urlopen)
        monkeypatch.setattr(gist_mod.time, "sleep", lambda s: sleeps.append(s))

        out = gist_mod._api_request("GET", "/x", "tok")
        assert out == {"ok": True}
        assert calls["n"] == 3
        # Two failures → two backoff sleeps, growing exponentially.
        assert len(sleeps) == 2
        assert sleeps[1] > sleeps[0]

    def test_retries_timeout_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = {"n": 0}

        def fake_urlopen(req, timeout=None):  # noqa: ANN001
            calls["n"] += 1
            if calls["n"] == 1:
                raise TimeoutError("ssl handshake")
            return _FakeResp(b"{}")

        monkeypatch.setattr(gist_mod.urllib.request, "urlopen", fake_urlopen)
        monkeypatch.setattr(gist_mod.time, "sleep", lambda s: None)
        assert gist_mod._api_request("POST", "/x", "tok", {"a": 1}) == {}
        assert calls["n"] == 2

    def test_retries_5xx(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = {"n": 0}

        def fake_urlopen(req, timeout=None):  # noqa: ANN001
            calls["n"] += 1
            if calls["n"] < 2:
                raise _http_error(502)
            return _FakeResp(b"{}")

        monkeypatch.setattr(gist_mod.urllib.request, "urlopen", fake_urlopen)
        monkeypatch.setattr(gist_mod.time, "sleep", lambda s: None)
        gist_mod._api_request("GET", "/x", "tok")
        assert calls["n"] == 2

    def test_no_retry_on_non_retryable_status(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = {"n": 0}

        def fake_urlopen(req, timeout=None):  # noqa: ANN001
            calls["n"] += 1
            raise _http_error(404)

        monkeypatch.setattr(gist_mod.urllib.request, "urlopen", fake_urlopen)
        monkeypatch.setattr(gist_mod.time, "sleep", lambda s: None)
        with pytest.raises(urllib.error.HTTPError):
            gist_mod._api_request("GET", "/x", "tok")
        assert calls["n"] == 1  # failed fast, no retry

    def test_gives_up_after_max_retries(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = {"n": 0}

        def fake_urlopen(req, timeout=None):  # noqa: ANN001
            calls["n"] += 1
            raise urllib.error.URLError("down")

        monkeypatch.setattr(gist_mod.urllib.request, "urlopen", fake_urlopen)
        monkeypatch.setattr(gist_mod.time, "sleep", lambda s: None)
        with pytest.raises(urllib.error.URLError):
            gist_mod._api_request("GET", "/x", "tok")
        assert calls["n"] == gist_mod._MAX_RETRIES

    def test_honors_retry_after_header(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = {"n": 0}

        def fake_urlopen(req, timeout=None):  # noqa: ANN001
            calls["n"] += 1
            if calls["n"] == 1:
                raise _http_error(429, retry_after="7")
            return _FakeResp(b"{}")

        sleeps: list[float] = []
        monkeypatch.setattr(gist_mod.urllib.request, "urlopen", fake_urlopen)
        monkeypatch.setattr(gist_mod.time, "sleep", lambda s: sleeps.append(s))
        gist_mod._api_request("POST", "/x", "tok", {"b": 2})
        assert sleeps == [7.0]

    def test_backoff_capped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def fake_urlopen(req, timeout=None):  # noqa: ANN001
            raise urllib.error.URLError("down")

        sleeps: list[float] = []
        monkeypatch.setattr(gist_mod.urllib.request, "urlopen", fake_urlopen)
        monkeypatch.setattr(gist_mod.time, "sleep", lambda s: sleeps.append(s))
        with pytest.raises(urllib.error.URLError):
            gist_mod._api_request("GET", "/x", "tok")
        assert all(s <= gist_mod._MAX_BACKOFF_SECONDS for s in sleeps)


# ── token / enabled ─────────────────────────────────────────────────────


class TestEnabled:
    def test_disabled_by_default(self, tmp_config: Path) -> None:
        assert github_token() == ""
        assert gist_enabled() is False

    def test_enabled_when_token_set(self, tmp_config: Path) -> None:
        import ilan.config as cfg

        c = cfg.load()
        c["github-token"] = "ghp_secret"
        cfg.save(c)
        assert github_token() == "ghp_secret"
        assert gist_enabled() is True


# ── GistSyncer ──────────────────────────────────────────────────────────


@pytest.fixture()
def store(tmp_workdir: Path) -> Store:
    return Store(tmp_workdir)


@pytest.fixture()
def syncer(store: Store) -> GistSyncer:
    return GistSyncer(store, threading.Lock())


class _FakeGitHub:
    """Records create_gist / post_comment calls in place of the real API."""

    def __init__(self) -> None:
        self.created: list[tuple[str, str]] = []
        self.comments: list[tuple[str, str]] = []
        self._counter = 0

    def create_gist(self, token, filename, content, description):  # noqa: ANN001
        self._counter += 1
        gid = f"gid-{self._counter}"
        self.created.append((filename, description))
        return gid, f"https://gist.github.com/u/{gid}"

    def post_comment(self, token, gist_id, body):  # noqa: ANN001
        self.comments.append((gist_id, body))


@pytest.fixture()
def fake_gh(monkeypatch: pytest.MonkeyPatch) -> _FakeGitHub:
    fake = _FakeGitHub()
    monkeypatch.setattr(gist_mod, "github_token", lambda: "tok")
    monkeypatch.setattr(gist_mod, "gist_enabled", lambda: True)
    monkeypatch.setattr(gist_mod, "create_gist", fake.create_gist)
    monkeypatch.setattr(gist_mod, "post_comment", fake.post_comment)
    return fake


class TestSyncTask:
    def test_creates_gist_and_posts_all(
        self, store: Store, syncer: GistSyncer, fake_gh: _FakeGitHub
    ) -> None:
        store.put_task(Task(name="t1", prompt="p"))
        store.append_log("t1", "user", "hi")
        store.append_log("t1", "assistant", "yo")

        syncer.sync_task("t1")

        assert len(fake_gh.created) == 1
        assert len(fake_gh.comments) == 2
        task = store.get_task("t1")
        assert task is not None
        assert task.gist_id == "gid-1"
        assert task.gist_url.endswith("gid-1")
        assert task.gist_synced_count == 2

    def test_incremental_only_posts_new(
        self, store: Store, syncer: GistSyncer, fake_gh: _FakeGitHub
    ) -> None:
        store.put_task(Task(name="t1", prompt="p"))
        store.append_log("t1", "user", "hi")
        syncer.sync_task("t1")
        assert len(fake_gh.comments) == 1

        store.append_log("t1", "assistant", "reply")
        syncer.sync_task("t1")
        # Only the new message is posted; gist is not recreated.
        assert len(fake_gh.created) == 1
        assert len(fake_gh.comments) == 2
        task = store.get_task("t1")
        assert task is not None
        assert task.gist_synced_count == 2

    def test_backfills_existing_history(
        self, store: Store, syncer: GistSyncer, fake_gh: _FakeGitHub
    ) -> None:
        # A pre-existing task with history but no gist yet: first sync mirrors
        # the ENTIRE conversation.
        store.put_task(Task(name="old", prompt="p"))
        for i in range(4):
            store.append_log("old", "user", f"m{i}")
        syncer.sync_task("old")
        assert len(fake_gh.created) == 1
        assert len(fake_gh.comments) == 4

    def test_no_gist_when_no_entries(
        self, store: Store, syncer: GistSyncer, fake_gh: _FakeGitHub
    ) -> None:
        store.put_task(Task(name="empty", prompt="p"))
        syncer.sync_task("empty")
        assert fake_gh.created == []
        assert fake_gh.comments == []

    def test_missing_task_is_noop(
        self, syncer: GistSyncer, fake_gh: _FakeGitHub
    ) -> None:
        syncer.sync_task("ghost")
        assert fake_gh.created == []

    def test_disabled_no_token(
        self, store: Store, syncer: GistSyncer, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(gist_mod, "github_token", lambda: "")
        store.put_task(Task(name="t1", prompt="p"))
        store.append_log("t1", "user", "hi")
        syncer.sync_task("t1")
        task = store.get_task("t1")
        assert task is not None
        assert task.gist_id is None

    def test_partial_post_failure_advances_count(
        self, store: Store, syncer: GistSyncer, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If posting fails mid-way, already-posted comments still count so a
        retry doesn't double-post them."""
        monkeypatch.setattr(gist_mod, "github_token", lambda: "tok")
        monkeypatch.setattr(
            gist_mod, "create_gist", lambda *a, **k: ("gid-x", "https://g/gid-x")
        )
        calls = {"n": 0}

        def flaky_post(token, gist_id, body):  # noqa: ANN001
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("boom")

        monkeypatch.setattr(gist_mod, "post_comment", flaky_post)

        store.put_task(Task(name="t1", prompt="p"))
        store.append_log("t1", "user", "a")
        store.append_log("t1", "assistant", "b")
        store.append_log("t1", "user", "c")

        with pytest.raises(RuntimeError):
            syncer.sync_task("t1")

        task = store.get_task("t1")
        assert task is not None
        # First comment posted before the failure counts.
        assert task.gist_synced_count == 1
        assert task.gist_id == "gid-x"


class TestEnqueue:
    def test_enqueue_dedups(
        self, syncer: GistSyncer, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(gist_mod, "gist_enabled", lambda: True)
        syncer.enqueue("t1")
        syncer.enqueue("t1")
        assert syncer._queue.qsize() == 1

    def test_enqueue_noop_when_disabled(
        self, syncer: GistSyncer, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(gist_mod, "gist_enabled", lambda: False)
        syncer.enqueue("t1")
        assert syncer._queue.qsize() == 0

    def test_worker_processes_queue(
        self, store: Store, syncer: GistSyncer, fake_gh: _FakeGitHub
    ) -> None:
        store.put_task(Task(name="t1", prompt="p"))
        store.append_log("t1", "user", "hi")
        syncer.start()
        try:
            syncer.enqueue("t1")
            deadline = threading.Event()
            for _ in range(50):
                if store.get_task("t1").gist_id is not None:
                    break
                deadline.wait(0.05)
        finally:
            syncer.stop()
        task = store.get_task("t1")
        assert task is not None
        assert task.gist_id == "gid-1"

    def test_worker_survives_sync_error(
        self, store: Store, syncer: GistSyncer, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(gist_mod, "gist_enabled", lambda: True)

        def boom(name):  # noqa: ANN001
            raise RuntimeError("kaboom")

        monkeypatch.setattr(syncer, "sync_task", boom)
        syncer.start()
        try:
            syncer.enqueue("t1")
            threading.Event().wait(0.2)
            # Worker thread must still be alive after swallowing the error.
            assert syncer._thread is not None
            assert syncer._thread.is_alive()
        finally:
            syncer.stop()
