"""Unit tests for the CodexBackend adapter.

Fixtures mirror the real ``codex exec --json`` event stream and rollout
transcript schema captured from codex-cli 0.144.5.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import ilan.config as cfg
from ilan.backends.base import TokenUsage
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
    def test_fresh_argv_shape(self, backend: CodexBackend, tmp_config: Path) -> None:
        cmd, env = backend.build_command(None, resume=False, session_id=None)
        assert cmd[:2] == ["codex", "exec"]
        assert "resume" not in cmd
        assert "--json" in cmd
        assert "--skip-git-repo-check" in cmd
        assert "--dangerously-bypass-approvals-and-sandbox" in cmd
        assert cmd[-1] == "-"  # `-` tells codex to read the prompt from stdin
        assert isinstance(env, dict)

    def test_default_model_when_no_override(self, backend: CodexBackend, tmp_config: Path) -> None:
        cmd, _ = backend.build_command(None, resume=False, session_id=None)
        assert cmd[cmd.index("--model") + 1] == "gpt-5.6-sol"

    def test_resume_inserts_session(self, backend: CodexBackend, tmp_config: Path) -> None:
        cmd, _ = backend.build_command(None, resume=True, session_id="sid-9")
        assert cmd[:4] == ["codex", "exec", "resume", "sid-9"]
        assert cmd[-1] == "-"

    def test_resume_without_session_id_stays_fresh(
        self, backend: CodexBackend, tmp_config: Path
    ) -> None:
        cmd, _ = backend.build_command(None, resume=True, session_id=None)
        assert "resume" not in cmd

    def test_model_override_passed(self, backend: CodexBackend, tmp_config: Path) -> None:
        cmd, _ = backend.build_command("gpt-5.6-sol", resume=False, session_id=None)
        assert cmd[cmd.index("--model") + 1] == "gpt-5.6-sol"

    def test_fable_override_falls_back_to_default(
        self, backend: CodexBackend, tmp_config: Path
    ) -> None:
        """A stale Fable override (from ``ilan max`` before a switch to codex)
        is Claude-only, so codex ignores it and spawns the codex default."""
        cmd, _ = backend.build_command(
            "claude-fable-5", resume=False, session_id=None
        )
        assert cmd[cmd.index("--model") + 1] == "gpt-5.6-sol"

    def test_uses_configured_model_codex(
        self, backend: CodexBackend, tmp_config: Path
    ) -> None:
        cfg.save({**cfg.DEFAULTS, "model-codex": "gpt-5.1-codex-max"})
        cmd, _ = backend.build_command(None, resume=False, session_id=None)
        assert cmd[cmd.index("--model") + 1] == "gpt-5.1-codex-max"

    def test_fable_override_falls_back_to_configured_model_codex(
        self, backend: CodexBackend, tmp_config: Path
    ) -> None:
        cfg.save({**cfg.DEFAULTS, "model-codex": "gpt-5.1-codex-max"})
        cmd, _ = backend.build_command(
            "claude-fable-5", resume=False, session_id=None
        )
        assert cmd[cmd.index("--model") + 1] == "gpt-5.1-codex-max"

    def test_api_key_codex_sets_openai_key(
        self, backend: CodexBackend, tmp_config: Path,
    ) -> None:
        cfg.save({
            **cfg.DEFAULTS,
            "api-key-mode": True,
            "api-key-codex": "sk-codex-live",
        })
        _, env = backend.build_command(None, resume=False, session_id=None)
        assert env["OPENAI_API_KEY"] == "sk-codex-live"

    def test_default_effort_passed_as_reasoning_config(
        self, backend: CodexBackend, tmp_config: Path
    ) -> None:
        cmd, _ = backend.build_command(None, resume=False, session_id=None)
        assert cmd[cmd.index("-c") + 1] == 'model_reasoning_effort="xhigh"'

    def test_configured_effort_passed_as_reasoning_config(
        self, backend: CodexBackend, tmp_config: Path
    ) -> None:
        cfg.save({**cfg.DEFAULTS, "effort": "medium"})
        cmd, _ = backend.build_command(None, resume=False, session_id=None)
        assert cmd[cmd.index("-c") + 1] == 'model_reasoning_effort="medium"'

    def test_no_api_key_omits_openai_key(
        self, backend: CodexBackend, tmp_config: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        cfg.save({"model-claude": "opus"})
        _, env = backend.build_command(None, resume=False, session_id=None)
        assert "OPENAI_API_KEY" not in env

    def test_disabled_api_key_mode_removes_openai_key(
        self, backend: CodexBackend, tmp_config: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "inherited-key")
        cfg.save({
            **cfg.DEFAULTS,
            "api-key-mode": False,
            "api-key-codex": "sk-configured",
        })
        _, env = backend.build_command(None, resume=False, session_id=None)
        assert "OPENAI_API_KEY" not in env

    def test_enabled_mode_without_configured_key_uses_subscription(
        self, backend: CodexBackend, tmp_config: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "inherited-key")
        cfg.save({**cfg.DEFAULTS, "api-key-mode": True})
        _, env = backend.build_command(None, resume=False, session_id=None)
        assert "OPENAI_API_KEY" not in env


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

    def test_uses_latest_cumulative_snapshot(
        self, backend: CodexBackend, tmp_path: Path
    ) -> None:
        out = tmp_path / "out.jsonl"
        out.write_text("\n".join([
            json.dumps({"type": "turn.completed",
                        "usage": {"input_tokens": 100, "cached_input_tokens": 40, "output_tokens": 5}}),
            json.dumps({"type": "turn.completed",
                        "usage": {"input_tokens": 200, "cached_input_tokens": 150, "output_tokens": 7}}),
        ]) + "\n")
        parsed = backend.parse_output(out)
        assert parsed is not None
        assert parsed.input_tokens == 50
        assert parsed.cache_read_input_tokens == 150
        assert parsed.output_tokens == 7


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

    def test_last_turn_token_usage_subtracts_previous_task(
        self, backend: CodexBackend, tmp_path: Path
    ) -> None:
        log = tmp_path / "rollout.jsonl"
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
            json.dumps({
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {"total_token_usage": {
                        "input_tokens": 1_300,
                        "cached_input_tokens": 900,
                        "output_tokens": 80,
                    }},
                },
            }),
            json.dumps({"type": "event_msg",
                        "payload": {"type": "task_complete"}}),
        ]) + "\n")

        assert backend.last_turn_token_usage(log) == TokenUsage(
            input_tokens=100,
            output_tokens=30,
            cache_read_input_tokens=200,
        )

    def test_last_turn_token_usage_fresh_session(
        self, backend: CodexBackend, tmp_path: Path
    ) -> None:
        log = tmp_path / "rollout.jsonl"
        log.write_text("\n".join([
            json.dumps({"type": "event_msg",
                        "payload": {"type": "task_started"}}),
            json.dumps({
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {"total_token_usage": {
                        "input_tokens": 12_464,
                        "cached_input_tokens": 9_984,
                        "output_tokens": 6,
                    }},
                },
            }),
            json.dumps({"type": "event_msg",
                        "payload": {"type": "task_complete"}}),
        ]) + "\n")

        assert backend.last_turn_token_usage(log) == TokenUsage(
            input_tokens=2_480,
            output_tokens=6,
            cache_read_input_tokens=9_984,
        )

    def test_last_turn_token_usage_ignores_incomplete_latest_task(
        self, backend: CodexBackend, tmp_path: Path
    ) -> None:
        log = tmp_path / "rollout.jsonl"
        log.write_text("\n".join([
            json.dumps({"type": "event_msg",
                        "payload": {"type": "task_started"}}),
            json.dumps({
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {"total_token_usage": {
                        "input_tokens": 100,
                        "cached_input_tokens": 40,
                        "output_tokens": 5,
                    }},
                },
            }),
            json.dumps({"type": "event_msg",
                        "payload": {"type": "task_complete"}}),
            json.dumps({"type": "event_msg",
                        "payload": {"type": "task_started"}}),
        ]) + "\n")

        assert backend.last_turn_token_usage(log) is None

    def test_last_turn_token_usage_handles_counter_reset(
        self, backend: CodexBackend, tmp_path: Path
    ) -> None:
        log = tmp_path / "rollout.jsonl"
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
            json.dumps({
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {"total_token_usage": {
                        "input_tokens": 100,
                        "cached_input_tokens": 40,
                        "output_tokens": 5,
                    }},
                },
            }),
            json.dumps({"type": "event_msg",
                        "payload": {"type": "task_complete"}}),
        ]) + "\n")

        assert backend.last_turn_token_usage(log) == TokenUsage(
            input_tokens=60,
            output_tokens=5,
            cache_read_input_tokens=40,
        )

    def test_last_assistant_token_usage_uses_final_agent_message(
        self, backend: CodexBackend, tmp_path: Path
    ) -> None:
        log = tmp_path / "rollout.jsonl"
        log.write_text("\n".join([
            json.dumps({"type": "event_msg",
                        "payload": {"type": "task_started"}}),
            json.dumps({"type": "event_msg",
                        "payload": {"type": "agent_message"}}),
            json.dumps({"type": "event_msg", "payload": {
                "type": "token_count",
                "info": {"last_token_usage": {
                    "input_tokens": 100,
                    "cached_input_tokens": 40,
                    "output_tokens": 5,
                }},
            }}),
            # A tool-only model call must not replace the message usage.
            json.dumps({"type": "event_msg", "payload": {
                "type": "token_count",
                "info": {"last_token_usage": {
                    "input_tokens": 200,
                    "cached_input_tokens": 150,
                    "output_tokens": 7,
                }},
            }}),
            json.dumps({"type": "event_msg",
                        "payload": {"type": "agent_message"}}),
            json.dumps({"type": "event_msg", "payload": {
                "type": "token_count",
                "info": {"last_token_usage": {
                    "input_tokens": 156_079,
                    "cached_input_tokens": 155_392,
                    "output_tokens": 138,
                }},
            }}),
            json.dumps({"type": "event_msg",
                        "payload": {"type": "task_complete"}}),
        ]) + "\n")

        assert backend.last_assistant_token_usage(log) == TokenUsage(
            input_tokens=687,
            output_tokens=138,
            cache_read_input_tokens=155_392,
        )

    def test_last_assistant_token_usage_ignores_incomplete_latest_task(
        self, backend: CodexBackend, tmp_path: Path
    ) -> None:
        log = tmp_path / "rollout.jsonl"
        log.write_text("\n".join([
            json.dumps({"type": "event_msg",
                        "payload": {"type": "task_started"}}),
            json.dumps({"type": "event_msg",
                        "payload": {"type": "agent_message"}}),
            json.dumps({"type": "event_msg", "payload": {
                "type": "token_count",
                "info": {"last_token_usage": {
                    "input_tokens": 100,
                    "cached_input_tokens": 40,
                    "output_tokens": 5,
                }},
            }}),
            json.dumps({"type": "event_msg",
                        "payload": {"type": "task_complete"}}),
            json.dumps({"type": "event_msg",
                        "payload": {"type": "task_started"}}),
        ]) + "\n")

        assert backend.last_assistant_token_usage(log) is None
