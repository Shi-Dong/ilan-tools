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

    def test_md_with_line_numbers_prefixes_each_visual_line(
        self, runner: CliRunner, tmp_config: Path
    ) -> None:
        """Each rendered visual line of the Markdown output gets a ``[N]`` prefix.

        For a pipe-table the rendered output collapses to one visual row per
        source row, so the prefix counter ranges from ``[1]`` through ``[4]``.
        """
        _enable_line_number_and_markdown(tmp_config)
        client = _make_client()
        client.get_logs.return_value = self._logs()  # the 4-row pipe table
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["tail", "my-task", "-n", "1"])
        assert result.exit_code == 0
        # Table rendered (no raw separator survives) AND each visual row
        # carries a [N] prefix.
        assert "|---|---|" not in result.output
        for n in (1, 2, 3, 4):
            assert f"[{n}]" in result.output
        # The two data rows still show their cell values, alongside the prefix.
        out_lines = result.output.splitlines()
        pod0_line = next(l for l in out_lines if "pod-0" in l)
        pod1_line = next(l for l in out_lines if "pod-1" in l)
        assert "[3]" in pod0_line
        assert "[4]" in pod1_line

    def test_md_with_line_numbers_caches_visual_lines_for_at_refs(
        self, runner: CliRunner, tmp_config: Path
    ) -> None:
        """The ``@N`` cache stores the visual rendered lines (clean, no ANSI)
        so that a subsequent reply's ``@N`` quotes exactly what the user saw."""
        _enable_line_number_and_markdown(tmp_config)
        client = _make_client()
        client.get_logs.return_value = self._logs()  # 4-row pipe table
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["tail", "my-task", "-n", "1"])
        assert result.exit_code == 0
        cached = cfg.load_last_tail("my-task")
        # Four visual rows for four source rows in the rendered table.
        assert len(cached) == 4
        # Cached strings are plain text — no ANSI escape sequences.
        for c in cached:
            assert "\x1b[" not in c
        # Data rows include the cell values verbatim.
        assert any("pod-0" in c and "Pending" in c for c in cached)
        assert any("pod-1" in c and "Running" in c for c in cached)


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
