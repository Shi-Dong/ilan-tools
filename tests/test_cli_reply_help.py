"""Tests for ``-h``/``--help`` on the reply commands."""

from __future__ import annotations

import re
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from ilan.cli import main

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

# One help text serves all three spellings; each check runs through all of
# them rather than trusting that the wiring matches.
_REPLY_PREFIXES = [["task", "reply"], ["reply"], ["re"]]

# Every flag the command takes, as Click lists it under Options.
_REPLY_FLAGS = [
    "-n, --num",
    "-m, --md",
    "--line-number / --no-line-number",
    "--max",
    "--unmax",
    "-t, --every",
    "-e, --editor",
    "-u, --update",
    "-h, --help",
]


@pytest.fixture()
def runner() -> CliRunner:
    return CliRunner()


def _help(runner: CliRunner, argv: list[str]) -> str:
    result = runner.invoke(main, argv)
    assert result.exit_code == 0, result.output
    return _ANSI_RE.sub("", result.output)


class TestDashH:
    @pytest.mark.parametrize("prefix", _REPLY_PREFIXES)
    def test_it_prints_the_same_help_as_dash_dash_help(
        self, runner: CliRunner, prefix: list[str]
    ) -> None:
        assert _help(runner, [*prefix, "-h"]) == _help(runner, [*prefix, "--help"])

    @pytest.mark.parametrize("prefix", _REPLY_PREFIXES)
    def test_every_flag_is_listed(self, runner: CliRunner, prefix: list[str]) -> None:
        out = _help(runner, [*prefix, "-h"])
        for flag in _REPLY_FLAGS:
            assert flag in out, flag

    def test_it_wins_over_a_task_name_and_touches_no_server(
        self, runner: CliRunner
    ) -> None:
        """`ilan re fix-bug -h` asks for help; it is not a reply to fix-bug."""
        client = MagicMock()
        with patch("ilan.cli._client", return_value=client):
            out = _help(runner, ["re", "fix-bug", "-h"])
        assert "[OPTIONS] NAME [MESSAGE]" in out
        client.reply.assert_not_called()
        client.get_task.assert_not_called()

    def test_other_commands_keep_the_default(self, runner: CliRunner) -> None:
        """Only reply opted in; elsewhere -h is still an unknown option."""
        result = runner.invoke(main, ["tail", "my-task", "-h"])
        assert result.exit_code != 0
        assert "No such option: -h" in result.output


class TestWhatTheHelpSays:
    @pytest.mark.parametrize("prefix", _REPLY_PREFIXES)
    def test_it_explains_each_way_the_flags_combine(
        self, runner: CliRunner, prefix: list[str]
    ) -> None:
        out = " ".join(_help(runner, [*prefix, "-h"]).split())
        # Sending, reading, looping (both -t forms and -u), the editor, the model.
        assert "Without MESSAGE, nothing is sent and the task's tail is shown" in out
        assert "-t DURATION together with MESSAGE sends it now and again" in out
        assert "-t DURATION on its own switches it to that cadence" in out
        assert "-u opens that message in your editor" in out
        assert "leaving the cadence alone" in out
        assert "-e writes MESSAGE in the editor" in out
        assert "--max and --unmax switch the task's model" in out

    @pytest.mark.parametrize("prefix", _REPLY_PREFIXES)
    def test_it_ends_with_an_example_of_each_mode(
        self, runner: CliRunner, prefix: list[str]
    ) -> None:
        out = _help(runner, [*prefix, "-h"])
        examples = out[out.index("Examples:"):]
        for snippet in (
            "ilan re fix-bug ",
            "-n 3 -m",
            '"Use the OAuth2 flow"',
            "-e ",
            '"Status?" -t 1h',
            "-t 30m",
            "-u ",
            '"Try again" --max',
        ):
            assert snippet in examples, snippet

    @pytest.mark.parametrize("prefix", _REPLY_PREFIXES)
    def test_the_examples_are_kept_on_their_own_lines(
        self, runner: CliRunner, prefix: list[str]
    ) -> None:
        """Click rewraps prose; the example table must come through verbatim."""
        out = _help(runner, [*prefix, "-h"])
        examples = out[out.index("Examples:"):].splitlines()[1:]
        commands = [line for line in examples if line.strip()]
        assert len(commands) == 8
        assert all(line.lstrip().startswith("ilan re fix-bug") for line in commands)

    @pytest.mark.parametrize("prefix", _REPLY_PREFIXES)
    def test_it_fits_in_eighty_columns(
        self, runner: CliRunner, prefix: list[str]
    ) -> None:
        for line in _help(runner, [*prefix, "-h"]).splitlines():
            assert len(line) <= 80, line


class TestTheCommandListingsAreUnchanged:
    def test_the_shorthands_still_read_as_shorthands(self, runner: CliRunner) -> None:
        """The long help must not leak into `ilan --help`'s one-line summaries."""
        out = _help(runner, ["--help"])
        assert out.count("Shorthand for 'ilan task reply'.") == 2

    def test_task_reply_keeps_its_summary(self, runner: CliRunner) -> None:
        out = " ".join(_help(runner, ["task", "--help"]).split())
        assert "Send a response to a task." in out
