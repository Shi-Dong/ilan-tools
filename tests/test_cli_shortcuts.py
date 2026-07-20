"""Tests for CLI shortcut changes: ``ilan ls <name>`` → tail, and
``ilan undone`` / ``ilan undiscard`` top-level shorthands.
"""

from __future__ import annotations

import re
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from ilan.cli import _build_name_cell, main
from ilan.models import ENGINE_CLAUDE, ENGINE_CODEX, ENGINE_NAME_STYLE


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _strip_ansi(s: str) -> str:
    """Remove ANSI escape sequences so substring asserts survive Rich styling.

    The reply hint splits its prose and the ``ilan re <handle>`` command into
    two differently-styled spans; the resulting reset codes break a literal
    contiguous substring match against the rendered output.
    """
    return _ANSI_RE.sub("", s)


@pytest.fixture()
def runner() -> CliRunner:
    return CliRunner()


def _make_client(**overrides) -> MagicMock:
    """Build a mock Client with sensible defaults."""
    client = MagicMock()
    client.ensure_server.return_value = {}
    client.version_mismatch = None
    client.is_remote = False
    client.get_last_model.return_value = {"model": "claude-opus-4-8"}
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

    def test_ls_never_shows_cost_column(
        self, runner: CliRunner, tmp_config, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The Cost column is gone; a wide terminal still keeps Created."""
        import ilan.cli as cli_mod
        from rich.console import Console

        monkeypatch.setattr(cli_mod, "console", Console(width=140, force_terminal=True))
        client = _make_client()
        client.list_tasks.return_value = {
            "tasks": [
                {
                    "name": "wide-task",
                    "alias": None,
                    "status": "WORKING",
                    "created_at": "2026-04-13T00:00:00+00:00",
                    "status_changed_at": "2026-04-13T01:00:00+00:00",
                    "needs_review": False,
                },
            ],
        }
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["ls"])
        assert result.exit_code == 0
        out = _strip_ansi(result.output)
        assert "Cost" not in out
        assert "Created" in out

    def test_ls_narrow_drops_created(
        self, runner: CliRunner, tmp_config, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A narrow terminal drops the Created column from ``ls``."""
        import ilan.cli as cli_mod
        from rich.console import Console

        monkeypatch.setattr(cli_mod, "console", Console(width=70, force_terminal=True))
        client = _make_client()
        client.list_tasks.return_value = {
            "tasks": [
                {
                    "name": "narrow-task",
                    "alias": None,
                    "status": "WORKING",
                    "created_at": "2026-04-13T00:00:00+00:00",
                    "status_changed_at": "2026-04-13T01:00:00+00:00",
                    "needs_review": False,
                },
            ],
        }
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["ls"])
        assert result.exit_code == 0
        out = _strip_ansi(result.output)
        assert "narrow-task" in out
        assert "Last Changed" in out
        assert "Cost" not in out
        assert "Created" not in out

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
        assert "To reply to the task, run ilan re aa" in _strip_ansi(result.output)

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
        assert "To reply to the task, run ilan re my-task" in _strip_ansi(result.output)

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
        assert "To reply to the task, run ilan re my-task" in _strip_ansi(result.output)

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
        assert "To reply to the task, run ilan re aa" in _strip_ansi(result.output)

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
        assert "To reply to the task, run ilan re aa" in _strip_ansi(result.output)

    def test_tail_hint_command_uses_distinct_color(self) -> None:
        """The ``ilan re <alias>`` portion is styled distinctly from the prose.

        The prose stays dim (SGR 2); the command portion drops dim and
        switches to bright red (SGR 91) so it actually pops against the gray
        prose. We render
        through a real Rich console with forced truecolor so the styling
        actually emits (CliRunner's captured output runs Rich in a degraded
        color mode that drops standalone foreground colors).
        """
        import io

        from rich.console import Console

        from ilan import cli as cli_mod

        buf = io.StringIO()
        forced = Console(
            file=buf,
            force_terminal=True,
            color_system="truecolor",
            no_color=False,
            width=120,
        )
        with patch.object(cli_mod, "console", forced):
            cli_mod._print_reply_hint("aa")
        out = buf.getvalue()
        # Rich emits one SGR per span. The prose span is plain dim
        # (``\x1b[2m``); the command span is bright red (``\x1b[91m``).
        assert "\x1b[2mTo reply to the task, run " in out
        assert "\x1b[91milan re aa" in out


# ── last-model line above the reply hint ────────────────────────────


class TestTailLastModelHint:
    """``ilan tail`` shows the model of the last assistant message just above
    the reply hint."""

    def _assistant_tail(self) -> dict:
        return {
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

    def test_model_line_printed_above_reply_hint(
        self, runner: CliRunner, tmp_config
    ) -> None:
        client = _make_client()
        client.get_tail.return_value = self._assistant_tail()
        client.get_last_model.return_value = {
            "name": "my-task",
            "model": "claude-opus-4-8",
        }
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["tail", "my-task"])
        assert result.exit_code == 0
        out = _strip_ansi(result.output)
        assert "The last assistant message is generated by claude-opus-4-8" in out
        # Ordering: model line sits directly above the reply hint.
        assert out.index("generated by claude-opus-4-8") < out.index(
            "To reply to the task"
        )
        client.get_last_model.assert_called_once_with("my-task")

    def test_warns_on_server_error(self, runner: CliRunner, tmp_config) -> None:
        client = _make_client()
        client.get_tail.return_value = self._assistant_tail()
        client.get_last_model.return_value = {"error": "No session log path"}
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["tail", "my-task"])
        assert result.exit_code == 0
        out = _strip_ansi(result.output)
        assert "The last assistant message is generated by" not in out
        assert (
            "Could not determine the last assistant model: No session log path" in out
        )
        assert "To reply to the task, run ilan re aa" in out

    def test_warns_when_server_unreachable(
        self, runner: CliRunner, tmp_config
    ) -> None:
        client = _make_client()
        client.get_tail.return_value = self._assistant_tail()
        client.get_last_model.side_effect = ConnectionError("boom")
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["tail", "my-task"])
        assert result.exit_code == 0
        out = _strip_ansi(result.output)
        assert "The last assistant message is generated by" not in out
        assert (
            "Could not determine the last assistant model: cannot reach the ilan server"
            in out
        )
        assert "To reply to the task, run ilan re aa" in out

    def test_model_name_uses_yellow(self) -> None:
        """The model name renders in yellow (SGR 33); the prose stays dim."""
        import io

        from rich.console import Console

        from ilan import cli as cli_mod

        client = _make_client()
        client.get_last_model.return_value = {"model": "claude-opus-4-8"}
        buf = io.StringIO()
        forced = Console(
            file=buf,
            force_terminal=True,
            color_system="truecolor",
            no_color=False,
            width=120,
        )
        with patch.object(cli_mod, "console", forced):
            cli_mod._print_last_model_hint(client, "my-task")
        out = buf.getvalue()
        assert "\x1b[2mThe last assistant message is generated by " in out
        assert "\x1b[33mclaude-opus-4-8" in out

    def test_uses_piggybacked_model_without_extra_request(
        self, runner: CliRunner, tmp_config
    ) -> None:
        """When the tail response carries ``last_assistant_model``, the model
        line is printed from it directly — no second ``get_last_model`` call."""
        client = _make_client()
        tail = self._assistant_tail()
        tail["last_assistant_model"] = "claude-haiku-4-5"
        client.get_tail.return_value = tail
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["tail", "my-task"])
        assert result.exit_code == 0
        out = _strip_ansi(result.output)
        assert "The last assistant message is generated by claude-haiku-4-5" in out
        client.get_last_model.assert_not_called()

    def test_falls_back_to_request_when_not_piggybacked(
        self, runner: CliRunner, tmp_config
    ) -> None:
        """An older server that omits ``last_assistant_model`` still works via
        the explicit ``get_last_model`` fallback."""
        client = _make_client()
        tail = self._assistant_tail()  # no last_assistant_model key
        client.get_tail.return_value = tail
        client.get_last_model.return_value = {"model": "claude-opus-4-8"}
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["tail", "my-task"])
        assert result.exit_code == 0
        out = _strip_ansi(result.output)
        assert "The last assistant message is generated by claude-opus-4-8" in out
        client.get_last_model.assert_called_once_with("my-task")


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


# ── ilan max / unmax (Fable model) ──────────────────────────────────


class TestMaxShorthand:
    def test_max_success(self, runner: CliRunner, tmp_config) -> None:
        client = _make_client()
        client.max_task.return_value = {"name": "my-task", "model": "claude-fable-5"}
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["max", "my-task"])
        assert result.exit_code == 0
        assert "FABLE" in result.output
        client.max_task.assert_called_once_with("my-task")

    def test_task_max_success(self, runner: CliRunner, tmp_config) -> None:
        client = _make_client()
        client.max_task.return_value = {"name": "my-task", "model": "claude-fable-5"}
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["task", "max", "my-task"])
        assert result.exit_code == 0
        assert "FABLE" in result.output
        client.max_task.assert_called_once_with("my-task")

    def test_max_error(self, runner: CliRunner, tmp_config) -> None:
        client = _make_client()
        client.max_task.return_value = {"error": "Task 'bad' not found"}
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["max", "bad"])
        assert result.exit_code != 0
        assert "not found" in result.output

    def test_max_no_args_shows_usage(self, runner: CliRunner, tmp_config) -> None:
        result = runner.invoke(main, ["max"])
        assert result.exit_code != 0


class TestUnmaxShorthand:
    def test_unmax_success(self, runner: CliRunner, tmp_config) -> None:
        client = _make_client()
        client.unmax_task.return_value = {"name": "my-task", "model": None}
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["unmax", "my-task"])
        assert result.exit_code == 0
        assert "default model" in result.output
        client.unmax_task.assert_called_once_with("my-task")

    def test_task_unmax_success(self, runner: CliRunner, tmp_config) -> None:
        client = _make_client()
        client.unmax_task.return_value = {"name": "my-task", "model": None}
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["task", "unmax", "my-task"])
        assert result.exit_code == 0
        assert "default model" in result.output
        client.unmax_task.assert_called_once_with("my-task")

    def test_unmax_error(self, runner: CliRunner, tmp_config) -> None:
        client = _make_client()
        client.unmax_task.return_value = {"error": "Task 'bad' not found"}
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["unmax", "bad"])
        assert result.exit_code != 0
        assert "not found" in result.output

    def test_unmax_no_args_shows_usage(self, runner: CliRunner, tmp_config) -> None:
        result = runner.invoke(main, ["unmax"])
        assert result.exit_code != 0


# ── FABLE rendering in ilan ls ──────────────────────────────────────


class TestFableRendering:
    def test_ls_shows_fable_for_maxed_task(
        self, runner: CliRunner, tmp_config, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # The FABLE note lives in the Name column, which is always shown —
        # even on a narrow terminal. Force a narrow console to prove it.
        import ilan.cli as cli_mod
        from rich.console import Console

        monkeypatch.setattr(cli_mod, "console", Console(width=70, force_terminal=True))
        client = _make_client()
        client.list_tasks.return_value = {
            "tasks": [
                {
                    "name": "maxed-task",
                    "alias": "aa",
                    "status": "WORKING",
                    "created_at": "2026-04-13T00:00:00+00:00",
                    "status_changed_at": "2026-04-13T01:00:00+00:00",
                    "needs_review": False,
                    "model": "claude-fable-5",
                },
            ],
        }
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["ls"])
        assert result.exit_code == 0
        assert "FABLE" in result.output

    def test_ls_no_fable_for_default_task(self, runner: CliRunner, tmp_config) -> None:
        client = _make_client()
        client.list_tasks.return_value = {
            "tasks": [
                {
                    "name": "plain-task",
                    "alias": "aa",
                    "status": "WORKING",
                    "created_at": "2026-04-13T00:00:00+00:00",
                    "status_changed_at": "2026-04-13T01:00:00+00:00",
                    "needs_review": False,
                    "model": None,
                },
            ],
        }
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["ls"])
        assert result.exit_code == 0
        assert "FABLE" not in result.output

    def test_fable_note_is_in_name_cell(self) -> None:
        """The FABLE note lives in the Name cell, on its own line."""
        row = {
            "name": "maxed-task",
            "alias": "aa",
            "status": "WORKING",
            "needs_review": False,
            "model": "claude-fable-5",
        }
        name_cell = _build_name_cell(row, "")
        assert "FABLE" in name_cell.plain
        # The note sits on a separate line beneath the "(alias) name".
        assert name_cell.plain.splitlines() == ["(aa) maxed-task", "FABLE"]

    def test_name_cell_no_fable_for_default_task(self) -> None:
        row = {"name": "plain-task", "alias": "", "status": "WORKING",
               "needs_review": False, "model": None}
        name_cell = _build_name_cell(row, "")
        assert "FABLE" not in name_cell.plain


# ── ilan add --claude / --codex ─────────────────────────────────────


class TestAddAgent:
    def test_codex_flag_passed_through(self, runner: CliRunner, tmp_config) -> None:
        client = _make_client()
        client.add_task.return_value = {"ok": True}
        with patch("ilan.cli._client", return_value=client), \
             patch("ilan.cli.shutil.which", return_value="/usr/bin/tmux"):
            result = runner.invoke(
                main, ["add", "-n", "codex-task", "-d", "do it", "--codex"]
            )
        assert result.exit_code == 0
        client.add_task.assert_called_once_with("codex-task", "do it", "codex")

    def test_claude_flag_passed_through(self, runner: CliRunner, tmp_config) -> None:
        client = _make_client()
        client.add_task.return_value = {"ok": True}
        with patch("ilan.cli._client", return_value=client), \
             patch("ilan.cli.shutil.which", return_value="/usr/bin/tmux"):
            result = runner.invoke(
                main, ["add", "-n", "claude-task", "-d", "do it", "--claude"]
            )
        assert result.exit_code == 0
        client.add_task.assert_called_once_with("claude-task", "do it", "claude")

    def test_agent_omitted_defaults_to_none(self, runner: CliRunner, tmp_config) -> None:
        client = _make_client()
        client.add_task.return_value = {"ok": True}
        with patch("ilan.cli._client", return_value=client), \
             patch("ilan.cli.shutil.which", return_value="/usr/bin/tmux"):
            result = runner.invoke(main, ["add", "-n", "plain-task", "-d", "do it"])
        assert result.exit_code == 0
        client.add_task.assert_called_once_with("plain-task", "do it", None)


# ── engine colour in the name cell ──────────────────────────────────

class TestNameCellEngineColour:
    def _row(self, engine: str | None) -> dict:
        row = {"name": "t", "alias": "", "status": "WORKING", "needs_review": False}
        if engine is not None:
            row["engine"] = engine
        return row

    def test_claude_name_uses_orange_style(self) -> None:
        cell = _build_name_cell(self._row(ENGINE_CLAUDE), "")
        styles = " ".join(str(span.style) for span in cell.spans)
        assert ENGINE_NAME_STYLE[ENGINE_CLAUDE] in styles

    def test_codex_name_uses_blue_style(self) -> None:
        cell = _build_name_cell(self._row(ENGINE_CODEX), "")
        styles = " ".join(str(span.style) for span in cell.spans)
        assert ENGINE_NAME_STYLE[ENGINE_CODEX] in styles

    def test_missing_engine_defaults_to_claude_style(self) -> None:
        cell = _build_name_cell(self._row(None), "")
        styles = " ".join(str(span.style) for span in cell.spans)
        assert ENGINE_NAME_STYLE[ENGINE_CLAUDE] in styles


class TestSwitchBackendCommand:
    def test_calls_client_and_reports_transition(
        self, runner: CliRunner, tmp_config
    ) -> None:
        client = _make_client()
        client.switch_backend.return_value = {
            "ok": True,
            "name": "my-task",
            "from_engine": "claude",
            "engine": "codex",
        }
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["task", "switch-backend", "my-task"])
        assert result.exit_code == 0
        client.switch_backend.assert_called_once_with("my-task")
        out = _strip_ansi(result.output)
        assert "my-task" in out
        assert "claude" in out and "codex" in out

    def test_error_exits_nonzero(self, runner: CliRunner, tmp_config) -> None:
        client = _make_client()
        client.switch_backend.return_value = {"error": "Task foo is DONE; cannot switch its backend."}
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["task", "switch-backend", "foo"])
        assert result.exit_code == 1
