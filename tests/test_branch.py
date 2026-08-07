"""Tests for ``ilan task branch`` — Store helper and server endpoint."""

from __future__ import annotations

import json
import signal
import threading
import time
import uuid
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from ilan.models import ALIAS_POOL, ENGINE_CLAUDE, ENGINE_CODEX, Task, TaskStatus
from ilan.runner import Runner
from ilan.server import IlanServer
from ilan.store import Store


# ── Store.branch_task ───────────────────────────────────────────────────


@pytest.fixture()
def store(tmp_workdir: Path) -> Store:
    return Store(tmp_workdir)


class TestStoreBranch:
    def test_branch_forks_session_and_copies_logs(
        self, store: Store, tmp_path: Path,
    ) -> None:
        # Stage a real on-disk Claude session log so the fork path runs.
        sessions_dir = tmp_path / "claude_sessions"
        sessions_dir.mkdir()
        parent_session = sessions_dir / "sid-1.jsonl"
        parent_session.write_text(
            '{"sessionId": "sid-1", "type": "user", "message": "hello"}\n'
            '{"sessionId": "sid-1", "type": "assistant", "message": "hi"}\n'
        )

        parent = Task(
            name="parent",
            prompt="root prompt",
            session_id="sid-1",
            session_log_path=str(parent_session),
            alias="aa",
            task_hash="abcd1234",
        )
        store.put_task(parent)
        store.append_log("parent", "user", "hello")
        store.append_log("parent", "assistant", "hi")

        child = store.branch_task(
            parent, "child",
            alias="bb", task_hash="deadbeef", now="2026-01-01T00:00:00+00:00",
        )

        assert child.name == "child"
        assert child.parent_name == "parent"
        assert child.alias == "bb"
        assert child.task_hash == "deadbeef"
        assert child.status == TaskStatus.WORKING
        assert child.cached_replies == []
        assert child.cost_usd == 0.0
        # A claude parent yields a claude child that resumes the forked native
        # session (no catch-up needed — the fork carries the full history).
        assert child.engine == ENGINE_CLAUDE
        assert child.awaiting_catchup is False
        assert child.gist_branch_point == 2
        assert child.gist_synced_count == 2
        assert child.gist_branch_parent_name == "parent"
        assert child.gist_parent_comment_url is None

        # Child got its own session_id (a UUID) distinct from the parent.
        assert child.session_id != "sid-1"
        assert child.session_id is not None
        uuid.UUID(child.session_id)  # raises if not a valid UUID
        # Child's session log lives next to the parent's, named by the new UUID,
        # and contains a copy of the parent's history.
        assert child.session_log_path is not None
        child_session = Path(child.session_log_path)
        assert child_session.parent == parent_session.parent
        assert child_session.name == f"{child.session_id}.jsonl"
        assert child_session.exists()
        assert child_session.read_text() == parent_session.read_text()
        # The two paths refer to different files so post-branch writes diverge.
        child_session.write_text(child_session.read_text() + "child line\n")
        assert "child line" not in parent_session.read_text()

        child_logs = store.read_logs("child")
        assert len(child_logs) == 2
        assert child_logs[0].content == "hello"
        assert child_logs[1].content == "hi"

        # Parent is untouched.
        parent_reloaded = store.get_task("parent")
        assert parent_reloaded is not None
        assert parent_reloaded.session_id == "sid-1"
        assert parent_reloaded.session_log_path == str(parent_session)
        assert parent_reloaded.parent_name is None

    def test_branch_codex_parent_carries_engine_and_spawns_fresh(
        self, store: Store, tmp_path: Path,
    ) -> None:
        """A codex parent yields a codex child that spawns a fresh session.

        The Claude-style copy-to-<uuid>.jsonl fork is unresolvable for codex
        (codex resolves by rollout-* naming), so the child starts fresh with
        ``awaiting_catchup`` set and no native session is copied. The inherited
        unified log is still copied so catch-up can seed the fresh session.
        """
        codex_session = tmp_path / "rollout-2026-07-19T00-00-00-abc.jsonl"
        codex_session.write_text('{"type": "user"}\n')

        parent = Task(
            name="parent",
            prompt="root prompt",
            engine=ENGINE_CODEX,
            session_id="codex-sid",
            session_log_path=str(codex_session),
            alias="aa",
            task_hash="abcd1234",
        )
        store.put_task(parent)
        store.append_log("parent", "user", "hello")
        store.append_log("parent", "assistant", "hi")

        child = store.branch_task(
            parent, "child",
            alias="bb", task_hash="deadbeef", now="2026-01-01T00:00:00+00:00",
        )

        assert child.engine == ENGINE_CODEX
        # No un-forkable native session is inherited; the child spawns fresh.
        assert child.session_id is None
        assert child.session_log_path is None
        assert child.awaiting_catchup is True
        # The Claude-style fork file was NOT created next to the parent's.
        assert list(tmp_path.glob("*.jsonl")) == [codex_session]
        # The unified ilan log was still copied so catch-up has history to seed.
        child_logs = store.read_logs("child")
        assert [e.content for e in child_logs] == ["hello", "hi"]

    def test_branch_without_parent_session_log_skips_fork(
        self, store: Store,
    ) -> None:
        """No on-disk Claude session log → child inherits parent's id verbatim.

        This path is reached via the public API only when the server-side
        endpoint's ``find_session_log`` check is bypassed (e.g. tests calling
        ``branch_task`` directly with a fake path).  We keep it as a
        pass-through so unit tests stay decoupled from the filesystem.
        """
        parent = Task(
            name="parent", prompt="p",
            session_id="sid-1",
            session_log_path="/does/not/exist.jsonl",
        )
        store.put_task(parent)

        child = store.branch_task(
            parent, "child",
            alias="cc", task_hash="1111aaaa", now="2026-01-01T00:00:00+00:00",
        )
        assert child.session_id == "sid-1"
        assert child.session_log_path == "/does/not/exist.jsonl"

    def test_branch_without_parent_log(self, store: Store) -> None:
        """Branching a task with no ilan log yet yields an empty child log."""
        parent = Task(name="parent", prompt="p", session_id="sid-1")
        store.put_task(parent)

        child = store.branch_task(
            parent, "child",
            alias="cc", task_hash="1111aaaa", now="2026-01-01T00:00:00+00:00",
        )
        assert child.parent_name == "parent"
        assert store.read_logs("child") == []

    def test_rename_updates_children_parent_name(self, store: Store) -> None:
        parent = Task(name="old-parent", prompt="p", session_id="sid-1")
        store.put_task(parent)
        store.branch_task(
            parent, "child",
            alias="cc", task_hash="1111aaaa", now="2026-01-01T00:00:00+00:00",
        )

        store.rename_task("old-parent", "new-parent")

        child = store.get_task("child")
        assert child is not None
        assert child.parent_name == "new-parent"
        assert child.gist_branch_parent_name == "new-parent"

    def test_delete_reparents_children_to_grandparent(self, store: Store) -> None:
        """Deleting a middle task re-parents its children onto its parent."""
        grand = Task(name="grand", prompt="p", session_id="sid-1")
        store.put_task(grand)
        parent = store.branch_task(
            grand, "parent",
            alias="pp", task_hash="1111aaaa", now="2026-01-01T00:00:00+00:00",
        )
        store.branch_task(
            parent, "child",
            alias="cc", task_hash="2222bbbb", now="2026-01-02T00:00:00+00:00",
        )

        store.delete_task("parent")

        child = store.get_task("child")
        assert child is not None
        assert child.parent_name == "grand"
        # The parent link is re-pointed, but the vanished task is remembered so
        # ``ilan task tree`` can still draw a tombstone in its place.
        assert child.deleted_ancestors == ["parent"]
        # Gist lineage still points at the task whose history was inherited.
        assert child.gist_branch_parent_name == "parent"

    def test_delete_root_orphans_children(self, store: Store) -> None:
        root = Task(name="root", prompt="p", session_id="sid-1")
        store.put_task(root)
        store.branch_task(
            root, "child",
            alias="cc", task_hash="2222bbbb", now="2026-01-01T00:00:00+00:00",
        )

        store.delete_task("root")

        child = store.get_task("child")
        assert child is not None
        assert child.parent_name is None
        assert child.deleted_ancestors == ["root"]

    def test_chained_deletes_accumulate_tombstones_nearest_first(self, store: Store) -> None:
        """Deleting grand after parent leaves child with both, nearest first."""
        grand = Task(name="grand", prompt="p", session_id="sid-1")
        store.put_task(grand)
        parent = store.branch_task(
            grand, "parent",
            alias="pp", task_hash="1111aaaa", now="2026-01-01T00:00:00+00:00",
        )
        store.branch_task(
            parent, "child",
            alias="cc", task_hash="2222bbbb", now="2026-01-02T00:00:00+00:00",
        )

        store.delete_task("parent")
        store.delete_task("grand")

        child = store.get_task("child")
        assert child is not None
        assert child.parent_name is None
        assert child.deleted_ancestors == ["parent", "grand"]

    def test_siblings_record_the_same_tombstone(self, store: Store) -> None:
        grand = Task(name="grand", prompt="p", session_id="sid-1")
        store.put_task(grand)
        parent = store.branch_task(
            grand, "parent",
            alias="pp", task_hash="1111aaaa", now="2026-01-01T00:00:00+00:00",
        )
        for name, alias, task_hash in (
            ("kid-a", "ka", "2222bbbb"), ("kid-b", "kb", "3333cccc"),
        ):
            store.branch_task(
                parent, name,
                alias=alias, task_hash=task_hash, now="2026-01-02T00:00:00+00:00",
            )

        store.delete_task("parent")

        for name in ("kid-a", "kid-b"):
            kid = store.get_task(name)
            assert kid is not None
            assert kid.parent_name == "grand"
            assert kid.deleted_ancestors == ["parent"]


# ── server /tasks/<name>/branch ─────────────────────────────────────────


@pytest.fixture()
def ilan_server(tmp_workdir: Path, tmp_config: Path, env_with_mock_claude: None):
    import ilan.config as cfg_mod

    cfg_mod.save({**cfg_mod.DEFAULTS, "workdir": str(tmp_workdir)})

    server = IlanServer()

    def _fake_start(task) -> bool:
        task.set_status(TaskStatus.WORKING)
        server.store.put_task(task)
        return True

    server.runner.start = _fake_start  # type: ignore[method-assign]
    server.runner.reap_finished = lambda: None  # type: ignore[method-assign]

    with patch.object(signal, "signal"):
        # Small poll_interval: shutdown() blocks for up to one serve-loop
        # tick, and this function-scoped fixture tears a server down per test.
        t = threading.Thread(
            target=server.run,
            kwargs={"host": "127.0.0.1", "port": 0, "poll_interval": 0.01},
            daemon=True,
        )
        t.start()

        deadline = time.monotonic() + 5
        port = None
        while time.monotonic() < deadline:
            if server._httpd is not None:
                port = server._httpd.server_address[1]
                break
            time.sleep(0.05)
        assert port is not None
        server._test_port = port  # type: ignore[attr-defined]
        server._test_url = f"http://127.0.0.1:{port}"  # type: ignore[attr-defined]

        yield server

        server.shutdown()
        t.join(timeout=3)


def _post(server: IlanServer, path: str, body: dict | None = None) -> tuple[int, dict]:
    url = f"{server._test_url}{path}"  # type: ignore[attr-defined]
    data = json.dumps(body).encode() if body else None
    req = Request(url, data=data, method="POST")
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read())
    except HTTPError as exc:
        return exc.code, json.loads(exc.read())


def _seed_parent(server: IlanServer, *, session_id: str | None = "sid-1") -> Task:
    """Put a parent task directly into the store with an established session."""
    parent = Task(
        name="parent-task",
        prompt="root prompt",
        created_at="2026-01-01T00:00:00+00:00",
        status_changed_at="2026-01-01T00:00:00+00:00",
        session_id=session_id,
        session_log_path="/fake/sid-1.jsonl" if session_id else None,
        alias="aa",
        task_hash="abcd1234",
    )
    server.store.put_task(parent)
    server.store.append_log("parent-task", "user", "hello")
    server.store.append_log("parent-task", "assistant", "hi")
    return parent


class TestServerBranchEndpoint:
    def test_branch_success(self, ilan_server: IlanServer) -> None:
        _seed_parent(ilan_server)
        with patch.object(Runner, "find_session_log", return_value=Path("/fake/sid-1.jsonl")):
            code, resp = _post(
                ilan_server, "/tasks/parent-task/branch",
                {"new_name": "child-task", "message": "try plan B"},
            )
        assert code == 200, resp
        assert resp["ok"] is True
        assert resp["name"] == "child-task"
        assert resp["parent_name"] == "parent-task"

        child = ilan_server.store.get_task("child-task")
        assert child is not None
        assert child.parent_name == "parent-task"
        assert child.session_id == "sid-1"
        assert child.cached_replies == ["try plan B"]
        assert child.awaiting_branch_notice is True
        assert child.gist_branch_point == 2
        assert child.gist_synced_count == 2
        assert child.gist_branch_parent_name == "parent-task"
        logs = ilan_server.store.read_logs("child-task")
        # Copied 2 parent entries + 1 new user message.
        assert [e.content for e in logs] == ["hello", "hi", "try plan B"]

    def test_branch_codex_parent_yields_fresh_codex_child(
        self, ilan_server: IlanServer,
    ) -> None:
        """Branching a codex parent produces a codex child that spawns fresh."""
        parent = Task(
            name="codex-parent",
            prompt="root prompt",
            created_at="2026-01-01T00:00:00+00:00",
            status_changed_at="2026-01-01T00:00:00+00:00",
            engine=ENGINE_CODEX,
            session_id="codex-sid",
            session_log_path="/fake/rollout-x-abc.jsonl",
            alias="aa",
            task_hash="abcd1234",
        )
        ilan_server.store.put_task(parent)
        ilan_server.store.append_log("codex-parent", "user", "hello")
        ilan_server.store.append_log("codex-parent", "assistant", "hi")

        with patch.object(
            Runner, "find_session_log",
            return_value=Path("/fake/rollout-x-abc.jsonl"),
        ):
            code, resp = _post(
                ilan_server, "/tasks/codex-parent/branch",
                {"new_name": "codex-child", "message": "try plan B"},
            )
        assert code == 200, resp

        child = ilan_server.store.get_task("codex-child")
        assert child is not None
        assert child.engine == ENGINE_CODEX
        assert child.session_id is None
        assert child.awaiting_catchup is True
        assert child.cached_replies == ["try plan B"]
        logs = ilan_server.store.read_logs("codex-child")
        assert [e.content for e in logs] == ["hello", "hi", "try plan B"]

    def test_branch_refuses_without_message(self, ilan_server: IlanServer) -> None:
        """A branch that would only continue the parent's work is a reply to
        the parent, not a branch — a child spawned without its own assignment
        just resumes the parent's in-flight instructions verbatim."""
        _seed_parent(ilan_server)
        with patch.object(Runner, "find_session_log", return_value=Path("/fake/sid-1.jsonl")):
            code, resp = _post(
                ilan_server, "/tasks/parent-task/branch",
                {"new_name": "child-task"},
            )
        assert code == 400
        assert "requires a first assignment" in resp["error"]
        assert ilan_server.store.get_task("child-task") is None

    def test_branch_refuses_when_parent_has_no_session(self, ilan_server: IlanServer) -> None:
        _seed_parent(ilan_server, session_id=None)
        code, resp = _post(
            ilan_server, "/tasks/parent-task/branch",
            {"new_name": "child-task", "message": "try plan B"},
        )
        assert code == 409
        assert "no Claude Code session" in resp["error"]

    def test_branch_refuses_when_session_log_missing(self, ilan_server: IlanServer) -> None:
        _seed_parent(ilan_server)
        with patch.object(Runner, "find_session_log", return_value=None):
            code, resp = _post(
                ilan_server, "/tasks/parent-task/branch",
                {"new_name": "child-task", "message": "try plan B"},
            )
        assert code == 409
        assert "not found on disk" in resp["error"]

    def test_branch_refuses_name_collision(self, ilan_server: IlanServer) -> None:
        _seed_parent(ilan_server)
        ilan_server.store.put_task(Task(
            name="child-task", prompt="p",
            created_at="2026-01-01T00:00:00+00:00",
        ))
        with patch.object(Runner, "find_session_log", return_value=Path("/fake/sid-1.jsonl")):
            code, resp = _post(
                ilan_server, "/tasks/parent-task/branch",
                {"new_name": "child-task", "message": "try plan B"},
            )
        assert code == 409
        assert "already exists" in resp["error"]

    def test_branch_refuses_invalid_new_name(self, ilan_server: IlanServer) -> None:
        _seed_parent(ilan_server)
        with patch.object(Runner, "find_session_log", return_value=Path("/fake/sid-1.jsonl")):
            code, resp = _post(
                ilan_server, "/tasks/parent-task/branch",
                {"new_name": "x"},
            )
        assert code == 400
        assert "at least 3" in resp["error"]

    def test_branch_refuses_when_alias_pool_exhausted(self, ilan_server: IlanServer) -> None:
        _seed_parent(ilan_server)
        # Occupy every alias with dummy tasks.
        for i, alias in enumerate(ALIAS_POOL):
            if alias == "aa":  # already used by parent
                continue
            ilan_server.store.put_task(Task(
                name=f"filler-{i:03d}", prompt="p", alias=alias,
                created_at="2026-01-01T00:00:00+00:00",
            ))
        with patch.object(Runner, "find_session_log", return_value=Path("/fake/sid-1.jsonl")):
            code, resp = _post(
                ilan_server, "/tasks/parent-task/branch",
                {"new_name": "child-task", "message": "try plan B"},
            )
        assert code == 409
        assert "Alias pool exhausted" in resp["error"]
        assert ilan_server.store.get_task("child-task") is None

    def test_list_tasks_exposes_parent_name(self, ilan_server: IlanServer) -> None:
        _seed_parent(ilan_server)
        with patch.object(Runner, "find_session_log", return_value=Path("/fake/sid-1.jsonl")):
            _post(
                ilan_server, "/tasks/parent-task/branch",
                {"new_name": "child-task", "message": "try plan B"},
            )
        url = f"{ilan_server._test_url}/tasks"  # type: ignore[attr-defined]
        with urlopen(Request(url), timeout=5) as r:
            rows = json.loads(r.read())["tasks"]
        by_name = {row["name"]: row for row in rows}
        assert by_name["child-task"]["parent_name"] == "parent-task"
        assert by_name["parent-task"]["parent_name"] is None


# ── list filtering: terminal tasks are hidden regardless of descendants ─


def _list_default(server: IlanServer) -> list[dict]:
    url = f"{server._test_url}/tasks"  # type: ignore[attr-defined]
    with urlopen(Request(url), timeout=5) as r:
        return json.loads(r.read())["tasks"]


class TestListTerminalFiltering:
    def test_done_middle_hidden_even_when_grandchild_active(self, ilan_server: IlanServer) -> None:
        """A→B→C where B is DONE but C is active: default ls still hides B."""
        parent = Task(
            name="A", prompt="p", session_id="sid-1",
            status=TaskStatus.NEEDS_ATTENTION, alias="aa",
            created_at="2026-01-01T00:00:00+00:00",
            status_changed_at="2026-01-01T00:00:00+00:00",
        )
        mid = Task(
            name="B", prompt="p", session_id="sid-1",
            status=TaskStatus.DONE, parent_name="A",
            created_at="2026-01-02T00:00:00+00:00",
            status_changed_at="2026-01-02T00:00:00+00:00",
        )
        child = Task(
            name="C", prompt="p", session_id="sid-1",
            status=TaskStatus.WORKING, parent_name="B", alias="bb",
            created_at="2026-01-03T00:00:00+00:00",
            status_changed_at="2026-01-03T00:00:00+00:00",
        )
        for t in (parent, mid, child):
            ilan_server.store.put_task(t)

        names = {r["name"] for r in _list_default(ilan_server)}
        assert names == {"A", "C"}

    def test_done_leaf_hidden_without_active_descendants(self, ilan_server: IlanServer) -> None:
        """A DONE task with no descendants is hidden from default ls."""
        ilan_server.store.put_task(Task(
            name="solo", prompt="p",
            status=TaskStatus.DONE,
            created_at="2026-01-01T00:00:00+00:00",
            status_changed_at="2026-01-01T00:00:00+00:00",
        ))
        ilan_server.store.put_task(Task(
            name="active-solo", prompt="p", alias="aa",
            status=TaskStatus.NEEDS_ATTENTION,
            created_at="2026-01-01T00:00:00+00:00",
            status_changed_at="2026-01-01T00:00:00+00:00",
        ))
        names = {r["name"] for r in _list_default(ilan_server)}
        assert names == {"active-solo"}


# ── delete with active descendants ───────────────────────────────────


def _delete(server: IlanServer, path: str) -> tuple[int, dict]:
    url = f"{server._test_url}{path}"  # type: ignore[attr-defined]
    req = Request(url, method="DELETE")
    try:
        with urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read())
    except HTTPError as exc:
        return exc.code, json.loads(exc.read())


class TestDeleteWithDescendants:
    def _seed_chain(self, server: IlanServer) -> None:
        server.store.put_task(Task(
            name="A", prompt="p", status=TaskStatus.NEEDS_ATTENTION, alias="aa",
            created_at="2026-01-01T00:00:00+00:00",
        ))
        server.store.put_task(Task(
            name="B", prompt="p", status=TaskStatus.DONE, parent_name="A",
            created_at="2026-01-02T00:00:00+00:00",
        ))
        server.store.put_task(Task(
            name="C", prompt="p", status=TaskStatus.WORKING, parent_name="B", alias="bb",
            created_at="2026-01-03T00:00:00+00:00",
        ))

    def test_allows_active_descendant(self, ilan_server: IlanServer) -> None:
        self._seed_chain(ilan_server)
        code, resp = _delete(ilan_server, "/tasks/A")
        assert code == 200
        assert resp["ok"] is True
        assert ilan_server.store.get_task("A") is None
        # The surviving branch is preserved even though C is still active.
        b = ilan_server.store.get_task("B")
        c = ilan_server.store.get_task("C")
        assert b is not None
        assert b.parent_name is None
        assert c is not None
        assert c.parent_name == "B"

    def test_allows_when_only_terminal_descendants(self, ilan_server: IlanServer) -> None:
        """A→B where both are DONE: deletion remains allowed."""
        ilan_server.store.put_task(Task(
            name="A", prompt="p", status=TaskStatus.DONE,
            created_at="2026-01-01T00:00:00+00:00",
        ))
        ilan_server.store.put_task(Task(
            name="B", prompt="p", status=TaskStatus.DONE, parent_name="A",
            created_at="2026-01-02T00:00:00+00:00",
        ))
        code, resp = _delete(ilan_server, "/tasks/A")
        assert code == 200
        assert resp["ok"] is True
        assert ilan_server.store.get_task("A") is None

# ── CLI: branch -n flag, rm, clean skips parents ───────────────────────


def _make_cli_client(**overrides):
    from unittest.mock import MagicMock
    client = MagicMock()
    client.ensure_server.return_value = {}
    client.version_mismatch = None
    client.is_remote = False
    for k, v in overrides.items():
        setattr(client, k, v)
    return client


class TestBranchCliFlags:
    def test_branch_requires_dash_n(self, tmp_config) -> None:
        from click.testing import CliRunner
        from ilan.cli import main
        runner = CliRunner()
        client = _make_cli_client()
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["branch", "parent"])
        assert result.exit_code != 0
        assert "Missing option" in result.output or "-n" in result.output

    def test_branch_requires_a_message(self, tmp_config) -> None:
        """No -d/-f means the child has no assignment of its own — refuse
        client-side before the server is even asked."""
        from click.testing import CliRunner
        from ilan.cli import main
        runner = CliRunner()
        client = _make_cli_client()
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["branch", "parent", "-n", "child"])
        assert result.exit_code != 0
        assert "Exactly one of --file / --description" in result.output
        client.branch_task.assert_not_called()

    def test_branch_with_dash_n_and_description(self, tmp_config) -> None:
        from click.testing import CliRunner
        from ilan.cli import main
        runner = CliRunner()
        client = _make_cli_client()
        client.branch_task.return_value = {"ok": True, "name": "child", "parent_name": "parent"}
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, [
                "task", "branch", "parent", "-n", "child", "-d", "try plan B",
            ])
        assert result.exit_code == 0, result.output
        client.branch_task.assert_called_once_with("parent", "child", "try plan B")


class TestRm:
    def test_rm_leaf_deletes(self, tmp_config) -> None:
        from click.testing import CliRunner
        from ilan.cli import main
        runner = CliRunner()
        client = _make_cli_client()
        client.get_task.return_value = {"task": {"name": "X"}}
        client.delete_task.return_value = {"ok": True}
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["task", "rm", "X", "-y"])
        assert result.exit_code == 0
        client.list_tasks.assert_not_called()
        client.delete_task.assert_called_once_with("X")

    def test_rm_allows_active_descendants_without_force(self, tmp_config) -> None:
        from click.testing import CliRunner
        from ilan.cli import main
        runner = CliRunner()
        client = _make_cli_client()
        client.get_task.return_value = {"task": {"name": "A"}}
        client.delete_task.return_value = {"ok": True}
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["task", "rm", "A", "-y"])
        assert result.exit_code == 0, result.output
        client.list_tasks.assert_not_called()
        client.delete_task.assert_called_once_with("A")

    def test_rm_allows_parent_and_child_together(self, tmp_config) -> None:
        from click.testing import CliRunner
        from ilan.cli import main
        runner = CliRunner()
        client = _make_cli_client()
        # Return canonical names for both lookups.
        client.get_task.side_effect = [
            {"task": {"name": "A"}}, {"task": {"name": "C"}},
        ]
        client.delete_task.return_value = {"ok": True}
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["task", "rm", "A", "C", "-y"])
        assert result.exit_code == 0, result.output
        deleted = [c.args[0] for c in client.delete_task.call_args_list]
        assert set(deleted) == {"A", "C"}

    def test_remove_is_top_level_alias(self, tmp_config) -> None:
        from click.testing import CliRunner
        from ilan.cli import main
        runner = CliRunner()
        client = _make_cli_client()
        client.get_task.return_value = {"task": {"name": "A"}}
        client.delete_task.return_value = {"ok": True}
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["remove", "A", "-y"])
        assert result.exit_code == 0, result.output
        client.delete_task.assert_called_once_with("A")


class TestCleanSkipsParents:
    def test_clean_skips_tasks_with_children(self, tmp_config) -> None:
        from click.testing import CliRunner
        from ilan.cli import main
        runner = CliRunner()
        client = _make_cli_client()
        # A is the parent; B is the child. Both old enough to be eligible,
        # but A must be spared because it has a child.
        old_ts = "2020-01-01T00:00:00+00:00"
        client.list_tasks.return_value = {"tasks": [
            {
                "name": "A", "status": "DONE", "alias": None,
                "created_at": old_ts, "status_changed_at": old_ts,
                "needs_review": False, "cost_usd": 0.0, "sleep_seconds": None,
                "parent_name": None,
            },
            {
                "name": "B", "status": "DONE", "alias": None,
                "created_at": old_ts, "status_changed_at": old_ts,
                "needs_review": False, "cost_usd": 0.0, "sleep_seconds": None,
                "parent_name": "A",
            },
        ]}
        client.delete_task.return_value = {"ok": True}
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["clean", "1h", "-y"])
        assert result.exit_code == 0, result.output
        # Only B should be deleted; A is spared because it has a child.
        deleted_names = [c.args[0] for c in client.delete_task.call_args_list]
        assert deleted_names == ["B"]
        assert "Skipped" in result.output
        assert "A" in result.output
