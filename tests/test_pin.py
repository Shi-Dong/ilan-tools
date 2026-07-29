"""Tests for ``ilan task pin`` / ``unpin`` — model, server endpoints, and CLI."""

from __future__ import annotations

import json
import re
import signal
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest
from click.testing import CliRunner
from rich.console import Console

import ilan.cli as cli_mod
from ilan.cli import _build_name_cell, main
from ilan.models import Task, TaskStatus
from ilan.server import IlanServer
from ilan.store import Store


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _strip_ansi(s: str) -> str:
    return _ANSI_RE.sub("", s)


# ── model / store ───────────────────────────────────────────────────────


class TestPinnedField:
    def test_defaults_to_false_and_round_trips(self, tmp_workdir: Path) -> None:
        store = Store(tmp_workdir)
        store.put_task(Task(name="plain", prompt="p"))
        store.put_task(Task(name="stuck", prompt="p", pinned=True))

        tasks = store.load_tasks()
        assert tasks["plain"].pinned is False
        assert tasks["stuck"].pinned is True

    def test_from_dict_without_pinned_defaults_false(self) -> None:
        # Tasks saved before this field existed must still load.
        t = Task.from_dict({"name": "old", "prompt": "p", "status": "WORKING"})
        assert t.pinned is False

    def test_to_dict_carries_pinned(self) -> None:
        assert Task(name="t", prompt="p", pinned=True).to_dict()["pinned"] is True

    def test_branched_child_is_not_pinned(self, tmp_workdir: Path) -> None:
        """Pinning a parent shouldn't silently pin everything branched off it."""
        store = Store(tmp_workdir)
        parent = Task(name="parent", prompt="p", pinned=True)
        store.put_task(parent)
        child = store.branch_task(
            parent, "child",
            alias="cc", task_hash="1111aaaa", now="2026-01-01T00:00:00+00:00",
        )
        assert child.pinned is False


# ── server endpoints ────────────────────────────────────────────────────


@pytest.fixture()
def ilan_server(tmp_workdir: Path, tmp_config: Path, env_with_mock_claude: None):
    import ilan.config as cfg_mod

    cfg_mod.save({**cfg_mod.DEFAULTS, "workdir": str(tmp_workdir)})

    server = IlanServer()
    server.runner.start = lambda task: True  # type: ignore[method-assign]
    server.runner.reap_finished = lambda: None  # type: ignore[method-assign]

    with patch.object(signal, "signal"):
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
        server._test_url = f"http://127.0.0.1:{port}"  # type: ignore[attr-defined]

        yield server

        server.shutdown()
        t.join(timeout=3)


def _post(server: IlanServer, path: str) -> tuple[int, dict]:
    req = Request(f"{server._test_url}{path}", method="POST")  # type: ignore[attr-defined]
    try:
        with urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read())
    except HTTPError as exc:
        return exc.code, json.loads(exc.read())


def _list(server: IlanServer, *, show_all: bool = False) -> list[dict]:
    url = f"{server._test_url}/tasks" + ("?all=true" if show_all else "")  # type: ignore[attr-defined]
    with urlopen(url, timeout=5) as resp:
        return json.loads(resp.read())["tasks"]


def _seed(
    server: IlanServer,
    name: str,
    *,
    hour: int,
    alias: str | None = None,
    pinned: bool = False,
    status: TaskStatus = TaskStatus.WORKING,
) -> None:
    ts = f"2026-07-29T{hour:02d}:00:00+00:00"
    server.store.put_task(Task(
        name=name, prompt="p", status=status, created_at=ts, status_changed_at=ts,
        alias=alias, pinned=pinned,
    ))


class TestPinEndpoints:
    def test_pin_sets_the_flag(self, ilan_server: IlanServer) -> None:
        _seed(ilan_server, "alpha", hour=0)
        code, body = _post(ilan_server, "/tasks/alpha/pin")
        assert code == 200
        assert body == {"ok": True, "name": "alpha", "pinned": True}
        assert ilan_server.store.get_task("alpha").pinned is True

    def test_pin_is_idempotent(self, ilan_server: IlanServer) -> None:
        _seed(ilan_server, "alpha", hour=0)
        _post(ilan_server, "/tasks/alpha/pin")
        code, body = _post(ilan_server, "/tasks/alpha/pin")
        assert code == 200
        assert body["pinned"] is True
        assert ilan_server.store.get_task("alpha").pinned is True

    def test_unpin_clears_the_flag(self, ilan_server: IlanServer) -> None:
        _seed(ilan_server, "alpha", hour=0, pinned=True)
        code, body = _post(ilan_server, "/tasks/alpha/unpin")
        assert code == 200
        assert body == {"ok": True, "name": "alpha", "pinned": False}
        assert ilan_server.store.get_task("alpha").pinned is False

    def test_unpin_an_unpinned_task_is_fine(self, ilan_server: IlanServer) -> None:
        _seed(ilan_server, "alpha", hour=0)
        code, body = _post(ilan_server, "/tasks/alpha/unpin")
        assert code == 200
        assert body["pinned"] is False

    def test_pin_accepts_an_alias(self, ilan_server: IlanServer) -> None:
        _seed(ilan_server, "alpha", hour=0, alias="aa")
        code, body = _post(ilan_server, "/tasks/aa/pin")
        assert code == 200
        assert body["name"] == "alpha"
        assert ilan_server.store.get_task("alpha").pinned is True

    def test_pin_unknown_task_is_404(self, ilan_server: IlanServer) -> None:
        code, body = _post(ilan_server, "/tasks/nope/pin")
        assert code == 404
        assert "not found" in body["error"]


class TestListOrdering:
    def test_pinned_float_to_the_top(self, ilan_server: IlanServer) -> None:
        _seed(ilan_server, "first", hour=0)
        _seed(ilan_server, "second", hour=1)
        _seed(ilan_server, "third", hour=2)

        _post(ilan_server, "/tasks/third/pin")

        assert [r["name"] for r in _list(ilan_server)] == ["third", "first", "second"]

    def test_pinned_are_ordered_by_created_at(self, ilan_server: IlanServer) -> None:
        _seed(ilan_server, "oldest", hour=0)
        _seed(ilan_server, "middle", hour=1)
        _seed(ilan_server, "newest", hour=2)

        # Pinned newest-first; the listing must still put the oldest pin on top.
        _post(ilan_server, "/tasks/newest/pin")
        _post(ilan_server, "/tasks/middle/pin")

        assert [r["name"] for r in _list(ilan_server)] == [
            "middle", "newest", "oldest",
        ]

    def test_unpinning_restores_creation_order(self, ilan_server: IlanServer) -> None:
        _seed(ilan_server, "first", hour=0)
        _seed(ilan_server, "second", hour=1)

        _post(ilan_server, "/tasks/second/pin")
        _post(ilan_server, "/tasks/second/unpin")

        assert [r["name"] for r in _list(ilan_server)] == ["first", "second"]

    def test_rows_carry_the_pinned_flag(self, ilan_server: IlanServer) -> None:
        _seed(ilan_server, "first", hour=0)
        _seed(ilan_server, "second", hour=1, pinned=True)
        rows = {r["name"]: r["pinned"] for r in _list(ilan_server)}
        assert rows == {"first": False, "second": True}

    @pytest.mark.parametrize("status", [TaskStatus.DONE, TaskStatus.DISCARDED])
    def test_a_pinned_terminal_task_shows_without_all(
        self, ilan_server: IlanServer, status: TaskStatus,
    ) -> None:
        """A pin overrides the default terminal-status filter."""
        _seed(ilan_server, "active", hour=0)
        _seed(ilan_server, "finished", hour=1, pinned=True, status=status)

        assert [r["name"] for r in _list(ilan_server)] == ["finished", "active"]

    @pytest.mark.parametrize("status", [TaskStatus.DONE, TaskStatus.DISCARDED])
    def test_an_unpinned_terminal_task_still_needs_all(
        self, ilan_server: IlanServer, status: TaskStatus,
    ) -> None:
        """Only a pin lifts the filter; terminal tasks are otherwise hidden."""
        _seed(ilan_server, "active", hour=0)
        _seed(ilan_server, "finished", hour=1, status=status)

        assert [r["name"] for r in _list(ilan_server)] == ["active"]
        assert [r["name"] for r in _list(ilan_server, show_all=True)] == [
            "active", "finished",
        ]

    def test_unpinning_a_terminal_task_hides_it_again(
        self, ilan_server: IlanServer,
    ) -> None:
        _seed(ilan_server, "active", hour=0)
        _seed(ilan_server, "finished", hour=1, status=TaskStatus.DONE)

        _post(ilan_server, "/tasks/finished/pin")
        assert [r["name"] for r in _list(ilan_server)] == ["finished", "active"]

        _post(ilan_server, "/tasks/finished/unpin")
        assert [r["name"] for r in _list(ilan_server)] == ["active"]
        # Still reachable with -a, exactly as before it was ever pinned.
        assert "finished" in [r["name"] for r in _list(ilan_server, show_all=True)]


# ── CLI ─────────────────────────────────────────────────────────────────


@pytest.fixture()
def runner() -> CliRunner:
    return CliRunner()


def _row(name: str, *, pinned: bool = False, alias: str | None = None) -> dict:
    return {
        "name": name,
        "alias": alias,
        "status": "WORKING",
        "created_at": "2026-07-29T00:00:00+00:00",
        "status_changed_at": "2026-07-29T00:00:00+00:00",
        "needs_review": False,
        "pinned": pinned,
    }


class TestNameCellMarker:
    def test_pinned_row_gets_the_marker(self) -> None:
        assert _build_name_cell(_row("alpha", pinned=True)).plain == "* alpha"

    def test_marker_precedes_the_alias(self) -> None:
        cell = _build_name_cell(_row("alpha", pinned=True, alias="aa"))
        assert cell.plain == "* (aa) alpha"

    def test_unpinned_row_has_no_marker(self) -> None:
        assert _build_name_cell(_row("alpha", alias="aa")).plain == "(aa) alpha"

    def test_row_without_the_field_has_no_marker(self) -> None:
        # A newer client talking to an older server still renders.
        row = _row("alpha")
        del row["pinned"]
        assert _build_name_cell(row).plain == "alpha"


class TestPinCommands:
    def test_task_pin_reports_success(self, runner: CliRunner, tmp_config) -> None:
        client = MagicMock()
        client.pin_task.return_value = {"ok": True, "name": "alpha", "pinned": True}
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["task", "pin", "alpha"])
        assert result.exit_code == 0
        client.pin_task.assert_called_once_with("alpha")
        assert "pinned" in _strip_ansi(result.output)

    def test_task_unpin_reports_success(self, runner: CliRunner, tmp_config) -> None:
        client = MagicMock()
        client.unpin_task.return_value = {"ok": True, "name": "alpha", "pinned": False}
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["task", "unpin", "alpha"])
        assert result.exit_code == 0
        client.unpin_task.assert_called_once_with("alpha")
        assert "unpinned" in _strip_ansi(result.output)

    @pytest.mark.parametrize("args", [["pin", "aa"], ["task", "pin", "aa"]])
    def test_shorthand_and_subcommand_hit_the_same_endpoint(
        self, runner: CliRunner, tmp_config, args: list[str],
    ) -> None:
        client = MagicMock()
        client.pin_task.return_value = {"ok": True, "name": "alpha", "pinned": True}
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, args)
        assert result.exit_code == 0
        client.pin_task.assert_called_once_with("aa")

    @pytest.mark.parametrize("args", [["unpin", "aa"], ["task", "unpin", "aa"]])
    def test_unpin_shorthand_and_subcommand_match(
        self, runner: CliRunner, tmp_config, args: list[str],
    ) -> None:
        client = MagicMock()
        client.unpin_task.return_value = {"ok": True, "name": "alpha", "pinned": False}
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, args)
        assert result.exit_code == 0
        client.unpin_task.assert_called_once_with("aa")

    def test_pin_error_exits_nonzero(self, runner: CliRunner, tmp_config) -> None:
        client = MagicMock()
        client.pin_task.return_value = {"error": "Task nope not found"}
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["pin", "nope"])
        assert result.exit_code == 1
        assert "not found" in _strip_ansi(result.output)


class TestLsRendersMarker:
    def test_ls_shows_the_marker_on_pinned_rows_only(
        self, runner: CliRunner, tmp_config, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(cli_mod, "console", Console(width=200, force_terminal=False))
        client = MagicMock()
        client.ensure_server.return_value = {}
        client.version_mismatch = None
        client.is_remote = False
        # The server sends pinned rows first; the client keeps that order.
        client.list_tasks.return_value = {
            "tasks": [_row("pinned-task", pinned=True), _row("plain-task")],
        }
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["ls"])
        assert result.exit_code == 0
        out = _strip_ansi(result.output)
        assert out.index("pinned-task") < out.index("plain-task")
        pinned_line = next(l for l in out.splitlines() if "pinned-task" in l)
        plain_line = next(l for l in out.splitlines() if "plain-task" in l)
        assert "* pinned-task" in pinned_line
        assert "* plain-task" not in plain_line
