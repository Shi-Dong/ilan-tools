"""Tests for ilan.models — Task, LogEntry, TaskStatus, alias pool."""

from __future__ import annotations

from datetime import datetime

import pytest
from rich.style import Style

from ilan.models import (
    AGENT_IN_LOOP_LABEL,
    AGENT_IN_LOOP_STYLE,
    ALIAS_POOL,
    API,
    ENGINE_CLAUDE,
    ENGINE_CODEX,
    ASTRA_MODEL,
    ENGINE_NAME_STYLE,
    FABLE_MODEL,
    LEGACY_ASTRA_MODELS,
    LEGACY_FABLE_MODELS,
    MAX_MODELS,
    STYLE_FOR_STATUS,
    VALID_ENGINES,
    LogEntry,
    Task,
    TaskStatus,
    display_status,
    foreign_max_model,
    format_cost_usd,
    generate_task_hash,
    max_model_for,
    max_tag,
    other_engine,
    parse_task_number,
    tag_for_max_model,
    validate_task_name,
)


# ── TaskStatus ──────────────────────────────────────────────────────────


class TestTaskStatus:
    def test_terminal_states(self) -> None:
        assert TaskStatus.DONE.is_terminal
        assert TaskStatus.DISCARDED.is_terminal

    def test_non_terminal_states(self) -> None:
        for status in (
            TaskStatus.WORKING,
            TaskStatus.NEEDS_ATTENTION,
            TaskStatus.AGENT_FINISHED,
            TaskStatus.ERROR,
        ):
            assert not status.is_terminal

    def test_string_value_roundtrip(self) -> None:
        for status in TaskStatus:
            assert TaskStatus(status.value) is status

    def test_is_str_subclass(self) -> None:
        assert isinstance(TaskStatus.DONE, str)
        assert TaskStatus.DONE == "DONE"


# ── display_status ──────────────────────────────────────────────────────


def _rgb(style: str) -> tuple[int, int, int] | None:
    color = Style.parse(style).color
    if color is None:  # e.g. "dim", which sets no colour at all
        return None
    t = color.get_truecolor()
    return t.red, t.green, t.blue


class TestDisplayStatus:
    @pytest.mark.parametrize(
        "status", [TaskStatus.AGENT_FINISHED, TaskStatus.NEEDS_ATTENTION]
    )
    def test_cycling_task_reads_as_in_loop(self, status: TaskStatus) -> None:
        assert display_status(status, 3600) == (
            AGENT_IN_LOOP_LABEL,
            AGENT_IN_LOOP_STYLE,
        )

    @pytest.mark.parametrize(
        "status", [TaskStatus.AGENT_FINISHED, TaskStatus.NEEDS_ATTENTION]
    )
    def test_same_statuses_unchanged_without_a_cycle(
        self, status: TaskStatus
    ) -> None:
        for seconds in (None, 0):
            assert display_status(status, seconds) == (
                status.value,
                STYLE_FOR_STATUS[status],
            )

    @pytest.mark.parametrize(
        "status",
        [
            TaskStatus.WORKING,
            TaskStatus.ERROR,
            TaskStatus.DONE,
            TaskStatus.DISCARDED,
        ],
    )
    def test_other_statuses_keep_their_label_while_cycling(
        self, status: TaskStatus
    ) -> None:
        assert display_status(status, 3600) == (
            status.value,
            STYLE_FOR_STATUS[status],
        )

    def test_in_loop_colour_is_purple(self) -> None:
        rgb = _rgb(AGENT_IN_LOOP_STYLE)
        assert rgb is not None
        red, green, blue = rgb
        assert red > green and blue > green

    def test_in_loop_colour_matches_no_other_status(self) -> None:
        in_loop = _rgb(AGENT_IN_LOOP_STYLE)
        others = {_rgb(style) for style in STYLE_FOR_STATUS.values()}
        assert in_loop not in others

    def test_in_loop_style_is_not_reused_by_a_real_status(self) -> None:
        assert AGENT_IN_LOOP_STYLE not in STYLE_FOR_STATUS.values()

    def test_in_loop_label_is_not_a_real_status(self) -> None:
        with pytest.raises(ValueError):
            TaskStatus(AGENT_IN_LOOP_LABEL)


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


# ── validate_task_name ─────────────────────────────────────────────────


class TestValidateTaskName:
    @pytest.mark.parametrize("name", [
        "fix-bug",
        "my_task",
        "Task123",
        "abc",
        "A-long_Task-Name_99",
    ])
    def test_valid_names(self, name: str) -> None:
        assert validate_task_name(name) is None

    @pytest.mark.parametrize("name", ["ab", "x", ""])
    def test_too_short(self, name: str) -> None:
        err = validate_task_name(name)
        assert err is not None
        assert "at least 3" in err

    @pytest.mark.parametrize("name", [
        "has space",
        "hello!",
        "a.b.c",
        "foo/bar",
        "name@here",
        "col:on",
        "semi;colon",
    ])
    def test_invalid_characters(self, name: str) -> None:
        err = validate_task_name(name)
        assert err is not None
        assert "letters, digits" in err

    def test_short_and_invalid_reports_length_first(self) -> None:
        """A 2-char name with bad chars should fail on length, not charset."""
        err = validate_task_name("a!")
        assert err is not None
        assert "at least 3" in err

    @pytest.mark.parametrize("name", ["123", "0042", "99999"])
    def test_all_digit_names_rejected(self, name: str) -> None:
        err = validate_task_name(name)
        assert err is not None
        assert "all digits" in err

    @pytest.mark.parametrize("name", ["12a", "a12", "1-2", "1_2"])
    def test_names_merely_containing_digits_allowed(self, name: str) -> None:
        assert validate_task_name(name) is None


# ── parse_task_number ──────────────────────────────────────────────────


class TestParseTaskNumber:
    @pytest.mark.parametrize(("value", "expected"), [
        ("1", 1),
        ("42", 42),
        ("007", 7),
    ])
    def test_parses_digits(self, value: str, expected: int) -> None:
        assert parse_task_number(value) == expected

    @pytest.mark.parametrize("value", ["", "a1", "1a", "-1", "1.0", "#1", " 1"])
    def test_rejects_non_numbers(self, value: str) -> None:
        assert parse_task_number(value) is None


# ── Task ────────────────────────────────────────────────────────────────


class TestTask:
    def _make_task(self, **overrides) -> Task:
        defaults = {
            "name": "test-task",
            "prompt": "Do something",
            "status": TaskStatus.WORKING,
            "created_at": "2025-01-01T00:00:00+00:00",
            "status_changed_at": "2025-01-01T00:00:00+00:00",
        }
        defaults.update(overrides)
        return Task(**defaults)

    def test_default_fields(self) -> None:
        t = Task(name="x", prompt="y")
        assert t.status == TaskStatus.WORKING
        assert t.session_id is None
        assert t.pid is None
        assert t.cached_replies == []
        assert t.alias is None
        assert t.needs_review is False

    def test_set_status_updates_timestamp(self) -> None:
        t = self._make_task(status=TaskStatus.NEEDS_ATTENTION)
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
            needs_review=True,
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
        assert t2.needs_review == t.needs_review

    def test_to_dict_keys(self) -> None:
        t = self._make_task()
        d = t.to_dict()
        expected_keys = {
            "name", "prompt", "status", "created_at", "status_changed_at",
            "session_id", "session_log_path", "pid", "cached_replies", "alias",
            "number",
            "task_hash", "needs_review", "pinned", "input_tokens", "output_tokens",
            "cache_read_input_tokens", "cost_usd", "sleep_seconds",
            "reply_every_seconds", "reply_every_message", "reply_every_next_at",
            "parent_name", "deleted_ancestors",
            "summary_one_liner", "model", "last_assistant_model",
            "spawn_effort", "last_assistant_effort",
            "spawn_budget", "last_assistant_budget", "last_assistant_cost_usd",
            "gist_id", "gist_url", "gist_synced_count", "gist_branch_point",
            "gist_branch_parent_name", "gist_parent_comment_url",
            "gist_title_name", "gist_description",
            "engine", "sessions", "log_cursors", "awaiting_catchup",
            "awaiting_branch_notice",
        }
        assert set(d.keys()) == expected_keys

    def test_from_dict_with_missing_optional_fields(self) -> None:
        """Backward compatibility: old dicts may lack newer fields."""
        d = {"name": "old", "prompt": "p", "status": "WORKING"}
        t = Task.from_dict(d)
        assert t.name == "old"
        assert t.status == TaskStatus.WORKING
        assert t.created_at == ""
        assert t.session_id is None
        assert t.cached_replies == []
        assert t.alias is None
        assert t.needs_review is False
        assert t.deleted_ancestors == []

    def test_from_dict_migrates_legacy_unclaimed(self) -> None:
        """Tasks persisted before UNCLAIMED was retired load as NEEDS_ATTENTION."""
        d = {"name": "old", "prompt": "p", "status": "UNCLAIMED"}
        t = Task.from_dict(d)
        assert t.status == TaskStatus.NEEDS_ATTENTION

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

    def test_task_hash_roundtrip(self) -> None:
        t = self._make_task(task_hash="abcd1234")
        d = t.to_dict()
        assert d["task_hash"] == "abcd1234"
        t2 = Task.from_dict(d)
        assert t2.task_hash == "abcd1234"

    def test_task_hash_default_none(self) -> None:
        t = Task(name="x", prompt="y")
        assert t.task_hash is None

    def test_from_dict_missing_task_hash(self) -> None:
        d = {"name": "old", "prompt": "p", "status": "WORKING"}
        t = Task.from_dict(d)
        assert t.task_hash is None

    def test_model_default_none(self) -> None:
        t = Task(name="x", prompt="y")
        assert t.model is None

    def test_model_roundtrip(self) -> None:
        t = self._make_task(model=FABLE_MODEL)
        d = t.to_dict()
        assert d["model"] == FABLE_MODEL
        t2 = Task.from_dict(d)
        assert t2.model == FABLE_MODEL

    def test_from_dict_missing_model(self) -> None:
        d = {"name": "old", "prompt": "p", "status": "UNCLAIMED"}
        t = Task.from_dict(d)
        assert t.model is None

    def test_effort_fields_roundtrip(self) -> None:
        t = self._make_task()
        t.spawn_effort = "xhigh"
        t.last_assistant_effort = "medium"
        t2 = Task.from_dict(t.to_dict())
        assert t2.spawn_effort == "xhigh"
        assert t2.last_assistant_effort == "medium"

    def test_from_dict_missing_effort_fields(self) -> None:
        d = {"name": "old", "prompt": "p", "status": "WORKING"}
        t = Task.from_dict(d)
        assert t.spawn_effort is None
        assert t.last_assistant_effort is None

    def test_budget_fields_roundtrip(self) -> None:
        t = self._make_task()
        t.spawn_budget = "API"
        t.last_assistant_budget = "Team"
        t2 = Task.from_dict(t.to_dict())
        assert t2.spawn_budget == "API"
        assert t2.last_assistant_budget == "Team"

    def test_from_dict_missing_budget_fields(self) -> None:
        d = {"name": "old", "prompt": "p", "status": "WORKING"}
        t = Task.from_dict(d)
        assert t.spawn_budget is None
        assert t.last_assistant_budget is None

    def test_gist_fields_default(self) -> None:
        t = Task(name="x", prompt="y")
        assert t.gist_id is None
        assert t.gist_url is None
        assert t.gist_synced_count == 0
        assert t.gist_branch_point == 0
        assert t.gist_branch_parent_name is None
        assert t.gist_parent_comment_url is None
        assert t.gist_title_name is None
        assert t.gist_description is None

    def test_gist_fields_roundtrip(self) -> None:
        t = self._make_task()
        t.gist_id = "gid123"
        t.gist_url = "https://gist.github.com/u/gid123"
        t.gist_synced_count = 5
        t.gist_branch_point = 3
        t.gist_branch_parent_name = "parent-task"
        t.gist_parent_comment_url = "https://gist.github.com/u/parent#comment"
        t.gist_title_name = "my-task"
        t.gist_description = "ilan task (my-task)"
        d = t.to_dict()
        t2 = Task.from_dict(d)
        assert t2.gist_id == "gid123"
        assert t2.gist_url == "https://gist.github.com/u/gid123"
        assert t2.gist_synced_count == 5
        assert t2.gist_branch_point == 3
        assert t2.gist_branch_parent_name == "parent-task"
        assert t2.gist_parent_comment_url == (
            "https://gist.github.com/u/parent#comment"
        )
        assert t2.gist_title_name == "my-task"
        assert t2.gist_description == "ilan task (my-task)"

    def test_from_dict_missing_gist_fields(self) -> None:
        d = {"name": "old", "prompt": "p", "status": "UNCLAIMED"}
        t = Task.from_dict(d)
        assert t.gist_id is None
        assert t.gist_url is None
        assert t.gist_synced_count == 0
        assert t.gist_branch_point == 0
        assert t.gist_branch_parent_name is None
        assert t.gist_parent_comment_url is None
        assert t.gist_title_name is None
        assert t.gist_description is None

    def test_legacy_gist_description_name_is_treated_as_stale(self) -> None:
        d = {
            "name": "old",
            "prompt": "p",
            "status": "WORKING",
            "gist_description_name": "old",
        }
        t = Task.from_dict(d)
        assert t.gist_description is None

    # ── engine / per-backend session map ────────────────────────────────

    def test_engine_defaults_to_claude(self) -> None:
        t = Task(name="x", prompt="y")
        assert t.engine == ENGINE_CLAUDE
        assert t.sessions == {}

    def test_engine_and_sessions_roundtrip(self) -> None:
        t = self._make_task(engine=ENGINE_CODEX)
        t.set_session_for(ENGINE_CLAUDE, "claude-sid")
        t.set_session_for(ENGINE_CODEX, "codex-sid")
        d = t.to_dict()
        assert d["engine"] == ENGINE_CODEX
        assert d["sessions"] == {"claude": "claude-sid", "codex": "codex-sid"}
        t2 = Task.from_dict(d)
        assert t2.engine == ENGINE_CODEX
        assert t2.sessions == {"claude": "claude-sid", "codex": "codex-sid"}

    def test_log_cursors_and_catchup_roundtrip(self) -> None:
        t = self._make_task()
        t.log_cursors = {"claude": 3, "codex": 1}
        t.awaiting_catchup = True
        d = t.to_dict()
        assert d["log_cursors"] == {"claude": 3, "codex": 1}
        assert d["awaiting_catchup"] is True
        t2 = Task.from_dict(d)
        assert t2.log_cursors == {"claude": 3, "codex": 1}
        assert t2.awaiting_catchup is True

    def test_from_dict_missing_cursor_fields_default(self) -> None:
        d = {"name": "old", "prompt": "p", "status": "UNCLAIMED"}
        t = Task.from_dict(d)
        assert t.log_cursors == {}
        assert t.awaiting_catchup is False

    def test_from_dict_missing_engine_defaults_claude(self) -> None:
        d = {"name": "old", "prompt": "p", "status": "UNCLAIMED"}
        t = Task.from_dict(d)
        assert t.engine == ENGINE_CLAUDE
        assert t.sessions == {}

    def test_from_dict_migrates_legacy_session_id(self) -> None:
        """A pre-map task with only session_id seeds the map under its engine."""
        d = {"name": "old", "prompt": "p", "status": "WORKING",
             "session_id": "legacy-sid"}
        t = Task.from_dict(d)
        assert t.sessions == {"claude": "legacy-sid"}

    def test_from_dict_explicit_sessions_not_overwritten(self) -> None:
        d = {"name": "x", "prompt": "p", "status": "WORKING",
             "session_id": "active-sid", "engine": "codex",
             "sessions": {"claude": "c", "codex": "active-sid"}}
        t = Task.from_dict(d)
        assert t.sessions == {"claude": "c", "codex": "active-sid"}

    def test_other_engine_toggles(self) -> None:
        assert other_engine(ENGINE_CLAUDE) == ENGINE_CODEX
        assert other_engine(ENGINE_CODEX) == ENGINE_CLAUDE

    def test_engine_name_style_covers_all_engines(self) -> None:
        assert set(ENGINE_NAME_STYLE) == set(VALID_ENGINES)
        assert ENGINE_NAME_STYLE[ENGINE_CLAUDE] == "orange1"
        assert ENGINE_NAME_STYLE[ENGINE_CODEX] == "light_sky_blue1"


# ── Max models (`ilan max`) ─────────────────────────────────────────────


class TestMaxModels:
    def test_model_ids(self) -> None:
        assert FABLE_MODEL == "claude-fable-5-1"
        assert ASTRA_MODEL == "gpt-6-astra"

    def test_every_engine_has_a_max_model(self) -> None:
        """A backend without one would silently max to Claude's Fable id,
        which its CLI cannot load."""
        assert set(MAX_MODELS) == set(VALID_ENGINES)

    def test_the_tags_are_distinct(self) -> None:
        """The tag is the whole point of showing it: two backends sharing one
        would stop saying which model a task is burning."""
        tags = [entry.tag for entry in MAX_MODELS.values()]
        assert len(set(tags)) == len(tags)

    def test_max_model_for_each_engine(self) -> None:
        assert max_model_for(ENGINE_CLAUDE) == FABLE_MODEL
        assert max_model_for(ENGINE_CODEX) == ASTRA_MODEL

    def test_max_model_for_falls_back_like_a_spawn_does(self) -> None:
        """An absent engine predates the field and an unknown one is driven by
        the Claude backend, so both max to Claude's model."""
        assert max_model_for(None) == FABLE_MODEL
        assert max_model_for("no-such-engine") == FABLE_MODEL

    # max_tag is the predicate behind the red tag in `ls`, the dashboard and
    # the web app. It is one function so the three cannot disagree; these pin
    # the two halves of what it asks.
    def test_max_tag_needs_the_pin(self) -> None:
        assert max_tag(ENGINE_CLAUDE, FABLE_MODEL) == "FABLE"
        assert max_tag(ENGINE_CODEX, ASTRA_MODEL) == "ASTRA"
        assert max_tag(ENGINE_CLAUDE, None) is None
        assert max_tag(ENGINE_CLAUDE, "claude-opus-4-7") is None
        assert max_tag(ENGINE_CODEX, "gpt-5.6-sol") is None

    def test_max_tag_keeps_a_legacy_pin(self) -> None:
        """A task maxed before a bump stays tagged, exactly as `ls` does."""
        for engine, entry in MAX_MODELS.items():
            for legacy in entry.legacy:
                assert max_tag(engine, legacy) == entry.tag

    def test_max_tag_needs_the_owning_backend(self) -> None:
        """Each max model is one backend's: the other drops the pin at spawn
        time, so a task sitting there is not on it.

        The pin itself is untouched, which is why this takes the model as an
        argument rather than clearing it — switching back has to put the tag
        back.
        """
        assert max_tag(ENGINE_CODEX, FABLE_MODEL) is None
        assert max_tag(ENGINE_CLAUDE, ASTRA_MODEL) is None
        for legacy in LEGACY_FABLE_MODELS:
            assert max_tag(ENGINE_CODEX, legacy) is None
        for legacy in LEGACY_ASTRA_MODELS:
            assert max_tag(ENGINE_CLAUDE, legacy) is None

    def test_max_tag_treats_no_engine_as_the_default(self) -> None:
        """A task that predates the engine field runs on the default backend,
        which is Claude — the same fallback `_build_name_cell` takes."""
        assert max_tag(None, FABLE_MODEL) == "FABLE"
        assert max_tag(None, None) is None

    def test_tag_for_max_model_reads_a_bare_id(self) -> None:
        assert tag_for_max_model(FABLE_MODEL) == "FABLE"
        assert tag_for_max_model(ASTRA_MODEL) == "ASTRA"
        assert tag_for_max_model("claude-opus-4-7") is None
        assert tag_for_max_model(None) is None

    def test_tag_for_max_model_reads_a_legacy_id(self) -> None:
        for entry in MAX_MODELS.values():
            for legacy in entry.legacy:
                assert tag_for_max_model(legacy) == entry.tag

    # foreign_max_model is what keeps a backend from being handed a model it
    # cannot load, which is the failure a backend switch would otherwise cause.
    def test_foreign_max_model_spots_the_other_backends_pin(self) -> None:
        assert foreign_max_model(ENGINE_CODEX, FABLE_MODEL)
        assert foreign_max_model(ENGINE_CLAUDE, ASTRA_MODEL)

    def test_foreign_max_model_keeps_its_own_pin(self) -> None:
        assert not foreign_max_model(ENGINE_CLAUDE, FABLE_MODEL)
        assert not foreign_max_model(ENGINE_CODEX, ASTRA_MODEL)

    def test_foreign_max_model_keeps_a_legacy_pin_of_its_own(self) -> None:
        """A superseded id is still this backend's, so it is still run."""
        for engine, entry in MAX_MODELS.items():
            for legacy in entry.legacy:
                assert not foreign_max_model(engine, legacy)

    def test_foreign_max_model_spots_the_other_backends_legacy_pin(self) -> None:
        for legacy in LEGACY_FABLE_MODELS:
            assert foreign_max_model(ENGINE_CODEX, legacy)
        for legacy in LEGACY_ASTRA_MODELS:
            assert foreign_max_model(ENGINE_CLAUDE, legacy)

    def test_foreign_max_model_ignores_an_ordinary_model(self) -> None:
        """Only a max pin is dropped: a plain override is the caller's own."""
        assert not foreign_max_model(ENGINE_CODEX, "gpt-5.6-sol")
        assert not foreign_max_model(ENGINE_CLAUDE, "claude-opus-4-7")
        assert not foreign_max_model(ENGINE_CODEX, None)

    def test_legacy_ids_still_read_as_maxed(self) -> None:
        """Tasks maxed before a model bump keep the id they were pinned to.
        They must still count as that backend's max model, or the other backend
        would hand its CLI a model it cannot load."""
        assert LEGACY_FABLE_MODELS  # a bump without an entry here is a bug
        for entry in MAX_MODELS.values():
            for legacy in entry.legacy:
                assert entry.matches(legacy)

    def test_a_current_id_is_not_listed_as_legacy(self) -> None:
        for entry in MAX_MODELS.values():
            assert entry.model not in entry.legacy


# ── generate_task_hash ─────────────────────────────────────────────────


class TestGenerateTaskHash:
    def test_length(self) -> None:
        h = generate_task_hash()
        assert len(h) == 8

    def test_hex_chars(self) -> None:
        h = generate_task_hash()
        assert all(c in "0123456789abcdef" for c in h)

    def test_uniqueness(self) -> None:
        hashes = {generate_task_hash() for _ in range(100)}
        assert len(hashes) == 100


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

    def test_effort_roundtrip(self) -> None:
        e = LogEntry.now("assistant", "response", model="claude-opus-4-8", effort="xhigh")
        d = e.to_dict()
        assert d["effort"] == "xhigh"
        e2 = LogEntry.from_dict(d)
        assert e2.effort == "xhigh"

    def test_effort_omitted_when_unset(self) -> None:
        e = LogEntry.now("assistant", "response")
        assert "effort" not in e.to_dict()
        assert LogEntry.from_dict({"role": "user", "content": "hi"}).effort is None

    def test_task_alias_roundtrip(self) -> None:
        e = LogEntry.now("assistant", "response", task_alias="ds")
        d = e.to_dict()
        assert d["task_alias"] == "ds"
        assert LogEntry.from_dict(d).task_alias == "ds"

    def test_task_alias_omitted_when_unset(self) -> None:
        e = LogEntry.now("assistant", "response")
        assert "task_alias" not in e.to_dict()
        assert LogEntry.from_dict({"role": "user", "content": "hi"}).task_alias is None

    def test_cost_roundtrip(self) -> None:
        e = LogEntry.now("assistant", "response", model="claude-opus-5", cost_usd=1.25)
        d = e.to_dict()
        assert d["cost_usd"] == 1.25
        assert LogEntry.from_dict(d).cost_usd == 1.25

    def test_cost_omitted_when_unset_or_zero(self) -> None:
        assert "cost_usd" not in LogEntry.now("assistant", "response").to_dict()
        assert "cost_usd" not in LogEntry.now(
            "assistant", "response", cost_usd=0.0
        ).to_dict()
        assert LogEntry.from_dict({"role": "user", "content": "hi"}).cost_usd is None

    def test_token_usage_roundtrip_preserves_zero(self) -> None:
        e = LogEntry.now(
            "assistant",
            "response",
            input_tokens=123,
            output_tokens=45,
            cache_read_input_tokens=0,
        )
        d = e.to_dict()
        assert d["input_tokens"] == 123
        assert d["output_tokens"] == 45
        assert d["cache_read_input_tokens"] == 0
        e2 = LogEntry.from_dict(d)
        assert e2.input_tokens == 123
        assert e2.output_tokens == 45
        assert e2.cache_read_input_tokens == 0

    def test_token_usage_omitted_when_unknown(self) -> None:
        d = LogEntry.now("assistant", "response").to_dict()
        assert "input_tokens" not in d
        assert "output_tokens" not in d
        assert "cache_read_input_tokens" not in d


class TestFormatCostUsd:
    def test_rounds_to_cents(self) -> None:
        assert format_cost_usd(1.2345, API) == "$1.23"
        assert format_cost_usd(1.999, API) == "$2.00"
        assert format_cost_usd(2, API) == "$2.00"

    def test_absent_cost_renders_nothing(self) -> None:
        """Zero means "not priced by the backend", not "free"."""
        assert format_cost_usd(None, API) is None
        assert format_cost_usd(0.0, API) is None

    def test_sub_cent_cost_rounds_to_zero(self) -> None:
        """Two decimals is the agreed precision; a fraction of a cent shows $0.00."""
        assert format_cost_usd(0.004, API) == "$0.00"

    def test_subscription_spend_is_withheld(self) -> None:
        """A plan's price is notional, so it is never shown as a dollar amount."""
        assert format_cost_usd(1.25, "Team") is None
        assert format_cost_usd(1.25, "Max") is None

    def test_unknown_budget_withholds_cost(self) -> None:
        """Unreadable credentials must not be assumed to mean an API key."""
        assert format_cost_usd(1.25, None) is None
