"""Unit tests for the ClaudeBackend adapter.

These pin the CLI-specific behaviour that used to live inline in ``Runner``
so the backend refactor stays a pure no-op and future backends have a
reference for the contract.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import ilan.config as cfg
from ilan.backends.claude import ClaudeBackend


@pytest.fixture()
def backend() -> ClaudeBackend:
    return ClaudeBackend()


class TestBuildCommand:
    def test_basic_argv_shape(self, backend: ClaudeBackend, tmp_config: Path) -> None:
        cmd, _ = backend.build_command(None, resume=False, session_id=None)
        # No positional prompt: the prompt travels on stdin.
        assert cmd[:2] == ["claude", "-p"]
        assert "--dangerously-skip-permissions" in cmd
        assert cmd[cmd.index("--output-format") + 1] == "json"
        assert "--resume" not in cmd

    def test_model_override_wins(self, backend: ClaudeBackend, tmp_config: Path) -> None:
        cmd, _ = backend.build_command("claude-fable-5", resume=False, session_id=None)
        assert cmd[cmd.index("--model") + 1] == "claude-fable-5"

    def test_falls_back_to_config_model(
        self, backend: ClaudeBackend, tmp_config: Path
    ) -> None:
        cmd, _ = backend.build_command(None, resume=False, session_id=None)
        assert cmd[cmd.index("--model") + 1] == "opus"

    def test_resume_appends_session(self, backend: ClaudeBackend, tmp_config: Path) -> None:
        cmd, _ = backend.build_command(None, resume=True, session_id="sid-42")
        assert cmd[cmd.index("--resume") + 1] == "sid-42"

    def test_resume_without_session_id_omits_flag(
        self, backend: ClaudeBackend, tmp_config: Path
    ) -> None:
        cmd, _ = backend.build_command(None, resume=True, session_id=None)
        assert "--resume" not in cmd

    def test_glm_sets_endpoint_and_token(
        self, backend: ClaudeBackend, tmp_config: Path
    ) -> None:
        cfg.save({"api-key-glm": "zai-secret", "api-key-claude": "sk-should-be-dropped"})
        cmd, env = backend.build_command("glm", resume=False, session_id=None)
        assert env["ANTHROPIC_BASE_URL"] == "https://api.z.ai/api/anthropic"
        assert env["ANTHROPIC_AUTH_TOKEN"] == "zai-secret"
        assert "ANTHROPIC_API_KEY" not in env

    def test_non_glm_omits_endpoint_sets_api_key(
        self, backend: ClaudeBackend, tmp_config: Path
    ) -> None:
        cfg.save({"api-key-claude": "sk-live", "model": "opus"})
        _, env = backend.build_command(None, resume=False, session_id=None)
        assert "ANTHROPIC_BASE_URL" not in env
        assert env["ANTHROPIC_API_KEY"] == "sk-live"


class TestParseOutput:
    def test_parses_full_result(self, backend: ClaudeBackend, tmp_path: Path) -> None:
        out = tmp_path / "out.json"
        out.write_text(json.dumps({
            "session_id": "sid-1",
            "result": "done\n[STATUS: DONE]",
            "is_error": False,
            "usage": {"input_tokens": 10, "output_tokens": 3, "cache_read_input_tokens": 5},
            "total_cost_usd": 1.5,
        }))
        parsed = backend.parse_output(out)
        assert parsed is not None
        assert parsed.session_id == "sid-1"
        assert parsed.result_text == "done\n[STATUS: DONE]"
        assert parsed.is_error is False
        assert parsed.input_tokens == 10
        assert parsed.output_tokens == 3
        assert parsed.cache_read_input_tokens == 5
        assert parsed.cost_usd == pytest.approx(1.5)

    def test_missing_file_returns_none(self, backend: ClaudeBackend, tmp_path: Path) -> None:
        assert backend.parse_output(tmp_path / "nope.json") is None

    def test_malformed_json_returns_none(self, backend: ClaudeBackend, tmp_path: Path) -> None:
        out = tmp_path / "out.json"
        out.write_text("{not json")
        assert backend.parse_output(out) is None

    def test_defaults_when_usage_absent(self, backend: ClaudeBackend, tmp_path: Path) -> None:
        out = tmp_path / "out.json"
        out.write_text(json.dumps({"session_id": "s", "result": "r", "is_error": True}))
        parsed = backend.parse_output(out)
        assert parsed is not None
        assert parsed.is_error is True
        assert parsed.input_tokens == 0
        assert parsed.cost_usd == 0.0


class TestSessionLog:
    def test_find_session_log(
        self, backend: ClaudeBackend, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        projects = tmp_path / ".claude" / "projects" / "proj-a"
        projects.mkdir(parents=True)
        log = projects / "sid-x.jsonl"
        log.write_text("{}\n")
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        assert backend.find_session_log("sid-x") == log
        assert backend.find_session_log("missing") is None

    def test_last_assistant_model(self, backend: ClaudeBackend, tmp_path: Path) -> None:
        log = tmp_path / "s.jsonl"
        log.write_text(
            json.dumps({"message": {"role": "user", "content": "hi"}}) + "\n"
            + json.dumps({"message": {"role": "assistant", "model": "claude-opus-4-8"}}) + "\n"
        )
        assert backend.last_assistant_model(log) == "claude-opus-4-8"

    def test_last_assistant_model_none_when_absent(
        self, backend: ClaudeBackend, tmp_path: Path
    ) -> None:
        log = tmp_path / "s.jsonl"
        log.write_text(json.dumps({"message": {"role": "user", "content": "hi"}}) + "\n")
        assert backend.last_assistant_model(log) is None
