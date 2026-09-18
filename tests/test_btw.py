"""Parent-derived side-question names are reserved while holding the server lock."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import pytest

from ilan.client import Client
from ilan.models import Task
from ilan.server import IlanServer
from ilan.store import Store
from tests.helpers import post_json


@pytest.mark.parametrize("parent_name", ["parent-task", "Review_bug-42", "xxx-original-task"])
@pytest.mark.parametrize("suffixes, expected", [
    ([], ""), ([""], "-2"), (["", "-2"], "-3"),
    (["-2"], ""), (["", "-3"], "-2"),
])
def test_next_free_parent_name(
    tmp_workdir: Path, parent_name: str, suffixes: list[str], expected: str,
) -> None:
    store = Store(tmp_workdir)
    base = f"xxx-{parent_name}-btw"
    for suffix in suffixes:
        store.put_task(Task(
            name=base + suffix, prompt="An existing task",
            created_at="2026-01-01T00:00:00+00:00",
            status_changed_at="2026-01-01T00:00:00+00:00",
        ))
    before = store.load_tasks()
    assert store.next_available_btw_name(parent_name) == base + expected
    assert store.load_tasks() == before


@pytest.fixture()
def parent(ilan_server: IlanServer) -> Task:
    parent = Task(
        name="parent-task", prompt="Watch the job",
        created_at="2026-01-01T00:00:00+00:00",
        status_changed_at="2026-01-01T00:00:00+00:00",
        engine="codex", session_id="parent-session",
        sessions={"codex": "parent-session"}, alias="aa",
    )
    with ilan_server.lock:
        ilan_server.store.put_task(parent)
        ilan_server.store.append_log(parent.name, "user", parent.prompt)
    return parent


def test_concurrent_requests_get_distinct_names(
    ilan_server: IlanServer, parent: Task,
) -> None:
    def ask(number: int) -> dict:
        client = Client(base_url=ilan_server._test_url)
        return client.btw_task("aa", f"Question {number}")

    with patch.object(
        ilan_server.runner, "find_session_log", return_value=Path("/fake/session.jsonl"),
    ), ThreadPoolExecutor(max_workers=3) as pool:
        responses = list(pool.map(ask, range(3)))
    names = [response["name"] for response in responses]
    assert set(names) == {
        "xxx-parent-task-btw", "xxx-parent-task-btw-2", "xxx-parent-task-btw-3",
    }
    with ilan_server.lock:
        assert ilan_server.store.get_task(parent.name) == parent
        for number, name in enumerate(names):
            child = ilan_server.store.get_task(name)
            assert child is not None
            assert child.parent_name == parent.name
            assert child.cached_replies == [f"Question {number}"]
            assert ilan_server.store.read_logs(name)[-1].content == f"Question {number}"


@pytest.mark.parametrize("extra", [{"new_name": "custom"}, {"new_name": None}, {"other": True}])
def test_btw_endpoint_rejects_extra_fields(
    ilan_server: IlanServer, parent: Task, extra: dict,
) -> None:
    response = post_json(ilan_server, "/tasks/aa/btw", {"message": "Why?", **extra})
    assert "only a message" in response["error"]
    with ilan_server.lock:
        assert list(ilan_server.store.load_tasks()) == [parent.name]


@pytest.mark.parametrize("message", [None, "", " \n", 42, [], {}])
def test_btw_endpoint_requires_a_nonempty_string(
    ilan_server: IlanServer, parent: Task, message: object,
) -> None:
    response = post_json(ilan_server, "/tasks/aa/btw", {"message": message})
    assert "first assignment" in response["error"]
    with ilan_server.lock:
        assert list(ilan_server.store.load_tasks()) == [parent.name]


def test_refused_branch_does_not_allocate_a_name(
    ilan_server: IlanServer, parent: Task,
) -> None:
    with patch.object(ilan_server.runner, "find_session_log", return_value=None), \
         patch.object(ilan_server.store, "next_available_btw_name") as name:
        response = post_json(ilan_server, "/tasks/aa/btw", {"message": "Why?"})
    assert "not found on disk" in response["error"]
    name.assert_not_called()
    with ilan_server.lock:
        assert list(ilan_server.store.load_tasks()) == [parent.name]
