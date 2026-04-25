"""Tests for Markdown rendering of `ilan re` / `ilan task tail` output."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

import ilan.config as cfg
from ilan.cli import main


@pytest.fixture()
def runner() -> CliRunner:
    return CliRunner()


def _make_client() -> MagicMock:
    client = MagicMock()
    client.ensure_server.return_value = {}
    client.version_mismatch = None
    client.is_remote = False
    return client


def _enable_markdown(tmp_config: Path) -> None:
    tmp_config.parent.mkdir(parents=True, exist_ok=True)
    with open(tmp_config, "w") as f:
        json.dump({"markdown": True}, f)


def _enable_line_number_and_markdown(tmp_config: Path) -> None:
    tmp_config.parent.mkdir(parents=True, exist_ok=True)
    with open(tmp_config, "w") as f:
        json.dump({"line-number": True, "markdown": True}, f)


# A pipe table — verifies that Rich's Markdown renderer turns it into a
# boxed table rather than printing raw `|---|---|` source.
_TABLE_MD = (
    "| Pod | Status |\n"
    "|---|---|\n"
    "| pod-0 | Pending |\n"
    "| pod-1 | Running |\n"
)


class TestTailMarkdown:
    def _logs(self, content: str = _TABLE_MD) -> dict:
        return {
            "logs": [
                {"role": "assistant", "content": content,
                 "timestamp": "2026-04-25T00:00:00+00:00"},
            ],
        }

    def test_off_by_default_prints_raw_pipes(
        self, runner: CliRunner, tmp_config: Path
    ) -> None:
        client = _make_client()
        client.get_logs.return_value = self._logs()
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["tail", "my-task", "-n", "1"])
        assert result.exit_code == 0
        # Default: no rendering — the literal pipe-separator row survives.
        assert "|---|---|" in result.output

    def test_flag_renders_table(self, runner: CliRunner, tmp_config: Path) -> None:
        client = _make_client()
        client.get_logs.return_value = self._logs()
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["tail", "my-task", "-n", "1", "-m"])
        assert result.exit_code == 0
        # Rendered: pipe-separator gone, cell values still present.
        assert "|---|---|" not in result.output
        assert "pod-0" in result.output and "pod-1" in result.output

    def test_config_default_renders_table(
        self, runner: CliRunner, tmp_config: Path
    ) -> None:
        _enable_markdown(tmp_config)
        client = _make_client()
        client.get_logs.return_value = self._logs()
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["tail", "my-task", "-n", "1"])
        assert result.exit_code == 0
        assert "|---|---|" not in result.output
        assert "pod-0" in result.output

    def test_re_shortcut_supports_md_flag(
        self, runner: CliRunner, tmp_config: Path
    ) -> None:
        client = _make_client()
        client.get_logs.return_value = self._logs()
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["re", "my-task", "-n", "1", "-m"])
        assert result.exit_code == 0
        assert "|---|---|" not in result.output

    def test_user_messages_are_not_markdown_rendered(
        self, runner: CliRunner, tmp_config: Path
    ) -> None:
        # User content with literal pipes should pass through unchanged
        # (only assistant messages get the Markdown treatment).
        client = _make_client()
        client.get_logs.return_value = {
            "logs": [
                {"role": "user", "content": "raw |---| pipes",
                 "timestamp": "2026-04-25T00:00:00+00:00"},
            ],
        }
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["tail", "my-task", "-n", "1", "-m"])
        assert result.exit_code == 0
        assert "raw |---| pipes" in result.output

    def test_md_flag_with_line_numbers_caches_lines_for_at_refs(
        self, runner: CliRunner, tmp_config: Path
    ) -> None:
        # `--md` suppresses the on-screen [N] prefixes, but the cache that
        # backs `@N` reply expansion must still be populated when
        # line-number mode is on.
        _enable_line_number_and_markdown(tmp_config)
        client = _make_client()
        client.get_logs.return_value = {
            "logs": [
                {"role": "assistant", "content": "first\nsecond",
                 "timestamp": "2026-04-25T00:00:00+00:00"},
            ],
        }
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["tail", "my-task", "-n", "1"])
        assert result.exit_code == 0
        # The numbered prefix `[1]` must NOT appear on screen — Markdown wins.
        assert "[1] first" not in result.output
        # But the cache must be populated so `@1` still works on next reply.
        assert cfg.load_last_tail("my-task") == ["first", "second"]


class TestSetConfigMarkdown:
    def test_bool_config_roundtrips(self, tmp_config: Path) -> None:
        cfg.save({**cfg.DEFAULTS, "markdown": cfg.parse_bool("true")})
        assert cfg.load()["markdown"] is True
        cfg.save({**cfg.DEFAULTS, "markdown": cfg.parse_bool("false")})
        assert cfg.load()["markdown"] is False

    def test_set_writes_local_not_server(
        self, runner: CliRunner, tmp_config: Path
    ) -> None:
        client = _make_client()
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["config", "set", "markdown", "true"])
        assert result.exit_code == 0
        client.set_config.assert_not_called()
        assert cfg.load()["markdown"] is True
        assert "client-side" in result.output
