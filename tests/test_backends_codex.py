"""Unit tests for the CodexBackend adapter.

Fixtures mirror the real ``codex exec --json`` event stream and rollout
transcript schema captured from codex-cli 0.144.5.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ilan.backends.codex import CodexBackend

# One full turn as codex streams it to stdout under --json.
STREAM = "\n".join([
    json.dumps({"type": "thread.started", "thread_id": "019f6ffb-uuid"}),
    json.dumps({"type": "turn.started"}),
    json.dumps({"type": "item.completed",
                "item": {"id": "item_0", "type": "agent_message", "text": "pineapple\n[STATUS: DONE]"}}),
    json.dumps({"type": "turn.completed",
                "usage": {"input_tokens": 12464, "cached_input_tokens": 9984,
                          "output_tokens": 6, "reasoning_output_tokens": 0}}),
]) + "\n"


@pytest.fixture()
def backend() -> CodexBackend:
    return CodexBackend()


class TestBuildCommand:
    def test_fresh_argv_shape(self, backend: CodexBackend) -> None:
        cmd, env = backend.build_command("do it", None, resume=False, session_id=None)
        assert cmd[:2] == ["codex", "exec"]
        assert "resume" not in cmd
        assert "--json" in cmd
        assert "--skip-git-repo-check" in cmd
        assert "--dangerously-bypass-approvals-and-sandbox" in cmd
        assert cmd[-1] == "do it"  # prompt is the positional tail
        assert isinstance(env, dict)

    def test_default_model_when_no_override(self, backend: CodexBackend) -> None:
        cmd, _ = backend.build_command("do it", None, resume=False, session_id=None)
        assert cmd[cmd.index("--model") + 1] == "gpt-5.6-sol"

    def test_resume_inserts_session(self, backend: CodexBackend) -> None:
        cmd, _ = backend.build_command("go on", None, resume=True, session_id="sid-9")
        assert cmd[:4] == ["codex", "exec", "resume", "sid-9"]
        assert cmd[-1] == "go on"

    def test_resume_without_session_id_stays_fresh(self, backend: CodexBackend) -> None:
        cmd, _ = backend.build_command("go on", None, resume=True, session_id=None)
        assert "resume" not in cmd

    def test_model_override_passed(self, backend: CodexBackend) -> None:
        cmd, _ = backend.build_command("x", "gpt-5.6-sol", resume=False, session_id=None)
        assert cmd[cmd.index("--model") + 1] == "gpt-5.6-sol"


class TestParseOutput:
    def test_parses_stream(self, backend: CodexBackend, tmp_path: Path) -> None:
        out = tmp_path / "out.jsonl"
        out.write_text(STREAM)
        parsed = backend.parse_output(out)
        assert parsed is not None
        assert parsed.session_id == "019f6ffb-uuid"
        assert parsed.result_text == "pineapple\n[STATUS: DONE]"
        assert parsed.is_error is False
        # uncached input = 12464 - 9984
        assert parsed.input_tokens == 2480
        assert parsed.cache_read_input_tokens == 9984
        assert parsed.output_tokens == 6
        assert parsed.cost_usd == 0.0

    def test_last_agent_message_wins(self, backend: CodexBackend, tmp_path: Path) -> None:
        out = tmp_path / "out.jsonl"
        out.write_text("\n".join([
            json.dumps({"type": "thread.started", "thread_id": "t"}),
            json.dumps({"type": "item.completed",
                        "item": {"type": "agent_message", "text": "first"}}),
            json.dumps({"type": "item.completed",
                        "item": {"type": "agent_message", "text": "final"}}),
        ]) + "\n")
        parsed = backend.parse_output(out)
        assert parsed is not None
        assert parsed.result_text == "final"

    def test_ignores_non_message_items(self, backend: CodexBackend, tmp_path: Path) -> None:
        out = tmp_path / "out.jsonl"
        out.write_text("\n".join([
            json.dumps({"type": "thread.started", "thread_id": "t"}),
            json.dumps({"type": "item.completed",
                        "item": {"type": "command_execution", "text": "ls"}}),
            json.dumps({"type": "item.completed",
                        "item": {"type": "agent_message", "text": "answer"}}),
        ]) + "\n")
        parsed = backend.parse_output(out)
        assert parsed is not None
        assert parsed.result_text == "answer"

    def test_error_event_sets_is_error(self, backend: CodexBackend, tmp_path: Path) -> None:
        out = tmp_path / "out.jsonl"
        out.write_text("\n".join([
            json.dumps({"type": "thread.started", "thread_id": "t"}),
            json.dumps({"type": "error", "message": "boom"}),
        ]) + "\n")
        parsed = backend.parse_output(out)
        assert parsed is not None
        assert parsed.is_error is True

    def test_skips_malformed_lines(self, backend: CodexBackend, tmp_path: Path) -> None:
        out = tmp_path / "out.jsonl"
        out.write_text(
            "{not json\n"
            + json.dumps({"type": "thread.started", "thread_id": "t"}) + "\n"
            + json.dumps({"type": "item.completed",
                          "item": {"type": "agent_message", "text": "ok"}}) + "\n"
        )
        parsed = backend.parse_output(out)
        assert parsed is not None
        assert parsed.session_id == "t"
        assert parsed.result_text == "ok"

    def test_missing_file_returns_none(self, backend: CodexBackend, tmp_path: Path) -> None:
        assert backend.parse_output(tmp_path / "nope.jsonl") is None

    def test_empty_stream_returns_none(self, backend: CodexBackend, tmp_path: Path) -> None:
        out = tmp_path / "out.jsonl"
        out.write_text("\n\n")
        assert backend.parse_output(out) is None

    def test_accumulates_multiple_turns(self, backend: CodexBackend, tmp_path: Path) -> None:
        out = tmp_path / "out.jsonl"
        out.write_text("\n".join([
            json.dumps({"type": "turn.completed",
                        "usage": {"input_tokens": 100, "cached_input_tokens": 40, "output_tokens": 5}}),
            json.dumps({"type": "turn.completed",
                        "usage": {"input_tokens": 200, "cached_input_tokens": 150, "output_tokens": 7}}),
        ]) + "\n")
        parsed = backend.parse_output(out)
        assert parsed is not None
        assert parsed.input_tokens == 60 + 50
        assert parsed.cache_read_input_tokens == 190
        assert parsed.output_tokens == 12


class TestSessionLog:
    def test_find_session_log(
        self, backend: CodexBackend, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        day = tmp_path / ".codex" / "sessions" / "2026" / "07" / "17"
        day.mkdir(parents=True)
        log = day / "rollout-2026-07-17T05-09-42-019f6ffb-uuid.jsonl"
        log.write_text("{}\n")
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        assert backend.find_session_log("019f6ffb-uuid") == log
        assert backend.find_session_log("missing") is None

    def test_last_assistant_model(self, backend: CodexBackend, tmp_path: Path) -> None:
        log = tmp_path / "rollout.jsonl"
        log.write_text("\n".join([
            json.dumps({"type": "session_meta", "payload": {"session_id": "s"}}),
            json.dumps({"type": "turn_context", "payload": {"model": "gpt-5.6-sol"}}),
            json.dumps({"type": "response_item", "payload": {"type": "message"}}),
        ]) + "\n")
        assert backend.last_assistant_model(log) == "gpt-5.6-sol"

    def test_last_assistant_model_returns_latest(
        self, backend: CodexBackend, tmp_path: Path
    ) -> None:
        log = tmp_path / "rollout.jsonl"
        log.write_text("\n".join([
            json.dumps({"type": "turn_context", "payload": {"model": "old"}}),
            json.dumps({"type": "turn_context", "payload": {"model": "new"}}),
        ]) + "\n")
        assert backend.last_assistant_model(log) == "new"

    def test_last_assistant_model_none_when_absent(
        self, backend: CodexBackend, tmp_path: Path
    ) -> None:
        log = tmp_path / "rollout.jsonl"
        log.write_text(json.dumps({"type": "session_meta", "payload": {}}) + "\n")
        assert backend.last_assistant_model(log) is None
