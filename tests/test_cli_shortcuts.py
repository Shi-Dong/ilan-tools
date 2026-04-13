"""Tests for CLI shortcut changes: ``ilan ls <name>`` → tail, and
``ilan undone`` / ``ilan undiscard`` top-level shorthands.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from ilan.cli import main


@pytest.fixture()
def runner() -> CliRunner:
    return CliRunner()


def _make_client(**overrides) -> MagicMock:
    """Build a mock Client with sensible defaults."""
    client = MagicMock()
    client.ensure_server.return_value = {}
    client.version_mismatch = None
    client.is_remote = False
    for k, v in overrides.items():
        setattr(client, k, v)
    return client


# ── ilan ls (no args) still lists tasks ─────────────────────────────


class TestLsNoArgs:
    def test_ls_shows_table(self, runner: CliRunner, tmp_config) -> None:
        client = _make_client()
        client.list_tasks.return_value = {
            "tasks": [
                {
                    "name": "my-task",
                    "alias": "aa",
                    "status": "WORKING",
                    "cost_usd": 1.23,
                    "created_at": "2026-04-13T00:00:00+00:00",
                    "status_changed_at": "2026-04-13T01:00:00+00:00",
                    "needs_review": False,
                },
            ],
        }
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["ls"])
        assert result.exit_code == 0
        assert "my-task" in result.output
        client.list_tasks.assert_called_once_with(show_all=False)

    def test_task_ls_shows_table(self, runner: CliRunner, tmp_config) -> None:
        client = _make_client()
        client.list_tasks.return_value = {"tasks": []}
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["task", "ls"])
        assert result.exit_code == 0
        client.list_tasks.assert_called_once_with(show_all=False)

    def test_ls_all_flag(self, runner: CliRunner, tmp_config) -> None:
        client = _make_client()
        client.list_tasks.return_value = {"tasks": []}
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["ls", "-a"])
        assert result.exit_code == 0
        client.list_tasks.assert_called_once_with(show_all=True)

    def test_task_ls_all_flag(self, runner: CliRunner, tmp_config) -> None:
        client = _make_client()
        client.list_tasks.return_value = {"tasks": []}
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["task", "ls", "-a"])
        assert result.exit_code == 0
        client.list_tasks.assert_called_once_with(show_all=True)


# ── ilan ls <name> delegates to tail ────────────────────────────────


class TestLsWithName:
    def test_ls_name_calls_tail(self, runner: CliRunner, tmp_config) -> None:
        """``ilan ls my-task`` should show tail output, not the task table."""
        client = _make_client()
        client.get_tail.return_value = {
            "entries": [
                {
                    "role": "assistant",
                    "content": "Hello from tail",
                    "timestamp": "2026-04-13T01:00:00+00:00",
                },
            ],
        }
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["ls", "my-task"])
        assert result.exit_code == 0
        assert "Hello from tail" in result.output
        client.get_tail.assert_called_once_with("my-task")
        client.list_tasks.assert_not_called()

    def test_task_ls_name_calls_tail(self, runner: CliRunner, tmp_config) -> None:
        """``ilan task ls my-task`` should also delegate to tail."""
        client = _make_client()
        client.get_tail.return_value = {
            "entries": [
                {
                    "role": "assistant",
                    "content": "Tail via task subcommand",
                    "timestamp": "2026-04-13T01:00:00+00:00",
                },
            ],
        }
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["task", "ls", "my-task"])
        assert result.exit_code == 0
        assert "Tail via task subcommand" in result.output
        client.get_tail.assert_called_once_with("my-task")

    def test_ls_name_error_forwarded(self, runner: CliRunner, tmp_config) -> None:
        """If the task doesn't exist, the error from get_tail is shown."""
        client = _make_client()
        client.get_tail.return_value = {"error": "Task 'no-such' not found"}
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["ls", "no-such"])
        assert result.exit_code != 0
        assert "not found" in result.output.lower()

    def test_ls_name_with_alias(self, runner: CliRunner, tmp_config) -> None:
        """Aliases (short names) should also work with ``ilan ls``."""
        client = _make_client()
        client.get_tail.return_value = {
            "entries": [
                {
                    "role": "assistant",
                    "content": "Alias tail",
                    "timestamp": "2026-04-13T01:00:00+00:00",
                },
            ],
        }
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["ls", "aa"])
        assert result.exit_code == 0
        assert "Alias tail" in result.output
        client.get_tail.assert_called_once_with("aa")


# ── ilan undone ─────────────────────────────────────────────────────


class TestUndoneShorthand:
    def test_undone_success(self, runner: CliRunner, tmp_config) -> None:
        client = _make_client()
        client.undone.return_value = {"name": "my-task"}
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["undone", "my-task"])
        assert result.exit_code == 0
        assert "NEEDS_ATTENTION" in result.output
        client.undone.assert_called_once_with("my-task")

    def test_task_undone_success(self, runner: CliRunner, tmp_config) -> None:
        client = _make_client()
        client.undone.return_value = {"name": "my-task"}
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["task", "undone", "my-task"])
        assert result.exit_code == 0
        assert "NEEDS_ATTENTION" in result.output

    def test_undone_error(self, runner: CliRunner, tmp_config) -> None:
        client = _make_client()
        client.undone.return_value = {"error": "Task 'bad' is not DONE"}
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["undone", "bad"])
        assert result.exit_code != 0
        assert "not DONE" in result.output

    def test_undone_no_args_shows_usage(self, runner: CliRunner, tmp_config) -> None:
        result = runner.invoke(main, ["undone"])
        assert result.exit_code != 0


# ── ilan undiscard ──────────────────────────────────────────────────


class TestUndiscardShorthand:
    def test_undiscard_success(self, runner: CliRunner, tmp_config) -> None:
        client = _make_client()
        client.undiscard.return_value = {"name": "my-task"}
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["undiscard", "my-task"])
        assert result.exit_code == 0
        assert "NEEDS_ATTENTION" in result.output
        client.undiscard.assert_called_once_with("my-task")

    def test_task_undiscard_success(self, runner: CliRunner, tmp_config) -> None:
        client = _make_client()
        client.undiscard.return_value = {"name": "my-task"}
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["task", "undiscard", "my-task"])
        assert result.exit_code == 0
        assert "NEEDS_ATTENTION" in result.output

    def test_undiscard_error(self, runner: CliRunner, tmp_config) -> None:
        client = _make_client()
        client.undiscard.return_value = {"error": "Task 'bad' is not DISCARDED"}
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["undiscard", "bad"])
        assert result.exit_code != 0
        assert "not DISCARDED" in result.output

    def test_undiscard_no_args_shows_usage(self, runner: CliRunner, tmp_config) -> None:
        result = runner.invoke(main, ["undiscard"])
        assert result.exit_code != 0
