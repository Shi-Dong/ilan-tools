"""Tests for ``ilan clean`` — delete tasks older than a given duration."""

from __future__ import annotations

import json
import signal
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import click
import pytest

from ilan.cli import _parse_duration
from ilan.models import TaskStatus
from ilan.server import IlanServer


# ── _parse_duration unit tests ─────────────────────────────────────────


class TestParseDuration:
    def test_hours(self) -> None:
        assert _parse_duration("5h") == timedelta(hours=5)

    def test_days(self) -> None:
        assert _parse_duration("3d") == timedelta(days=3)

    def test_uppercase(self) -> None:
        assert _parse_duration("2H") == timedelta(hours=2)
        assert _parse_duration("7D") == timedelta(days=7)

    def test_whitespace(self) -> None:
        assert _parse_duration("  12h  ") == timedelta(hours=12)

    def test_invalid_unit(self) -> None:
        with pytest.raises(click.BadParameter):
            _parse_duration("5m")

    def test_no_number(self) -> None:
        with pytest.raises(click.BadParameter):
            _parse_duration("h")

    def test_empty(self) -> None:
        with pytest.raises(click.BadParameter):
            _parse_duration("")

    def test_negative(self) -> None:
        with pytest.raises(click.BadParameter):
            _parse_duration("-3h")


# ── integration tests via server ───────────────────────────────────────


@pytest.fixture()
def ilan_server(tmp_workdir: Path, tmp_config: Path, env_with_mock_claude: None):
    """Start an IlanServer on an ephemeral port for testing."""
    import ilan.config as cfg_mod

    cfg_mod.save({**cfg_mod.DEFAULTS, "workdir": str(tmp_workdir)})

    server = IlanServer()
    server.runner.schedule = lambda: None

    with patch.object(signal, "signal"):
        t = threading.Thread(target=server.run, kwargs={"host": "127.0.0.1", "port": 0}, daemon=True)
        t.start()

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


def _make_task_old(server: IlanServer, name: str, hours_ago: int) -> None:
    """Backdate a task's status_changed_at to simulate an old task."""
    with server.lock:
        task = server.store.get_task(name)
        old_ts = (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()
        task.status_changed_at = old_ts
        task.created_at = old_ts
        server.store.put_task(task)


class TestCleanIntegration:
    def test_clean_deletes_old_tasks(self, ilan_server: IlanServer) -> None:
        # Create tasks: one old, one fresh
        _post(ilan_server, "/tasks", {"name": "old-task", "prompt": "P"})
        _post(ilan_server, "/tasks", {"name": "fresh-task", "prompt": "P"})

        _make_task_old(ilan_server, "old-task", hours_ago=10)

        # Verify both exist
        resp = _get(ilan_server, "/tasks?all=true")
        names = [t["name"] for t in resp["tasks"]]
        assert "old-task" in names
        assert "fresh-task" in names

        # List tasks and check which ones would be cleaned (older than 5h)
        resp = _get(ilan_server, "/tasks?all=true")
        cutoff = datetime.now(timezone.utc) - timedelta(hours=5)
        stale = []
        for t in resp["tasks"]:
            changed = t.get("status_changed_at") or t.get("created_at", "")
            if changed and datetime.fromisoformat(changed) < cutoff:
                stale.append(t["name"])

        assert "old-task" in stale
        assert "fresh-task" not in stale

        # Delete the stale tasks
        for name in stale:
            _delete(ilan_server, f"/tasks/{name}")

        # Verify old-task is gone, fresh-task remains
        resp = _get(ilan_server, "/tasks?all=true")
        names = [t["name"] for t in resp["tasks"]]
        assert "old-task" not in names
        assert "fresh-task" in names

    def test_clean_nothing_to_delete(self, ilan_server: IlanServer) -> None:
        _post(ilan_server, "/tasks", {"name": "new-task", "prompt": "P"})

        resp = _get(ilan_server, "/tasks?all=true")
        cutoff = datetime.now(timezone.utc) - timedelta(hours=5)
        stale = []
        for t in resp["tasks"]:
            changed = t.get("status_changed_at") or t.get("created_at", "")
            if changed and datetime.fromisoformat(changed) < cutoff:
                stale.append(t["name"])

        assert stale == []

    def test_clean_respects_days(self, ilan_server: IlanServer) -> None:
        _post(ilan_server, "/tasks", {"name": "day-old", "prompt": "P"})
        _make_task_old(ilan_server, "day-old", hours_ago=48)

        resp = _get(ilan_server, "/tasks?all=true")
        cutoff = datetime.now(timezone.utc) - timedelta(days=1)
        stale = [
            t["name"] for t in resp["tasks"]
            if datetime.fromisoformat(t.get("status_changed_at") or t["created_at"]) < cutoff
        ]

        assert "day-old" in stale

    def test_clean_includes_terminal_tasks(self, ilan_server: IlanServer) -> None:
        """Clean should also catch DONE/DISCARDED tasks."""
        _post(ilan_server, "/tasks", {"name": "done-old", "prompt": "P"})
        _post(ilan_server, "/tasks/done-old/done")
        _make_task_old(ilan_server, "done-old", hours_ago=10)

        resp = _get(ilan_server, "/tasks?all=true")
        cutoff = datetime.now(timezone.utc) - timedelta(hours=5)
        stale = [
            t["name"] for t in resp["tasks"]
            if datetime.fromisoformat(t.get("status_changed_at") or t["created_at"]) < cutoff
        ]

        assert "done-old" in stale
