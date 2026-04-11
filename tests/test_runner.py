"""Tests for ilan.runner — status parsing, prompt building, spawn/reap."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from ilan.models import Task, TaskStatus
from ilan.runner import Runner, STATUS_SUFFIX
from ilan.store import Store


@pytest.fixture()
def store(tmp_workdir: Path) -> Store:
    return Store(tmp_workdir)


@pytest.fixture()
def runner(store: Store) -> Runner:
    return Runner(store)


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
        with patch.object(Runner, "_find_session_log", return_value=Path("/fake/sid-1.jsonl")):
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
        with patch.object(Runner, "_find_session_log", return_value=Path("/fake/sid-1.jsonl")):
            prompt, resume = runner._build_prompt(t)
        assert "r1" in prompt
        assert "r2" in prompt
        assert resume is True
        assert t.cached_replies == []

    def test_multiple_cached_replies_joined(self, runner: Runner) -> None:
        t = Task(name="t", prompt="Do X", cached_replies=["a", "b", "c"])
        prompt, resume = runner._build_prompt(t)
        assert "a\n\nb\n\nc" in prompt


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

        with patch.object(Runner, "_find_session_log", return_value=Path("/fake/sid-1.jsonl")):
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
        out = {"session_id": "sid-6", "result": "All good\n[STATUS: DONE]", "is_error": False}
        store.output_path("t6").write_text(json.dumps(out))

        runner._try_reap(t)
        logs = store.read_logs("t6")
        assert len(logs) == 1
        assert logs[0].role == "assistant"
        assert "All good" in logs[0].content

    def test_reap_empty_result_no_log(self, store: Store, runner: Runner) -> None:
        t = Task(name="t7", prompt="p", status=TaskStatus.WORKING, pid=99999)
        store.put_task(t)
        out = {"session_id": "sid-7", "result": "", "is_error": False}
        store.output_path("t7").write_text(json.dumps(out))

        runner._try_reap(t)
        logs = store.read_logs("t7")
        assert len(logs) == 0


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

    def test_spawn_appends_user_log(
        self, store: Store, tmp_workdir: Path, tmp_config: Path,
        env_with_mock_claude: None,
    ) -> None:
        """First spawn (not resume) should log the user prompt."""
        import ilan.config as cfg_mod

        cfg_mod.save({**cfg_mod.DEFAULTS, "workdir": str(tmp_workdir)})

        runner = Runner(store)
        t = Task(name="log-test", prompt="my prompt")
        store.put_task(t)

        runner._spawn(t, "my prompt", resume=False)
        logs = store.read_logs("log-test")
        assert len(logs) == 1
        assert logs[0].role == "user"
        assert logs[0].content == "my prompt"

        proc = runner._procs.get("log-test")
        if proc:
            proc.wait(timeout=5)

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


# ── schedule ────────────────────────────────────────────────────────────


class TestSchedule:
    def test_schedule_claims_unclaimed_tasks(
        self, store: Store, tmp_workdir: Path, tmp_config: Path,
        env_with_mock_claude: None,
    ) -> None:
        import ilan.config as cfg_mod

        cfg_mod.save({**cfg_mod.DEFAULTS, "workdir": str(tmp_workdir), "num-agents": 2})

        runner = Runner(store)
        for i in range(3):
            t = Task(name=f"sched-{i}", prompt=f"task {i}", created_at=f"2025-01-0{i+1}T00:00:00+00:00")
            store.put_task(t)

        runner.schedule()

        tasks = store.load_tasks()
        working = [t for t in tasks.values() if t.status == TaskStatus.WORKING]
        unclaimed = [t for t in tasks.values() if t.status == TaskStatus.UNCLAIMED]
        assert len(working) == 2
        assert len(unclaimed) == 1

        # Clean up
        for proc in runner._procs.values():
            proc.wait(timeout=5)

    def test_schedule_respects_max_agents(
        self, store: Store, tmp_workdir: Path, tmp_config: Path,
        env_with_mock_claude: None,
    ) -> None:
        import ilan.config as cfg_mod

        cfg_mod.save({**cfg_mod.DEFAULTS, "workdir": str(tmp_workdir), "num-agents": 1})

        runner = Runner(store)
        for i in range(3):
            t = Task(name=f"max-{i}", prompt=f"task {i}", created_at=f"2025-01-0{i+1}T00:00:00+00:00")
            store.put_task(t)

        runner.schedule()

        tasks = store.load_tasks()
        working = [t for t in tasks.values() if t.status == TaskStatus.WORKING]
        assert len(working) == 1

        for proc in runner._procs.values():
            proc.wait(timeout=5)
