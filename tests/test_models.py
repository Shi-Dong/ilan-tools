"""Tests for ilan.models — Task, LogEntry, TaskStatus, alias pool."""

from __future__ import annotations

from datetime import datetime

from ilan.models import ALIAS_POOL, LogEntry, Task, TaskStatus


# ── TaskStatus ──────────────────────────────────────────────────────────


class TestTaskStatus:
    def test_terminal_states(self) -> None:
        assert TaskStatus.DONE.is_terminal
        assert TaskStatus.DISCARDED.is_terminal

    def test_non_terminal_states(self) -> None:
        for status in (
            TaskStatus.UNCLAIMED,
            TaskStatus.WORKING,
            TaskStatus.NEEDS_ATTENTION,
            TaskStatus.AGENT_FINISHED,
            TaskStatus.ERROR,
        ):
            assert not status.is_terminal

    def test_claimable(self) -> None:
        assert TaskStatus.UNCLAIMED.is_claimable
        for status in (
            TaskStatus.WORKING,
            TaskStatus.NEEDS_ATTENTION,
            TaskStatus.AGENT_FINISHED,
            TaskStatus.DONE,
            TaskStatus.DISCARDED,
            TaskStatus.ERROR,
        ):
            assert not status.is_claimable

    def test_string_value_roundtrip(self) -> None:
        for status in TaskStatus:
            assert TaskStatus(status.value) is status

    def test_is_str_subclass(self) -> None:
        assert isinstance(TaskStatus.DONE, str)
        assert TaskStatus.DONE == "DONE"


# ── ALIAS_POOL ──────────────────────────────────────────────────────────


class TestAliasPool:
    def test_length(self) -> None:
        assert len(ALIAS_POOL) == 9 * 9 - 1  # 81 combos minus 1 banned ("ls")

    def test_all_unique(self) -> None:
        assert len(set(ALIAS_POOL)) == len(ALIAS_POOL)

    def test_all_two_chars(self) -> None:
        for alias in ALIAS_POOL:
            assert len(alias) == 2

    def test_valid_chars(self) -> None:
        valid = set("asdfghjkl")
        for alias in ALIAS_POOL:
            assert set(alias) <= valid


# ── Task ────────────────────────────────────────────────────────────────


class TestTask:
    def _make_task(self, **overrides) -> Task:
        defaults = {
            "name": "test-task",
            "prompt": "Do something",
            "status": TaskStatus.UNCLAIMED,
            "created_at": "2025-01-01T00:00:00+00:00",
            "status_changed_at": "2025-01-01T00:00:00+00:00",
        }
        defaults.update(overrides)
        return Task(**defaults)

    def test_default_fields(self) -> None:
        t = Task(name="x", prompt="y")
        assert t.status == TaskStatus.UNCLAIMED
        assert t.session_id is None
        assert t.pid is None
        assert t.cached_replies == []
        assert t.alias is None

    def test_set_status_updates_timestamp(self) -> None:
        t = self._make_task()
        old_ts = t.status_changed_at
        t.set_status(TaskStatus.WORKING)
        assert t.status == TaskStatus.WORKING
        assert t.status_changed_at != old_ts
        # Should be a valid ISO timestamp
        dt = datetime.fromisoformat(t.status_changed_at)
        assert dt.tzinfo is not None

    def test_to_dict_roundtrip(self) -> None:
        t = self._make_task(
            session_id="sid-123",
            pid=42,
            cached_replies=["reply1"],
            alias="as",
        )
        d = t.to_dict()
        t2 = Task.from_dict(d)
        assert t2.name == t.name
        assert t2.prompt == t.prompt
        assert t2.status == t.status
        assert t2.created_at == t.created_at
        assert t2.status_changed_at == t.status_changed_at
        assert t2.session_id == t.session_id
        assert t2.pid == t.pid
        assert t2.cached_replies == t.cached_replies
        assert t2.alias == t.alias

    def test_to_dict_keys(self) -> None:
        t = self._make_task()
        d = t.to_dict()
        expected_keys = {
            "name", "prompt", "status", "created_at", "status_changed_at",
            "session_id", "session_log_path", "pid", "cached_replies", "alias",
        }
        assert set(d.keys()) == expected_keys

    def test_from_dict_with_missing_optional_fields(self) -> None:
        """Backward compatibility: old dicts may lack newer fields."""
        d = {"name": "old", "prompt": "p", "status": "UNCLAIMED"}
        t = Task.from_dict(d)
        assert t.name == "old"
        assert t.status == TaskStatus.UNCLAIMED
        assert t.created_at == ""
        assert t.session_id is None
        assert t.cached_replies == []
        assert t.alias is None

    def test_from_dict_status_changed_at_fallback(self) -> None:
        """status_changed_at falls back to created_at if missing."""
        d = {
            "name": "x",
            "prompt": "p",
            "status": "WORKING",
            "created_at": "2025-06-01T00:00:00+00:00",
        }
        t = Task.from_dict(d)
        assert t.status_changed_at == "2025-06-01T00:00:00+00:00"

    def test_status_serialized_as_string(self) -> None:
        t = self._make_task(status=TaskStatus.NEEDS_ATTENTION)
        d = t.to_dict()
        assert d["status"] == "NEEDS_ATTENTION"
        assert isinstance(d["status"], str)


# ── LogEntry ────────────────────────────────────────────────────────────


class TestLogEntry:
    def test_to_dict(self) -> None:
        e = LogEntry(role="user", content="hello", timestamp="2025-01-01T00:00:00+00:00")
        d = e.to_dict()
        assert d == {"role": "user", "content": "hello", "timestamp": "2025-01-01T00:00:00+00:00"}

    def test_from_dict(self) -> None:
        d = {"role": "assistant", "content": "world", "timestamp": "ts1"}
        e = LogEntry.from_dict(d)
        assert e.role == "assistant"
        assert e.content == "world"
        assert e.timestamp == "ts1"

    def test_from_dict_missing_timestamp(self) -> None:
        d = {"role": "user", "content": "hi"}
        e = LogEntry.from_dict(d)
        assert e.timestamp == ""

    def test_now_factory(self) -> None:
        e = LogEntry.now("user", "test content")
        assert e.role == "user"
        assert e.content == "test content"
        dt = datetime.fromisoformat(e.timestamp)
        assert dt.tzinfo is not None

    def test_roundtrip(self) -> None:
        e = LogEntry.now("assistant", "response text")
        d = e.to_dict()
        e2 = LogEntry.from_dict(d)
        assert e2.role == e.role
        assert e2.content == e.content
        assert e2.timestamp == e.timestamp
