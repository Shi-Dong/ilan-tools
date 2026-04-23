"""Tests for ``ilan task branch`` — Store helper, server endpoint, tree rendering."""

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

from ilan.cli import _order_tasks_as_forest
from ilan.models import ALIAS_POOL, Task, TaskStatus
from ilan.runner import Runner
from ilan.server import IlanServer
from ilan.store import Store


# ── Store.branch_task ───────────────────────────────────────────────────


@pytest.fixture()
def store(tmp_workdir: Path) -> Store:
    return Store(tmp_workdir)


class TestStoreBranch:
    def test_branch_copies_session_and_logs(self, store: Store) -> None:
        parent = Task(
            name="parent",
            prompt="root prompt",
            session_id="sid-1",
            session_log_path="/fake/sid-1.jsonl",
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
        assert child.session_id == "sid-1"
        assert child.session_log_path == "/fake/sid-1.jsonl"
        assert child.alias == "bb"
        assert child.task_hash == "deadbeef"
        assert child.status == TaskStatus.UNCLAIMED
        assert child.cached_replies == []
        assert child.cost_usd == 0.0

        child_logs = store.read_logs("child")
        assert len(child_logs) == 2
        assert child_logs[0].content == "hello"
        assert child_logs[1].content == "hi"

        # Parent is untouched.
        parent_reloaded = store.get_task("parent")
        assert parent_reloaded is not None
        assert parent_reloaded.session_id == "sid-1"
        assert parent_reloaded.parent_name is None

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


# ── _order_tasks_as_forest ──────────────────────────────────────────────


def _row(name: str, parent: str | None = None, created_at: str = "") -> dict:
    return {
        "name": name,
        "status": "UNCLAIMED",
        "created_at": created_at or f"2026-01-01T00:00:{ord(name[0]):02d}+00:00",
        "status_changed_at": "",
        "alias": None,
        "needs_review": False,
        "cost_usd": 0.0,
        "sleep_seconds": None,
        "parent_name": parent,
    }


class TestForestOrdering:
    def test_flat_list_no_prefixes(self) -> None:
        rows = [_row("a"), _row("b")]
        ordered = _order_tasks_as_forest(rows)
        assert [(r["name"], p) for r, p in ordered] == [("a", ""), ("b", "")]

    def test_simple_parent_child(self) -> None:
        rows = [_row("parent"), _row("child", parent="parent")]
        ordered = _order_tasks_as_forest(rows)
        assert [(r["name"], p) for r, p in ordered] == [
            ("parent", ""),
            ("child", "└─ "),
        ]

    def test_multiple_siblings_get_branch_glyphs(self) -> None:
        rows = [
            _row("parent"),
            _row("a", parent="parent", created_at="2026-01-01T00:00:01+00:00"),
            _row("b", parent="parent", created_at="2026-01-01T00:00:02+00:00"),
            _row("c", parent="parent", created_at="2026-01-01T00:00:03+00:00"),
        ]
        ordered = _order_tasks_as_forest(rows)
        names_prefixes = [(r["name"], p) for r, p in ordered]
        assert names_prefixes == [
            ("parent", ""),
            ("a", "├─ "),
            ("b", "├─ "),
            ("c", "└─ "),
        ]

    def test_grandchild_uses_double_indent(self) -> None:
        rows = [
            _row("grand"),
            _row("parent", parent="grand"),
            _row("child", parent="parent"),
        ]
        ordered = _order_tasks_as_forest(rows)
        assert [(r["name"], p) for r, p in ordered] == [
            ("grand", ""),
            ("parent", "└─ "),
            ("child", "   └─ "),
        ]

    def test_pipe_drawn_when_ancestor_has_siblings(self) -> None:
        rows = [
            _row("root"),
            _row("p1", parent="root", created_at="2026-01-01T00:00:01+00:00"),
            _row("c1", parent="p1", created_at="2026-01-01T00:00:02+00:00"),
            _row("p2", parent="root", created_at="2026-01-01T00:00:03+00:00"),
        ]
        ordered = _order_tasks_as_forest(rows)
        assert [(r["name"], p) for r, p in ordered] == [
            ("root", ""),
            ("p1", "├─ "),
            ("c1", "│  └─ "),
            ("p2", "└─ "),
        ]

    def test_orphan_rendered_as_root(self) -> None:
        """A child whose parent is filtered out (e.g. parent is DONE and hidden)."""
        rows = [_row("child", parent="missing-parent")]
        ordered = _order_tasks_as_forest(rows)
        assert [(r["name"], p) for r, p in ordered] == [("child", "")]


# ── server /tasks/<name>/branch ─────────────────────────────────────────


@pytest.fixture()
def ilan_server(tmp_workdir: Path, tmp_config: Path, env_with_mock_claude: None):
    import ilan.config as cfg_mod

    cfg_mod.save({**cfg_mod.DEFAULTS, "workdir": str(tmp_workdir)})

    server = IlanServer()
    server.runner.schedule = lambda: None

    with patch.object(signal, "signal"):
        t = threading.Thread(
            target=server.run,
            kwargs={"host": "127.0.0.1", "port": 0},
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
        with patch.object(Runner, "_find_session_log", return_value=Path("/fake/sid-1.jsonl")):
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
        logs = ilan_server.store.read_logs("child-task")
        # Copied 2 parent entries + 1 new user message.
        assert [e.content for e in logs] == ["hello", "hi", "try plan B"]

    def test_branch_no_message(self, ilan_server: IlanServer) -> None:
        _seed_parent(ilan_server)
        with patch.object(Runner, "_find_session_log", return_value=Path("/fake/sid-1.jsonl")):
            code, resp = _post(
                ilan_server, "/tasks/parent-task/branch",
                {"new_name": "child-task"},
            )
        assert code == 200
        child = ilan_server.store.get_task("child-task")
        assert child is not None
        assert child.cached_replies == []

    def test_branch_refuses_when_parent_has_no_session(self, ilan_server: IlanServer) -> None:
        _seed_parent(ilan_server, session_id=None)
        code, resp = _post(
            ilan_server, "/tasks/parent-task/branch",
            {"new_name": "child-task"},
        )
        assert code == 409
        assert "no Claude Code session" in resp["error"]

    def test_branch_refuses_when_session_log_missing(self, ilan_server: IlanServer) -> None:
        _seed_parent(ilan_server)
        with patch.object(Runner, "_find_session_log", return_value=None):
            code, resp = _post(
                ilan_server, "/tasks/parent-task/branch",
                {"new_name": "child-task"},
            )
        assert code == 409
        assert "not found on disk" in resp["error"]

    def test_branch_refuses_name_collision(self, ilan_server: IlanServer) -> None:
        _seed_parent(ilan_server)
        ilan_server.store.put_task(Task(
            name="child-task", prompt="p",
            created_at="2026-01-01T00:00:00+00:00",
        ))
        with patch.object(Runner, "_find_session_log", return_value=Path("/fake/sid-1.jsonl")):
            code, resp = _post(
                ilan_server, "/tasks/parent-task/branch",
                {"new_name": "child-task"},
            )
        assert code == 409
        assert "already exists" in resp["error"]

    def test_branch_refuses_invalid_new_name(self, ilan_server: IlanServer) -> None:
        _seed_parent(ilan_server)
        with patch.object(Runner, "_find_session_log", return_value=Path("/fake/sid-1.jsonl")):
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
        with patch.object(Runner, "_find_session_log", return_value=Path("/fake/sid-1.jsonl")):
            code, resp = _post(
                ilan_server, "/tasks/parent-task/branch",
                {"new_name": "child-task"},
            )
        assert code == 409
        assert "Alias pool exhausted" in resp["error"]
        assert ilan_server.store.get_task("child-task") is None

    def test_list_tasks_exposes_parent_name(self, ilan_server: IlanServer) -> None:
        _seed_parent(ilan_server)
        with patch.object(Runner, "_find_session_log", return_value=Path("/fake/sid-1.jsonl")):
            _post(
                ilan_server, "/tasks/parent-task/branch",
                {"new_name": "child-task"},
            )
        url = f"{ilan_server._test_url}/tasks"  # type: ignore[attr-defined]
        with urlopen(Request(url), timeout=5) as r:
            rows = json.loads(r.read())["tasks"]
        by_name = {row["name"]: row for row in rows}
        assert by_name["child-task"]["parent_name"] == "parent-task"
        assert by_name["parent-task"]["parent_name"] is None


# ── list filtering: terminal ancestors kept when descendants are active ─


def _list_default(server: IlanServer) -> list[dict]:
    url = f"{server._test_url}/tasks"  # type: ignore[attr-defined]
    with urlopen(Request(url), timeout=5) as r:
        return json.loads(r.read())["tasks"]


class TestListTerminalAncestors:
    def test_done_middle_kept_when_grandchild_active(self, ilan_server: IlanServer) -> None:
        """A→B→C where B is DONE but C is active: default ls must keep B."""
        parent = Task(
            name="A", prompt="p", session_id="sid-1",
            status=TaskStatus.UNCLAIMED, alias="aa",
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
        assert names == {"A", "B", "C"}

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
            status=TaskStatus.UNCLAIMED,
            created_at="2026-01-01T00:00:00+00:00",
            status_changed_at="2026-01-01T00:00:00+00:00",
        ))
        names = {r["name"] for r in _list_default(ilan_server)}
        assert names == {"active-solo"}


# ── delete refuses active descendants unless force ─────────────────────


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
            name="A", prompt="p", status=TaskStatus.UNCLAIMED, alias="aa",
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

    def test_refuses_when_active_descendant(self, ilan_server: IlanServer) -> None:
        self._seed_chain(ilan_server)
        code, resp = _delete(ilan_server, "/tasks/A")
        assert code == 409
        assert "active descendant" in resp["error"]
        assert "C" in resp["error"]
        # Nothing was removed.
        assert ilan_server.store.get_task("A") is not None

    def test_allows_when_only_terminal_descendants(self, ilan_server: IlanServer) -> None:
        """A→B where both are DONE: deletion allowed without --force."""
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

    def test_force_overrides_refusal(self, ilan_server: IlanServer) -> None:
        self._seed_chain(ilan_server)
        code, resp = _delete(ilan_server, "/tasks/A?force=true")
        assert code == 200
        assert resp["ok"] is True
        assert ilan_server.store.get_task("A") is None
        # Child's parent_name re-parented onto grandparent (None here).
        b = ilan_server.store.get_task("B")
        assert b is not None
        assert b.parent_name is None


# ── CLI: branch -n flag, rm -f, clean skips parents ────────────────────


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

    def test_branch_with_dash_n_invokes_client(self, tmp_config) -> None:
        from click.testing import CliRunner
        from ilan.cli import main
        runner = CliRunner()
        client = _make_cli_client()
        client.branch_task.return_value = {
            "ok": True, "name": "child", "parent_name": "parent",
        }
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["branch", "parent", "-n", "child"])
        assert result.exit_code == 0, result.output
        client.branch_task.assert_called_once_with("parent", "child", None)
        assert "Branched" in result.output

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


def _row_for_rm(name: str, status: str = "DONE", parent: str | None = None) -> dict:
    return {
        "name": name, "status": status, "alias": None,
        "created_at": "2026-01-01T00:00:00+00:00",
        "status_changed_at": "2026-01-01T00:00:00+00:00",
        "needs_review": False, "cost_usd": 0.0, "sleep_seconds": None,
        "parent_name": parent,
    }


class TestRmForceFlag:
    def test_rm_leaf_deletes(self, tmp_config) -> None:
        from click.testing import CliRunner
        from ilan.cli import main
        runner = CliRunner()
        client = _make_cli_client()
        client.get_task.return_value = {"task": {"name": "X"}}
        client.list_tasks.return_value = {"tasks": [_row_for_rm("X")]}
        client.delete_task.return_value = {"ok": True}
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["task", "rm", "X", "-y"])
        assert result.exit_code == 0
        # Batch pre-check cleared, so the server call uses force=True.
        client.delete_task.assert_called_once_with("X", force=True)

    def test_rm_refuses_when_active_descendant_outside_batch(self, tmp_config) -> None:
        from click.testing import CliRunner
        from ilan.cli import main
        runner = CliRunner()
        client = _make_cli_client()
        client.get_task.return_value = {"task": {"name": "A"}}
        client.list_tasks.return_value = {"tasks": [
            _row_for_rm("A", status="DONE"),
            _row_for_rm("C", status="WORKING", parent="A"),
        ]}
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["task", "rm", "A", "-y"])
        assert result.exit_code == 1
        assert "active" in result.output
        assert "C" in result.output
        client.delete_task.assert_not_called()

    def test_rm_allows_parent_and_child_together(self, tmp_config) -> None:
        """Active descendants are fine as long as they're also being deleted."""
        from click.testing import CliRunner
        from ilan.cli import main
        runner = CliRunner()
        client = _make_cli_client()
        # Return canonical names for both lookups.
        client.get_task.side_effect = [
            {"task": {"name": "A"}}, {"task": {"name": "C"}},
        ]
        client.list_tasks.return_value = {"tasks": [
            _row_for_rm("A", status="DONE"),
            _row_for_rm("C", status="WORKING", parent="A"),
        ]}
        client.delete_task.return_value = {"ok": True}
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["task", "rm", "A", "C", "-y"])
        assert result.exit_code == 0, result.output
        deleted = [c.args[0] for c in client.delete_task.call_args_list]
        assert set(deleted) == {"A", "C"}

    def test_rm_refuses_when_grandchild_active_outside_batch(self, tmp_config) -> None:
        """A→B→C; deleting {A, B} leaves C active → refuse."""
        from click.testing import CliRunner
        from ilan.cli import main
        runner = CliRunner()
        client = _make_cli_client()
        client.get_task.side_effect = [
            {"task": {"name": "A"}}, {"task": {"name": "B"}},
        ]
        client.list_tasks.return_value = {"tasks": [
            _row_for_rm("A", status="DONE"),
            _row_for_rm("B", status="DONE", parent="A"),
            _row_for_rm("C", status="WORKING", parent="B"),
        ]}
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["task", "rm", "A", "B", "-y"])
        assert result.exit_code == 1
        assert "C" in result.output
        client.delete_task.assert_not_called()

    def test_rm_force_skips_client_check(self, tmp_config) -> None:
        from click.testing import CliRunner
        from ilan.cli import main
        runner = CliRunner()
        client = _make_cli_client()
        client.get_task.return_value = {"task": {"name": "A"}}
        client.delete_task.return_value = {"ok": True}
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["task", "rm", "A", "-y", "-f"])
        assert result.exit_code == 0
        # With -f, we must not bother list_tasks for a pre-check.
        client.list_tasks.assert_not_called()
        client.delete_task.assert_called_once_with("A", force=True)


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
