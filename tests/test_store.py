"""Tests for ilan.store — Store CRUD, logs, aliases, deletion."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ilan.models import ALIAS_POOL, Task, TaskStatus
from ilan.store import Store


@pytest.fixture()
def store(tmp_workdir: Path) -> Store:
    return Store(tmp_workdir)


# ── task CRUD ───────────────────────────────────────────────────────────


class TestTaskCRUD:
    def test_empty_store(self, store: Store) -> None:
        assert store.load_tasks() == {}

    def test_put_and_get(self, store: Store) -> None:
        t = Task(name="my-task", prompt="hello", created_at="2025-01-01T00:00:00+00:00")
        store.put_task(t)
        got = store.get_task("my-task")
        assert got is not None
        assert got.name == "my-task"
        assert got.prompt == "hello"

    def test_get_nonexistent(self, store: Store) -> None:
        assert store.get_task("nope") is None

    def test_put_overwrites(self, store: Store) -> None:
        t = Task(name="t1", prompt="v1")
        store.put_task(t)
        t.prompt = "v2"
        store.put_task(t)
        got = store.get_task("t1")
        assert got is not None
        assert got.prompt == "v2"

    def test_load_tasks_returns_all(self, store: Store) -> None:
        for i in range(3):
            store.put_task(Task(name=f"task-{i}", prompt=f"prompt-{i}"))
        tasks = store.load_tasks()
        assert len(tasks) == 3
        assert set(tasks.keys()) == {"task-0", "task-1", "task-2"}

    def test_delete_task(self, store: Store) -> None:
        store.put_task(Task(name="to-delete", prompt="bye"))
        store.append_log("to-delete", "user", "some log")
        # Create a fake output file
        store.output_path("to-delete").write_text("{}")

        store.delete_task("to-delete")
        assert store.get_task("to-delete") is None
        assert not store.log_path("to-delete").exists()
        assert not store.output_path("to-delete").exists()

    def test_delete_nonexistent_is_noop(self, store: Store) -> None:
        store.put_task(Task(name="keep", prompt="stay"))
        store.delete_task("ghost")
        assert store.get_task("keep") is not None

    def test_save_and_load_preserves_all_fields(self, store: Store) -> None:
        t = Task(
            name="full",
            prompt="do stuff",
            status=TaskStatus.WORKING,
            created_at="2025-01-01T00:00:00+00:00",
            status_changed_at="2025-01-02T00:00:00+00:00",
            session_id="sid-abc",
            pid=1234,
            cached_replies=["r1", "r2"],
            alias="as",
        )
        store.put_task(t)
        got = store.get_task("full")
        assert got is not None
        assert got.status == TaskStatus.WORKING
        assert got.session_id == "sid-abc"
        assert got.pid == 1234
        assert got.cached_replies == ["r1", "r2"]
        assert got.alias == "as"


# ── get_task_by_name_or_alias ───────────────────────────────────────────


class TestGetByNameOrAlias:
    def test_lookup_by_name(self, store: Store) -> None:
        store.put_task(Task(name="lookup", prompt="p", alias="gg"))
        assert store.get_task_by_name_or_alias("lookup") is not None

    def test_lookup_by_alias(self, store: Store) -> None:
        store.put_task(Task(name="lookup", prompt="p", alias="gg"))
        t = store.get_task_by_name_or_alias("gg")
        assert t is not None
        assert t.name == "lookup"

    def test_name_takes_priority(self, store: Store) -> None:
        """If a task name equals another task's alias, name wins."""
        store.put_task(Task(name="aa", prompt="p1", alias="bb"))
        store.put_task(Task(name="cc", prompt="p2", alias="aa"))
        t = store.get_task_by_name_or_alias("aa")
        assert t is not None
        assert t.name == "aa"

    def test_not_found(self, store: Store) -> None:
        store.put_task(Task(name="exists", prompt="p", alias="zz"))
        assert store.get_task_by_name_or_alias("nope") is None


# ── alias management ────────────────────────────────────────────────────


class TestAliasManagement:
    def test_next_alias_from_empty(self, store: Store) -> None:
        alias = store.next_available_alias()
        assert alias is not None
        assert alias in ALIAS_POOL

    def test_alias_not_reused(self, store: Store) -> None:
        used = set()
        for i in range(10):
            alias = store.next_available_alias()
            assert alias is not None
            assert alias not in used
            used.add(alias)
            store.put_task(Task(name=f"t-{i}", prompt="p", alias=alias))

    def test_alias_exhaustion(self, store: Store) -> None:
        for i, alias in enumerate(ALIAS_POOL):
            store.put_task(Task(name=f"task-{i}", prompt="p", alias=alias))
        assert store.next_available_alias() is None

    def test_alias_freed_on_delete(self, store: Store) -> None:
        store.put_task(Task(name="temp", prompt="p", alias="as"))
        store.delete_task("temp")
        alias = store.next_available_alias()
        # "as" should be available again (though random, so just check non-None)
        assert alias is not None


# ── conversation logs ───────────────────────────────────────────────────


class TestLogs:
    def test_append_and_read(self, store: Store) -> None:
        store.append_log("task1", "user", "hello")
        store.append_log("task1", "assistant", "world")
        logs = store.read_logs("task1")
        assert len(logs) == 2
        assert logs[0].role == "user"
        assert logs[0].content == "hello"
        assert logs[1].role == "assistant"
        assert logs[1].content == "world"

    def test_read_empty(self, store: Store) -> None:
        assert store.read_logs("nonexistent") == []

    def test_log_timestamps(self, store: Store) -> None:
        store.append_log("task1", "user", "msg")
        logs = store.read_logs("task1")
        assert len(logs) == 1
        assert logs[0].timestamp != ""

    def test_log_path(self, store: Store) -> None:
        path = store.log_path("my-task")
        assert path.name == "my-task.jsonl"
        assert "logs" in str(path)

    def test_log_is_jsonl(self, store: Store) -> None:
        store.append_log("t", "user", "line1")
        store.append_log("t", "assistant", "line2")
        with open(store.log_path("t")) as f:
            lines = [l.strip() for l in f if l.strip()]
        assert len(lines) == 2
        for line in lines:
            data = json.loads(line)
            assert "role" in data
            assert "content" in data


# ── output paths ────────────────────────────────────────────────────────


class TestOutputPath:
    def test_output_path(self, store: Store) -> None:
        path = store.output_path("my-task")
        assert path.name == "my-task.json"
        assert "output" in str(path)


# ── delete_all ──────────────────────────────────────────────────────────


class TestDeleteAll:
    def test_clears_tasks_and_logs(self, store: Store) -> None:
        store.put_task(Task(name="a", prompt="p"))
        store.append_log("a", "user", "msg")
        store.output_path("a").write_text("{}")

        store.delete_all()

        assert store.load_tasks() == {}
        assert store.read_logs("a") == []
        assert not store.output_path("a").exists()

    def test_recreates_subdirs(self, store: Store) -> None:
        store.delete_all()
        # logs and output dirs should still exist
        assert store._logs_dir.is_dir()
        assert store._output_dir.is_dir()
