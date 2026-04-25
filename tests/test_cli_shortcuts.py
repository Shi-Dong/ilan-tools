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


# ── reply hint at end of tail ───────────────────────────────────────


class TestTailReplyHint:
    """``ilan tail`` ends with a reminder line pointing at ``ilan re <alias>``."""

    def test_tail_prints_reply_hint_with_alias(self, runner: CliRunner, tmp_config) -> None:
        """When the server returns an alias, the hint uses it."""
        client = _make_client()
        client.get_tail.return_value = {
            "name": "my-task",
            "alias": "aa",
            "entries": [
                {
                    "role": "assistant",
                    "content": "Hello",
                    "timestamp": "2026-04-13T01:00:00+00:00",
                },
            ],
        }
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["tail", "my-task"])
        assert result.exit_code == 0
        assert "To reply to the task, run ilan re aa" in result.output

    def test_tail_hint_falls_back_to_name_without_alias(
        self, runner: CliRunner, tmp_config
    ) -> None:
        """If the task has no alias, fall back to the task name."""
        client = _make_client()
        client.get_tail.return_value = {
            "name": "my-task",
            "alias": None,
            "entries": [
                {
                    "role": "assistant",
                    "content": "Hi",
                    "timestamp": "2026-04-13T01:00:00+00:00",
                },
            ],
        }
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["tail", "my-task"])
        assert result.exit_code == 0
        assert "To reply to the task, run ilan re my-task" in result.output

    def test_tail_hint_falls_back_to_input_for_old_server(
        self, runner: CliRunner, tmp_config
    ) -> None:
        """Old servers omit ``alias``/``name``; fall back to the user-supplied arg."""
        client = _make_client()
        client.get_tail.return_value = {
            "entries": [
                {
                    "role": "assistant",
                    "content": "Hi",
                    "timestamp": "2026-04-13T01:00:00+00:00",
                },
            ],
        }
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["tail", "my-task"])
        assert result.exit_code == 0
        assert "To reply to the task, run ilan re my-task" in result.output

    def test_tail_n_prints_reply_hint(self, runner: CliRunner, tmp_config) -> None:
        """The hint also appears when ``-n`` routes through ``/logs``."""
        client = _make_client()
        client.get_logs.return_value = {
            "name": "my-task",
            "alias": "aa",
            "logs": [
                {
                    "role": "assistant",
                    "content": "Hi",
                    "timestamp": "2026-04-13T01:00:00+00:00",
                },
            ],
        }
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["tail", "my-task", "-n", "1"])
        assert result.exit_code == 0
        assert "To reply to the task, run ilan re aa" in result.output

    def test_tail_hint_shown_when_no_logs_yet(self, runner: CliRunner, tmp_config) -> None:
        """Even when the server warns there are no logs, show the hint."""
        client = _make_client()
        client.get_tail.return_value = {
            "name": "my-task",
            "alias": "aa",
            "entries": [],
            "warning": "No logs yet.",
        }
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["tail", "my-task"])
        assert result.exit_code == 0
        assert "To reply to the task, run ilan re aa" in result.output


# ── -n flag on tail / ls / re ───────────────────────────────────────


class TestTailNFlag:
    """`-n N` uses /logs + client-side slicing so it works against old servers."""

    @staticmethod
    def _logs_response() -> dict:
        return {
            "logs": [
                {"role": "user", "content": "u0", "timestamp": "2026-04-13T00:00:00+00:00"},
                {"role": "assistant", "content": "a0", "timestamp": "2026-04-13T00:00:01+00:00"},
                {"role": "user", "content": "u1", "timestamp": "2026-04-13T00:00:02+00:00"},
                {"role": "assistant", "content": "a1", "timestamp": "2026-04-13T00:00:03+00:00"},
                {"role": "user", "content": "u2", "timestamp": "2026-04-13T00:00:04+00:00"},
                {"role": "assistant", "content": "a2", "timestamp": "2026-04-13T00:00:05+00:00"},
            ],
        }

    def test_ilan_tail_n_uses_logs_and_slices(self, runner: CliRunner, tmp_config) -> None:
        client = _make_client()
        client.get_logs.return_value = self._logs_response()
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["tail", "my-task", "-n", "3"])
        assert result.exit_code == 0
        client.get_logs.assert_called_once_with("my-task")
        client.get_tail.assert_not_called()
        assert "a1" in result.output
        assert "u2" in result.output
        assert "a2" in result.output
        assert "u0" not in result.output
        assert "a0" not in result.output

    def test_task_tail_n_uses_logs(self, runner: CliRunner, tmp_config) -> None:
        client = _make_client()
        client.get_logs.return_value = self._logs_response()
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["task", "tail", "my-task", "-n", "2"])
        assert result.exit_code == 0
        client.get_logs.assert_called_once_with("my-task")
        assert "u2" in result.output
        assert "a2" in result.output
        assert "a1" not in result.output

    def test_ls_name_n_uses_logs(self, runner: CliRunner, tmp_config) -> None:
        client = _make_client()
        client.get_logs.return_value = self._logs_response()
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["ls", "my-task", "-n", "3"])
        assert result.exit_code == 0
        client.get_logs.assert_called_once_with("my-task")
        client.list_tasks.assert_not_called()
        client.get_tail.assert_not_called()

    def test_task_ls_name_n_uses_logs(self, runner: CliRunner, tmp_config) -> None:
        client = _make_client()
        client.get_logs.return_value = self._logs_response()
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["task", "ls", "my-task", "-n", "2"])
        assert result.exit_code == 0
        client.get_logs.assert_called_once_with("my-task")

    def test_re_no_message_n_uses_logs(self, runner: CliRunner, tmp_config) -> None:
        client = _make_client()
        client.get_logs.return_value = self._logs_response()
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["re", "my-task", "-n", "4"])
        assert result.exit_code == 0
        client.get_logs.assert_called_once_with("my-task")
        client.reply.assert_not_called()
        client.get_tail.assert_not_called()

    def test_reply_no_message_n_uses_logs(self, runner: CliRunner, tmp_config) -> None:
        client = _make_client()
        client.get_logs.return_value = self._logs_response()
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["reply", "my-task", "-n", "2"])
        assert result.exit_code == 0
        client.get_logs.assert_called_once_with("my-task")

    def test_task_reply_no_message_n_uses_logs(self, runner: CliRunner, tmp_config) -> None:
        client = _make_client()
        client.get_logs.return_value = self._logs_response()
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["task", "reply", "my-task", "-n", "2"])
        assert result.exit_code == 0
        client.get_logs.assert_called_once_with("my-task")

    def test_re_with_message_ignores_n(self, runner: CliRunner, tmp_config) -> None:
        """When a message is given, -n is irrelevant and reply is called."""
        client = _make_client()
        client.reply.return_value = {"message": "replied"}
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["re", "my-task", "hello"])
        assert result.exit_code == 0
        client.reply.assert_called_once_with("my-task", "hello")
        client.get_logs.assert_not_called()
        client.get_tail.assert_not_called()

    def test_tail_n_larger_than_logs(self, runner: CliRunner, tmp_config) -> None:
        """If N exceeds log length, return everything."""
        client = _make_client()
        client.get_logs.return_value = self._logs_response()
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["tail", "my-task", "-n", "100"])
        assert result.exit_code == 0
        for content in ("u0", "a0", "u1", "a1", "u2", "a2"):
            assert content in result.output

    def test_tail_n_empty_logs(self, runner: CliRunner, tmp_config) -> None:
        client = _make_client()
        client.get_logs.return_value = {"logs": []}
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["tail", "my-task", "-n", "5"])
        assert result.exit_code == 0
        assert "No logs yet." in result.output

    def test_tail_n_propagates_warning(self, runner: CliRunner, tmp_config) -> None:
        """A warning on /logs should surface to `-n` users too."""
        client = _make_client()
        client.get_logs.return_value = {
            "logs": [
                {"role": "assistant", "content": "kept", "timestamp": "2026-04-13T00:00:00+00:00"},
            ],
            "warning": "Log was compacted.",
        }
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["tail", "my-task", "-n", "1"])
        assert result.exit_code == 0
        assert "Log was compacted." in result.output
        assert "kept" in result.output

    def test_tail_n_error_forwarded(self, runner: CliRunner, tmp_config) -> None:
        client = _make_client()
        client.get_logs.return_value = {"error": "Task 'no-such' not found"}
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["tail", "no-such", "-n", "3"])
        assert result.exit_code != 0
        assert "not found" in result.output.lower()

    def test_tail_n_invalid_int(self, runner: CliRunner, tmp_config) -> None:
        client = _make_client()
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["tail", "my-task", "-n", "abc"])
        assert result.exit_code != 0
        client.get_logs.assert_not_called()
        client.get_tail.assert_not_called()


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


# ── ilan unread ─────────────────────────────────────────────────────


class TestUnreadShorthand:
    def test_unread_success(self, runner: CliRunner, tmp_config) -> None:
        client = _make_client()
        client.mark_unread.return_value = {"name": "my-task"}
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["unread", "my-task"])
        assert result.exit_code == 0
        assert "unread" in result.output
        client.mark_unread.assert_called_once_with("my-task")

    def test_task_unread_success(self, runner: CliRunner, tmp_config) -> None:
        client = _make_client()
        client.mark_unread.return_value = {"name": "my-task"}
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["task", "unread", "my-task"])
        assert result.exit_code == 0
        assert "unread" in result.output

    def test_unread_multiple(self, runner: CliRunner, tmp_config) -> None:
        client = _make_client()
        client.mark_unread.side_effect = [{"name": "a"}, {"name": "b"}]
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["unread", "a", "b"])
        assert result.exit_code == 0
        assert client.mark_unread.call_count == 2

    def test_unread_error(self, runner: CliRunner, tmp_config) -> None:
        client = _make_client()
        client.mark_unread.return_value = {"error": "Task 'bad' not found"}
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["unread", "bad"])
        assert result.exit_code != 0
        assert "not found" in result.output

    def test_unread_no_args_shows_usage(self, runner: CliRunner, tmp_config) -> None:
        result = runner.invoke(main, ["unread"])
        assert result.exit_code != 0
