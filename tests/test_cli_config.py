"""Tests for ``ilan config show`` — masking of secret keys."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from click.testing import CliRunner

from ilan.cli import _mask_secret, main


@pytest.fixture()
def runner() -> CliRunner:
    return CliRunner()


def _patch_client(monkeypatch: pytest.MonkeyPatch, server_cfg: dict) -> MagicMock:
    client = MagicMock()
    client.get_config.return_value = {"config": server_cfg}
    monkeypatch.setattr("ilan.cli._client", lambda: client)
    return client


class TestMaskSecret:
    def test_empty_returns_empty(self) -> None:
        assert _mask_secret("") == ""

    def test_short_key_shown_after_asterisks(self) -> None:
        assert _mask_secret("abc") == "**abc"

    def test_long_key_shows_last_five(self) -> None:
        assert _mask_secret("sk-ant-api03-XXXXX-YYYYY-ZZZZZ") == "**ZZZZZ"

    def test_exactly_five_chars(self) -> None:
        assert _mask_secret("abcde") == "**abcde"

    def test_six_chars_shows_last_five(self) -> None:
        assert _mask_secret("abcdef") == "**bcdef"


class TestConfigShowMasks:
    def test_claude_key_is_masked(
        self, runner: CliRunner, tmp_config, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _patch_client(monkeypatch, {"api-key-claude": "sk-ant-api03-ABCDE"})
        result = runner.invoke(main, ["config", "show"])
        assert result.exit_code == 0
        assert "sk-ant-api03-ABCDE" not in result.output
        assert "**ABCDE" in result.output

    def test_codex_key_is_masked(
        self, runner: CliRunner, tmp_config, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _patch_client(monkeypatch, {"api-key-codex": "sk-openai-XYZ12"})
        result = runner.invoke(main, ["config", "show"])
        assert result.exit_code == 0
        assert "sk-openai-XYZ12" not in result.output
        assert "**XYZ12" in result.output

    def test_empty_secret_shows_empty_value(
        self, runner: CliRunner, tmp_config, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _patch_client(
            monkeypatch,
            {"api-key-claude": "", "api-key-codex": ""},
        )
        result = runner.invoke(main, ["config", "show"])
        assert result.exit_code == 0
        assert "api-key-claude" in result.output
        assert "api-key-codex" in result.output
        assert "**" not in result.output

    def test_non_secret_values_are_not_masked(
        self, runner: CliRunner, tmp_config, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _patch_client(
            monkeypatch,
            {"model": "opus", "effort": "high"},
        )
        result = runner.invoke(main, ["config", "show"])
        assert result.exit_code == 0
        assert "opus" in result.output
        assert "high" in result.output
