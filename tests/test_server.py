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

import ilan.server as srv_mod
from ilan import __version__
from ilan.models import FABLE_MODEL, TaskStatus
from ilan.server import IlanServer, read_server_info


@pytest.fixture()
def ilan_server(tmp_workdir: Path, tmp_config: Path, env_with_mock_claude: None):
    """Start an IlanServer on an ephemeral port and tear it down after the test.

    The runner is patched to not spawn real agent processes: ``start`` mimics
    a successful spawn (task flips to WORKING) and the reaper loop is a
    no-op, so tests can exercise individual routes in isolation.
    """
    import ilan.config as cfg_mod

    cfg_mod.save({**cfg_mod.DEFAULTS, "workdir": str(tmp_workdir)})

    server = IlanServer()

    def _fake_start(task) -> bool:
        task.set_status(TaskStatus.WORKING)
        server.store.put_task(task)
        return True

    server.runner.start = _fake_start  # type: ignore[method-assign]
    server.runner.reap_finished = lambda: None  # type: ignore[method-assign]

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


# ── read_server_info ────────────────────────────────────────────────────


class TestReadServerInfo:
    """The pid-file liveness probe, including the cross-user EPERM case."""

    _INFO = {"pid": 12345, "port": 4526}

    def _pid_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        pf = tmp_path / "server.pid"
        monkeypatch.setattr("ilan.server.pid_file_path", lambda: pf)
        return pf

    def test_missing_pid_file_returns_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        self._pid_file(tmp_path, monkeypatch)
        assert read_server_info() is None

    def test_alive_pid_returns_info(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        pf = self._pid_file(tmp_path, monkeypatch)
        pf.write_text(json.dumps(self._INFO))
        monkeypatch.setattr("ilan.server.os.kill", lambda pid, sig: None)
        assert read_server_info() == self._INFO
        assert pf.exists()

    def test_permission_error_means_alive_and_keeps_pid_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """EPERM from kill(pid, 0) = pid exists but is owned by another user.

        A client running as a different account than the server must still
        see the server as alive, and must not delete its pid file.
        """
        pf = self._pid_file(tmp_path, monkeypatch)
        pf.write_text(json.dumps(self._INFO))

        def _kill(pid: int, sig: int) -> None:
            raise PermissionError

        monkeypatch.setattr("ilan.server.os.kill", _kill)
        assert read_server_info() == self._INFO
        assert pf.exists()

    def test_dead_pid_removes_pid_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        pf = self._pid_file(tmp_path, monkeypatch)
        pf.write_text(json.dumps(self._INFO))

        def _kill(pid: int, sig: int) -> None:
            raise ProcessLookupError

        monkeypatch.setattr("ilan.server.os.kill", _kill)
        assert read_server_info() is None
        assert not pf.exists()

    def test_corrupt_json_removes_pid_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        pf = self._pid_file(tmp_path, monkeypatch)
        pf.write_text("not json")
        assert read_server_info() is None
        assert not pf.exists()

    def test_missing_pid_key_removes_pid_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        pf = self._pid_file(tmp_path, monkeypatch)
        pf.write_text(json.dumps({"port": 4526}))
        assert read_server_info() is None
        assert not pf.exists()


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
        resp = _post(ilan_server, "/config/set", {"key": "dashboard-interval", "value": "3"})
        assert resp.get("ok") is True
        assert resp["value"] == 3

    def test_set_config_invalid_key(self, ilan_server: IlanServer) -> None:
        resp = _post(ilan_server, "/config/set", {"key": "bad-key", "value": "x"})
        assert "error" in resp

    def test_set_config_rejects_client_side_key(self, ilan_server: IlanServer) -> None:
        """Client-side keys (e.g. line-number) must not be settable via the server."""
        resp = _post(ilan_server, "/config/set", {"key": "line-number", "value": "true"})
        assert "error" in resp
        assert "client-side" in resp["error"]

    def test_set_config_default_backend_accepts_valid_engines(
        self, ilan_server: IlanServer
    ) -> None:
        for engine in ("codex", "claude"):
            resp = _post(ilan_server, "/config/set", {"key": "default-backend", "value": engine})
            assert resp.get("ok") is True
            assert resp["value"] == engine

    def test_set_config_default_backend_rejects_invalid_value(
        self, ilan_server: IlanServer
    ) -> None:
        resp = _post(ilan_server, "/config/set", {"key": "default-backend", "value": "gpt"})
        assert "error" in resp
        assert "default-backend" in resp["error"]
        # The bad value must not be persisted.
        conf = _get(ilan_server, "/config")["config"]
        assert conf["default-backend"] == "claude"


# ── Tasks CRUD ──────────────────────────────────────────────────────────


class TestTasksCRUD:
    def test_add_task(self, ilan_server: IlanServer) -> None:
        resp = _post(ilan_server, "/tasks", {"name": "test-task", "prompt": "Do something"})
        assert resp.get("ok") is True

    def test_add_task_logs_opening_prompt(self, ilan_server: IlanServer) -> None:
        """Creation records the opening prompt so the log always opens with it,
        even if a reply is logged before the first spawn."""
        _post(ilan_server, "/tasks", {"name": "log-open", "prompt": "build X"})
        logs = _get(ilan_server, "/tasks/log-open/logs")["logs"]
        assert [(e["role"], e["content"]) for e in logs] == [("user", "build X")]

    def test_reply_right_after_create_keeps_prompt_first(self, ilan_server: IlanServer) -> None:
        """A reply right after creation is logged after the opening
        prompt, preserving chronological order in the unified log."""
        _post(ilan_server, "/tasks", {"name": "reply-order", "prompt": "build X"})
        _post(ilan_server, "/tasks/reply-order/reply", {"message": "also do Y"})
        logs = _get(ilan_server, "/tasks/reply-order/logs")["logs"]
        assert [(e["role"], e["content"]) for e in logs] == [
            ("user", "build X"),
            ("user", "also do Y"),
        ]

    def test_add_task_defaults_to_claude_engine(self, ilan_server: IlanServer) -> None:
        _post(ilan_server, "/tasks", {"name": "eng-default", "prompt": "P"})
        task = _get(ilan_server, "/tasks/eng-default")["task"]
        assert task["engine"] == "claude"

    def test_add_task_with_codex_agent(self, ilan_server: IlanServer) -> None:
        _post(ilan_server, "/tasks", {"name": "eng-codex", "prompt": "P", "agent": "codex"})
        task = _get(ilan_server, "/tasks/eng-codex")["task"]
        assert task["engine"] == "codex"

    def test_add_task_invalid_agent_rejected(self, ilan_server: IlanServer) -> None:
        resp = _post(ilan_server, "/tasks", {"name": "eng-bad", "prompt": "P", "agent": "gpt"})
        assert "error" in resp
        assert _get(ilan_server, "/tasks/eng-bad").get("error")

    def test_add_task_uses_config_default_backend(self, ilan_server: IlanServer) -> None:
        _post(ilan_server, "/config/set", {"key": "default-backend", "value": "codex"})
        _post(ilan_server, "/tasks", {"name": "eng-cfg", "prompt": "P"})
        task = _get(ilan_server, "/tasks/eng-cfg")["task"]
        assert task["engine"] == "codex"

    def test_add_task_with_max_sets_fable_model(self, ilan_server: IlanServer) -> None:
        resp = _post(ilan_server, "/tasks", {"name": "max-task", "prompt": "P", "max": True})
        assert resp.get("ok") is True
        task = _get(ilan_server, "/tasks/max-task")["task"]
        assert task["model"] == FABLE_MODEL
        assert task["engine"] == "claude"

    def test_add_task_without_max_has_no_model(self, ilan_server: IlanServer) -> None:
        _post(ilan_server, "/tasks", {"name": "plain-model", "prompt": "P"})
        task = _get(ilan_server, "/tasks/plain-model")["task"]
        assert task["model"] is None

    def test_add_task_max_with_codex_rejected(self, ilan_server: IlanServer) -> None:
        resp = _post(
            ilan_server, "/tasks",
            {"name": "max-codex", "prompt": "P", "agent": "codex", "max": True},
        )
        assert "error" in resp
        assert _get(ilan_server, "/tasks/max-codex").get("error")

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

    def test_list_tasks_includes_engine(self, ilan_server: IlanServer) -> None:
        _post(ilan_server, "/tasks", {"name": "engine-list", "prompt": "P", "agent": "codex"})
        resp = _get(ilan_server, "/tasks")
        row = next(t for t in resp["tasks"] if t["name"] == "engine-list")
        assert row["engine"] == "codex"

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
        alias = _get(ilan_server, "/tasks/discard-test")["task"]["alias"]
        resp = _post(ilan_server, "/tasks/discard-test/discard")
        assert resp.get("ok") is True
        task = _get(ilan_server, "/tasks/discard-test")["task"]
        assert task["status"] == "DISCARDED"
        # The alias is kept so the task can be undiscarded by it.
        assert task["alias"] == alias
        assert task["alias"] is not None

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
        alias = _get(ilan_server, "/tasks/undisc-test")["task"]["alias"]
        _post(ilan_server, "/tasks/undisc-test/discard")
        resp = _post(ilan_server, "/tasks/undisc-test/undiscard")
        assert resp.get("ok") is True
        task = _get(ilan_server, "/tasks/undisc-test")["task"]
        assert task["status"] == "NEEDS_ATTENTION"
        # Undiscard restores the original alias rather than minting a new one.
        assert task["alias"] == alias

    def test_undiscard_by_alias(self, ilan_server: IlanServer) -> None:
        _post(ilan_server, "/tasks", {"name": "undisc-alias", "prompt": "P"})
        alias = _get(ilan_server, "/tasks/undisc-alias")["task"]["alias"]
        assert alias is not None
        _post(ilan_server, "/tasks/undisc-alias/discard")
        # A discarded task is still reachable by its alias.
        resp = _post(ilan_server, f"/tasks/{alias}/undiscard")
        assert resp.get("ok") is True
        assert resp["name"] == "undisc-alias"
        task = _get(ilan_server, "/tasks/undisc-alias")["task"]
        assert task["status"] == "NEEDS_ATTENTION"

    def test_undone_wrong_state(self, ilan_server: IlanServer) -> None:
        _post(ilan_server, "/tasks", {"name": "bad-undone", "prompt": "P"})
        resp = _post(ilan_server, "/tasks/bad-undone/undone")
        assert "error" in resp

    def test_undiscard_wrong_state(self, ilan_server: IlanServer) -> None:
        _post(ilan_server, "/tasks", {"name": "bad-undisc", "prompt": "P"})
        resp = _post(ilan_server, "/tasks/bad-undisc/undiscard")
        assert "error" in resp


# ── Set Alias ──────────────────────────────────────────────────────────


class TestSetAlias:
    def test_set_alias_success(self, ilan_server: IlanServer) -> None:
        _post(ilan_server, "/tasks", {"name": "alias-set", "prompt": "P"})
        resp = _post(ilan_server, "/tasks/alias-set/alias", {"alias": "aa"})
        assert resp.get("ok") is True
        assert resp["alias"] == "aa"
        task = _get(ilan_server, "/tasks/alias-set")["task"]
        assert task["alias"] == "aa"
        # The task is now reachable by its new alias.
        assert _get(ilan_server, "/tasks/aa")["task"]["name"] == "alias-set"

    def test_set_alias_uppercase_normalized(self, ilan_server: IlanServer) -> None:
        _post(ilan_server, "/tasks", {"name": "alias-upper", "prompt": "P"})
        resp = _post(ilan_server, "/tasks/alias-upper/alias", {"alias": "SD"})
        assert resp.get("ok") is True
        assert resp["alias"] == "sd"

    def test_set_alias_nonexistent_task(self, ilan_server: IlanServer) -> None:
        resp = _post(ilan_server, "/tasks/no-such-task/alias", {"alias": "aa"})
        assert "error" in resp

    def test_set_alias_rejected_when_done(self, ilan_server: IlanServer) -> None:
        _post(ilan_server, "/tasks", {"name": "alias-done", "prompt": "P"})
        _post(ilan_server, "/tasks/alias-done/done")
        resp = _post(ilan_server, "/tasks/alias-done/alias", {"alias": "aa"})
        assert "error" in resp
        task = _get(ilan_server, "/tasks/alias-done")["task"]
        assert task["alias"] is None

    def test_set_alias_rejected_when_discarded(self, ilan_server: IlanServer) -> None:
        _post(ilan_server, "/tasks", {"name": "alias-disc", "prompt": "P"})
        alias = _get(ilan_server, "/tasks/alias-disc")["task"]["alias"]
        _post(ilan_server, "/tasks/alias-disc/discard")
        resp = _post(ilan_server, "/tasks/alias-disc/alias", {"alias": "aa"})
        assert "error" in resp
        # A discarded task keeps its alias, but it can't be reassigned.
        task = _get(ilan_server, "/tasks/alias-disc")["task"]
        assert task["alias"] == alias

    def test_set_alias_wrong_length(self, ilan_server: IlanServer) -> None:
        _post(ilan_server, "/tasks", {"name": "alias-len", "prompt": "P"})
        before = _get(ilan_server, "/tasks/alias-len")["task"]["alias"]
        resp = _post(ilan_server, "/tasks/alias-len/alias", {"alias": "aaa"})
        assert "error" in resp
        after = _get(ilan_server, "/tasks/alias-len")["task"]["alias"]
        assert after == before

    def test_set_alias_illegal_letter(self, ilan_server: IlanServer) -> None:
        _post(ilan_server, "/tasks", {"name": "alias-letter", "prompt": "P"})
        before = _get(ilan_server, "/tasks/alias-letter")["task"]["alias"]
        # 'q' is not in the allowed letter-set 'asdfghjkl'.
        resp = _post(ilan_server, "/tasks/alias-letter/alias", {"alias": "qq"})
        assert "error" in resp
        after = _get(ilan_server, "/tasks/alias-letter")["task"]["alias"]
        assert after == before

    def test_set_alias_conflict(self, ilan_server: IlanServer) -> None:
        _post(ilan_server, "/tasks", {"name": "alias-a", "prompt": "P"})
        _post(ilan_server, "/tasks", {"name": "alias-b", "prompt": "P"})
        # Use alias-b's own (auto-assigned) alias so the conflict is real and
        # deterministic regardless of which aliases were randomly handed out.
        taken = _get(ilan_server, "/tasks/alias-b")["task"]["alias"]
        before = _get(ilan_server, "/tasks/alias-a")["task"]["alias"]
        resp = _post(ilan_server, "/tasks/alias-a/alias", {"alias": taken})
        assert "error" in resp
        task_a = _get(ilan_server, "/tasks/alias-a")["task"]
        assert task_a["alias"] == before

    def test_set_alias_self_no_op(self, ilan_server: IlanServer) -> None:
        _post(ilan_server, "/tasks", {"name": "alias-self", "prompt": "P"})
        _post(ilan_server, "/tasks/alias-self/alias", {"alias": "kl"})
        resp = _post(ilan_server, "/tasks/alias-self/alias", {"alias": "kl"})
        assert resp.get("ok") is True
        assert resp["alias"] == "kl"


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
    def test_reply_to_working_interrupts_and_resumes(self, ilan_server: IlanServer) -> None:
        _post(ilan_server, "/tasks", {"name": "reply-wk", "prompt": "P"})
        with patch.object(ilan_server.runner, "reply_to_working") as m:
            resp = _post(ilan_server, "/tasks/reply-wk/reply", {"message": "heads up"})
        assert resp.get("ok") is True
        assert "Interrupted" in resp["message"]
        m.assert_called_once()

    def test_reply_to_needs_attention_restarts_agent(self, ilan_server: IlanServer) -> None:
        _post(ilan_server, "/tasks", {"name": "reply-na", "prompt": "P"})
        # Manually set to NEEDS_ATTENTION
        with ilan_server.lock:
            task = ilan_server.store.get_task("reply-na")
            task.set_status(TaskStatus.NEEDS_ATTENTION)
            ilan_server.store.put_task(task)

        resp = _post(ilan_server, "/tasks/reply-na/reply", {"message": "fix it"})
        assert resp.get("ok") is True
        assert resp["message"] == "Reply sent. Agent resumed."

        task = _get(ilan_server, "/tasks/reply-na")["task"]
        assert task["status"] == "WORKING"
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

    def test_sleep_on_needs_attention_caches_and_restarts(
        self, ilan_server: IlanServer
    ) -> None:
        self._make_task_in_status(ilan_server, "sleep-na", TaskStatus.NEEDS_ATTENTION)
        resp = _post(ilan_server, "/tasks/sleep-na/sleep", {"seconds": 5})
        assert resp.get("ok") is True

        task = _get(ilan_server, "/tasks/sleep-na")["task"]
        assert task["status"] == "WORKING"
        assert task["sleep_seconds"] == 5
        assert (
            "Sleep 5 seconds and give me a quick report after the sleep finishes."
            in task["cached_replies"]
        )

    def test_sleep_on_agent_finished_caches_and_restarts(
        self, ilan_server: IlanServer
    ) -> None:
        self._make_task_in_status(ilan_server, "sleep-af", TaskStatus.AGENT_FINISHED)
        resp = _post(ilan_server, "/tasks/sleep-af/sleep", {"seconds": 5})
        assert resp.get("ok") is True

        task = _get(ilan_server, "/tasks/sleep-af")["task"]
        assert task["status"] == "WORKING"
        assert task["sleep_seconds"] == 5

    def test_sleep_on_working_rejected(self, ilan_server: IlanServer) -> None:
        self._make_task_in_status(ilan_server, "sleep-wk", TaskStatus.WORKING)
        resp = _post(ilan_server, "/tasks/sleep-wk/sleep", {"seconds": 5})
        assert "error" in resp
        assert "WORKING" in resp["error"]

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
        # Task is now WORKING with sleep_seconds=5. Flip it to NEEDS_ATTENTION
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

    def test_reply_on_working_sleeping_task_clears_sleep_seconds(
        self, ilan_server: IlanServer
    ) -> None:
        """``ilan re`` on a WORKING task that's executing a sleep should
        drop sleep_seconds so the suffix disappears once the agent is
        interrupted and resumed with the new reply."""
        self._make_task_in_status(ilan_server, "sleep-re-wk", TaskStatus.NEEDS_ATTENTION)
        _post(ilan_server, "/tasks/sleep-re-wk/sleep", {"seconds": 5})
        # Sleep restarted the agent: WORKING with sleep_seconds preserved,
        # since WORKING is the sleep-visible status.
        assert _get(ilan_server, "/tasks/sleep-re-wk")["task"]["sleep_seconds"] == 5

        # reply_to_working spawns ``claude`` which isn't available in the
        # test env; patch it out so we can assert the sleep_seconds
        # bookkeeping without needing a real subprocess.
        with patch.object(ilan_server.runner, "reply_to_working") as m:
            def _fake(task, message):  # type: ignore[no-untyped-def]
                # mirror what _spawn would do on a successful resume
                ilan_server.store.append_log(task.name, "user", message)
                task.set_status(TaskStatus.WORKING)
                ilan_server.store.put_task(task)
            m.side_effect = _fake
            resp = _post(ilan_server, "/tasks/sleep-re-wk/reply", {"message": "stop sleeping"})
        assert resp.get("ok") is True

        task = _get(ilan_server, "/tasks/sleep-re-wk")["task"]
        assert task["status"] == "WORKING"
        assert task["sleep_seconds"] is None


# ── Logs ────────────────────────────────────────────────────────────────


class TestLogs:
    def test_get_logs_empty(self, ilan_server: IlanServer) -> None:
        _post(ilan_server, "/tasks", {"name": "log-empty", "prompt": "P"})
        # Isolate the endpoint from the opening prompt logged at creation.
        ilan_server.store.log_path("log-empty").write_text("")
        resp = _get(ilan_server, "/tasks/log-empty/logs")
        assert resp["logs"] == []

    def test_get_logs_with_entries(self, ilan_server: IlanServer) -> None:
        _post(ilan_server, "/tasks", {"name": "log-full", "prompt": "P"})
        ilan_server.store.log_path("log-full").write_text("")
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
        assert [(e["role"], e["content"]) for e in entries] == [
            ("user", "u2"),
            ("assistant", "a2"),
            ("user", "u3"),
        ]

    def test_tail_last_msg_is_assistant(self, ilan_server: IlanServer) -> None:
        _post(ilan_server, "/tasks", {"name": "tail-asst-last", "prompt": "P"})
        ilan_server.store.append_log("tail-asst-last", "user", "u1")
        ilan_server.store.append_log("tail-asst-last", "assistant", "a1")
        ilan_server.store.append_log("tail-asst-last", "user", "u2")
        ilan_server.store.append_log("tail-asst-last", "assistant", "a2")

        resp = _get(ilan_server, "/tasks/tail-asst-last/tail")
        entries = resp["entries"]
        assert [(e["role"], e["content"]) for e in entries] == [
            ("user", "u2"),
            ("assistant", "a2"),
        ]

    def test_tail_no_user_before_assistant(self, ilan_server: IlanServer) -> None:
        # Conversation that opens with the assistant — there's no preceding
        # user message to prepend, so we just return the assistant + after.
        _post(ilan_server, "/tasks", {"name": "tail-asst-first", "prompt": "P"})
        ilan_server.store.log_path("tail-asst-first").write_text("")
        ilan_server.store.append_log("tail-asst-first", "assistant", "a1")
        ilan_server.store.append_log("tail-asst-first", "user", "u1")

        resp = _get(ilan_server, "/tasks/tail-asst-first/tail")
        entries = resp["entries"]
        assert [(e["role"], e["content"]) for e in entries] == [
            ("assistant", "a1"),
            ("user", "u1"),
        ]

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
        ilan_server.store.log_path("tail-n-big").write_text("")
        ilan_server.store.append_log("tail-n-big", "assistant", "a1")
        ilan_server.store.append_log("tail-n-big", "user", "u1")

        resp = _get(ilan_server, "/tasks/tail-n-big/tail?n=50")
        assert len(resp["entries"]) == 2

    def test_tail_n_empty_logs_warning(self, ilan_server: IlanServer) -> None:
        _post(ilan_server, "/tasks", {"name": "tail-n-empty", "prompt": "P"})
        ilan_server.store.log_path("tail-n-empty").write_text("")
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


class TestLastModel:
    def _make_session_log(self, tmp_path: Path, name: str, lines: list[dict]) -> Path:
        log = tmp_path / f"{name}.jsonl"
        with open(log, "w") as f:
            for entry in lines:
                f.write(json.dumps(entry) + "\n")
        return log

    def _attach_session_log(self, server: IlanServer, name: str, log_path: Path) -> None:
        with server.lock:
            task = server.store.get_task(name)
            task.session_id = log_path.stem
            task.session_log_path = str(log_path)
            server.store.put_task(task)

    def test_last_model_returns_last_assistant_model(
        self, ilan_server: IlanServer, tmp_path: Path
    ) -> None:
        _post(ilan_server, "/tasks", {"name": "lm-basic", "prompt": "P"})
        log = self._make_session_log(tmp_path, "lm-basic", [
            {"message": {"role": "user", "content": "hi"}},
            {"message": {"role": "assistant", "model": "claude-opus-4-7", "content": "a1"}},
            {"message": {"role": "user", "content": "ok"}},
            {"message": {"role": "assistant", "model": "claude-haiku-4-5", "content": "a2"}},
        ])
        self._attach_session_log(ilan_server, "lm-basic", log)

        resp = _get(ilan_server, "/tasks/lm-basic/last-model")
        assert resp["name"] == "lm-basic"
        assert resp["model"] == "claude-haiku-4-5"

    def test_last_model_skips_non_assistant_tail(
        self, ilan_server: IlanServer, tmp_path: Path
    ) -> None:
        _post(ilan_server, "/tasks", {"name": "lm-skip", "prompt": "P"})
        log = self._make_session_log(tmp_path, "lm-skip", [
            {"message": {"role": "assistant", "model": "claude-opus-4-7", "content": "a"}},
            {"message": {"role": "user", "content": "later user msg"}},
            {"someOtherField": "summary or sidechain"},
        ])
        self._attach_session_log(ilan_server, "lm-skip", log)

        resp = _get(ilan_server, "/tasks/lm-skip/last-model")
        assert resp["model"] == "claude-opus-4-7"

    def test_last_model_no_session(self, ilan_server: IlanServer) -> None:
        _post(ilan_server, "/tasks", {"name": "lm-nosess", "prompt": "P"})
        resp = _get(ilan_server, "/tasks/lm-nosess/last-model")
        assert "error" in resp

    def test_last_model_missing_log_file(
        self, ilan_server: IlanServer, tmp_path: Path
    ) -> None:
        _post(ilan_server, "/tasks", {"name": "lm-missing", "prompt": "P"})
        ghost = tmp_path / "ghost.jsonl"
        self._attach_session_log(ilan_server, "lm-missing", ghost)
        resp = _get(ilan_server, "/tasks/lm-missing/last-model")
        assert "error" in resp

    def test_last_model_no_assistant_entry(
        self, ilan_server: IlanServer, tmp_path: Path
    ) -> None:
        _post(ilan_server, "/tasks", {"name": "lm-noasst", "prompt": "P"})
        log = self._make_session_log(tmp_path, "lm-noasst", [
            {"message": {"role": "user", "content": "hi"}},
        ])
        self._attach_session_log(ilan_server, "lm-noasst", log)
        resp = _get(ilan_server, "/tasks/lm-noasst/last-model")
        assert "error" in resp

    def test_last_model_fast_path_uses_cache(self, ilan_server: IlanServer) -> None:
        """A cached ``last_assistant_model`` is returned without a session log."""
        _post(ilan_server, "/tasks", {"name": "lm-cached", "prompt": "P"})
        with ilan_server.lock:
            task = ilan_server.store.get_task("lm-cached")
            task.last_assistant_model = "claude-sonnet-4-6"
            ilan_server.store.put_task(task)
        resp = _get(ilan_server, "/tasks/lm-cached/last-model")
        assert resp["model"] == "claude-sonnet-4-6"

    def test_last_model_backfills_cache(
        self, ilan_server: IlanServer, tmp_path: Path
    ) -> None:
        """The fallback scan writes the resolved model back onto the task so the
        next lookup is a cache hit."""
        _post(ilan_server, "/tasks", {"name": "lm-backfill", "prompt": "P"})
        log = self._make_session_log(tmp_path, "lm-backfill", [
            {"message": {"role": "assistant", "model": "claude-opus-4-7", "content": "a"}},
        ])
        self._attach_session_log(ilan_server, "lm-backfill", log)
        resp = _get(ilan_server, "/tasks/lm-backfill/last-model")
        assert resp["model"] == "claude-opus-4-7"
        # Cache populated as a side effect of the scan.
        task = ilan_server.store.get_task("lm-backfill")
        assert task.last_assistant_model == "claude-opus-4-7"

    def test_tail_includes_last_assistant_model(self, ilan_server: IlanServer) -> None:
        _post(ilan_server, "/tasks", {"name": "lm-tail", "prompt": "P"})
        ilan_server.store.append_log("lm-tail", "assistant", "hi")
        with ilan_server.lock:
            task = ilan_server.store.get_task("lm-tail")
            task.last_assistant_model = "claude-opus-4-8"
            ilan_server.store.put_task(task)
        resp = _get(ilan_server, "/tasks/lm-tail/tail")
        assert resp["last_assistant_model"] == "claude-opus-4-8"

    def test_logs_includes_last_assistant_model(self, ilan_server: IlanServer) -> None:
        _post(ilan_server, "/tasks", {"name": "lm-logs", "prompt": "P"})
        ilan_server.store.append_log("lm-logs", "assistant", "hi")
        with ilan_server.lock:
            task = ilan_server.store.get_task("lm-logs")
            task.last_assistant_model = "claude-opus-4-8"
            ilan_server.store.put_task(task)
        resp = _get(ilan_server, "/tasks/lm-logs/logs")
        assert resp["last_assistant_model"] == "claude-opus-4-8"


# ── History URL ────────────────────────────────────────────────────────


class TestHistoryUrl:
    GIST_URL = "https://gist.github.com/u/gid1"

    def _attach_gist(self, server: IlanServer, name: str) -> None:
        with server.lock:
            task = server.store.get_task(name)
            task.gist_id = "gid1"
            task.gist_url = self.GIST_URL
            server.store.put_task(task)

    def test_history_url_unknown_task(self, ilan_server: IlanServer) -> None:
        resp = _get(ilan_server, "/tasks/no-such-task/history-url")
        assert "error" in resp

    def test_history_url_no_gist(self, ilan_server: IlanServer) -> None:
        _post(ilan_server, "/tasks", {"name": "hu-nogist", "prompt": "P"})
        resp = _get(ilan_server, "/tasks/hu-nogist/history-url")
        assert resp == {"url": None}

    def test_history_url_no_token_returns_gist_page(
        self, ilan_server: IlanServer, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _post(ilan_server, "/tasks", {"name": "hu-notoken", "prompt": "P"})
        self._attach_gist(ilan_server, "hu-notoken")
        monkeypatch.setattr(srv_mod, "github_token", lambda: "")
        resp = _get(ilan_server, "/tasks/hu-notoken/history-url")
        assert resp == {"url": self.GIST_URL}

    def test_history_url_deep_links_last_comment(
        self, ilan_server: IlanServer, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _post(ilan_server, "/tasks", {"name": "hu-deep", "prompt": "P"})
        self._attach_gist(ilan_server, "hu-deep")
        deep = f"{self.GIST_URL}?permalink_comment_id=7#gistcomment-7"
        monkeypatch.setattr(srv_mod, "github_token", lambda: "tok")
        monkeypatch.setattr(
            srv_mod,
            "last_comment_url",
            lambda token, gist_id, html_url: deep,
        )
        resp = _get(ilan_server, "/tasks/hu-deep/history-url")
        assert resp == {"url": deep}

    def test_history_url_api_failure_falls_back(
        self, ilan_server: IlanServer, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _post(ilan_server, "/tasks", {"name": "hu-fail", "prompt": "P"})
        self._attach_gist(ilan_server, "hu-fail")
        monkeypatch.setattr(srv_mod, "github_token", lambda: "tok")

        def boom(token, gist_id, html_url):
            raise RuntimeError("GitHub down")

        monkeypatch.setattr(srv_mod, "last_comment_url", boom)
        resp = _get(ilan_server, "/tasks/hu-fail/history-url")
        assert resp == {"url": self.GIST_URL}


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


# ── Kill ────────────────────────────────────────────────────────────────


class TestKill:
    def test_kill_non_working_fails(self, ilan_server: IlanServer) -> None:
        _post(ilan_server, "/tasks", {"name": "kill-idle", "prompt": "P"})
        with ilan_server.lock:
            task = ilan_server.store.get_task("kill-idle")
            task.set_status(TaskStatus.NEEDS_ATTENTION)
            ilan_server.store.put_task(task)
        resp = _post(ilan_server, "/tasks/kill-idle/kill")
        assert "error" in resp


# ── Max / Unmax (Fable model) ───────────────────────────────────────────


class TestMaxUnmax:
    def test_max_sets_fable_model(self, ilan_server: IlanServer) -> None:
        _post(ilan_server, "/tasks", {"name": "max-test", "prompt": "P"})
        resp = _post(ilan_server, "/tasks/max-test/max")
        assert resp.get("ok") is True
        assert resp["model"] == "claude-fable-5"

        task = _get(ilan_server, "/tasks/max-test")["task"]
        assert task["model"] == "claude-fable-5"

    def test_unmax_clears_model(self, ilan_server: IlanServer) -> None:
        _post(ilan_server, "/tasks", {"name": "unmax-test", "prompt": "P"})
        _post(ilan_server, "/tasks/unmax-test/max")
        resp = _post(ilan_server, "/tasks/unmax-test/unmax")
        assert resp.get("ok") is True
        assert resp["model"] is None

        task = _get(ilan_server, "/tasks/unmax-test")["task"]
        assert task["model"] is None

    def test_new_task_has_no_model(self, ilan_server: IlanServer) -> None:
        _post(ilan_server, "/tasks", {"name": "model-fresh", "prompt": "P"})
        task = _get(ilan_server, "/tasks/model-fresh")["task"]
        assert task["model"] is None

    def test_list_includes_model(self, ilan_server: IlanServer) -> None:
        _post(ilan_server, "/tasks", {"name": "model-list", "prompt": "P"})
        _post(ilan_server, "/tasks/model-list/max")
        resp = _get(ilan_server, "/tasks")
        row = next(t for t in resp["tasks"] if t["name"] == "model-list")
        assert row["model"] == "claude-fable-5"

    def test_max_accepts_alias(self, ilan_server: IlanServer) -> None:
        _post(ilan_server, "/tasks", {"name": "max-alias", "prompt": "P"})
        alias = _get(ilan_server, "/tasks/max-alias")["task"]["alias"]
        assert alias is not None
        resp = _post(ilan_server, f"/tasks/{alias}/max")
        assert resp.get("ok") is True
        assert resp["name"] == "max-alias"

    def test_max_on_codex_is_noop_with_warning(self, ilan_server: IlanServer) -> None:
        """Fable is Claude-only, so maxing a codex task changes nothing and
        returns a warning instead of a broken model override."""
        _post(ilan_server, "/tasks", {"name": "max-codex", "prompt": "P", "agent": "codex"})
        resp = _post(ilan_server, "/tasks/max-codex/max")
        assert resp.get("ok") is True
        assert resp.get("model") is None  # unchanged, not set to Fable
        assert "warning" in resp
        assert "codex" in resp["warning"]

        task = _get(ilan_server, "/tasks/max-codex")["task"]
        assert task["model"] is None  # no Fable override persisted
        assert task["engine"] == "codex"

    def test_max_after_switch_to_codex_is_noop(self, ilan_server: IlanServer) -> None:
        """A claude task maxed to Fable, then switched to codex, is a no-op if
        re-maxed on codex. The task keeps its Fable ``model`` (switch-backend
        doesn't rewrite it), but the codex backend ignores a Claude-only
        override at spawn time — see test_backends_codex."""
        _post(ilan_server, "/tasks", {"name": "max-then-switch", "prompt": "P"})
        _post(ilan_server, "/tasks/max-then-switch/max")  # claude → Fable
        _park(ilan_server, "max-then-switch")
        _post(ilan_server, "/tasks/max-then-switch/switch-backend")  # → codex
        resp = _post(ilan_server, "/tasks/max-then-switch/max")
        assert resp.get("ok") is True
        assert "warning" in resp

    def test_max_unknown_task_404(self, ilan_server: IlanServer) -> None:
        resp = _post(ilan_server, "/tasks/does-not-exist/max")
        assert "error" in resp

    def test_unmax_unknown_task_404(self, ilan_server: IlanServer) -> None:
        resp = _post(ilan_server, "/tasks/does-not-exist/unmax")
        assert "error" in resp


# ── Switch backend ──────────────────────────────────────────────────────


def _park(
    server: IlanServer, name: str,
    status: TaskStatus = TaskStatus.AGENT_FINISHED,
) -> None:
    """Move a (fake-spawned) WORKING task to a switchable resting status."""
    with server.lock:
        task = server.store.get_task(name)
        assert task is not None
        task.set_status(status)
        server.store.put_task(task)


class TestSwitchBackend:
    def test_toggles_claude_to_codex(self, ilan_server: IlanServer) -> None:
        _post(ilan_server, "/tasks", {"name": "sw-1", "prompt": "P"})
        _park(ilan_server, "sw-1")
        resp = _post(ilan_server, "/tasks/sw-1/switch-backend")
        assert resp.get("ok") is True
        assert resp["from_engine"] == "claude"
        assert resp["engine"] == "codex"
        task = _get(ilan_server, "/tasks/sw-1")["task"]
        assert task["engine"] == "codex"

    def test_roundtrip_toggles_back(self, ilan_server: IlanServer) -> None:
        _post(ilan_server, "/tasks", {"name": "sw-2", "prompt": "P"})
        _park(ilan_server, "sw-2")
        _post(ilan_server, "/tasks/sw-2/switch-backend")
        resp = _post(ilan_server, "/tasks/sw-2/switch-backend")
        assert resp["from_engine"] == "codex"
        assert resp["engine"] == "claude"
        task = _get(ilan_server, "/tasks/sw-2")["task"]
        assert task["engine"] == "claude"

    def test_accepts_alias(self, ilan_server: IlanServer) -> None:
        _post(ilan_server, "/tasks", {"name": "sw-alias", "prompt": "P"})
        _park(ilan_server, "sw-alias")
        alias = _get(ilan_server, "/tasks/sw-alias")["task"]["alias"]
        assert alias is not None
        resp = _post(ilan_server, f"/tasks/{alias}/switch-backend")
        assert resp.get("ok") is True
        assert resp["name"] == "sw-alias"
        assert resp["engine"] == "codex"

    def test_unknown_task_404(self, ilan_server: IlanServer) -> None:
        resp = _post(ilan_server, "/tasks/does-not-exist/switch-backend")
        assert "error" in resp

    def test_rejects_terminal_task(self, ilan_server: IlanServer) -> None:
        _post(ilan_server, "/tasks", {"name": "sw-done", "prompt": "P"})
        _post(ilan_server, "/tasks/sw-done/done")
        resp = _post(ilan_server, "/tasks/sw-done/switch-backend")
        assert "error" in resp
        assert "DONE" in resp["error"]

    def test_list_reflects_engine_after_switch(self, ilan_server: IlanServer) -> None:
        _post(ilan_server, "/tasks", {"name": "sw-list", "prompt": "P"})
        _park(ilan_server, "sw-list")
        _post(ilan_server, "/tasks/sw-list/switch-backend")
        resp = _get(ilan_server, "/tasks")
        row = next(t for t in resp["tasks"] if t["name"] == "sw-list")
        assert row["engine"] == "codex"

    def test_rejects_working_task(self, ilan_server: IlanServer) -> None:
        """A WORKING task cannot be switched: the server warns and does
        nothing — the agent is not killed and the engine does not flip."""
        _post(ilan_server, "/tasks", {"name": "sw-working", "prompt": "P"})

        calls: list[str] = []
        ilan_server.runner.kill = lambda t: calls.append("kill")  # type: ignore[method-assign]

        resp = _post(ilan_server, "/tasks/sw-working/switch-backend")
        assert "error" in resp
        assert "WORKING" in resp["error"]
        assert calls == []

        task = _get(ilan_server, "/tasks/sw-working")["task"]
        assert task["status"] == "WORKING"
        assert task["engine"] == "claude"


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
