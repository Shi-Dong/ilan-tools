"""Integration tests for ilan.server — HTTP routes with a real server."""

from __future__ import annotations

import json
import signal
import threading
import time
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from ilan import __version__
from ilan.models import TaskStatus
from ilan.server import IlanServer


@pytest.fixture()
def ilan_server(tmp_workdir: Path, tmp_config: Path, env_with_mock_claude: None):
    """Start an IlanServer on an ephemeral port and tear it down after the test.

    The scheduler loop is patched to not auto-spawn agents, so tests can
    exercise individual routes in isolation.
    """
    import ilan.config as cfg_mod

    cfg_mod.save({**cfg_mod.DEFAULTS, "workdir": str(tmp_workdir)})

    server = IlanServer()

    # Patch schedule to be a no-op so tests control task state explicitly
    server.runner.schedule = lambda: None

    # Patch signal.signal to avoid "signal only works in main thread" error
    with patch.object(signal, "signal"):
        t = threading.Thread(target=server.run, kwargs={"host": "127.0.0.1", "port": 0}, daemon=True)
        t.start()

        # Wait for server to be ready
        deadline = time.monotonic() + 5
        port = None
        while time.monotonic() < deadline:
            if server._httpd is not None:
                port = server._httpd.server_address[1]
                break
            time.sleep(0.05)

        assert port is not None, "Server did not start in time"
        server._test_port = port  # type: ignore[attr-defined]
        server._test_url = f"http://127.0.0.1:{port}"  # type: ignore[attr-defined]

        yield server

        server.shutdown()
        t.join(timeout=3)


def _get(server: IlanServer, path: str) -> dict:
    url = f"{server._test_url}{path}"  # type: ignore[attr-defined]
    req = Request(url)
    try:
        with urlopen(req, timeout=5) as resp:
            return json.loads(resp.read())
    except HTTPError as exc:
        return json.loads(exc.read())


def _post(server: IlanServer, path: str, body: dict | None = None) -> dict:
    url = f"{server._test_url}{path}"  # type: ignore[attr-defined]
    data = json.dumps(body).encode() if body else None
    req = Request(url, data=data, method="POST")
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urlopen(req, timeout=5) as resp:
            return json.loads(resp.read())
    except HTTPError as exc:
        return json.loads(exc.read())


def _delete(server: IlanServer, path: str) -> dict:
    url = f"{server._test_url}{path}"  # type: ignore[attr-defined]
    req = Request(url, method="DELETE")
    try:
        with urlopen(req, timeout=5) as resp:
            return json.loads(resp.read())
    except HTTPError as exc:
        return json.loads(exc.read())


# ── Health & Version ────────────────────────────────────────────────────


class TestHealthVersion:
    def test_health(self, ilan_server: IlanServer) -> None:
        resp = _get(ilan_server, "/health")
        assert resp["status"] == "ok"

    def test_version(self, ilan_server: IlanServer) -> None:
        resp = _get(ilan_server, "/version")
        assert resp["version"] == __version__
        assert "commit" in resp


# ── Config ──────────────────────────────────────────────────────────────


class TestConfig:
    def test_get_config(self, ilan_server: IlanServer) -> None:
        resp = _get(ilan_server, "/config")
        assert "config" in resp
        assert resp["config"]["model"] == "opus"

    def test_set_config(self, ilan_server: IlanServer) -> None:
        resp = _post(ilan_server, "/config/set", {"key": "model", "value": "sonnet"})
        assert resp.get("ok") is True
        assert resp["value"] == "sonnet"

        # Verify it persists
        resp = _get(ilan_server, "/config")
        assert resp["config"]["model"] == "sonnet"

    def test_set_config_int_key(self, ilan_server: IlanServer) -> None:
        resp = _post(ilan_server, "/config/set", {"key": "num-agents", "value": "3"})
        assert resp.get("ok") is True
        assert resp["value"] == 3

    def test_set_config_invalid_key(self, ilan_server: IlanServer) -> None:
        resp = _post(ilan_server, "/config/set", {"key": "bad-key", "value": "x"})
        assert "error" in resp


# ── Tasks CRUD ──────────────────────────────────────────────────────────


class TestTasksCRUD:
    def test_add_task(self, ilan_server: IlanServer) -> None:
        resp = _post(ilan_server, "/tasks", {"name": "test-task", "prompt": "Do something"})
        assert resp.get("ok") is True

    def test_add_duplicate_task(self, ilan_server: IlanServer) -> None:
        _post(ilan_server, "/tasks", {"name": "dup-task", "prompt": "A"})
        resp = _post(ilan_server, "/tasks", {"name": "dup-task", "prompt": "B"})
        assert "error" in resp

    def test_add_task_short_name(self, ilan_server: IlanServer) -> None:
        resp = _post(ilan_server, "/tasks", {"name": "ab", "prompt": "Too short"})
        assert "error" in resp

    def test_add_task_invalid_chars(self, ilan_server: IlanServer) -> None:
        resp = _post(ilan_server, "/tasks", {"name": "has space", "prompt": "P"})
        assert "error" in resp

    def test_add_task_special_chars(self, ilan_server: IlanServer) -> None:
        resp = _post(ilan_server, "/tasks", {"name": "foo/bar!", "prompt": "P"})
        assert "error" in resp

    def test_add_task_with_underscores_and_dashes(self, ilan_server: IlanServer) -> None:
        resp = _post(ilan_server, "/tasks", {"name": "my_task-1", "prompt": "P"})
        assert resp.get("ok") is True

    def test_list_tasks(self, ilan_server: IlanServer) -> None:
        _post(ilan_server, "/tasks", {"name": "list-test", "prompt": "P"})
        resp = _get(ilan_server, "/tasks")
        assert "tasks" in resp
        names = [t["name"] for t in resp["tasks"]]
        assert "list-test" in names

    def test_list_tasks_hides_terminal(self, ilan_server: IlanServer) -> None:
        _post(ilan_server, "/tasks", {"name": "will-done", "prompt": "P"})
        _post(ilan_server, "/tasks/will-done/done")
        resp = _get(ilan_server, "/tasks")
        names = [t["name"] for t in resp["tasks"]]
        assert "will-done" not in names

    def test_list_tasks_all(self, ilan_server: IlanServer) -> None:
        _post(ilan_server, "/tasks", {"name": "see-all", "prompt": "P"})
        _post(ilan_server, "/tasks/see-all/done")
        resp = _get(ilan_server, "/tasks?all=true")
        names = [t["name"] for t in resp["tasks"]]
        assert "see-all" in names

    def test_get_task(self, ilan_server: IlanServer) -> None:
        _post(ilan_server, "/tasks", {"name": "get-me", "prompt": "Hello"})
        resp = _get(ilan_server, "/tasks/get-me")
        assert resp["task"]["name"] == "get-me"
        assert resp["task"]["prompt"] == "Hello"

    def test_get_task_by_alias(self, ilan_server: IlanServer) -> None:
        _post(ilan_server, "/tasks", {"name": "alias-test", "prompt": "P"})
        # Find the alias
        resp = _get(ilan_server, "/tasks/alias-test")
        alias = resp["task"]["alias"]
        if alias:
            resp2 = _get(ilan_server, f"/tasks/{alias}")
            assert resp2["task"]["name"] == "alias-test"

    def test_get_task_not_found(self, ilan_server: IlanServer) -> None:
        resp = _get(ilan_server, "/tasks/nonexistent")
        assert "error" in resp

    def test_delete_task(self, ilan_server: IlanServer) -> None:
        _post(ilan_server, "/tasks", {"name": "del-me", "prompt": "P"})
        resp = _delete(ilan_server, "/tasks/del-me")
        assert resp.get("ok") is True
        resp = _get(ilan_server, "/tasks/del-me")
        assert "error" in resp


# ── Task State Transitions ──────────────────────────────────────────────


class TestTaskStateTransitions:
    def test_done(self, ilan_server: IlanServer) -> None:
        _post(ilan_server, "/tasks", {"name": "done-test", "prompt": "P"})
        resp = _post(ilan_server, "/tasks/done-test/done")
        assert resp.get("ok") is True
        task = _get(ilan_server, "/tasks/done-test")["task"]
        assert task["status"] == "DONE"
        assert task["alias"] is None

    def test_discard(self, ilan_server: IlanServer) -> None:
        _post(ilan_server, "/tasks", {"name": "discard-test", "prompt": "P"})
        resp = _post(ilan_server, "/tasks/discard-test/discard")
        assert resp.get("ok") is True
        task = _get(ilan_server, "/tasks/discard-test")["task"]
        assert task["status"] == "DISCARDED"
        assert task["alias"] is None

    def test_undone(self, ilan_server: IlanServer) -> None:
        _post(ilan_server, "/tasks", {"name": "undone-test", "prompt": "P"})
        _post(ilan_server, "/tasks/undone-test/done")
        resp = _post(ilan_server, "/tasks/undone-test/undone")
        assert resp.get("ok") is True
        task = _get(ilan_server, "/tasks/undone-test")["task"]
        assert task["status"] == "NEEDS_ATTENTION"
        assert task["alias"] is not None

    def test_undiscard(self, ilan_server: IlanServer) -> None:
        _post(ilan_server, "/tasks", {"name": "undisc-test", "prompt": "P"})
        _post(ilan_server, "/tasks/undisc-test/discard")
        resp = _post(ilan_server, "/tasks/undisc-test/undiscard")
        assert resp.get("ok") is True
        task = _get(ilan_server, "/tasks/undisc-test")["task"]
        assert task["status"] == "NEEDS_ATTENTION"

    def test_undone_wrong_state(self, ilan_server: IlanServer) -> None:
        _post(ilan_server, "/tasks", {"name": "bad-undone", "prompt": "P"})
        resp = _post(ilan_server, "/tasks/bad-undone/undone")
        assert "error" in resp

    def test_undiscard_wrong_state(self, ilan_server: IlanServer) -> None:
        _post(ilan_server, "/tasks", {"name": "bad-undisc", "prompt": "P"})
        resp = _post(ilan_server, "/tasks/bad-undisc/undiscard")
        assert "error" in resp


# ── Task Hash ──────────────────────────────────────────────────────────


class TestTaskHash:
    def test_task_gets_hash_on_creation(self, ilan_server: IlanServer) -> None:
        _post(ilan_server, "/tasks", {"name": "hash-test", "prompt": "P"})
        task = _get(ilan_server, "/tasks/hash-test")["task"]
        assert task["task_hash"] is not None
        assert len(task["task_hash"]) == 8

    def test_task_hash_is_hex(self, ilan_server: IlanServer) -> None:
        _post(ilan_server, "/tasks", {"name": "hex-test", "prompt": "P"})
        task = _get(ilan_server, "/tasks/hex-test")["task"]
        assert all(c in "0123456789abcdef" for c in task["task_hash"])

    def test_done_calls_tmux_cleanup(self, ilan_server: IlanServer) -> None:
        _post(ilan_server, "/tasks", {"name": "tmux-done", "prompt": "P"})
        task = _get(ilan_server, "/tasks/tmux-done")["task"]
        task_hash = task["task_hash"]

        with patch("ilan.server.kill_tmux_sessions_by_prefix") as mock_kill:
            _post(ilan_server, "/tasks/tmux-done/done")
            mock_kill.assert_called_once_with(task_hash)

    def test_discard_calls_tmux_cleanup(self, ilan_server: IlanServer) -> None:
        _post(ilan_server, "/tasks", {"name": "tmux-disc", "prompt": "P"})
        task = _get(ilan_server, "/tasks/tmux-disc")["task"]
        task_hash = task["task_hash"]

        with patch("ilan.server.kill_tmux_sessions_by_prefix") as mock_kill:
            _post(ilan_server, "/tasks/tmux-disc/discard")
            mock_kill.assert_called_once_with(task_hash)

    def test_delete_calls_tmux_cleanup(self, ilan_server: IlanServer) -> None:
        _post(ilan_server, "/tasks", {"name": "tmux-del", "prompt": "P"})
        task = _get(ilan_server, "/tasks/tmux-del")["task"]
        task_hash = task["task_hash"]

        with patch("ilan.server.kill_tmux_sessions_by_prefix") as mock_kill:
            _delete(ilan_server, "/tasks/tmux-del")
            mock_kill.assert_called_once_with(task_hash)


# ── Reply ───────────────────────────────────────────────────────────────


class TestReply:
    def test_reply_to_unclaimed_caches(self, ilan_server: IlanServer) -> None:
        _post(ilan_server, "/tasks", {"name": "reply-uncl", "prompt": "P"})
        resp = _post(ilan_server, "/tasks/reply-uncl/reply", {"message": "heads up"})
        assert resp.get("ok") is True
        assert "warning" in resp  # "Task is UNCLAIMED. Reply cached."

        task = _get(ilan_server, "/tasks/reply-uncl")["task"]
        assert "heads up" in task["cached_replies"]

    def test_reply_to_needs_attention_sets_unclaimed(self, ilan_server: IlanServer) -> None:
        _post(ilan_server, "/tasks", {"name": "reply-na", "prompt": "P"})
        # Manually set to NEEDS_ATTENTION
        with ilan_server.lock:
            task = ilan_server.store.get_task("reply-na")
            task.set_status(TaskStatus.NEEDS_ATTENTION)
            ilan_server.store.put_task(task)

        resp = _post(ilan_server, "/tasks/reply-na/reply", {"message": "fix it"})
        assert resp.get("ok") is True

        task = _get(ilan_server, "/tasks/reply-na")["task"]
        assert task["status"] == "UNCLAIMED"
        assert "fix it" in task["cached_replies"]

    def test_reply_to_terminal_fails(self, ilan_server: IlanServer) -> None:
        _post(ilan_server, "/tasks", {"name": "reply-done", "prompt": "P"})
        _post(ilan_server, "/tasks/reply-done/done")
        resp = _post(ilan_server, "/tasks/reply-done/reply", {"message": "too late"})
        assert "error" in resp


# ── Sleep ───────────────────────────────────────────────────────────────


class TestSleep:
    def _make_task_in_status(
        self, ilan_server: IlanServer, name: str, status: TaskStatus
    ) -> None:
        _post(ilan_server, "/tasks", {"name": name, "prompt": "P"})
        with ilan_server.lock:
            task = ilan_server.store.get_task(name)
            task.set_status(status)
            ilan_server.store.put_task(task)

    def test_sleep_on_needs_attention_caches_and_unclaims(
        self, ilan_server: IlanServer
    ) -> None:
        self._make_task_in_status(ilan_server, "sleep-na", TaskStatus.NEEDS_ATTENTION)
        resp = _post(ilan_server, "/tasks/sleep-na/sleep", {"seconds": 5})
        assert resp.get("ok") is True

        task = _get(ilan_server, "/tasks/sleep-na")["task"]
        assert task["status"] == "UNCLAIMED"
        assert task["sleep_seconds"] == 5
        assert "Sleep 5 seconds and report back" in task["cached_replies"]

    def test_sleep_on_agent_finished_caches_and_unclaims(
        self, ilan_server: IlanServer
    ) -> None:
        self._make_task_in_status(ilan_server, "sleep-af", TaskStatus.AGENT_FINISHED)
        resp = _post(ilan_server, "/tasks/sleep-af/sleep", {"seconds": 5})
        assert resp.get("ok") is True

        task = _get(ilan_server, "/tasks/sleep-af")["task"]
        assert task["status"] == "UNCLAIMED"
        assert task["sleep_seconds"] == 5

    def test_sleep_on_working_rejected(self, ilan_server: IlanServer) -> None:
        self._make_task_in_status(ilan_server, "sleep-wk", TaskStatus.WORKING)
        resp = _post(ilan_server, "/tasks/sleep-wk/sleep", {"seconds": 5})
        assert "error" in resp
        assert "WORKING" in resp["error"]

    def test_sleep_on_unclaimed_rejected(self, ilan_server: IlanServer) -> None:
        _post(ilan_server, "/tasks", {"name": "sleep-uncl", "prompt": "P"})
        resp = _post(ilan_server, "/tasks/sleep-uncl/sleep", {"seconds": 5})
        assert "error" in resp

    def test_sleep_on_terminal_rejected(self, ilan_server: IlanServer) -> None:
        _post(ilan_server, "/tasks", {"name": "sleep-done", "prompt": "P"})
        _post(ilan_server, "/tasks/sleep-done/done")
        resp = _post(ilan_server, "/tasks/sleep-done/sleep", {"seconds": 5})
        assert "error" in resp

    def test_sleep_rejects_non_positive_seconds(
        self, ilan_server: IlanServer
    ) -> None:
        self._make_task_in_status(ilan_server, "sleep-zero", TaskStatus.NEEDS_ATTENTION)
        resp = _post(ilan_server, "/tasks/sleep-zero/sleep", {"seconds": 0})
        assert "error" in resp

    def test_sleep_seconds_cleared_on_exit_sleep_states(
        self, ilan_server: IlanServer
    ) -> None:
        self._make_task_in_status(ilan_server, "sleep-clear", TaskStatus.NEEDS_ATTENTION)
        _post(ilan_server, "/tasks/sleep-clear/sleep", {"seconds": 5})
        # Task is now UNCLAIMED with sleep_seconds=5. Flip it to NEEDS_ATTENTION
        # via set_status and verify sleep_seconds is dropped.
        with ilan_server.lock:
            task = ilan_server.store.get_task("sleep-clear")
            task.set_status(TaskStatus.NEEDS_ATTENTION)
            ilan_server.store.put_task(task)
        task = _get(ilan_server, "/tasks/sleep-clear")["task"]
        assert task["sleep_seconds"] is None

    def test_done_on_sleeping_task_clears_sleep_seconds(
        self, ilan_server: IlanServer
    ) -> None:
        self._make_task_in_status(ilan_server, "sleep-done", TaskStatus.NEEDS_ATTENTION)
        _post(ilan_server, "/tasks/sleep-done/sleep", {"seconds": 5})
        assert _get(ilan_server, "/tasks/sleep-done")["task"]["sleep_seconds"] == 5

        _post(ilan_server, "/tasks/sleep-done/done")
        task = _get(ilan_server, "/tasks/sleep-done")["task"]
        assert task["status"] == "DONE"
        assert task["sleep_seconds"] is None

    def test_discard_on_sleeping_task_clears_sleep_seconds(
        self, ilan_server: IlanServer
    ) -> None:
        self._make_task_in_status(ilan_server, "sleep-disc", TaskStatus.NEEDS_ATTENTION)
        _post(ilan_server, "/tasks/sleep-disc/sleep", {"seconds": 5})
        assert _get(ilan_server, "/tasks/sleep-disc")["task"]["sleep_seconds"] == 5

        _post(ilan_server, "/tasks/sleep-disc/discard")
        task = _get(ilan_server, "/tasks/sleep-disc")["task"]
        assert task["status"] == "DISCARDED"
        assert task["sleep_seconds"] is None


# ── Logs ────────────────────────────────────────────────────────────────


class TestLogs:
    def test_get_logs_empty(self, ilan_server: IlanServer) -> None:
        _post(ilan_server, "/tasks", {"name": "log-empty", "prompt": "P"})
        resp = _get(ilan_server, "/tasks/log-empty/logs")
        assert resp["logs"] == []

    def test_get_logs_with_entries(self, ilan_server: IlanServer) -> None:
        _post(ilan_server, "/tasks", {"name": "log-full", "prompt": "P"})
        ilan_server.store.append_log("log-full", "user", "hello")
        ilan_server.store.append_log("log-full", "assistant", "hi there")
        resp = _get(ilan_server, "/tasks/log-full/logs")
        assert len(resp["logs"]) == 2

    def test_get_log_path(self, ilan_server: IlanServer) -> None:
        _post(ilan_server, "/tasks", {"name": "log-path-test", "prompt": "P"})
        resp = _get(ilan_server, "/tasks/log-path-test/log-path")
        assert "path" in resp
        assert resp["path"].endswith("log-path-test.jsonl")

    def test_get_log_path_not_found(self, ilan_server: IlanServer) -> None:
        resp = _get(ilan_server, "/tasks/nonexistent/log-path")
        assert "error" in resp

    def test_tail_returns_last_assistant(self, ilan_server: IlanServer) -> None:
        _post(ilan_server, "/tasks", {"name": "tail-test", "prompt": "P"})
        ilan_server.store.append_log("tail-test", "user", "u1")
        ilan_server.store.append_log("tail-test", "assistant", "a1")
        ilan_server.store.append_log("tail-test", "user", "u2")
        ilan_server.store.append_log("tail-test", "assistant", "a2")
        ilan_server.store.append_log("tail-test", "user", "u3")

        resp = _get(ilan_server, "/tasks/tail-test/tail")
        entries = resp["entries"]
        assert len(entries) == 2  # last assistant + user after
        assert entries[0]["role"] == "assistant"
        assert entries[0]["content"] == "a2"
        assert entries[1]["role"] == "user"

    def test_tail_empty(self, ilan_server: IlanServer) -> None:
        _post(ilan_server, "/tasks", {"name": "tail-empty", "prompt": "P"})
        resp = _get(ilan_server, "/tasks/tail-empty/tail")
        assert "warning" in resp

    def test_tail_no_assistant(self, ilan_server: IlanServer) -> None:
        _post(ilan_server, "/tasks", {"name": "tail-noasst", "prompt": "P"})
        ilan_server.store.append_log("tail-noasst", "user", "only user msg")
        resp = _get(ilan_server, "/tasks/tail-noasst/tail")
        assert "warning" in resp

    def test_tail_n_returns_last_n_combined(self, ilan_server: IlanServer) -> None:
        _post(ilan_server, "/tasks", {"name": "tail-n", "prompt": "P"})
        ilan_server.store.append_log("tail-n", "user", "u1")
        ilan_server.store.append_log("tail-n", "assistant", "a1")
        ilan_server.store.append_log("tail-n", "user", "u2")
        ilan_server.store.append_log("tail-n", "assistant", "a2")
        ilan_server.store.append_log("tail-n", "user", "u3")

        resp = _get(ilan_server, "/tasks/tail-n/tail?n=4")
        entries = resp["entries"]
        assert [(e["role"], e["content"]) for e in entries] == [
            ("assistant", "a1"),
            ("user", "u2"),
            ("assistant", "a2"),
            ("user", "u3"),
        ]

    def test_tail_n_no_assistant_still_returns(self, ilan_server: IlanServer) -> None:
        """With -n, return user-only messages (no assistant required)."""
        _post(ilan_server, "/tasks", {"name": "tail-n-user", "prompt": "P"})
        ilan_server.store.append_log("tail-n-user", "user", "u1")
        ilan_server.store.append_log("tail-n-user", "user", "u2")

        resp = _get(ilan_server, "/tasks/tail-n-user/tail?n=2")
        entries = resp["entries"]
        assert [(e["role"], e["content"]) for e in entries] == [
            ("user", "u1"),
            ("user", "u2"),
        ]

    def test_tail_n_larger_than_logs(self, ilan_server: IlanServer) -> None:
        _post(ilan_server, "/tasks", {"name": "tail-n-big", "prompt": "P"})
        ilan_server.store.append_log("tail-n-big", "assistant", "a1")
        ilan_server.store.append_log("tail-n-big", "user", "u1")

        resp = _get(ilan_server, "/tasks/tail-n-big/tail?n=50")
        assert len(resp["entries"]) == 2

    def test_tail_n_empty_logs_warning(self, ilan_server: IlanServer) -> None:
        _post(ilan_server, "/tasks", {"name": "tail-n-empty", "prompt": "P"})
        resp = _get(ilan_server, "/tasks/tail-n-empty/tail?n=4")
        assert "warning" in resp

    def test_tail_n_invalid(self, ilan_server: IlanServer) -> None:
        _post(ilan_server, "/tasks", {"name": "tail-n-bad", "prompt": "P"})
        ilan_server.store.append_log("tail-n-bad", "assistant", "a1")

        resp = _get(ilan_server, "/tasks/tail-n-bad/tail?n=abc")
        assert "error" in resp

        resp = _get(ilan_server, "/tasks/tail-n-bad/tail?n=0")
        assert "error" in resp

        resp = _get(ilan_server, "/tasks/tail-n-bad/tail?n=-2")
        assert "error" in resp


# ── Needs Review ───────────────────────────────────────────────────────


def _set_needs_review(server: IlanServer, name: str) -> None:
    """Set a task to NEEDS_ATTENTION with needs_review=True."""
    with server.lock:
        task = server.store.get_task(name)
        task.set_status(TaskStatus.NEEDS_ATTENTION)
        task.needs_review = True
        server.store.put_task(task)


class TestNeedsReview:
    def test_list_tasks_includes_needs_review(self, ilan_server: IlanServer) -> None:
        _post(ilan_server, "/tasks", {"name": "nr-list", "prompt": "P"})
        _set_needs_review(ilan_server, "nr-list")
        resp = _get(ilan_server, "/tasks")
        task_row = next(t for t in resp["tasks"] if t["name"] == "nr-list")
        assert task_row["needs_review"] is True

    def test_tail_clears_needs_review(self, ilan_server: IlanServer) -> None:
        _post(ilan_server, "/tasks", {"name": "nr-tail", "prompt": "P"})
        _set_needs_review(ilan_server, "nr-tail")
        ilan_server.store.append_log("nr-tail", "assistant", "done")

        _get(ilan_server, "/tasks/nr-tail/tail")

        task = _get(ilan_server, "/tasks/nr-tail")["task"]
        assert task["needs_review"] is False

    def test_logs_clears_needs_review(self, ilan_server: IlanServer) -> None:
        _post(ilan_server, "/tasks", {"name": "nr-logs", "prompt": "P"})
        _set_needs_review(ilan_server, "nr-logs")

        _get(ilan_server, "/tasks/nr-logs/logs")

        task = _get(ilan_server, "/tasks/nr-logs")["task"]
        assert task["needs_review"] is False

    def test_reply_clears_needs_review(self, ilan_server: IlanServer) -> None:
        _post(ilan_server, "/tasks", {"name": "nr-reply", "prompt": "P"})
        _set_needs_review(ilan_server, "nr-reply")

        _post(ilan_server, "/tasks/nr-reply/reply", {"message": "got it"})

        task = _get(ilan_server, "/tasks/nr-reply")["task"]
        assert task["needs_review"] is False

    def test_new_task_has_no_needs_review(self, ilan_server: IlanServer) -> None:
        _post(ilan_server, "/tasks", {"name": "nr-fresh", "prompt": "P"})
        resp = _get(ilan_server, "/tasks")
        task_row = next(t for t in resp["tasks"] if t["name"] == "nr-fresh")
        assert task_row["needs_review"] is False

    def test_done_clears_needs_review(self, ilan_server: IlanServer) -> None:
        _post(ilan_server, "/tasks", {"name": "nr-done", "prompt": "P"})
        _set_needs_review(ilan_server, "nr-done")

        _post(ilan_server, "/tasks/nr-done/done")

        task = _get(ilan_server, "/tasks/nr-done")["task"]
        assert task["needs_review"] is False

    def test_discard_clears_needs_review(self, ilan_server: IlanServer) -> None:
        _post(ilan_server, "/tasks", {"name": "nr-disc", "prompt": "P"})
        _set_needs_review(ilan_server, "nr-disc")

        _post(ilan_server, "/tasks/nr-disc/discard")

        task = _get(ilan_server, "/tasks/nr-disc")["task"]
        assert task["needs_review"] is False

    def test_unread_restores_needs_review(self, ilan_server: IlanServer) -> None:
        _post(ilan_server, "/tasks", {"name": "nr-unread", "prompt": "P"})
        _set_needs_review(ilan_server, "nr-unread")
        ilan_server.store.append_log("nr-unread", "assistant", "done")
        _get(ilan_server, "/tasks/nr-unread/tail")

        task = _get(ilan_server, "/tasks/nr-unread")["task"]
        assert task["needs_review"] is False

        resp = _post(ilan_server, "/tasks/nr-unread/unread")
        assert resp.get("ok") is True

        task = _get(ilan_server, "/tasks/nr-unread")["task"]
        assert task["needs_review"] is True

    def test_unread_is_idempotent(self, ilan_server: IlanServer) -> None:
        _post(ilan_server, "/tasks", {"name": "nr-idem", "prompt": "P"})
        _set_needs_review(ilan_server, "nr-idem")

        resp = _post(ilan_server, "/tasks/nr-idem/unread")
        assert resp.get("ok") is True

        task = _get(ilan_server, "/tasks/nr-idem")["task"]
        assert task["needs_review"] is True


# ── Summarize ───────────────────────────────────────────────────────────


class TestSummarize:
    def _seed(self, server: IlanServer, name: str) -> None:
        server.store.append_log(name, "user", "Do the thing.")
        server.store.append_log(
            name,
            "assistant",
            "Opened PR https://github.com/a/b/pull/42 for this.",
        )

    def test_summarize_writes_file_and_returns_text(
        self,
        ilan_server: IlanServer,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("MOCK_CLAUDE_RESPONSE", "## Summary\n\nLooks good.\n")
        _post(ilan_server, "/tasks", {"name": "sum-task", "prompt": "P"})
        self._seed(ilan_server, "sum-task")

        resp = _post(ilan_server, "/tasks/sum-task/summarize")
        assert resp.get("ok") is True
        assert resp["name"] == "sum-task"
        assert "Looks good." in resp["summary"]
        assert resp["reused"] is False
        assert Path(resp["summary_path"]).exists()

    def test_summarize_reuses_cached_on_unchanged_log(
        self,
        ilan_server: IlanServer,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("MOCK_CLAUDE_RESPONSE", "first")
        _post(ilan_server, "/tasks", {"name": "sum-cache", "prompt": "P"})
        self._seed(ilan_server, "sum-cache")

        first = _post(ilan_server, "/tasks/sum-cache/summarize")
        assert first["reused"] is False

        # Change mock response — if the server re-invokes claude, we'd
        # see the new text. Reusing the cache means we keep "first".
        monkeypatch.setenv("MOCK_CLAUDE_RESPONSE", "second")
        second = _post(ilan_server, "/tasks/sum-cache/summarize")
        assert second["reused"] is True
        assert second["summary"] == first["summary"]

    def test_summarize_unknown_task_404(self, ilan_server: IlanServer) -> None:
        resp = _post(ilan_server, "/tasks/does-not-exist/summarize")
        assert "error" in resp

    def test_summarize_accepts_alias(
        self,
        ilan_server: IlanServer,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("MOCK_CLAUDE_RESPONSE", "alias-summary")
        _post(ilan_server, "/tasks", {"name": "alias-task", "prompt": "P"})
        self._seed(ilan_server, "alias-task")
        alias = _get(ilan_server, "/tasks/alias-task")["task"]["alias"]
        assert alias is not None

        resp = _post(ilan_server, f"/tasks/{alias}/summarize")
        assert resp.get("ok") is True
        assert resp["name"] == "alias-task"


# ── Kill ────────────────────────────────────────────────────────────────


class TestKill:
    def test_kill_non_working_fails(self, ilan_server: IlanServer) -> None:
        _post(ilan_server, "/tasks", {"name": "kill-idle", "prompt": "P"})
        resp = _post(ilan_server, "/tasks/kill-idle/kill")
        assert "error" in resp


# ── Clear Everything ────────────────────────────────────────────────────


class TestClearEverything:
    def test_clear_everything(self, ilan_server: IlanServer) -> None:
        _post(ilan_server, "/tasks", {"name": "clear-1", "prompt": "P"})
        _post(ilan_server, "/tasks", {"name": "clear-2", "prompt": "P"})
        resp = _post(ilan_server, "/clear-everything")
        assert resp.get("ok") is True

        resp = _get(ilan_server, "/tasks?all=true")
        assert resp["tasks"] == []


# ── 404 ─────────────────────────────────────────────────────────────────


class TestNotFound:
    def test_unknown_route(self, ilan_server: IlanServer) -> None:
        url = f"{ilan_server._test_url}/nonexistent"  # type: ignore[attr-defined]
        req = Request(url)
        try:
            with urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read())
        except HTTPError as exc:
            data = json.loads(exc.read())
        assert "error" in data
