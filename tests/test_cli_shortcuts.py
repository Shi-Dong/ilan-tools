"""Tests for CLI shortcut changes: ``ilan ls <name>`` → tail, and
``ilan undone`` / ``ilan undiscard`` top-level shorthands.
"""

from __future__ import annotations

import re
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from ilan.cli import (
    NUMBER_STYLE,
    PIN_MARKER,
    _build_concise_task_line,
    _build_name_cell,
    main,
)
from ilan.models import ENGINE_CLAUDE, ENGINE_CODEX, ENGINE_NAME_STYLE


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _strip_ansi(s: str) -> str:
    """Remove ANSI escape sequences so substring asserts survive Rich styling.

    The reply hint splits its prose and the ``ilan re <handle>`` command into
    two differently-styled spans; the resulting reset codes break a literal
    contiguous substring match against the rendered output.
    """
    return _ANSI_RE.sub("", s)


def _unwrap(s: str) -> str:
    """Collapse whitespace so asserts survive Rich wrapping the line.

    Rich breaks output at the console width (80 columns when not a tty), which
    can land mid-sentence in the longer hint lines.
    """
    return " ".join(s.split())


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

    def test_ls_is_flat_and_creation_ordered(
        self, runner: CliRunner, tmp_config, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A branched child sorts by its own creation time, not under its parent."""
        import ilan.cli as cli_mod
        from rich.console import Console

        monkeypatch.setattr(cli_mod, "console", Console(width=200, force_terminal=True))
        client = _make_client()
        client.list_tasks.return_value = {
            "tasks": [
                {
                    "name": "root-task", "alias": None, "status": "WORKING",
                    "created_at": "2026-04-13T00:00:00+00:00",
                    "status_changed_at": "2026-04-13T00:00:00+00:00",
                    "needs_review": False, "parent_name": None,
                },
                {
                    "name": "other-task", "alias": None, "status": "WORKING",
                    "created_at": "2026-04-13T01:00:00+00:00",
                    "status_changed_at": "2026-04-13T01:00:00+00:00",
                    "needs_review": False, "parent_name": None,
                },
                {
                    "name": "child-task", "alias": None, "status": "WORKING",
                    "created_at": "2026-04-13T02:00:00+00:00",
                    "status_changed_at": "2026-04-13T02:00:00+00:00",
                    "needs_review": False, "parent_name": "root-task",
                },
            ],
        }
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["ls"])
        assert result.exit_code == 0
        out = _strip_ansi(result.output)
        assert out.index("root-task") < out.index("other-task") < out.index("child-task")
        # The child starts at the same column as the roots (no tree indent).
        # Rich's own table borders use the same box-drawing glyphs a tree
        # prefix would, so compare offsets rather than grepping for glyphs.
        offsets = {
            name: next(line.index(name) for line in out.splitlines() if name in line)
            for name in ("root-task", "other-task", "child-task")
        }
        assert len(set(offsets.values())) == 1

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

    def test_ls_concise_shows_only_pin_alias_name_and_status(
        self, runner: CliRunner, tmp_config, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import ilan.cli as cli_mod
        from rich.console import Console

        monkeypatch.setattr(
            cli_mod, "console", Console(width=10, force_terminal=True)
        )
        client = _make_client()
        client.get_config.return_value = {"config": {}}
        client.list_tasks.return_value = {
            "tasks": [
                {
                    "name": "a-very-long-task-name",
                    "alias": "as",
                    "status": "AGENT_FINISHED",
                    "engine": ENGINE_CLAUDE,
                    "pinned": True,
                    "needs_review": True,
                    "model": "claude-fable-5",
                    "created_at": "2026-04-13T00:00:00+00:00",
                    "status_changed_at": "2026-04-13T01:00:00+00:00",
                    "gist_url": "https://example.com/history",
                    "summary_one_liner": "Extra summary text",
                },
            ],
        }
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["ls", "-c"])
        assert result.exit_code == 0
        assert _strip_ansi(result.output) == (
            "→ (as) a-very-long-task-name AGENT_FINISHED\n"
        )
        client.list_tasks.assert_called_once_with(show_all=False)
        client.get_config.assert_not_called()

    def test_ls_all_concise_includes_terminal_tasks(
        self, runner: CliRunner, tmp_config, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import ilan.cli as cli_mod
        from rich.console import Console

        monkeypatch.setattr(
            cli_mod, "console", Console(width=200, force_terminal=True)
        )
        client = _make_client()
        client.list_tasks.return_value = {
            "tasks": [
                {
                    "name": "done-task",
                    "alias": None,
                    "status": "DONE",
                    "engine": ENGINE_CLAUDE,
                },
                {
                    "name": "discarded-task",
                    "alias": "dd",
                    "status": "DISCARDED",
                    "engine": ENGINE_CODEX,
                },
            ],
        }
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["ls", "-a", "-c"])
        assert result.exit_code == 0
        assert _strip_ansi(result.output) == (
            "done-task DONE\n"
            "(dd) discarded-task DISCARDED\n"
        )
        client.list_tasks.assert_called_once_with(show_all=True)

    def test_task_ls_concise_empty_is_silent(
        self, runner: CliRunner, tmp_config,
    ) -> None:
        client = _make_client()
        client.list_tasks.return_value = {"tasks": []}
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["task", "ls", "--concise"])
        assert result.exit_code == 0
        assert result.output == ""
        client.list_tasks.assert_called_once_with(show_all=False)

    def test_concise_line_preserves_pin_alias_name_and_status_styles(self) -> None:
        line = _build_concise_task_line(
            {
                "name": "styled-task",
                "alias": "as",
                "status": "AGENT_FINISHED",
                "engine": ENGINE_CLAUDE,
                "pinned": True,
            }
        )
        assert line.plain == "→ (as) styled-task AGENT_FINISHED"
        assert [
            (line.plain[span.start:span.end], span.style)
            for span in line.spans
        ] == [
            ("→ ", "bold yellow"),
            ("(as) ", "bold magenta"),
            ("styled-task", "bold orange1"),
            ("AGENT_FINISHED", "green"),
        ]

    @staticmethod
    def _one_liner_row() -> dict:
        return {
            "name": "summary-task",
            "alias": None,
            "status": "WORKING",
            "created_at": "2026-04-13T00:00:00+00:00",
            "status_changed_at": "2026-04-13T01:00:00+00:00",
            "needs_review": False,
            "summary_one_liner": "Refactoring the frobnicator",
        }

    def test_ls_shows_one_liner(
        self, runner: CliRunner, tmp_config, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import ilan.cli as cli_mod
        from rich.console import Console

        monkeypatch.setattr(cli_mod, "console", Console(width=200, force_terminal=True))
        client = _make_client()
        client.list_tasks.return_value = {"tasks": [self._one_liner_row()]}
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["ls"])
        assert result.exit_code == 0
        assert "Refactoring the frobnicator" in _strip_ansi(result.output)

    def test_ls_all_hides_one_liner(
        self, runner: CliRunner, tmp_config, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``ilan ls -a`` never shows the one-line summary."""
        import ilan.cli as cli_mod
        from rich.console import Console

        monkeypatch.setattr(cli_mod, "console", Console(width=200, force_terminal=True))
        client = _make_client()
        client.list_tasks.return_value = {"tasks": [self._one_liner_row()]}
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["ls", "-a"])
        assert result.exit_code == 0
        out = _strip_ansi(result.output)
        assert "summary-task" in out
        assert "Refactoring the frobnicator" not in out


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
        """When the server returns an alias, the hint offers alias and name."""
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
        assert (
            "To reply to the task, run ilan re aa, or ilan re my-task"
            in _strip_ansi(result.output)
        )

    def test_tail_hint_falls_back_to_name_without_alias(
        self, runner: CliRunner, tmp_config
    ) -> None:
        """If the task has no alias, only the task name is offered."""
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
        out = _strip_ansi(result.output)
        assert "To reply to the task, run ilan re my-task" in out
        assert ", or ilan re" not in out

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
        out = _strip_ansi(result.output)
        assert "To reply to the task, run ilan re my-task" in out
        assert ", or ilan re" not in out

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
        assert (
            "To reply to the task, run ilan re aa, or ilan re my-task"
            in _strip_ansi(result.output)
        )

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
        assert (
            "To reply to the task, run ilan re aa, or ilan re my-task"
            in _strip_ansi(result.output)
        )

    def test_tail_hint_command_uses_distinct_color(self) -> None:
        """Each whole ``ilan re <handle>`` command is styled apart from the prose.

        The prose stays dim (SGR 2); both commands — the command word
        included, not just the handle — drop dim and switch to bright red
        (SGR 91) so they pop against the gray prose. We render
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
            cli_mod._print_reply_hint("aa", "my-task")
        out = buf.getvalue()
        # Rich emits one SGR per span. The prose spans are plain dim
        # (``\x1b[2m``); each command span is bright red (``\x1b[91m``).
        assert "\x1b[2mTo reply to the task, run " in out
        assert "\x1b[91milan re aa" in out
        assert "\x1b[2m, or " in out
        assert "\x1b[91milan re my-task" in out


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

    def test_token_usage_printed_below_model_line(
        self, runner: CliRunner, tmp_config
    ) -> None:
        client = _make_client()
        tail = self._assistant_tail()
        tail["last_assistant_model"] = "claude-opus-5"
        tail["entries"][0].update({
            "input_tokens": 12_345,
            "output_tokens": 678,
            "cache_read_input_tokens": 90_000,
        })
        client.get_tail.return_value = tail
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["tail", "my-task"])
        assert result.exit_code == 0
        out = _unwrap(_strip_ansi(result.output))
        model_line = "The last assistant message is generated by claude-opus-5"
        token_line = "Tokens: input 12,345, output 678, cached input 90,000"
        assert model_line in out
        assert token_line in out
        assert out.index(model_line) < out.index(token_line) < out.index(
            "To reply to the task"
        )

    def test_unknown_token_usage_omits_line(
        self, runner: CliRunner, tmp_config
    ) -> None:
        client = _make_client()
        tail = self._assistant_tail()
        tail["last_assistant_model"] = "claude-opus-5"
        client.get_tail.return_value = tail
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["tail", "my-task"])
        assert result.exit_code == 0
        assert "Tokens:" not in _strip_ansi(result.output)

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

    def test_piggybacked_effort_renders_suffix(
        self, runner: CliRunner, tmp_config
    ) -> None:
        """A piggybacked ``last_assistant_effort`` renders an ``(effort: …)``
        suffix after the model name."""
        client = _make_client()
        tail = self._assistant_tail()
        tail["last_assistant_model"] = "claude-haiku-4-5"
        tail["last_assistant_effort"] = "xhigh"
        client.get_tail.return_value = tail
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["tail", "my-task"])
        assert result.exit_code == 0
        out = _strip_ansi(result.output)
        assert (
            "The last assistant message is generated by claude-haiku-4-5"
            " (effort: xhigh)" in out
        )
        client.get_last_model.assert_not_called()

    def test_missing_effort_omits_suffix(
        self, runner: CliRunner, tmp_config
    ) -> None:
        """Without an effort (older server / pre-upgrade task) the model line
        is unchanged — no empty ``(effort: )`` stub."""
        client = _make_client()
        tail = self._assistant_tail()
        tail["last_assistant_model"] = "claude-haiku-4-5"
        client.get_tail.return_value = tail
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["tail", "my-task"])
        assert result.exit_code == 0
        out = _strip_ansi(result.output)
        assert "The last assistant message is generated by claude-haiku-4-5" in out
        assert "(effort:" not in out

    def test_fallback_effort_from_last_model_response(
        self, runner: CliRunner, tmp_config
    ) -> None:
        """The ``get_last_model`` fallback also carries the effort."""
        client = _make_client()
        client.get_tail.return_value = self._assistant_tail()  # no piggyback
        client.get_last_model.return_value = {
            "model": "claude-opus-4-8",
            "effort": "medium",
        }
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["tail", "my-task"])
        assert result.exit_code == 0
        out = _strip_ansi(result.output)
        assert (
            "The last assistant message is generated by claude-opus-4-8"
            " (effort: medium)" in out
        )
        client.get_last_model.assert_called_once_with("my-task")

    def test_piggybacked_budget_renders_after_effort(
        self, runner: CliRunner, tmp_config
    ) -> None:
        """Effort and budget share one parenthetical, in that order."""
        client = _make_client()
        tail = self._assistant_tail()
        tail["last_assistant_model"] = "claude-opus-5"
        tail["last_assistant_effort"] = "xhigh"
        tail["last_assistant_budget"] = "Team"
        client.get_tail.return_value = tail
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["tail", "my-task"])
        assert result.exit_code == 0
        out = _unwrap(_strip_ansi(result.output))
        assert (
            "The last assistant message is generated by claude-opus-5"
            " (effort: xhigh, budget: Team)" in out
        )
        client.get_last_model.assert_not_called()

    def test_budget_without_effort_renders_alone(
        self, runner: CliRunner, tmp_config
    ) -> None:
        """A budget with no effort still gets a well-formed parenthetical."""
        client = _make_client()
        tail = self._assistant_tail()
        tail["last_assistant_model"] = "gpt-5.6-sol"
        tail["last_assistant_budget"] = "API"
        client.get_tail.return_value = tail
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["tail", "my-task"])
        assert result.exit_code == 0
        out = _strip_ansi(result.output)
        assert (
            "The last assistant message is generated by gpt-5.6-sol"
            " (budget: API)" in out
        )

    def test_missing_budget_omits_it(self, runner: CliRunner, tmp_config) -> None:
        """An unresolvable budget leaves the line as it was before."""
        client = _make_client()
        tail = self._assistant_tail()
        tail["last_assistant_model"] = "claude-haiku-4-5"
        tail["last_assistant_effort"] = "high"
        client.get_tail.return_value = tail
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["tail", "my-task"])
        assert result.exit_code == 0
        out = _strip_ansi(result.output)
        assert (
            "The last assistant message is generated by claude-haiku-4-5"
            " (effort: high)" in out
        )
        assert "budget" not in out

    def test_fallback_budget_from_last_model_response(
        self, runner: CliRunner, tmp_config
    ) -> None:
        """The ``get_last_model`` fallback also carries the budget."""
        client = _make_client()
        client.get_tail.return_value = self._assistant_tail()  # no piggyback
        client.get_last_model.return_value = {
            "model": "claude-opus-4-8",
            "effort": "medium",
            "budget": "Max",
        }
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["tail", "my-task"])
        assert result.exit_code == 0
        out = _unwrap(_strip_ansi(result.output))
        assert (
            "The last assistant message is generated by claude-opus-4-8"
            " (effort: medium, budget: Max)" in out
        )

    def test_piggybacked_cost_renders_after_budget(
        self, runner: CliRunner, tmp_config
    ) -> None:
        """Cost joins the same parenthetical, last and rounded to cents."""
        client = _make_client()
        tail = self._assistant_tail()
        tail["last_assistant_model"] = "claude-opus-5"
        tail["last_assistant_effort"] = "xhigh"
        tail["last_assistant_budget"] = "API"
        tail["last_assistant_cost_usd"] = 1.2345
        client.get_tail.return_value = tail
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["tail", "my-task"])
        assert result.exit_code == 0
        out = _unwrap(_strip_ansi(result.output))
        assert (
            "The last assistant message is generated by claude-opus-5"
            " (effort: xhigh, budget: API, cost: $1.23)" in out
        )
        client.get_last_model.assert_not_called()

    def test_cost_without_effort_renders_after_budget(
        self, runner: CliRunner, tmp_config
    ) -> None:
        client = _make_client()
        tail = self._assistant_tail()
        tail["last_assistant_model"] = "claude-opus-5"
        tail["last_assistant_budget"] = "API"
        tail["last_assistant_cost_usd"] = 0.5
        client.get_tail.return_value = tail
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["tail", "my-task"])
        assert result.exit_code == 0
        out = _unwrap(_strip_ansi(result.output))
        assert (
            "The last assistant message is generated by claude-opus-5"
            " (budget: API, cost: $0.50)" in out
        )

    def test_subscription_cost_is_withheld(
        self, runner: CliRunner, tmp_config
    ) -> None:
        """A plan's price is notional, so the tail line never shows it."""
        client = _make_client()
        tail = self._assistant_tail()
        tail["last_assistant_model"] = "claude-opus-5"
        tail["last_assistant_effort"] = "xhigh"
        tail["last_assistant_budget"] = "Team"
        tail["last_assistant_cost_usd"] = 1.2345
        client.get_tail.return_value = tail
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["tail", "my-task"])
        assert result.exit_code == 0
        out = _unwrap(_strip_ansi(result.output))
        assert (
            "The last assistant message is generated by claude-opus-5"
            " (effort: xhigh, budget: Team)" in out
        )
        assert "cost" not in out

    def test_zero_cost_omits_it(self, runner: CliRunner, tmp_config) -> None:
        """Codex reports no cost, which must not render as a bogus $0.00."""
        client = _make_client()
        tail = self._assistant_tail()
        tail["last_assistant_model"] = "gpt-5.6-sol"
        tail["last_assistant_budget"] = "API"
        tail["last_assistant_cost_usd"] = 0.0
        client.get_tail.return_value = tail
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["tail", "my-task"])
        assert result.exit_code == 0
        out = _unwrap(_strip_ansi(result.output))
        assert (
            "The last assistant message is generated by gpt-5.6-sol"
            " (budget: API)" in out
        )
        assert "cost" not in out

    def test_fallback_cost_from_last_model_response(
        self, runner: CliRunner, tmp_config
    ) -> None:
        """The ``get_last_model`` fallback also carries the cost."""
        client = _make_client()
        client.get_tail.return_value = self._assistant_tail()  # no piggyback
        client.get_last_model.return_value = {
            "model": "claude-opus-4-8",
            "effort": "medium",
            "budget": "API",
            "cost_usd": 2.0,
        }
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["tail", "my-task"])
        assert result.exit_code == 0
        out = _unwrap(_strip_ansi(result.output))
        assert (
            "The last assistant message is generated by claude-opus-4-8"
            " (effort: medium, budget: API, cost: $2.00)" in out
        )


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

    def test_ilan_tail_n_counts_assistant_messages(
        self, runner: CliRunner, tmp_config
    ) -> None:
        client = _make_client()
        client.get_logs.return_value = self._logs_response()
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["tail", "my-task", "-n", "2"])
        assert result.exit_code == 0
        client.get_logs.assert_called_once_with("my-task")
        client.get_tail.assert_not_called()
        assert "u1" in result.output
        assert "a1" in result.output
        assert "u2" in result.output
        assert "a2" in result.output
        assert "u0" not in result.output
        assert "a0" not in result.output

    def test_task_tail_n_uses_logs(self, runner: CliRunner, tmp_config) -> None:
        client = _make_client()
        client.get_logs.return_value = self._logs_response()
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["task", "tail", "my-task", "-n", "1"])
        assert result.exit_code == 0
        client.get_logs.assert_called_once_with("my-task")
        assert "u2" in result.output
        assert "a2" in result.output
        assert "a1" not in result.output

    def test_tail_n_includes_user_message_after_latest_assistant(
        self, runner: CliRunner, tmp_config
    ) -> None:
        client = _make_client()
        client.get_logs.return_value = {
            "last_assistant_model": "gpt-5.6-sol",
            "logs": [
                {
                    "role": "assistant",
                    "content": "answer",
                    "timestamp": "2026-04-13T00:00:00+00:00",
                    "input_tokens": 2_480,
                    "output_tokens": 6,
                    "cache_read_input_tokens": 9_984,
                },
                {
                    "role": "user",
                    "content": "follow-up",
                    "timestamp": "2026-04-13T00:01:00+00:00",
                },
            ],
        }
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["tail", "my-task", "-n", "1"])
        assert result.exit_code == 0
        out = _unwrap(_strip_ansi(result.output))
        assert "answer" in out
        assert "follow-up" in out
        assert "Tokens: input 2,480, output 6, cached input 9,984" in out

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
        """If N exceeds the assistant-message count, return everything."""
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


# ── reply confirmation names the task ───────────────────────────────


class TestReplyConfirmation:
    """``ilan reply`` echoes the server's confirmation with the task name
    picked out from the surrounding green prose."""

    @staticmethod
    def _forced_console(buf) -> object:
        from rich.console import Console

        return Console(
            file=buf, force_terminal=True, color_system="truecolor",
            no_color=False, width=120,
        )

    def test_task_name_is_bold_and_not_green(self) -> None:
        """The prose is green (SGR 32); the task name is bold (1) cyan (36)."""
        import io

        from ilan import cli as cli_mod

        buf = io.StringIO()
        with patch.object(cli_mod, "console", self._forced_console(buf)):
            cli_mod._print_reply_confirmation(
                "Reply sent to my-task. Agent resumed.", "my-task"
            )
        out = buf.getvalue()
        assert "\x1b[32mReply sent to " in out
        assert "\x1b[1;36mmy-task" in out
        assert "\x1b[32m. Agent resumed." in out

    def test_old_server_message_stays_plain_green(self) -> None:
        """Servers that predate the named confirmation send no ``name``."""
        import io

        from ilan import cli as cli_mod

        buf = io.StringIO()
        with patch.object(cli_mod, "console", self._forced_console(buf)):
            cli_mod._print_reply_confirmation("Reply sent. Agent resumed.", None)
        out = buf.getvalue()
        assert "\x1b[32mReply sent. Agent resumed." in out
        assert "\x1b[1;36m" not in out

    def test_reply_prints_named_confirmation(
        self, runner: CliRunner, tmp_config
    ) -> None:
        client = _make_client()
        client.reply.return_value = {
            "name": "my-task",
            "message": "Reply sent to my-task. Agent resumed.",
        }
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["reply", "aa", "go on"])
        assert result.exit_code == 0
        assert "Reply sent to my-task. Agent resumed." in _strip_ansi(result.output)


# ── --max / --unmax on reply ────────────────────────────────────────


class TestReplyMaxFlags:
    """``ilan reply --max`` switches the task to Fable, then posts the reply;
    ``--unmax`` resets the model first. The switch persists (it is the same
    server-side pin as ``ilan max`` / ``ilan unmax``). When the model is
    already in the requested state the switch is a silent no-op."""

    def _client_for_reply(self, model: str | None = None) -> MagicMock:
        client = _make_client()
        client.get_task.return_value = {"task": {"name": "my-task", "model": model}}
        client.reply.return_value = {
            "name": "my-task",
            "message": "Reply sent to my-task. Agent resumed.",
        }
        client.max_task.return_value = {"name": "my-task", "model": "claude-fable-5"}
        client.unmax_task.return_value = {"name": "my-task", "model": None}
        return client

    def test_reply_max_switches_then_replies(
        self, runner: CliRunner, tmp_config
    ) -> None:
        client = self._client_for_reply()
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["reply", "my-task", "go on", "--max"])
        assert result.exit_code == 0
        assert "FABLE" in result.output
        client.max_task.assert_called_once_with("my-task")
        client.reply.assert_called_once_with("my-task", "go on")
        # The model switch lands before the reply so this turn runs on Fable.
        names = [c[0] for c in client.method_calls]
        assert names.index("max_task") < names.index("reply")

    def test_re_max_variant(self, runner: CliRunner, tmp_config) -> None:
        client = self._client_for_reply()
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["re", "my-task", "go on", "--max"])
        assert result.exit_code == 0
        client.max_task.assert_called_once_with("my-task")
        client.reply.assert_called_once_with("my-task", "go on")

    def test_task_reply_max_variant(self, runner: CliRunner, tmp_config) -> None:
        client = self._client_for_reply()
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(
                main, ["task", "reply", "my-task", "go on", "--max"]
            )
        assert result.exit_code == 0
        client.max_task.assert_called_once_with("my-task")
        client.reply.assert_called_once_with("my-task", "go on")

    def test_reply_max_on_codex_warns_and_still_replies(
        self, runner: CliRunner, tmp_config
    ) -> None:
        """On a codex task the server warns and leaves the model untouched;
        the reply must still be posted normally."""
        client = self._client_for_reply()
        client.max_task.return_value = {
            "ok": True,
            "name": "my-task",
            "model": None,
            "warning": (
                "Task my-task runs on the codex backend; Fable "
                "(claude-fable-5) is a Claude-only model, so max did nothing."
            ),
        }
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["reply", "my-task", "go on", "--max"])
        assert result.exit_code == 0
        # Collapse whitespace: rich wraps long warning lines at terminal width.
        out = re.sub(r"\s+", " ", _strip_ansi(result.output))
        assert "Claude-only model, so max did nothing" in out
        assert "FABLE" not in out  # no "set to FABLE" success line
        assert "Reply sent to my-task." in out
        client.reply.assert_called_once_with("my-task", "go on")

    def test_reply_unmax_resets_then_replies(
        self, runner: CliRunner, tmp_config
    ) -> None:
        client = self._client_for_reply(model="claude-fable-5")
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["reply", "my-task", "go on", "--unmax"])
        assert result.exit_code == 0
        assert "default model" in result.output
        client.unmax_task.assert_called_once_with("my-task")
        client.reply.assert_called_once_with("my-task", "go on")
        names = [c[0] for c in client.method_calls]
        assert names.index("unmax_task") < names.index("reply")

    def test_max_on_fable_task_is_silent_noop(
        self, runner: CliRunner, tmp_config
    ) -> None:
        """A task already on Fable is left alone: no switch call, no output
        beyond the reply confirmation."""
        client = self._client_for_reply(model="claude-fable-5")
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["reply", "my-task", "go on", "--max"])
        assert result.exit_code == 0
        client.max_task.assert_not_called()
        client.reply.assert_called_once_with("my-task", "go on")
        out = _strip_ansi(result.output)
        assert "FABLE" not in out
        assert "Reply sent to my-task." in out

    def test_unmax_on_default_model_is_silent_noop(
        self, runner: CliRunner, tmp_config
    ) -> None:
        """A task already on the default model is left alone by --unmax."""
        client = self._client_for_reply(model=None)
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["reply", "my-task", "go on", "--unmax"])
        assert result.exit_code == 0
        client.unmax_task.assert_not_called()
        client.reply.assert_called_once_with("my-task", "go on")
        out = _strip_ansi(result.output)
        assert "default model" not in out
        assert "Reply sent to my-task." in out

    def test_max_and_unmax_are_mutually_exclusive(
        self, runner: CliRunner, tmp_config
    ) -> None:
        client = self._client_for_reply()
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(
                main, ["reply", "my-task", "go on", "--max", "--unmax"]
            )
        assert result.exit_code != 0
        assert "cannot be used together" in _strip_ansi(result.output)
        client.max_task.assert_not_called()
        client.unmax_task.assert_not_called()
        client.reply.assert_not_called()

    def test_max_without_message_errors(
        self, runner: CliRunner, tmp_config
    ) -> None:
        client = self._client_for_reply()
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["reply", "my-task", "--max"])
        assert result.exit_code != 0
        assert "require a response message" in _strip_ansi(result.output)
        client.max_task.assert_not_called()
        client.reply.assert_not_called()

    def test_max_error_aborts_before_reply(
        self, runner: CliRunner, tmp_config
    ) -> None:
        client = self._client_for_reply()
        client.max_task.return_value = {"error": "Task 'my-task' not found"}
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["reply", "my-task", "go on", "--max"])
        assert result.exit_code != 0
        client.reply.assert_not_called()

    def test_plain_reply_touches_no_model(
        self, runner: CliRunner, tmp_config
    ) -> None:
        client = self._client_for_reply()
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["reply", "my-task", "go on"])
        assert result.exit_code == 0
        client.get_task.assert_not_called()
        client.max_task.assert_not_called()
        client.unmax_task.assert_not_called()
        client.reply.assert_called_once_with("my-task", "go on")


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
        name_cell = _build_name_cell(row)
        assert "FABLE" in name_cell.plain
        # The note sits on a separate line beneath the "(alias) name".
        assert name_cell.plain.splitlines() == ["(aa) maxed-task", "FABLE"]

    def test_name_cell_no_fable_for_default_task(self) -> None:
        row = {"name": "plain-task", "alias": "", "status": "WORKING",
               "needs_review": False, "model": None}
        name_cell = _build_name_cell(row)
        assert "FABLE" not in name_cell.plain

    def test_name_cell_fable_shown_on_claude_engine(self) -> None:
        """A Fable task still driven by Claude keeps the FABLE note."""
        row = {"name": "maxed-task", "alias": "", "status": "WORKING",
               "needs_review": False, "model": "claude-fable-5",
               "engine": "claude"}
        name_cell = _build_name_cell(row)
        assert "FABLE" in name_cell.plain

    def test_name_cell_no_fable_after_switch_to_codex(self) -> None:
        """Fable is Claude-only: once the task is switched to Codex the note is
        dropped even though the stored model is still Fable."""
        row = {"name": "maxed-task", "alias": "", "status": "WORKING",
               "needs_review": False, "model": "claude-fable-5",
               "engine": "codex"}
        name_cell = _build_name_cell(row)
        assert "FABLE" not in name_cell.plain

    def test_name_cell_fable_shown_for_unknown_engine(self) -> None:
        """An unrecognized engine runs on the Claude backend (Runner._backend_for
        falls back to it), which honors the Fable override — so the tag shows."""
        row = {"name": "maxed-task", "alias": "", "status": "WORKING",
               "needs_review": False, "model": "claude-fable-5",
               "engine": "some-future-engine"}
        name_cell = _build_name_cell(row)
        assert "FABLE" in name_cell.plain


# ── task numbers in listings ────────────────────────────────────────


class TestTaskNumberDisplay:
    @staticmethod
    def _row(**overrides) -> dict:
        row = {
            "name": "closed-task",
            "alias": None,
            "number": 12,
            "status": "DONE",
            "needs_review": False,
            "created_at": "2026-04-13T00:00:00+00:00",
            "status_changed_at": "2026-04-13T01:00:00+00:00",
        }
        row.update(overrides)
        return row

    def test_concise_line_shows_number(self) -> None:
        line = _build_concise_task_line(self._row())
        assert line.plain == "12 closed-task DONE"

    def test_name_cell_shows_number(self) -> None:
        assert _build_name_cell(self._row()).plain == "12 closed-task"

    def test_number_is_dim(self) -> None:
        """The number is a quiet handle, not a headline like the alias."""
        cell = _build_name_cell(self._row())
        number_span = next(s for s in cell.spans if cell.plain[s.start:s.end] == "12 ")
        assert number_span.style == NUMBER_STYLE == "dim"

    def test_number_precedes_alias(self) -> None:
        """A DISCARDED task keeps its alias, so both markers show."""
        row = self._row(status="DISCARDED", alias="aa")
        assert _build_name_cell(row).plain == "12 (aa) closed-task"

    def test_number_follows_pin_marker(self) -> None:
        row = self._row(pinned=True)
        assert _build_name_cell(row).plain == f"{PIN_MARKER}12 closed-task"

    def test_live_task_hides_its_number(self) -> None:
        """A revived task keeps its number but cannot be undone by it."""
        row = self._row(status="NEEDS_ATTENTION", alias="aa")
        assert _build_name_cell(row).plain == "(aa) closed-task"
        assert _build_concise_task_line(row).plain == "(aa) closed-task NEEDS_ATTENTION"

    def test_task_closed_before_numbering_shows_none(self) -> None:
        """Tasks already DONE when this shipped have no number to show."""
        row = self._row(number=None)
        assert _build_name_cell(row).plain == "closed-task"

    def test_ls_all_shows_number(self, runner: CliRunner, tmp_config) -> None:
        client = _make_client()
        client.list_tasks.return_value = {"tasks": [self._row()]}
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["ls", "-a"])
        assert result.exit_code == 0
        assert "12 closed-task" in result.output

    def test_search_shows_number(self, runner: CliRunner, tmp_config) -> None:
        client = _make_client()
        client.list_tasks.return_value = {"tasks": [self._row()]}
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["search", "closed-task"])
        assert result.exit_code == 0
        assert "12 closed-task" in result.output

    def test_search_matches_on_number(self, runner: CliRunner, tmp_config) -> None:
        client = _make_client()
        client.list_tasks.return_value = {"tasks": [self._row()]}
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["search", "12"])
        assert result.exit_code == 0
        assert "closed-task" in result.output


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
        client.add_task.assert_called_once_with(
            "codex-task", "do it", "codex", max_model=False
        )

    def test_claude_flag_passed_through(self, runner: CliRunner, tmp_config) -> None:
        client = _make_client()
        client.add_task.return_value = {"ok": True}
        with patch("ilan.cli._client", return_value=client), \
             patch("ilan.cli.shutil.which", return_value="/usr/bin/tmux"):
            result = runner.invoke(
                main, ["add", "-n", "claude-task", "-d", "do it", "--claude"]
            )
        assert result.exit_code == 0
        client.add_task.assert_called_once_with(
            "claude-task", "do it", "claude", max_model=False
        )

    def test_agent_omitted_defaults_to_none(self, runner: CliRunner, tmp_config) -> None:
        client = _make_client()
        client.add_task.return_value = {"ok": True}
        with patch("ilan.cli._client", return_value=client), \
             patch("ilan.cli.shutil.which", return_value="/usr/bin/tmux"):
            result = runner.invoke(main, ["add", "-n", "plain-task", "-d", "do it"])
        assert result.exit_code == 0
        client.add_task.assert_called_once_with(
            "plain-task", "do it", None, max_model=False
        )

    def test_max_flag_implies_claude_and_passes_through(
        self, runner: CliRunner, tmp_config
    ) -> None:
        client = _make_client()
        client.add_task.return_value = {"ok": True}
        with patch("ilan.cli._client", return_value=client), \
             patch("ilan.cli.shutil.which", return_value="/usr/bin/tmux"):
            result = runner.invoke(
                main, ["add", "-n", "fable-task", "-d", "do it", "--max"]
            )
        assert result.exit_code == 0
        client.add_task.assert_called_once_with(
            "fable-task", "do it", "claude", max_model=True
        )

    def test_max_with_codex_is_rejected(
        self, runner: CliRunner, tmp_config
    ) -> None:
        client = _make_client()
        client.add_task.return_value = {"ok": True}
        with patch("ilan.cli._client", return_value=client), \
             patch("ilan.cli.shutil.which", return_value="/usr/bin/tmux"):
            result = runner.invoke(
                main, ["add", "-n", "bad", "-d", "do it", "--codex", "--max"]
            )
        assert result.exit_code == 1
        client.add_task.assert_not_called()


# ── engine colour in the name cell ──────────────────────────────────

class TestNameCellEngineColour:
    def _row(self, engine: str | None) -> dict:
        row = {"name": "t", "alias": "", "status": "WORKING", "needs_review": False}
        if engine is not None:
            row["engine"] = engine
        return row

    def test_claude_name_uses_orange_style(self) -> None:
        cell = _build_name_cell(self._row(ENGINE_CLAUDE))
        styles = " ".join(str(span.style) for span in cell.spans)
        assert ENGINE_NAME_STYLE[ENGINE_CLAUDE] in styles

    def test_codex_name_uses_blue_style(self) -> None:
        cell = _build_name_cell(self._row(ENGINE_CODEX))
        styles = " ".join(str(span.style) for span in cell.spans)
        assert ENGINE_NAME_STYLE[ENGINE_CODEX] in styles

    def test_missing_engine_defaults_to_claude_style(self) -> None:
        cell = _build_name_cell(self._row(None))
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

    def test_top_level_shortcut(self, runner: CliRunner, tmp_config) -> None:
        client = _make_client()
        client.switch_backend.return_value = {
            "ok": True,
            "name": "my-task",
            "from_engine": "claude",
            "engine": "codex",
        }
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["switch-backend", "my-task"])
        assert result.exit_code == 0
        client.switch_backend.assert_called_once_with("my-task")
        out = _strip_ansi(result.output)
        assert "my-task" in out
        assert "claude" in out and "codex" in out


class TestRenameCommand:
    def _rename_client(self) -> MagicMock:
        client = _make_client()
        client.rename_task.return_value = {"old_name": "old-task", "new_name": "new-task"}
        client.reply.return_value = {"message": "replied"}
        return client

    def test_rename_without_description_does_not_reply(
        self, runner: CliRunner, tmp_config
    ) -> None:
        client = self._rename_client()
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["task", "rename", "old-task", "new-task"])
        assert result.exit_code == 0
        client.rename_task.assert_called_once_with("old-task", "new-task")
        client.reply.assert_not_called()

    def test_rename_with_description_replies_to_new_name(
        self, runner: CliRunner, tmp_config
    ) -> None:
        client = self._rename_client()
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(
                main,
                ["task", "rename", "old-task", "new-task", "-d", "keep going"],
            )
        assert result.exit_code == 0
        client.rename_task.assert_called_once_with("old-task", "new-task")
        client.reply.assert_called_once_with("new-task", "keep going")

    def test_top_level_shortcut_with_description(
        self, runner: CliRunner, tmp_config
    ) -> None:
        client = self._rename_client()
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(
                main, ["rename", "old-task", "new-task", "-d", "keep going"]
            )
        assert result.exit_code == 0
        client.rename_task.assert_called_once_with("old-task", "new-task")
        client.reply.assert_called_once_with("new-task", "keep going")

    def test_rename_error_skips_reply(self, runner: CliRunner, tmp_config) -> None:
        client = self._rename_client()
        client.rename_task.return_value = {"error": "no such task"}
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(
                main, ["rename", "old-task", "new-task", "-d", "keep going"]
            )
        assert result.exit_code == 1
        client.reply.assert_not_called()
