"""Tests for ilan.runner — status parsing, prompt building, spawn/reap."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

import ilan.config as cfg
from ilan import budget
from ilan.backends import ClaudeBackend, CodexBackend
from ilan.models import (
    ASTRA_MODEL,
    ENGINE_CLAUDE,
    ENGINE_CODEX,
    FABLE_MODEL,
    LEGACY_ASTRA_MODELS,
    LEGACY_FABLE_MODELS,
    LogEntry,
    Task,
    TaskStatus,
)
from ilan.runner import (
    Runner,
    STATUS_SUFFIX,
    _CATCHUP_MAX_CHARS,
    _branch_divider,
    _branch_notice,
    _render_catchup,
    _tmux_instruction,
)
from ilan.store import Store


@pytest.fixture()
def store(tmp_workdir: Path) -> Store:
    return Store(tmp_workdir)


@pytest.fixture()
def runner(store: Store) -> Runner:
    return Runner(store)


@pytest.fixture(autouse=True)
def no_real_one_liner(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the one-liner generator for every test in this module.

    The real ``generate_one_liner`` reads the user's actual ilan config and,
    with no ``api-key-codex`` set, shells out to the ``codex`` CLI — a live
    LLM call on every reap that reaches AGENT_FINISHED/NEEDS_ATTENTION
    (2-5s per test, network, quota). Tests that exercise the one-liner flow
    override this with their own ``patch``.
    """
    monkeypatch.setattr("ilan.runner.generate_one_liner", lambda *_: None)


# ── _parse_status_marker ────────────────────────────────────────────────


class TestParseStatusMarker:
    def test_done_marker(self) -> None:
        resp = "I did the thing.\n\n[STATUS: DONE]"
        assert Runner._parse_status_marker(resp) == TaskStatus.AGENT_FINISHED

    def test_needs_attention_marker(self) -> None:
        resp = "I'm stuck.\n\n[STATUS: NEEDS_ATTENTION]"
        assert Runner._parse_status_marker(resp) == TaskStatus.NEEDS_ATTENTION

    def test_needs_attention_with_extra_spaces(self) -> None:
        resp = "Blocked.\n\n[STATUS:   NEEDS_ATTENTION]"
        assert Runner._parse_status_marker(resp) == TaskStatus.NEEDS_ATTENTION

    def test_no_marker(self) -> None:
        resp = "Just some text without a marker."
        assert Runner._parse_status_marker(resp) == TaskStatus.AGENT_FINISHED

    def test_empty_response(self) -> None:
        assert Runner._parse_status_marker("") == TaskStatus.AGENT_FINISHED

    def test_marker_in_middle(self) -> None:
        resp = "Start\n[STATUS: NEEDS_ATTENTION]\nEnd"
        assert Runner._parse_status_marker(resp) == TaskStatus.NEEDS_ATTENTION


# ── _build_prompt ───────────────────────────────────────────────────────


class TestBuildPrompt:
    def test_fresh_task(self, runner: Runner) -> None:
        t = Task(name="t", prompt="Do X")
        prompt, resume = runner._build_prompt(t)
        assert prompt == "Do X"
        assert resume is False

    def test_task_with_session_and_no_replies(self, runner: Runner) -> None:
        t = Task(name="t", prompt="Do X", session_id="sid-1")
        with patch.object(Runner, "find_session_log", return_value=Path("/fake/sid-1.jsonl")):
            prompt, resume = runner._build_prompt(t)
        assert prompt == "Please continue working on this task."
        assert resume is True

    def test_task_with_cached_replies_no_session(self, runner: Runner) -> None:
        t = Task(name="t", prompt="Do X", cached_replies=["fix it"])
        prompt, resume = runner._build_prompt(t)
        assert "Do X" in prompt
        assert "fix it" in prompt
        assert resume is False
        assert t.cached_replies == []  # cleared after build

    def test_task_with_cached_replies_and_session(self, runner: Runner) -> None:
        t = Task(name="t", prompt="Do X", session_id="sid-1", cached_replies=["r1", "r2"])
        with patch.object(Runner, "find_session_log", return_value=Path("/fake/sid-1.jsonl")):
            prompt, resume = runner._build_prompt(t)
        assert "r1" in prompt
        assert "r2" in prompt
        assert resume is True
        assert t.cached_replies == []

    def test_multiple_cached_replies_joined(self, runner: Runner) -> None:
        t = Task(name="t", prompt="Do X", cached_replies=["a", "b", "c"])
        prompt, resume = runner._build_prompt(t)
        assert "a\n\nb\n\nc" in prompt


class TestBuildPromptLostSession:
    """When a task's session log is unfindable (deleted, or created under
    another account's home), the fresh replacement session must be seeded
    with the unified-log transcript instead of just the original prompt."""

    def test_seeds_fresh_session_with_full_transcript(
        self, store: Store, runner: Runner
    ) -> None:
        # kill → ERROR → reply: the reply is in cached_replies AND the log.
        t = Task(name="ls1", prompt="Do X", engine=ENGINE_CODEX,
                 session_id="dead-sid", sessions={"codex": "dead-sid"},
                 log_cursors={"codex": 2}, cached_replies=["finish it"])
        store.append_log("ls1", "user", "Do X")
        store.append_log("ls1", "assistant", "did step 1")
        store.append_log("ls1", "user", "finish it")
        store.put_task(t)

        with patch.object(Runner, "find_session_log", return_value=None):
            prompt, resume = runner._build_prompt(t)

        assert resume is False
        assert "full conversation" in prompt.lower()
        assert "Do X" in prompt
        assert "did step 1" in prompt  # the turn that used to vanish
        assert "finish it" in prompt
        assert t.session_id is None
        assert t.session_log_path is None
        assert t.cached_replies == []
        # Consumed only at reap, so a failed spawn retries the same seed.
        assert t.awaiting_catchup is True
        assert t.log_cursors["codex"] == 0

    def test_seeds_even_without_cached_replies(
        self, store: Store, runner: Runner
    ) -> None:
        t = Task(name="ls2", prompt="Do X", engine=ENGINE_CLAUDE,
                 session_id="dead-sid", sessions={"claude": "dead-sid"})
        store.append_log("ls2", "user", "Do X")
        store.append_log("ls2", "assistant", "half done")
        store.put_task(t)

        with patch.object(Runner, "find_session_log", return_value=None):
            prompt, resume = runner._build_prompt(t)

        assert resume is False
        assert "half done" in prompt
        assert prompt != "Please continue working on this task."

    def test_clears_stale_session_map_entry(
        self, store: Store, runner: Runner
    ) -> None:
        """A later engine-switch round-trip must not resurrect the dead id."""
        t = Task(name="ls3", prompt="Do X", engine=ENGINE_CODEX,
                 session_id="dead-sid",
                 sessions={"codex": "dead-sid", "claude": "live-claude-sid"})
        store.append_log("ls3", "user", "Do X")
        store.put_task(t)

        with patch.object(Runner, "find_session_log", return_value=None):
            runner._build_prompt(t)

        assert "codex" not in t.sessions
        assert t.sessions["claude"] == "live-claude-sid"  # untouched


# ── _tmux_instruction ──────────────────────────────────────────────────


class TestTmuxInstruction:
    def test_contains_default_session_name(self) -> None:
        instr = _tmux_instruction("abc12345", "my-task")
        assert "abc12345-claude-my-task" in instr

    def test_contains_create_command(self) -> None:
        instr = _tmux_instruction("abc12345", "my-task")
        assert "tmux new-session -d -s abc12345-claude-my-task" in instr

    def test_contains_requirement_keyword(self) -> None:
        instr = _tmux_instruction("abc12345", "my-task")
        assert "TMUX SESSION REQUIREMENT" in instr

    def test_mentions_prefix_for_additional_sessions(self) -> None:
        instr = _tmux_instruction("abc12345", "my-task")
        assert "prefixed with `abc12345`" in instr


# ── STATUS_SUFFIX ───────────────────────────────────────────────────────


class TestStatusSuffix:
    def test_suffix_contains_markers(self) -> None:
        assert "[STATUS: DONE]" in STATUS_SUFFIX
        assert "[STATUS: NEEDS_ATTENTION]" in STATUS_SUFFIX

    def test_suffix_starts_with_separator(self) -> None:
        assert "---" in STATUS_SUFFIX


# ── _try_reap ───────────────────────────────────────────────────────────


class TestTryReap:
    def test_reap_done_output(self, store: Store, runner: Runner) -> None:
        t = Task(name="t1", prompt="p", status=TaskStatus.WORKING, pid=99999)
        store.put_task(t)
        # Write mock output
        out = {"session_id": "sid-1", "result": "Done!\n[STATUS: DONE]", "is_error": False}
        store.output_path("t1").write_text(json.dumps(out))

        with patch.object(Runner, "find_session_log", return_value=Path("/fake/sid-1.jsonl")):
            runner._try_reap(t)
        updated = store.get_task("t1")
        assert updated is not None
        assert updated.status == TaskStatus.AGENT_FINISHED
        assert updated.session_id == "sid-1"
        assert updated.pid is None

    def test_reap_needs_attention_output(self, store: Store, runner: Runner) -> None:
        t = Task(name="t2", prompt="p", status=TaskStatus.WORKING, pid=99999)
        store.put_task(t)
        out = {"session_id": "sid-2", "result": "Stuck\n[STATUS: NEEDS_ATTENTION]", "is_error": False}
        store.output_path("t2").write_text(json.dumps(out))

        runner._try_reap(t)
        updated = store.get_task("t2")
        assert updated is not None
        assert updated.status == TaskStatus.NEEDS_ATTENTION

    def test_reap_error_output(self, store: Store, runner: Runner) -> None:
        t = Task(name="t3", prompt="p", status=TaskStatus.WORKING, pid=99999)
        store.put_task(t)
        out = {"session_id": "sid-3", "result": "Error happened", "is_error": True}
        store.output_path("t3").write_text(json.dumps(out))

        runner._try_reap(t)
        updated = store.get_task("t3")
        assert updated is not None
        assert updated.status == TaskStatus.ERROR

    def test_reap_invalid_json(self, store: Store, runner: Runner) -> None:
        t = Task(name="t4", prompt="p", status=TaskStatus.WORKING, pid=99999)
        store.put_task(t)
        store.output_path("t4").write_text("not json")

        runner._try_reap(t)
        updated = store.get_task("t4")
        assert updated is not None
        assert updated.status == TaskStatus.ERROR

    def test_reap_missing_output(self, store: Store, runner: Runner) -> None:
        t = Task(name="t5", prompt="p", status=TaskStatus.WORKING, pid=99999)
        store.put_task(t)
        # Don't write any output file

        runner._try_reap(t)
        updated = store.get_task("t5")
        assert updated is not None
        assert updated.status == TaskStatus.ERROR

    def test_reap_appends_log(self, store: Store, runner: Runner) -> None:
        t = Task(name="t6", prompt="p", status=TaskStatus.WORKING, pid=99999)
        store.put_task(t)
        out = {
            "session_id": "sid-6",
            "result": "All good\n[STATUS: DONE]",
            "is_error": False,
            "usage": {
                "input_tokens": 123,
                "output_tokens": 45,
                "cache_read_input_tokens": 678,
            },
        }
        store.output_path("t6").write_text(json.dumps(out))

        runner._try_reap(t)
        logs = store.read_logs("t6")
        assert len(logs) == 1
        assert logs[0].role == "assistant"
        assert "All good" in logs[0].content
        assert logs[0].input_tokens is None
        assert logs[0].output_tokens is None
        assert logs[0].cache_read_input_tokens is None
        updated = store.get_task("t6")
        assert updated is not None
        assert updated.input_tokens == 123
        assert updated.output_tokens == 45
        assert updated.cache_read_input_tokens == 678

    def test_reap_separates_final_message_usage_from_invocation_totals(
        self, store: Store, runner: Runner, tmp_path: Path
    ) -> None:
        log = tmp_path / "sid-message-usage.jsonl"
        log.write_text(json.dumps({"message": {
            "role": "assistant",
            "model": "claude-opus-5",
            "content": [{"type": "text", "text": "All good"}],
            "usage": {
                "input_tokens": 2,
                "output_tokens": 10,
                "cache_read_input_tokens": 90,
            },
        }}) + "\n")
        t = Task(
            name="message-usage",
            prompt="p",
            status=TaskStatus.WORKING,
            pid=99999,
        )
        store.put_task(t)
        store.output_path("message-usage").write_text(json.dumps({
            "session_id": "sid-message-usage",
            "result": "All good\n[STATUS: DONE]",
            "is_error": False,
            "usage": {
                "input_tokens": 123,
                "output_tokens": 45,
                "cache_read_input_tokens": 678,
            },
        }))

        with patch.object(Runner, "find_session_log", return_value=log):
            runner._try_reap(t)

        reply = store.read_logs("message-usage")[-1]
        assert reply.input_tokens == 2
        assert reply.output_tokens == 10
        assert reply.cache_read_input_tokens == 90
        updated = store.get_task("message-usage")
        assert updated is not None
        assert updated.input_tokens == 123
        assert updated.output_tokens == 45
        assert updated.cache_read_input_tokens == 678

    def test_reap_separates_codex_message_usage_from_turn_delta(
        self, store: Store, runner: Runner, tmp_path: Path
    ) -> None:
        log = tmp_path / "rollout-codex-sid.jsonl"
        log.write_text("\n".join([
            json.dumps({"type": "event_msg",
                        "payload": {"type": "task_started"}}),
            json.dumps({
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {"total_token_usage": {
                        "input_tokens": 1_000,
                        "cached_input_tokens": 700,
                        "output_tokens": 50,
                    }},
                },
            }),
            json.dumps({"type": "event_msg",
                        "payload": {"type": "task_complete"}}),
            json.dumps({"type": "event_msg",
                        "payload": {"type": "task_started"}}),
            json.dumps({"type": "event_msg",
                        "payload": {"type": "agent_message"}}),
            json.dumps({
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "total_token_usage": {
                            "input_tokens": 1_300,
                            "cached_input_tokens": 900,
                            "output_tokens": 80,
                        },
                        "last_token_usage": {
                            "input_tokens": 130,
                            "cached_input_tokens": 100,
                            "output_tokens": 8,
                        },
                    },
                },
            }),
            json.dumps({"type": "event_msg",
                        "payload": {"type": "task_complete"}}),
        ]) + "\n")
        t = Task(
            name="codex-delta",
            prompt="p",
            status=TaskStatus.WORKING,
            pid=99999,
            engine=ENGINE_CODEX,
        )
        store.put_task(t)
        store.output_path("codex-delta").write_text("\n".join([
            json.dumps({"type": "thread.started", "thread_id": "codex-sid"}),
            json.dumps({
                "type": "item.completed",
                "item": {
                    "type": "agent_message",
                    "text": "done\n[STATUS: DONE]",
                },
            }),
            json.dumps({
                "type": "turn.completed",
                "usage": {
                    "input_tokens": 1_300,
                    "cached_input_tokens": 900,
                    "output_tokens": 80,
                },
            }),
        ]) + "\n")

        with (
            patch.object(Runner, "find_session_log", return_value=log),
            patch.object(CodexBackend, "last_assistant_model", return_value=None),
        ):
            runner._try_reap(t)

        reply = store.read_logs("codex-delta")[-1]
        assert reply.input_tokens == 30
        assert reply.output_tokens == 8
        assert reply.cache_read_input_tokens == 100
        updated = store.get_task("codex-delta")
        assert updated is not None
        assert updated.input_tokens == 100
        assert updated.output_tokens == 30
        assert updated.cache_read_input_tokens == 200

    def test_reap_caches_last_assistant_model(
        self, store: Store, runner: Runner, tmp_path: Path
    ) -> None:
        """Reaping a finished turn caches the model from the session log so
        ``ilan tail`` need not rescan it later."""
        log = tmp_path / "sid-model.jsonl"
        log.write_text(
            json.dumps({"message": {"role": "assistant",
                                    "model": "claude-opus-4-8", "content": "hi"}}) + "\n"
        )
        t = Task(name="t-model", prompt="p", status=TaskStatus.WORKING, pid=99999)
        store.put_task(t)
        out = {"session_id": "sid-model", "result": "ok\n[STATUS: DONE]", "is_error": False}
        store.output_path("t-model").write_text(json.dumps(out))

        with patch.object(Runner, "find_session_log", return_value=log):
            runner._try_reap(t)
        updated = store.get_task("t-model")
        assert updated is not None
        assert updated.last_assistant_model == "claude-opus-4-8"
        # The appended log entry carries this turn's detected model.
        logs = store.read_logs("t-model")
        assert logs[-1].role == "assistant"
        assert logs[-1].model == "claude-opus-4-8"

    def test_reap_log_model_none_when_detection_misses(
        self, store: Store, runner: Runner
    ) -> None:
        """When this turn's model can't be detected, the appended entry carries
        no model — it must not inherit the task's cached previous-turn model
        (which, after a backend switch, could be the other engine's)."""
        t = Task(name="t-stale", prompt="p", status=TaskStatus.WORKING, pid=99999)
        t.last_assistant_model = "claude-fable-5-1"  # stale prior-turn cache
        store.put_task(t)
        out = {"session_id": "sid-stale", "result": "ok\n[STATUS: DONE]", "is_error": False}
        store.output_path("t-stale").write_text(json.dumps(out))

        # No session log found → this turn's model is undetectable.
        with patch.object(Runner, "find_session_log", return_value=None):
            runner._try_reap(t)
        logs = store.read_logs("t-stale")
        assert logs[-1].role == "assistant"
        assert logs[-1].model is None

    def test_reap_records_task_alias_on_assistant_message(
        self, store: Store, runner: Runner
    ) -> None:
        """The log entry freezes the alias used by the asynchronous Gist sync."""
        t = Task(
            name="t-alias",
            prompt="p",
            status=TaskStatus.WORKING,
            pid=99999,
            alias="ds",
        )
        store.put_task(t)
        out = {
            "session_id": "sid-alias",
            "result": "ok\n[STATUS: DONE]",
            "is_error": False,
        }
        store.output_path("t-alias").write_text(json.dumps(out))

        with patch.object(Runner, "find_session_log", return_value=None):
            runner._try_reap(t)

        assert store.read_logs("t-alias")[-1].task_alias == "ds"

    def test_reap_caches_effort_from_spawn(
        self, store: Store, runner: Runner
    ) -> None:
        """The reasoning effort captured at spawn time is copied to the
        task's ``last_assistant_effort`` and onto the appended log entry
        (the session log itself records no effort)."""
        t = Task(name="t-effort", prompt="p", status=TaskStatus.WORKING, pid=99999)
        t.spawn_effort = "xhigh"
        store.put_task(t)
        out = {"session_id": "sid-e", "result": "ok\n[STATUS: DONE]", "is_error": False}
        store.output_path("t-effort").write_text(json.dumps(out))

        with patch.object(Runner, "find_session_log", return_value=None):
            runner._try_reap(t)
        updated = store.get_task("t-effort")
        assert updated is not None
        assert updated.last_assistant_effort == "xhigh"
        logs = store.read_logs("t-effort")
        assert logs[-1].role == "assistant"
        assert logs[-1].effort == "xhigh"

    def test_reap_without_spawn_effort_keeps_cache(
        self, store: Store, runner: Runner
    ) -> None:
        """A task spawned before efforts were recorded leaves the cached
        effort untouched and appends an effort-less entry."""
        t = Task(name="t-noeffort", prompt="p", status=TaskStatus.WORKING, pid=99999)
        store.put_task(t)
        out = {"session_id": "sid-ne", "result": "ok\n[STATUS: DONE]", "is_error": False}
        store.output_path("t-noeffort").write_text(json.dumps(out))

        with patch.object(Runner, "find_session_log", return_value=None):
            runner._try_reap(t)
        updated = store.get_task("t-noeffort")
        assert updated is not None
        assert updated.last_assistant_effort is None
        assert store.read_logs("t-noeffort")[-1].effort is None

    def test_reap_caches_budget_from_spawn(
        self, store: Store, runner: Runner
    ) -> None:
        """The paying account captured at spawn time is copied to the task's
        ``last_assistant_budget`` and onto the appended log entry."""
        t = Task(name="t-budget", prompt="p", status=TaskStatus.WORKING, pid=99999)
        t.spawn_budget = "Team"
        store.put_task(t)
        out = {"session_id": "sid-b", "result": "ok\n[STATUS: DONE]", "is_error": False}
        store.output_path("t-budget").write_text(json.dumps(out))

        with patch.object(Runner, "find_session_log", return_value=None):
            runner._try_reap(t)
        updated = store.get_task("t-budget")
        assert updated is not None
        assert updated.last_assistant_budget == "Team"
        logs = store.read_logs("t-budget")
        assert logs[-1].role == "assistant"
        assert logs[-1].budget == "Team"

    def test_reap_without_spawn_budget_keeps_cache(
        self, store: Store, runner: Runner
    ) -> None:
        """An unresolvable budget leaves the cache untouched and appends a
        budget-less entry rather than guessing."""
        t = Task(name="t-nobudget", prompt="p", status=TaskStatus.WORKING, pid=99999)
        store.put_task(t)
        out = {"session_id": "sid-nb", "result": "ok\n[STATUS: DONE]", "is_error": False}
        store.output_path("t-nobudget").write_text(json.dumps(out))

        with patch.object(Runner, "find_session_log", return_value=None):
            runner._try_reap(t)
        updated = store.get_task("t-nobudget")
        assert updated is not None
        assert updated.last_assistant_budget is None
        assert store.read_logs("t-nobudget")[-1].budget is None

    def test_reap_records_per_turn_cost(self, store: Store, runner: Runner) -> None:
        """Each turn is tagged with its own cost, not the running total.

        The backend prices one invocation at a time, so a second turn must not
        inherit the sum accumulated in ``Task.cost_usd``.
        """
        t = Task(name="t-turncost", prompt="p", status=TaskStatus.WORKING, pid=99999)
        store.put_task(t)
        out = {
            "session_id": "sid-tc",
            "result": "first\n[STATUS: DONE]",
            "is_error": False,
            "total_cost_usd": 1.25,
        }
        store.output_path("t-turncost").write_text(json.dumps(out))

        with patch.object(Runner, "find_session_log", return_value=None):
            runner._try_reap(t)
        updated = store.get_task("t-turncost")
        assert updated is not None
        assert updated.last_assistant_cost_usd == pytest.approx(1.25)
        assert store.read_logs("t-turncost")[-1].cost_usd == pytest.approx(1.25)

        t = updated
        t.status = TaskStatus.WORKING
        t.pid = 99999
        store.put_task(t)
        out["total_cost_usd"] = 0.75
        out["result"] = "second\n[STATUS: DONE]"
        store.output_path("t-turncost").write_text(json.dumps(out))

        with patch.object(Runner, "find_session_log", return_value=None):
            runner._try_reap(t)
        updated = store.get_task("t-turncost")
        assert updated is not None
        assert updated.cost_usd == pytest.approx(2.0)
        assert updated.last_assistant_cost_usd == pytest.approx(0.75)
        assert store.read_logs("t-turncost")[-1].cost_usd == pytest.approx(0.75)

    def test_reap_without_cost_leaves_entry_untagged(
        self, store: Store, runner: Runner
    ) -> None:
        """Codex reports no cost, so nothing is attributed for that turn."""
        t = Task(name="t-nocost", prompt="p", status=TaskStatus.WORKING, pid=99999)
        store.put_task(t)
        out = {"session_id": "sid-nc", "result": "ok\n[STATUS: DONE]", "is_error": False}
        store.output_path("t-nocost").write_text(json.dumps(out))

        with patch.object(Runner, "find_session_log", return_value=None):
            runner._try_reap(t)
        updated = store.get_task("t-nocost")
        assert updated is not None
        assert updated.last_assistant_cost_usd is None
        assert store.read_logs("t-nocost")[-1].cost_usd is None

    def test_reap_accumulates_cost(self, store: Store, runner: Runner) -> None:
        t = Task(name="t-cost", prompt="p", status=TaskStatus.WORKING, pid=99999)
        store.put_task(t)
        out = {
            "session_id": "sid-c1",
            "result": "First run\n[STATUS: DONE]",
            "is_error": False,
            "total_cost_usd": 1.25,
        }
        store.output_path("t-cost").write_text(json.dumps(out))

        runner._try_reap(t)
        updated = store.get_task("t-cost")
        assert updated is not None
        assert updated.cost_usd == pytest.approx(1.25)

        # Simulate a second invocation (e.g. after ilan reply)
        t = updated
        t.status = TaskStatus.WORKING
        t.pid = 99999
        store.put_task(t)
        out["total_cost_usd"] = 0.75
        out["result"] = "Second run\n[STATUS: DONE]"
        store.output_path("t-cost").write_text(json.dumps(out))

        runner._try_reap(t)
        updated = store.get_task("t-cost")
        assert updated is not None
        assert updated.cost_usd == pytest.approx(2.0)

    def test_reap_cost_defaults_to_zero(self, store: Store, runner: Runner) -> None:
        """Output without total_cost_usd should not break accumulation."""
        t = Task(name="t-no-cost", prompt="p", status=TaskStatus.WORKING, pid=99999)
        store.put_task(t)
        out = {"session_id": "sid-nc", "result": "Done\n[STATUS: DONE]", "is_error": False}
        store.output_path("t-no-cost").write_text(json.dumps(out))

        runner._try_reap(t)
        updated = store.get_task("t-no-cost")
        assert updated is not None
        assert updated.cost_usd == 0.0

    def test_reap_empty_result_no_log(self, store: Store, runner: Runner) -> None:
        t = Task(name="t7", prompt="p", status=TaskStatus.WORKING, pid=99999)
        store.put_task(t)
        out = {"session_id": "sid-7", "result": "", "is_error": False}
        store.output_path("t7").write_text(json.dumps(out))

        runner._try_reap(t)
        logs = store.read_logs("t7")
        assert len(logs) == 0

    def test_reap_sets_one_liner_on_agent_finished(
        self, store: Store, runner: Runner,
    ) -> None:
        t = Task(name="ol-fin", prompt="p", status=TaskStatus.WORKING, pid=99999)
        store.put_task(t)
        store.append_log("ol-fin", "user", "please summarize this")
        out = {
            "session_id": "sid-ol",
            "result": "Sure, all set.\n[STATUS: DONE]",
            "is_error": False,
        }
        store.output_path("ol-fin").write_text(json.dumps(out))

        with patch(
            "ilan.runner.generate_one_liner",
            return_value="Summary done.",
        ) as mock_gen:
            runner._try_reap(t)

        updated = store.get_task("ol-fin")
        assert updated is not None
        assert updated.summary_one_liner == "Summary done."
        last_user, last_assistant = mock_gen.call_args[0]
        assert last_user == "please summarize this"
        assert last_assistant.startswith("Sure, all set.")

    def test_reap_sets_one_liner_on_needs_attention(
        self, store: Store, runner: Runner,
    ) -> None:
        t = Task(name="ol-na", prompt="p", status=TaskStatus.WORKING, pid=99999)
        store.put_task(t)
        out = {
            "session_id": "sid-ol2",
            "result": "I am stuck.\n[STATUS: NEEDS_ATTENTION]",
            "is_error": False,
        }
        store.output_path("ol-na").write_text(json.dumps(out))

        with patch(
            "ilan.runner.generate_one_liner", return_value="Blocked on input.",
        ):
            runner._try_reap(t)

        updated = store.get_task("ol-na")
        assert updated is not None
        assert updated.status == TaskStatus.NEEDS_ATTENTION
        assert updated.summary_one_liner == "Blocked on input."

    def test_reap_skips_one_liner_on_error(
        self, store: Store, runner: Runner,
    ) -> None:
        t = Task(name="ol-err", prompt="p", status=TaskStatus.WORKING, pid=99999)
        store.put_task(t)
        out = {"session_id": "sid-ole", "result": "boom", "is_error": True}
        store.output_path("ol-err").write_text(json.dumps(out))

        with patch("ilan.runner.generate_one_liner") as mock_gen:
            runner._try_reap(t)

        mock_gen.assert_not_called()
        updated = store.get_task("ol-err")
        assert updated is not None
        assert updated.summary_one_liner is None

    def test_reap_one_liner_none_when_not_configured(
        self, store: Store, runner: Runner,
    ) -> None:
        t = Task(name="ol-noapi", prompt="p", status=TaskStatus.WORKING, pid=99999)
        store.put_task(t)
        out = {
            "session_id": "sid-ol3",
            "result": "All good.\n[STATUS: DONE]",
            "is_error": False,
        }
        store.output_path("ol-noapi").write_text(json.dumps(out))

        with patch("ilan.runner.generate_one_liner", return_value=None):
            runner._try_reap(t)

        updated = store.get_task("ol-noapi")
        assert updated is not None
        assert updated.summary_one_liner is None
        assert updated.status == TaskStatus.AGENT_FINISHED


# ── reply_to_working ────────────────────────────────────────────────────


class TestReplyToWorking:
    def test_clears_needs_review_after_reap(self, store: Store, runner: Runner) -> None:
        """`ilan re` on a WORKING task must not leave the unread marker on.

        When the running agent is killed mid-turn, ``_try_reap`` parses the
        interrupted output as if the agent had voluntarily finished, which
        flips ``needs_review`` to True. ``reply_to_working`` must clear it
        so the user isn't re-notified about output they're already replying to.
        """
        t = Task(name="re-wk", prompt="p", status=TaskStatus.WORKING,
                 session_id="sid-rw", session_log_path="/fake/sid-rw.jsonl")
        store.put_task(t)
        # Simulate a valid JSON result written by the (soon-to-be-killed) agent.
        out = {"session_id": "sid-rw", "result": "partial output", "is_error": False}
        store.output_path("re-wk").write_text(json.dumps(out))

        with patch.object(Runner, "kill", lambda self, task: None), \
             patch.object(Runner, "_spawn", lambda self, task, prompt, resume: True), \
             patch.object(Runner, "find_session_log",
                          return_value=Path("/fake/sid-rw.jsonl")):
            runner.reply_to_working(t, "new instructions")

        updated = store.get_task("re-wk")
        assert updated is not None
        assert updated.needs_review is False


# ── kill ────────────────────────────────────────────────────────────────


class TestKill:
    def test_sigterm_eperm_is_swallowed_and_pid_cleared(
        self, store: Store, runner: Runner,
    ) -> None:
        """A stored pid we cannot signal must not crash the kill.

        ``os.kill`` raises EPERM when the pid belongs to another user's
        process — the OS recycled the pid after the agent died, or the
        agent was spawned by a server running under a different account.
        ``_pid_alive`` deliberately reports such a pid as alive, so ``kill``
        goes on to SIGTERM it and must swallow the EPERM like it already
        swallows ESRCH; otherwise every reply/kill on the task 500s with
        ``[Errno 1] Operation not permitted``.
        """
        t = Task(name="eperm", prompt="p", status=TaskStatus.WORKING, pid=68563)
        store.put_task(t)

        with patch.object(Runner, "_pid_alive", return_value=True), \
             patch("ilan.runner.os.kill",
                   side_effect=PermissionError(1, "Operation not permitted")):
            runner.kill(t)  # must not raise

        assert t.pid is None


# ── _output_complete ────────────────────────────────────────────────────


class TestOutputComplete:
    def test_valid_json(self, store: Store, runner: Runner) -> None:
        store.output_path("t").write_text('{"ok": true}')
        assert runner._output_complete("t") is True

    def test_empty_file(self, store: Store, runner: Runner) -> None:
        store.output_path("t").write_text("")
        assert runner._output_complete("t") is False

    def test_missing_file(self, runner: Runner) -> None:
        assert runner._output_complete("nonexistent") is False

    def test_invalid_json(self, store: Store, runner: Runner) -> None:
        store.output_path("t").write_text("{broken")
        assert runner._output_complete("t") is False


# ── backend selection ───────────────────────────────────────────────────


class TestBackendSelection:
    def test_default_backends_registered(self, runner: Runner) -> None:
        assert isinstance(runner._backend_for(ENGINE_CLAUDE), ClaudeBackend)
        assert isinstance(runner._backend_for(ENGINE_CODEX), CodexBackend)

    def test_unknown_engine_falls_back_to_claude(self, runner: Runner) -> None:
        assert isinstance(runner._backend_for("mystery"), ClaudeBackend)

    def test_spawn_uses_task_engine_backend(self, store: Store, runner: Runner) -> None:
        """A codex task must be spawned through the CodexBackend command."""
        t = Task(name="codex-spawn", prompt="do it", engine=ENGINE_CODEX,
                 task_hash="abcd1234")
        store.put_task(t)
        captured: dict = {}

        def _fake_popen(cmd, **kwargs):  # type: ignore[no-untyped-def]
            captured["cmd"] = cmd
            raise FileNotFoundError  # short-circuit before real exec

        with patch("ilan.runner.subprocess.Popen", side_effect=_fake_popen):
            runner._spawn(t, "do it", resume=False)

        assert captured["cmd"][:2] == ["codex", "exec"]

    def test_find_session_log_routes_by_engine(self, runner: Runner) -> None:
        with patch.object(CodexBackend, "find_session_log", return_value=Path("/c")) as codex, \
             patch.object(ClaudeBackend, "find_session_log", return_value=Path("/a")) as claude:
            assert runner.find_session_log("sid", ENGINE_CODEX) == Path("/c")
            codex.assert_called_once_with("sid")
            claude.assert_not_called()


# ── _spawn with mock claude ─────────────────────────────────────────────


class TestSpawn:
    def test_spawn_sets_working_status(
        self, store: Store, tmp_workdir: Path, tmp_config: Path,
        env_with_mock_claude: None,
    ) -> None:
        """With mock claude on PATH, _spawn should start a process and set WORKING."""
        import ilan.config as cfg_mod

        cfg_mod.save({**cfg_mod.DEFAULTS, "workdir": str(tmp_workdir)})

        runner = Runner(store)
        t = Task(name="spawn-test", prompt="hello world")
        store.put_task(t)

        ok = runner._spawn(t, "hello world", resume=False)
        assert ok is True
        assert t.status == TaskStatus.WORKING
        assert t.pid is not None

        # Wait for mock claude to finish
        proc = runner._procs.get("spawn-test")
        if proc:
            proc.wait(timeout=5)

    def test_spawn_captures_effort_from_config(
        self, store: Store, tmp_workdir: Path, tmp_config: Path,
        env_with_mock_claude: None,
    ) -> None:
        """_spawn records the configured effort on the task, since neither
        backend's session log carries it."""
        import ilan.config as cfg_mod

        cfg_mod.save({
            **cfg_mod.DEFAULTS,
            "workdir": str(tmp_workdir),
            "effort": "medium",
        })

        runner = Runner(store)
        t = Task(name="spawn-effort", prompt="hello")
        store.put_task(t)

        ok = runner._spawn(t, "hello", resume=False)
        assert ok is True
        assert t.spawn_effort == "medium"
        updated = store.get_task("spawn-effort")
        assert updated is not None
        assert updated.spawn_effort == "medium"

        proc = runner._procs.get("spawn-effort")
        if proc:
            proc.wait(timeout=5)

    def test_spawn_captures_budget_for_the_engine(
        self, store: Store, tmp_workdir: Path, tmp_config: Path,
        env_with_mock_claude: None, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """_spawn resolves the paying account for the task's own engine, so a
        later config change can't retroactively relabel this turn."""
        cfg.save({**cfg.DEFAULTS, "workdir": str(tmp_workdir)})
        seen: list[tuple[str, dict[str, str]]] = []

        def fake_detect(engine: str, env: dict[str, str]) -> str:
            seen.append((engine, env))
            return "Team"

        monkeypatch.setattr(budget, "detect", fake_detect)

        runner = Runner(store)
        t = Task(name="spawn-budget", prompt="hello")
        store.put_task(t)

        ok = runner._spawn(t, "hello", resume=False)
        assert ok is True
        assert len(seen) == 1
        assert seen[0][0] == t.engine
        assert "ANTHROPIC_API_KEY" not in seen[0][1]
        assert t.spawn_budget == "Team"
        updated = store.get_task("spawn-budget")
        assert updated is not None
        assert updated.spawn_budget == "Team"

        proc = runner._procs.get("spawn-budget")
        if proc:
            proc.wait(timeout=5)

    def test_spawn_missing_claude_sets_error(
        self, store: Store, tmp_workdir: Path, tmp_config: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """If 'claude' binary is not on PATH, _spawn sets ERROR."""
        import ilan.config as cfg_mod

        cfg_mod.save({**cfg_mod.DEFAULTS, "workdir": str(tmp_workdir)})
        monkeypatch.setenv("PATH", "/nonexistent")

        runner = Runner(store)
        t = Task(name="no-claude", prompt="test")
        store.put_task(t)

        ok = runner._spawn(t, "test", resume=False)
        assert ok is False
        updated = store.get_task("no-claude")
        assert updated is not None
        assert updated.status == TaskStatus.ERROR

    def test_spawn_resume_does_not_log(
        self, store: Store, tmp_workdir: Path, tmp_config: Path,
        env_with_mock_claude: None,
    ) -> None:
        """Resume spawn should NOT append a user log."""
        import ilan.config as cfg_mod

        cfg_mod.save({**cfg_mod.DEFAULTS, "workdir": str(tmp_workdir)})

        runner = Runner(store)
        t = Task(name="resume-test", prompt="original", session_id="sid-1")
        store.put_task(t)

        runner._spawn(t, "continue", resume=True)
        logs = store.read_logs("resume-test")
        assert len(logs) == 0

        proc = runner._procs.get("resume-test")
        if proc:
            proc.wait(timeout=5)

    def test_spawn_includes_tmux_instruction(
        self, store: Store, tmp_workdir: Path, tmp_config: Path,
        env_with_mock_claude: None,
    ) -> None:
        """When task has a hash, spawn should inject tmux session instruction."""
        import ilan.config as cfg_mod

        cfg_mod.save({**cfg_mod.DEFAULTS, "workdir": str(tmp_workdir)})

        runner = Runner(store)
        t = Task(name="tmux-test", prompt="do work", task_hash="abc12345")
        store.put_task(t)

        with patch("subprocess.Popen") as mock_popen:
            mock_proc = mock_popen.return_value
            mock_proc.pid = 12345
            runner._spawn(t, "do work", resume=False)
            cmd = mock_popen.call_args[0][0]
            assert "TMUX SESSION REQUIREMENT" not in " ".join(cmd)  # never argv
            # The prompt travels via the prompt file, fed to stdin.
            prompt_text = store.prompt_path("tmux-test").read_text()
            assert "abc12345-claude-tmux-test" in prompt_text
            assert "TMUX SESSION REQUIREMENT" in prompt_text

    def test_spawn_no_tmux_instruction_without_hash(
        self, store: Store, tmp_workdir: Path, tmp_config: Path,
        env_with_mock_claude: None,
    ) -> None:
        """When task has no hash, spawn should not inject tmux instruction."""
        import ilan.config as cfg_mod

        cfg_mod.save({**cfg_mod.DEFAULTS, "workdir": str(tmp_workdir)})

        runner = Runner(store)
        t = Task(name="no-hash", prompt="do work")
        store.put_task(t)

        with patch("subprocess.Popen") as mock_popen:
            mock_proc = mock_popen.return_value
            mock_proc.pid = 12345
            runner._spawn(t, "do work", resume=False)
            prompt_text = store.prompt_path("no-hash").read_text()
            assert "TMUX SESSION REQUIREMENT" not in prompt_text

    def test_spawn_uses_task_model_override(
        self, store: Store, tmp_workdir: Path, tmp_config: Path,
        env_with_mock_claude: None,
    ) -> None:
        """A task with a model set (via ilan max) should pass --model <model>."""
        import ilan.config as cfg_mod

        cfg_mod.save({**cfg_mod.DEFAULTS, "workdir": str(tmp_workdir),
                      "model-claude": "claude-opus-4-7"})

        runner = Runner(store)
        t = Task(name="model-override", prompt="do work", model="claude-fable-5-1")
        store.put_task(t)

        with patch("subprocess.Popen") as mock_popen:
            mock_proc = mock_popen.return_value
            mock_proc.pid = 12345
            runner._spawn(t, "do work", resume=False)
            cmd = mock_popen.call_args[0][0]
            assert "--model" in cmd
            assert cmd[cmd.index("--model") + 1] == "claude-fable-5-1"

    def test_spawn_falls_back_to_config_model(
        self, store: Store, tmp_workdir: Path, tmp_config: Path,
        env_with_mock_claude: None,
    ) -> None:
        """A task without a model override should use the configured default."""
        import ilan.config as cfg_mod

        cfg_mod.save({**cfg_mod.DEFAULTS, "workdir": str(tmp_workdir),
                      "model-claude": "claude-sonnet-4-6"})

        runner = Runner(store)
        t = Task(name="model-default", prompt="do work")
        store.put_task(t)

        with patch("subprocess.Popen") as mock_popen:
            mock_proc = mock_popen.return_value
            mock_proc.pid = 12345
            runner._spawn(t, "do work", resume=False)
            cmd = mock_popen.call_args[0][0]
            assert "--model" in cmd
            assert cmd[cmd.index("--model") + 1] == "claude-sonnet-4-6"

    def test_spawn_feeds_prompt_via_stdin_and_captures_stderr(
        self, store: Store, tmp_workdir: Path, tmp_config: Path,
        env_with_mock_claude: None,
    ) -> None:
        """The prompt must reach the CLI on stdin (argv prompts segfault
        codex-cli at ~1 MB and can exceed ARG_MAX) and stderr must land in a
        per-task file so startup failures stay diagnosable."""
        import ilan.config as cfg_mod

        cfg_mod.save({**cfg_mod.DEFAULTS, "workdir": str(tmp_workdir)})

        runner = Runner(store)
        t = Task(name="stdin-test", prompt="p")
        store.put_task(t)

        with patch("subprocess.Popen") as mock_popen:
            mock_popen.return_value.pid = 12345
            runner._spawn(t, "the actual prompt", resume=False)
            kwargs = mock_popen.call_args.kwargs
            assert kwargs["stdin"].name == str(store.prompt_path("stdin-test"))
            assert kwargs["stderr"].name == str(store.stderr_path("stdin-test"))

        prompt_text = store.prompt_path("stdin-test").read_text()
        assert prompt_text.startswith("the actual prompt")
        assert prompt_text.endswith(STATUS_SUFFIX)

    def test_spawn_failure_preserves_pending_catchup_state(
        self, store: Store, tmp_workdir: Path, tmp_config: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A spawn that never exec'd must not persist consumed prompt state:
        the stored task keeps awaiting_catchup / cached_replies so the retry
        rebuilds the same prompt instead of silently dropping messages."""
        import ilan.config as cfg_mod

        cfg_mod.save({**cfg_mod.DEFAULTS, "workdir": str(tmp_workdir)})
        monkeypatch.setenv("PATH", "/nonexistent")

        runner = Runner(store)
        t = Task(name="fail-spawn", prompt="p", engine=ENGINE_CODEX,
                 awaiting_catchup=True, cached_replies=["finish it"])
        store.put_task(t)
        store.append_log("fail-spawn", "user", "p")
        store.append_log("fail-spawn", "user", "finish it")

        prompt, resume = runner._build_prompt(t)  # consumes cached_replies in-memory
        assert t.cached_replies == []
        ok = runner._spawn(t, prompt, resume=resume)
        assert ok is False

        stored = store.get_task("fail-spawn")
        assert stored is not None
        assert stored.status == TaskStatus.ERROR
        assert stored.awaiting_catchup is True
        assert stored.cached_replies == ["finish it"]


# ── start ───────────────────────────────────────────────────────────────


class TestStart:
    def test_start_spawns_all_tasks_immediately(
        self, store: Store, tmp_workdir: Path, tmp_config: Path,
        env_with_mock_claude: None,
    ) -> None:
        """Every task spawns the moment start() is called — there is no
        concurrency cap and no UNCLAIMED holding pen."""
        import ilan.config as cfg_mod

        cfg_mod.save({**cfg_mod.DEFAULTS, "workdir": str(tmp_workdir)})

        runner = Runner(store)
        for i in range(3):
            t = Task(name=f"start-{i}", prompt=f"task {i}", created_at=f"2025-01-0{i+1}T00:00:00+00:00")
            store.put_task(t)
            assert runner.start(t) is True

        tasks = store.load_tasks()
        working = [t for t in tasks.values() if t.status == TaskStatus.WORKING]
        assert len(working) == 3
        assert all(t.pid is not None for t in working)

        # Clean up
        for proc in runner._procs.values():
            proc.wait(timeout=5)


# ── _render_catchup ─────────────────────────────────────────────────────


class TestRenderCatchup:
    def _entries(self) -> list:
        return [LogEntry.now("user", "hello"), LogEntry.now("assistant", "hi there")]

    def test_fresh_header_and_content(self) -> None:
        out = _render_catchup(self._entries(), fresh=True)
        assert "taking over" in out.lower()
        assert "hello" in out and "hi there" in out
        assert "[User]" in out and "[Assistant]" in out

    def test_resume_header(self) -> None:
        out = _render_catchup(self._entries(), fresh=False)
        assert "while you were away" in out.lower()
        assert "hello" in out and "hi there" in out

    def test_caps_size_keeping_newest_turns(self) -> None:
        """Oversized history is truncated from the OLDEST end: a ~1 MB argv
        segfaults codex-cli and codex rejects stdin prompts over 1 MiB."""
        turn = "x" * 10_000
        entries = [
            LogEntry.now("user", f"turn-{i:04d} {turn}") for i in range(100)
        ]
        out = _render_catchup(entries, fresh=True)
        assert len(out) < _CATCHUP_MAX_CHARS + 20_000  # cap + header slack
        assert "turn-0099" in out  # newest kept
        assert "turn-0000" not in out  # oldest dropped
        assert "earlier turn(s) omitted" in out

    def test_no_omission_note_when_under_cap(self) -> None:
        out = _render_catchup(self._entries(), fresh=True)
        assert "omitted" not in out

    def test_single_oversized_turn_still_kept(self) -> None:
        """One turn larger than the cap must not render an empty history."""
        entries = [LogEntry.now("user", "y" * (_CATCHUP_MAX_CHARS + 1))]
        out = _render_catchup(entries, fresh=True)
        assert "yyy" in out
        assert "omitted" not in out


# ── branch notice ────────────────────────────────────────────────────────


class TestBranchNotice:
    """A branched child inherits its parent's conversation verbatim. Without a
    notice it reads the parent's in-flight instructions as its own — and, since
    every inherited turn ends in the parent's TMUX SESSION REQUIREMENT block,
    it sees the parent's hash far more often than the one hash that is its."""

    def _child(self, **kw) -> Task:
        return Task(
            name="child", prompt="root prompt", parent_name="parent",
            awaiting_branch_notice=True, **kw,
        )

    def test_claude_branch_puts_notice_before_the_assignment(
        self, runner: Runner,
    ) -> None:
        t = self._child(session_id="sid-c", cached_replies=["do the new thing"])
        with patch.object(Runner, "find_session_log",
                          return_value=Path("/fake/sid-c.jsonl")):
            prompt, resume = runner._build_prompt(t)

        assert resume is True
        assert "BRANCHED FROM `parent`" in prompt
        assert "REFERENCE CONTEXT ONLY" in prompt
        assert prompt.index("BRANCHED FROM") < prompt.index("do the new thing")

    def test_notice_disowns_the_inherited_tmux_prefix(self) -> None:
        notice = _branch_notice("parent", assignment_below=True)
        assert "task hash or tmux session prefix" in notice
        assert "belongs to `parent`" in notice
        # Points the child at the one block that carries its own hash.
        assert "TMUX SESSION REQUIREMENT" in notice

    def test_notice_without_a_parent_name(self) -> None:
        assert "another task" in _branch_notice(None, assignment_below=True)

    def test_no_notice_when_flag_unset(self, runner: Runner) -> None:
        t = Task(name="plain", prompt="root", session_id="sid-p",
                 parent_name="parent", cached_replies=["carry on"])
        with patch.object(Runner, "find_session_log",
                          return_value=Path("/fake/sid-p.jsonl")):
            prompt, _ = runner._build_prompt(t)
        assert "BRANCHED FROM" not in prompt

    def test_codex_branch_notice_replaces_the_continue_footer(
        self, store: Store, runner: Runner,
    ) -> None:
        """A codex child has no forkable session, so its history arrives as a
        rendered transcript. The 'taking over / continue the work' framing is
        exactly wrong there, and the notice must land last — after the history,
        which ends with the child's own assignment."""
        t = self._child(engine=ENGINE_CODEX, awaiting_catchup=True,
                        gist_branch_point=2)
        store.put_task(t)
        store.append_log("child", "user", "inherited question")
        store.append_log("child", "assistant", "inherited answer")
        store.append_log("child", "user", "do the new thing")

        prompt, resume = runner._build_prompt(t)

        assert resume is False
        assert "taking over" not in prompt.lower()
        assert "inherited as background context" in prompt
        assert "Please continue working on this task." not in prompt
        assert "inherited answer" in prompt
        assert prompt.index("END CONVERSATION HISTORY") < prompt.index(
            "BRANCHED FROM `parent`"
        )
        assert "final user message in the history above" in prompt
        # The divider and the notice coexist on the first spawn: the divider
        # walls off the inherited prefix, the notice names the assignment.
        assert (
            prompt.index("inherited answer")
            < prompt.index("BRANCH POINT: inherited history ends here")
            < prompt.index("do the new thing")
            < prompt.index("BRANCHED FROM `parent`")
        )

    def test_spawned_prompt_ends_with_the_childs_own_hash(
        self, store: Store, tmp_workdir: Path, tmp_config: Path,
    ) -> None:
        """End-to-end ordering: the notice tells the child to ignore the
        inherited prefix, and the block naming its real one comes after."""
        import ilan.config as cfg_mod

        cfg_mod.save({**cfg_mod.DEFAULTS, "workdir": str(tmp_workdir)})
        runner = Runner(store)
        t = self._child(session_id="sid-c", task_hash="cafe1234",
                        cached_replies=["do the new thing"])
        store.put_task(t)

        with patch.object(Runner, "find_session_log",
                          return_value=Path("/fake/sid-c.jsonl")), \
             patch("subprocess.Popen") as mock_popen:
            mock_popen.return_value.pid = 12345
            assert runner.start(t) is True

        text = store.prompt_path("child").read_text()
        assert text.index("BRANCHED FROM `parent`") < text.index("cafe1234")

    def test_spawn_failure_preserves_pending_notice(
        self, store: Store, tmp_workdir: Path, tmp_config: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A spawn that never exec'd must not burn the notice: the retry has to
        rebuild it, or the child silently loses its separation instruction."""
        import ilan.config as cfg_mod

        cfg_mod.save({**cfg_mod.DEFAULTS, "workdir": str(tmp_workdir)})
        monkeypatch.setenv("PATH", "/nonexistent")

        runner = Runner(store)
        t = self._child(session_id="sid-c", cached_replies=["do the new thing"])
        store.put_task(t)

        with patch.object(Runner, "find_session_log",
                          return_value=Path("/fake/sid-c.jsonl")):
            assert runner.start(t) is False

        stored = store.get_task("child")
        assert stored is not None
        assert stored.awaiting_branch_notice is True
        assert stored.cached_replies == ["do the new thing"]


# ── branch divider ───────────────────────────────────────────────────────


class TestBranchDivider:
    """The one-shot branch notice covers only the first prompt. Every later
    replay of the unified log (backend switch, lost-session reseed) must still
    mark where the inherited prefix ends, or the parent's turns read as this
    task's own history under a 'continue the work' framing."""

    def _branched(self, **kw) -> Task:
        # Notice already spent: this models a branched task well past its
        # first turn, which is exactly when replays used to lose the boundary.
        return Task(
            name="child", prompt="root prompt", parent_name="parent",
            gist_branch_point=2, **kw,
        )

    def _seed_log(self, store: Store) -> None:
        store.append_log("child", "user", "inherited question")
        store.append_log("child", "assistant", "inherited answer")
        store.append_log("child", "user", "own question")
        store.append_log("child", "assistant", "own answer")

    def test_switch_keeps_both_boundaries(
        self, store: Store, runner: Runner,
    ) -> None:
        """Switch semantics (continue this task) live in the header/footer;
        branch semantics (the prefix belongs to the parent) in the divider."""
        t = self._branched(engine=ENGINE_CODEX, awaiting_catchup=True)
        store.put_task(t)
        self._seed_log(store)

        prompt, resume = runner._build_prompt(t)

        assert resume is False
        assert "taking over" in prompt.lower()
        assert "Please continue working on this task." in prompt
        assert (
            prompt.index("inherited answer")
            < prompt.index("BRANCH POINT: inherited history ends here")
            < prompt.index("own question")
        )
        # No pending notice, so no assignment framing — just the divider.
        assert "BRANCHED FROM" not in prompt

    def test_no_divider_for_a_never_branched_switch(
        self, store: Store, runner: Runner,
    ) -> None:
        t = Task(name="child", prompt="root prompt", engine=ENGINE_CODEX,
                 awaiting_catchup=True)
        store.put_task(t)
        self._seed_log(store)

        prompt, _ = runner._build_prompt(t)

        assert "BRANCH POINT" not in prompt

    def test_lost_session_reseed_keeps_the_divider(
        self, store: Store, runner: Runner,
    ) -> None:
        """A vanished session log reseeds a fresh session from the whole
        unified log — the same full replay a switch does, same boundary."""
        t = self._branched(session_id="sid-gone",
                           log_cursors={ENGINE_CLAUDE: 4})
        store.put_task(t)
        self._seed_log(store)

        with patch.object(Runner, "find_session_log", return_value=None):
            prompt, resume = runner._build_prompt(t)

        assert resume is False
        assert "taking over" in prompt.lower()
        assert (
            prompt.index("inherited answer")
            < prompt.index("BRANCH POINT: inherited history ends here")
            < prompt.index("own question")
        )

    def test_no_divider_when_the_slice_is_purely_inherited(
        self, store: Store, runner: Runner,
    ) -> None:
        """Defensive: every branch logs its assignment at branch time, so no
        rendered slice should ever be purely inherited — but if one is, a
        divider claiming 'the turns below define this task's work' must not
        sit above zero turns, fighting the continue-the-work footer."""
        t = self._branched(engine=ENGINE_CODEX, awaiting_catchup=True)
        store.put_task(t)
        store.append_log("child", "user", "inherited question")
        store.append_log("child", "assistant", "inherited answer")

        prompt, resume = runner._build_prompt(t)

        assert resume is False
        assert "BRANCH POINT" not in prompt
        assert "taking over" in prompt.lower()
        assert "Please continue working on this task." in prompt

    def test_no_divider_when_resuming_past_the_branch_point(
        self, store: Store, runner: Runner,
    ) -> None:
        """A catch-up slice that starts after the branch point contains none
        of the parent's turns, so there is no prefix to wall off."""
        t = self._branched(engine=ENGINE_CODEX, awaiting_catchup=True,
                           session_id="sid-x",
                           log_cursors={ENGINE_CODEX: 3})
        store.put_task(t)
        self._seed_log(store)

        with patch.object(Runner, "find_session_log",
                          return_value=Path("/fake/sid-x.jsonl")):
            prompt, resume = runner._build_prompt(t)

        assert resume is True
        assert "while you were away" in prompt.lower()
        assert "BRANCH POINT" not in prompt
        assert "inherited answer" not in prompt  # slice starts past it

    def test_divider_dropped_with_a_fully_truncated_prefix(self) -> None:
        """When truncation eats the whole inherited prefix there is nothing
        left to separate — a dangling divider atop the child's own turns
        would mislabel them as inherited."""
        entries = [
            LogEntry.now("user", f"inh-{i} " + "x" * 10_000) for i in range(3)
        ] + [
            LogEntry.now("user", f"own-{i} " + "y" * 300_000) for i in range(2)
        ]
        out = _render_catchup(
            entries, fresh=True, inherited_count=3, parent_name="parent",
        )
        assert "earlier turn(s) omitted" in out
        assert "BRANCH POINT" not in out

    def test_divider_survives_mid_prefix_truncation(self) -> None:
        """Dropping only the oldest part of the prefix shifts the divider
        left by the drop count; it must still land between the surviving
        inherited turns and the task's own."""
        entries = [
            LogEntry.now("user", f"inh-{i:03d} " + "x" * 10_000)
            for i in range(60)
        ] + [
            LogEntry.now("user", "own question"),
            LogEntry.now("assistant", "own answer"),
        ]
        out = _render_catchup(
            entries, fresh=True, inherited_count=60, parent_name="parent",
        )
        assert "earlier turn(s) omitted" in out
        assert "inh-000" not in out  # oldest inherited dropped
        assert "inh-059" in out  # newest inherited kept
        assert (
            out.index("inh-059")
            < out.index("BRANCH POINT: inherited history ends here")
            < out.index("own question")
        )

    def test_divider_names_the_parent(self) -> None:
        divider = _branch_divider("parent")
        assert "inherited from `parent`" in divider
        assert "reference context" in divider
        assert "the parent task" in _branch_divider(None)


# ── _try_reap: cursor + session map ──────────────────────────────────────


class TestReapCursor:
    def test_reap_advances_cursor_and_mirrors_session(
        self, store: Store, runner: Runner
    ) -> None:
        t = Task(name="rc", prompt="p", status=TaskStatus.WORKING, pid=99999,
                 engine=ENGINE_CLAUDE)
        store.put_task(t)
        store.append_log("rc", "user", "p")  # pre-existing user turn
        out = {"session_id": "sid-rc", "result": "done\n[STATUS: DONE]", "is_error": False}
        store.output_path("rc").write_text(json.dumps(out))

        with patch.object(Runner, "find_session_log",
                          return_value=Path("/fake/sid-rc.jsonl")):
            runner._try_reap(t)

        updated = store.get_task("rc")
        assert updated is not None
        assert updated.sessions[ENGINE_CLAUDE] == "sid-rc"
        # user + newly-appended assistant turn
        assert updated.log_cursors[ENGINE_CLAUDE] == 2

    def test_reap_clears_awaiting_catchup(self, store: Store, runner: Runner) -> None:
        """Pending catch-up is consumed only here — after the turn completed."""
        t = Task(name="rc2", prompt="p", status=TaskStatus.WORKING, pid=99999,
                 engine=ENGINE_CODEX, awaiting_catchup=True)
        store.put_task(t)
        store.append_log("rc2", "user", "p")
        out = "\n".join([
            json.dumps({"type": "thread.started", "thread_id": "codex-sid"}),
            json.dumps({"type": "item.completed",
                        "item": {"type": "agent_message",
                                 "text": "caught up\n[STATUS: DONE]"}}),
            json.dumps({
                "type": "turn.completed",
                "usage": {
                    "input_tokens": 100,
                    "cached_input_tokens": 70,
                    "output_tokens": 9,
                },
            }),
        ]) + "\n"
        store.output_path("rc2").write_text(out)

        with patch.object(Runner, "find_session_log",
                          return_value=Path("/fake/rollout-codex-sid.jsonl")), \
             patch.object(CodexBackend, "last_assistant_model", return_value=None):
            runner._try_reap(t)

        updated = store.get_task("rc2")
        assert updated is not None
        assert updated.awaiting_catchup is False
        assert updated.log_cursors[ENGINE_CODEX] == 2
        reply = store.read_logs("rc2")[-1]
        assert reply.input_tokens is None
        assert reply.output_tokens is None
        assert reply.cache_read_input_tokens is None

    def test_reap_clears_branch_notice(self, store: Store, runner: Runner) -> None:
        """The notice is one-shot: spent here, so the child's second turn is a
        normal turn rather than a repeated 'you are a new task' preamble."""
        t = Task(name="rc3", prompt="p", status=TaskStatus.WORKING, pid=99999,
                 engine=ENGINE_CLAUDE, parent_name="parent",
                 awaiting_branch_notice=True)
        store.put_task(t)
        store.append_log("rc3", "user", "do the new thing")
        out = {"session_id": "sid-rc3", "result": "done\n[STATUS: DONE]",
               "is_error": False}
        store.output_path("rc3").write_text(json.dumps(out))

        with patch.object(Runner, "find_session_log",
                          return_value=Path("/fake/sid-rc3.jsonl")):
            runner._try_reap(t)

        updated = store.get_task("rc3")
        assert updated is not None
        assert updated.awaiting_branch_notice is False


# ── switch_engine ────────────────────────────────────────────────────────


class TestSwitchEngine:
    @pytest.mark.parametrize(
        ("source_engine", "source_model", "target_engine", "expected_model"),
        [
            (ENGINE_CLAUDE, model, ENGINE_CODEX, ASTRA_MODEL)
            for model in (FABLE_MODEL, *LEGACY_FABLE_MODELS)
        ] + [
            (ENGINE_CODEX, model, ENGINE_CLAUDE, FABLE_MODEL)
            for model in (ASTRA_MODEL, *LEGACY_ASTRA_MODELS)
        ] + [
            # Old switches could leave a Fable pin on a Codex task.
            (ENGINE_CODEX, "claude-fable-5", ENGINE_CLAUDE, FABLE_MODEL),
        ],
    )
    @pytest.mark.parametrize("resume", [False, True])
    def test_max_model_reaches_spawn_after_switch(
        self, store: Store, runner: Runner, tmp_config: Path, tmp_workdir: Path,
        source_engine: str, source_model: str, target_engine: str,
        expected_model: str, resume: bool,
    ) -> None:
        """Persist the translated pin and pass it to the real backend builder."""
        cfg.save({**cfg.DEFAULTS, "workdir": str(tmp_workdir)})
        task = Task(
            name="switch-max", prompt="p", engine=source_engine,
            model=source_model, status=TaskStatus.AGENT_FINISHED,
            session_id="source-session",
            sessions={target_engine: "target-session"} if resume else {},
        )
        store.put_task(task)

        runner.switch_engine(task, target_engine)

        stored = store.get_task(task.name)
        assert stored is not None
        assert stored.engine == target_engine
        assert stored.model == expected_model
        assert stored.sessions[source_engine] == "source-session"
        assert stored.session_id == ("target-session" if resume else None)
        with (
            patch("ilan.runner.subprocess.Popen") as popen,
            patch("ilan.runner.budget.detect", return_value=None),
        ):
            popen.return_value.pid = 12345
            assert runner._spawn(stored, "continue", resume=resume)
        cmd = popen.call_args.args[0]
        assert cmd[0] == target_engine
        assert cmd[cmd.index("--model") + 1] == expected_model
        assert ("target-session" in cmd) is resume

    @pytest.mark.parametrize(
        ("source_engine", "target_engine"),
        [(ENGINE_CLAUDE, ENGINE_CODEX), (ENGINE_CODEX, ENGINE_CLAUDE)],
    )
    @pytest.mark.parametrize("model", [None, "custom-model"])
    def test_switch_preserves_non_max_model(
        self, store: Store, runner: Runner, source_engine: str,
        target_engine: str, model: str | None,
    ) -> None:
        task = Task(name="switch-plain", prompt="p", engine=source_engine, model=model)
        store.put_task(task)
        runner.switch_engine(task, target_engine)
        stored = store.get_task(task.name)
        assert stored is not None
        assert stored.engine == target_engine
        assert stored.model == model

    def test_noop_when_same_engine(self, store: Store, runner: Runner) -> None:
        t = Task(name="s1", prompt="p", engine=ENGINE_CLAUDE, session_id="a")
        store.put_task(t)
        runner.switch_engine(t, ENGINE_CLAUDE)
        assert t.engine == ENGINE_CLAUDE
        assert t.session_id == "a"
        assert t.awaiting_catchup is False

    def test_flips_engine_and_restores_target_session(
        self, store: Store, runner: Runner
    ) -> None:
        t = Task(name="s2", prompt="p", engine=ENGINE_CLAUDE, session_id="claude-sid",
                 sessions={"claude": "claude-sid", "codex": "codex-sid"})
        store.append_log("s2", "user", "p")
        store.put_task(t)
        runner.switch_engine(t, ENGINE_CODEX)
        assert t.engine == ENGINE_CODEX
        assert t.session_id == "codex-sid"
        assert t.sessions["claude"] == "claude-sid"
        assert t.session_log_path is None

    def test_sets_awaiting_catchup_when_target_behind(
        self, store: Store, runner: Runner
    ) -> None:
        t = Task(name="s3", prompt="p", engine=ENGINE_CLAUDE, session_id="c",
                 sessions={"claude": "c"}, log_cursors={"claude": 2})
        store.append_log("s3", "user", "p")
        store.append_log("s3", "assistant", "a")
        store.put_task(t)
        runner.switch_engine(t, ENGINE_CODEX)  # codex cursor 0 < len 2
        assert t.awaiting_catchup is True

    def test_no_catchup_when_no_history(self, store: Store, runner: Runner) -> None:
        t = Task(name="s4", prompt="p", engine=ENGINE_CLAUDE)
        store.put_task(t)  # empty log
        runner.switch_engine(t, ENGINE_CODEX)
        assert t.awaiting_catchup is False

    def test_no_catchup_when_target_already_current(
        self, store: Store, runner: Runner
    ) -> None:
        t = Task(name="s5", prompt="p", engine=ENGINE_CLAUDE, session_id="c",
                 sessions={"claude": "c", "codex": "x"},
                 log_cursors={"claude": 2, "codex": 2})
        store.append_log("s5", "user", "p")
        store.append_log("s5", "assistant", "a")
        store.put_task(t)
        runner.switch_engine(t, ENGINE_CODEX)  # codex cursor 2 == len 2
        assert t.awaiting_catchup is False

    def test_roundtrip_preserves_both_sessions(
        self, store: Store, runner: Runner
    ) -> None:
        t = Task(name="s6", prompt="p", engine=ENGINE_CLAUDE, session_id="c",
                 sessions={"claude": "c"})
        store.append_log("s6", "user", "p")
        store.put_task(t)
        runner.switch_engine(t, ENGINE_CODEX)
        assert t.session_id is None  # codex never ran
        # Simulate a codex reap establishing its own session.
        t.set_session_for(ENGINE_CODEX, "codex-new")
        t.session_id = "codex-new"
        runner.switch_engine(t, ENGINE_CLAUDE)
        assert t.session_id == "c"
        assert t.sessions[ENGINE_CODEX] == "codex-new"


# ── _build_catchup_prompt ────────────────────────────────────────────────


class TestCatchupPrompt:
    def test_fresh_session_renders_full_transcript(
        self, store: Store, runner: Runner
    ) -> None:
        t = Task(name="cp1", prompt="orig", engine=ENGINE_CODEX, awaiting_catchup=True)
        store.append_log("cp1", "user", "orig")
        store.append_log("cp1", "assistant", "did step 1")
        store.append_log("cp1", "user", "keep going")
        store.put_task(t)
        prompt, resume = runner._build_prompt(t)
        assert resume is False
        assert "did step 1" in prompt and "keep going" in prompt
        assert "full conversation" in prompt.lower()
        # Catch-up state is only consumed at reap time, after the turn
        # verifiably completed — a failed spawn must not lose the catch-up.
        assert t.awaiting_catchup is True
        assert t.log_cursors.get(ENGINE_CODEX, 0) == 0

    def test_resumed_session_injects_only_interim(
        self, store: Store, runner: Runner
    ) -> None:
        t = Task(name="cp2", prompt="orig", engine=ENGINE_CLAUDE, session_id="claude-sid",
                 awaiting_catchup=True, sessions={"claude": "claude-sid"},
                 log_cursors={"claude": 1})
        store.append_log("cp2", "user", "orig")            # index 0 (seen)
        store.append_log("cp2", "assistant", "other work")  # index 1
        store.append_log("cp2", "user", "please merge")     # index 2
        store.put_task(t)
        with patch.object(Runner, "find_session_log",
                          return_value=Path("/fake/claude-sid.jsonl")):
            prompt, resume = runner._build_prompt(t)
        assert resume is True
        assert "other work" in prompt and "please merge" in prompt
        assert "orig" not in prompt  # already seen
        assert t.log_cursors["claude"] == 1  # advanced only at reap

    def test_noop_when_caught_up_falls_back_to_continue(
        self, store: Store, runner: Runner
    ) -> None:
        t = Task(name="cp3", prompt="orig", engine=ENGINE_CLAUDE, session_id="sid",
                 awaiting_catchup=True, sessions={"claude": "sid"},
                 log_cursors={"claude": 2})
        store.append_log("cp3", "user", "orig")
        store.append_log("cp3", "assistant", "done")
        store.put_task(t)
        with patch.object(Runner, "find_session_log", return_value=Path("/x")):
            prompt, resume = runner._build_prompt(t)
        assert resume is True
        assert prompt == "Please continue working on this task."
        assert t.awaiting_catchup is True  # cleared only when the turn reaps

    def test_clears_cached_replies(self, store: Store, runner: Runner) -> None:
        t = Task(name="cp4", prompt="orig", engine=ENGINE_CODEX, awaiting_catchup=True,
                 cached_replies=["finish it"])
        store.append_log("cp4", "user", "orig")
        store.append_log("cp4", "assistant", "half done")
        store.append_log("cp4", "user", "finish it")
        store.put_task(t)
        _, _ = runner._build_prompt(t)
        assert t.cached_replies == []

    def test_lazy_switch_then_start_seeds_fresh_codex(
        self, store: Store, runner: Runner
    ) -> None:
        """End-to-end: Claude finishes, user switches to Codex and replies; the
        next spawn seeds a fresh Codex session with the whole transcript."""
        t = Task(name="e2e", prompt="build X", engine=ENGINE_CLAUDE,
                 session_id="claude-sid", sessions={"claude": "claude-sid"},
                 log_cursors={"claude": 2}, status=TaskStatus.AGENT_FINISHED)
        store.append_log("e2e", "user", "build X")
        store.append_log("e2e", "assistant", "built half of X")
        store.put_task(t)

        runner.switch_engine(t, ENGINE_CODEX)
        assert t.awaiting_catchup is True

        # Server appends the reply to the log and caches it before starting.
        store.append_log("e2e", "user", "finish it with codex")
        t.cached_replies = ["finish it with codex"]

        prompt, resume = runner._build_prompt(t)
        assert resume is False
        assert "built half of X" in prompt
        assert "finish it with codex" in prompt
        assert t.cached_replies == []
        # Still pending until the codex turn actually completes and reaps.
        assert t.awaiting_catchup is True
        assert t.log_cursors.get(ENGINE_CODEX, 0) == 0

    def test_fresh_switch_spawn_does_not_reappend_history(
        self, store: Store, tmp_workdir: Path, tmp_config: Path,
        env_with_mock_claude: None,
    ) -> None:
        cfg.save({**cfg.DEFAULTS, "workdir": str(tmp_workdir)})

        runner = Runner(store)
        t = Task(name="fs", prompt="orig")
        store.put_task(t)
        store.append_log("fs", "user", "orig")
        store.append_log("fs", "assistant", "a")

        runner._spawn(t, "catch-up text", resume=False)
        assert len(store.read_logs("fs")) == 2  # history not duplicated

        proc = runner._procs.get("fs")
        if proc:
            proc.wait(timeout=5)
