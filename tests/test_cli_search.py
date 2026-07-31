"""Tests for ``ilan search PATTERN``."""

from __future__ import annotations

import re
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner
from rich.console import Console

import ilan.cli as cli_mod
from ilan.cli import main
from ilan.models import ENGINE_CLAUDE, ENGINE_CODEX


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _strip_ansi(s: str) -> str:
    return _ANSI_RE.sub("", s)


@pytest.fixture()
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture()
def wide_console(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force a terminal-like console wide enough that no line wraps."""
    monkeypatch.setattr(cli_mod, "console", Console(width=200, force_terminal=True))


def _make_client(tasks: list[dict]) -> MagicMock:
    client = MagicMock()
    client.ensure_server.return_value = {}
    client.version_mismatch = None
    client.is_remote = False
    client.list_tasks.return_value = {"tasks": tasks}
    return client


def _tasks() -> list[dict]:
    return [
        {
            "name": "fix-router-timeout",
            "alias": "as",
            "status": "WORKING",
            "engine": ENGINE_CLAUDE,
            "status_changed_at": None,
        },
        {
            "name": "add-router-metrics",
            "alias": "df",
            "status": "AGENT_FINISHED",
            "engine": ENGINE_CODEX,
        },
        {
            "name": "update-readme",
            "alias": None,
            "status": "DONE",
            "engine": ENGINE_CLAUDE,
        },
    ]


def _invoke(runner: CliRunner, pattern: str, tasks: list[dict] | None = None):
    client = _make_client(_tasks() if tasks is None else tasks)
    with patch("ilan.cli._client", return_value=client):
        result = runner.invoke(main, ["search", pattern])
    return result, client


class TestSearch:
    def test_prints_only_matching_lines(
        self, runner: CliRunner, tmp_config, wide_console,
    ) -> None:
        result, _ = _invoke(runner, "router")
        assert result.exit_code == 0
        assert _strip_ansi(result.output) == (
            "(as) fix-router-timeout WORKING\n"
            "(df) add-router-metrics AGENT_FINISHED\n"
        )

    def test_keeps_the_ls_colors(
        self, runner: CliRunner, tmp_config, wide_console,
    ) -> None:
        result, _ = _invoke(runner, "update-readme")
        assert result.exit_code == 0
        assert "\x1b[" in result.output
        assert _strip_ansi(result.output) == "update-readme DONE\n"

    def test_searches_terminal_tasks_too(
        self, runner: CliRunner, tmp_config, wide_console,
    ) -> None:
        result, client = _invoke(runner, "readme")
        assert result.exit_code == 0
        assert _strip_ansi(result.output) == "update-readme DONE\n"
        client.list_tasks.assert_called_once_with(show_all=True)

    def test_match_is_case_insensitive(
        self, runner: CliRunner, tmp_config, wide_console,
    ) -> None:
        result, _ = _invoke(runner, "ROUTER-METRICS")
        assert _strip_ansi(result.output) == "(df) add-router-metrics AGENT_FINISHED\n"

    def test_matches_alias_and_status(
        self, runner: CliRunner, tmp_config, wide_console,
    ) -> None:
        by_alias, _ = _invoke(runner, "(as)")
        assert _strip_ansi(by_alias.output) == "(as) fix-router-timeout WORKING\n"
        by_status, _ = _invoke(runner, "working")
        assert _strip_ansi(by_status.output) == "(as) fix-router-timeout WORKING\n"

    def test_no_match_reports_instead_of_printing_nothing(
        self, runner: CliRunner, tmp_config, wide_console,
    ) -> None:
        result, _ = _invoke(runner, "nope")
        assert result.exit_code == 0
        assert _strip_ansi(result.output) == "No task matching 'nope'.\n"

    def test_no_tasks_at_all(
        self, runner: CliRunner, tmp_config, wide_console,
    ) -> None:
        result, _ = _invoke(runner, "router", tasks=[])
        assert result.exit_code == 0
        assert _strip_ansi(result.output) == "No task matching 'router'.\n"

    def test_pattern_is_a_literal_substring_not_a_regex(
        self, runner: CliRunner, tmp_config, wide_console,
    ) -> None:
        result, _ = _invoke(runner, "fix.router")
        assert _strip_ansi(result.output) == "No task matching 'fix.router'.\n"

    def test_markup_in_pattern_is_echoed_literally(
        self, runner: CliRunner, tmp_config, wide_console,
    ) -> None:
        result, _ = _invoke(runner, "[bold]")
        assert result.exit_code == 0
        assert _strip_ansi(result.output) == "No task matching '[bold]'.\n"

    def test_pattern_is_required(self, runner: CliRunner, tmp_config) -> None:
        result = runner.invoke(main, ["search"])
        assert result.exit_code != 0


class TestSearchMatchesDisplayStatus:
    """A cycling task is searchable by the status it displays, not the stored one."""

    def _cycling(self) -> list[dict]:
        return [
            {
                "name": "watch-the-run",
                "alias": "as",
                "status": "AGENT_FINISHED",
                "engine": ENGINE_CLAUDE,
                "reply_every_seconds": 3600,
            }
        ]

    def test_found_by_in_loop(
        self, runner: CliRunner, tmp_config, wide_console,
    ) -> None:
        result, _ = _invoke(runner, "AGENT_IN_LOOP", tasks=self._cycling())
        assert _strip_ansi(result.output) == (
            "(as) watch-the-run AGENT_IN_LOOP (responding every 1h)\n"
        )

    def test_not_found_by_stored_status(
        self, runner: CliRunner, tmp_config, wide_console,
    ) -> None:
        result, _ = _invoke(runner, "AGENT_FINISHED", tasks=self._cycling())
        assert _strip_ansi(result.output) == "No task matching 'AGENT_FINISHED'.\n"
