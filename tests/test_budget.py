"""Tests for :mod:`ilan.budget` — resolving who pays for a spawn."""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from ilan import budget


def _codex_id_token(plan: str) -> str:
    """Build a Codex id_token whose payload segment names *plan*."""
    payload = {"https://api.openai.com/auth": {"chatgpt_plan_type": plan}}
    raw = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    return f"header.{raw}.signature"


def _write(path: Path, data: dict) -> Path:
    path.write_text(json.dumps(data))
    return path


class TestClaudeBudget:
    def test_spawn_env_api_key_reports_api(self) -> None:
        assert budget.detect(
            "claude", {"ANTHROPIC_API_KEY": "sk-ant-configured"}
        ) == "API"

    def test_env_api_key_reports_api(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-from-env")
        assert budget.detect("claude") == "API"

    def test_env_auth_token_reports_api(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A gateway token (ANTHROPIC_AUTH_TOKEN) is not subscription billing."""
        monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "gateway-token")
        assert budget.detect("claude") == "API"

    def test_subscription_type_titlecased(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = _write(
            tmp_path / "claude.json",
            {"claudeAiOauth": {"subscriptionType": "team"}},
        )
        monkeypatch.setattr(budget, "_CLAUDE_CREDENTIALS_FILE", path)
        assert budget.detect("claude") == "Team"

    def test_explicit_spawn_env_ignores_server_api_key(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "server-key")
        path = _write(
            tmp_path / "claude.json",
            {"claudeAiOauth": {"subscriptionType": "team"}},
        )
        monkeypatch.setattr(budget, "_CLAUDE_CREDENTIALS_FILE", path)
        assert budget.detect("claude", {}) == "Team"

    def test_keychain_used_when_file_holds_no_login(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The credentials file also stores MCP logins, so it can exist while
        holding no Claude login; the keychain must still be consulted."""
        path = _write(tmp_path / "claude.json", {"mcpOAuth": {"notion": {}}})
        monkeypatch.setattr(budget, "_CLAUDE_CREDENTIALS_FILE", path)
        monkeypatch.setattr(
            budget,
            "_read_keychain_json",
            lambda: {"claudeAiOauth": {"subscriptionType": "max"}},
        )
        assert budget.detect("claude") == "Max"

    def test_unreadable_credentials_report_unknown(self) -> None:
        assert budget.detect("claude") is None

    def test_blank_subscription_type_reports_unknown(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = _write(tmp_path / "claude.json", {"claudeAiOauth": {"subscriptionType": ""}})
        monkeypatch.setattr(budget, "_CLAUDE_CREDENTIALS_FILE", path)
        assert budget.detect("claude") is None


class TestCodexBudget:
    def test_chatgpt_login_reports_plan(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = _write(
            tmp_path / "codex.json",
            {
                "OPENAI_API_KEY": None,
                "auth_mode": "chatgpt",
                "tokens": {"id_token": _codex_id_token("team")},
            },
        )
        monkeypatch.setattr(budget, "_CODEX_AUTH_FILE", path)
        assert budget.detect("codex") == "Team"

    def test_apikey_auth_mode_reports_api(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = _write(
            tmp_path / "codex.json",
            {
                "auth_mode": "apikey",
                "tokens": {"id_token": _codex_id_token("team")},
            },
        )
        monkeypatch.setattr(budget, "_CODEX_AUTH_FILE", path)
        assert budget.detect("codex") == "API"

    def test_stored_api_key_reports_api(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = _write(tmp_path / "codex.json", {"OPENAI_API_KEY": "sk-stored"})
        monkeypatch.setattr(budget, "_CODEX_AUTH_FILE", path)
        assert budget.detect("codex") == "API"

    def test_spawn_env_api_key_reports_api(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = _write(
            tmp_path / "codex.json",
            {
                "auth_mode": "chatgpt",
                "tokens": {"id_token": _codex_id_token("team")},
            },
        )
        monkeypatch.setattr(budget, "_CODEX_AUTH_FILE", path)
        assert budget.detect("codex", {"OPENAI_API_KEY": "sk-configured"}) == "API"

    def test_explicit_spawn_env_ignores_server_api_key(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "server-key")
        path = _write(
            tmp_path / "codex.json",
            {
                "auth_mode": "chatgpt",
                "tokens": {"id_token": _codex_id_token("team")},
            },
        )
        monkeypatch.setattr(budget, "_CODEX_AUTH_FILE", path)
        assert budget.detect("codex", {}) == "Team"

    def test_missing_auth_file_reports_unknown(self) -> None:
        assert budget.detect("codex") is None

    def test_malformed_id_token_reports_unknown(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = _write(
            tmp_path / "codex.json",
            {"auth_mode": "chatgpt", "tokens": {"id_token": "not-a-jwt"}},
        )
        monkeypatch.setattr(budget, "_CODEX_AUTH_FILE", path)
        assert budget.detect("codex") is None


def test_unknown_engine_reports_unknown() -> None:
    assert budget.detect("gemini") is None
