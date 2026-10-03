"""Tests for ``ilan config show`` — config scopes and secret masking."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from click.testing import CliRunner

import ilan.config as cfg
from ilan.cli import _mask_secret, main


@pytest.fixture()
def runner() -> CliRunner:
    return CliRunner()


def _patch_client(monkeypatch: pytest.MonkeyPatch, server_cfg: dict) -> MagicMock:
    client = MagicMock()
    client.get_config.return_value = {"config": server_cfg}
    monkeypatch.setattr("ilan.cli._client", lambda: client)
    return client


class TestBareConfig:
    def test_matches_show(
        self, runner: CliRunner, tmp_config: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        cfg.save({**cfg.DEFAULTS, "line-number": True, "model-claude": "local-stale"})
        client = _patch_client(monkeypatch, {
            "model-claude": "server-live", "api-key-claude": "secret-ABCDE",
        })
        bare = runner.invoke(main, ["config"])
        client.get_config.assert_called_once_with()
        client.reset_mock()
        explicit = runner.invoke(main, ["config", "show"])
        client.get_config.assert_called_once_with()
        assert bare.exit_code == explicit.exit_code == 0
        assert bare.output == explicit.output
        assert "server-live" in bare.output
        assert "local-stale" not in bare.output
        assert "secret-ABCDE" not in bare.output
        assert "**ABCDE" in bare.output
        assert "Client-side configuration" in bare.output

    def test_propagates_show_error(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        client = _patch_client(monkeypatch, {})
        client.get_config.return_value = {"error": "Cannot fetch configuration"}
        result = runner.invoke(main, ["config"])
        assert result.exit_code == 1
        assert "Cannot fetch configuration" in result.output

    @pytest.mark.parametrize("args", [["--help"], ["unknown"], ["--unknown"]])
    def test_help_and_invalid_usage_do_not_fetch_config(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, args: list[str],
    ) -> None:
        client = _patch_client(monkeypatch, {})
        result = runner.invoke(main, ["config", *args])
        assert result.exit_code == (0 if args == ["--help"] else 2)
        client.get_config.assert_not_called()

    def test_set_does_not_show_config(
        self, runner: CliRunner, tmp_config: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        client = _patch_client(monkeypatch, {})
        result = runner.invoke(main, ["config", "set", "-y", "line-number", "true"])
        assert result.exit_code == 0, result.output
        assert cfg.load()["line-number"] is True
        client.get_config.assert_not_called()


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
            {"model-claude": "opus", "effort": "high"},
        )
        result = runner.invoke(main, ["config", "show"])
        assert result.exit_code == 0
        assert "opus" in result.output
        assert "high" in result.output


class TestConfigShowScopes:
    def test_renders_separate_server_and_client_tables(
        self, runner: CliRunner, tmp_config, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        cfg.save({**cfg.DEFAULTS, "line-number": True})
        _patch_client(
            monkeypatch,
            {**cfg.DEFAULTS, "line-number": False},
        )

        result = runner.invoke(main, ["config", "show"])

        assert result.exit_code == 0
        assert "Server-side configuration" in result.output
        assert "Client-side configuration" in result.output
        line_number_row = next(
            line for line in result.output.splitlines() if "line-number" in line
        )
        assert "True" in line_number_row
        assert result.output.count("line-number") == 1

    def test_server_table_uses_live_server_value(
        self, runner: CliRunner, tmp_config, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        cfg.save({**cfg.DEFAULTS, "model-claude": "local-stale"})
        client = _patch_client(
            monkeypatch,
            {**cfg.DEFAULTS, "model-claude": "server-live"},
        )

        result = runner.invoke(main, ["config", "show"])

        assert result.exit_code == 0
        client.get_config.assert_called_once_with()
        assert "server-live" in result.output
        assert "local-stale" not in result.output
