"""Tests for ``-h``/``--help`` on the add commands: a guide to ``ilan add``."""

from __future__ import annotations

import re
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from ilan.cli import main

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

# One help text serves both spellings; each check runs through both rather
# than trusting that the wiring matches.
_ADD_PREFIXES = [["task", "add"], ["add"]]

# Every flag the command takes, as Click lists it under Options.
_ADD_FLAGS = [
    "-n, --name",
    "-f, --file",
    "-d, --description",
    "--claude",
    "--codex",
    "--max",
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
    @pytest.mark.parametrize("prefix", _ADD_PREFIXES)
    def test_it_prints_the_same_help_as_dash_dash_help(
        self, runner: CliRunner, prefix: list[str]
    ) -> None:
        assert _help(runner, [*prefix, "-h"]) == _help(runner, [*prefix, "--help"])

    @pytest.mark.parametrize("prefix", _ADD_PREFIXES)
    def test_every_flag_is_listed(self, runner: CliRunner, prefix: list[str]) -> None:
        out = _help(runner, [*prefix, "-h"])
        for flag in _ADD_FLAGS:
            assert flag in out, flag

    def test_it_wins_over_a_bare_instruction_and_touches_no_server(
        self, runner: CliRunner
    ) -> None:
        """`ilan add "…" -h` asks for help; it does not add a task."""
        client = MagicMock()
        with patch("ilan.cli._client", return_value=client):
            out = _help(runner, ["add", "Check the flaky test", "-h"])
        assert "[OPTIONS] [INSTRUCTION]" in out
        client.add_task.assert_not_called()

    def test_it_wins_over_a_combination_the_command_would_refuse(
        self, runner: CliRunner
    ) -> None:
        """Help is answered before the flags are judged, so asking how they
        combine never comes back as the refusal itself."""
        client = MagicMock()
        with patch("ilan.cli._client", return_value=client):
            out = _help(runner, ["add", "Check the flaky test", "--max", "-h"])
        assert "takes no flags" not in out
        assert "Examples:" in out
        client.add_task.assert_not_called()

    def test_other_commands_keep_the_default(self, runner: CliRunner) -> None:
        """Only add and reply opted in; elsewhere -h is still an unknown option."""
        result = runner.invoke(main, ["ls", "-h"])
        assert result.exit_code != 0
        assert "No such option: -h" in result.output


class TestWhatTheHelpSays:
    @pytest.mark.parametrize("prefix", _ADD_PREFIXES)
    def test_it_explains_the_three_ways_to_give_the_prompt(
        self, runner: CliRunner, prefix: list[str]
    ) -> None:
        out = " ".join(_help(runner, [*prefix, "-h"]).split())
        assert '-d "…" gives it inline' in out
        assert "-f FILE reads it from a file" in out
        assert "A bare INSTRUCTION with no flag at all is the quick form" in out
        assert 'it means -d "…" on the defaults and takes nothing else' in out
        assert "refused rather than guessed at" in out
        assert "write the prompt after -d" in out

    @pytest.mark.parametrize("prefix", _ADD_PREFIXES)
    def test_it_explains_naming_and_burnable_names(
        self, runner: CliRunner, prefix: list[str]
    ) -> None:
        out = " ".join(_help(runner, [*prefix, "-h"]).split())
        assert "-n NAME is how every later command refers to the task" in out
        assert "three or more letters, digits, hyphens and underscores, not all digits" in out
        assert "two-letter alias" in out
        assert "generated burnable name such as xxx-cat-likes-fin" in out
        assert "ilan done and ilan discard delete a burnable task outright" in out
        assert "ilan rename turns a burnable task into a keeper" in out

    @pytest.mark.parametrize("prefix", _ADD_PREFIXES)
    def test_it_explains_the_backend_and_the_model(
        self, runner: CliRunner, prefix: list[str]
    ) -> None:
        out = " ".join(_help(runner, [*prefix, "-h"]).split())
        assert "--claude or --codex runs this task on that backend" in out
        assert "the default-backend config key decides" in out
        assert "--max starts the task on its backend's max model" in out
        assert "FABLE on Claude and ASTRA on Codex" in out
        assert "ilan unmax NAME steps it back down" in out
        assert "ilan max NAME steps a plain task up" in out

    @pytest.mark.parametrize("prefix", _ADD_PREFIXES)
    def test_it_says_what_to_run_next(
        self, runner: CliRunner, prefix: list[str]
    ) -> None:
        out = " ".join(_help(runner, [*prefix, "-h"]).split())
        assert "ilan ls shows the task and its status" in out
        assert "ilan tail NAME reads the agent's reply" in out
        assert 'ilan reply NAME "…" answers it' in out
        assert "ilan done NAME closes it" in out

    @pytest.mark.parametrize("prefix", _ADD_PREFIXES)
    def test_it_ends_with_an_example_of_each_form(
        self, runner: CliRunner, prefix: list[str]
    ) -> None:
        out = _help(runner, [*prefix, "-h"])
        examples = out[out.index("Examples:"):]
        for snippet in (
            'ilan add "Check whether the flaky test still flakes"',
            '-n fix-bug -d "Fix the crash in auth.py"',
            "-n refactor -f tasks/refactor.md",
            '-d "Port the parser to Rust" --codex',
            '-n hard-one -d "Prove the lemma" --max',
            '-d "Try the OAuth2 flow" --max',
        ):
            assert snippet in examples, snippet

    @pytest.mark.parametrize("prefix", _ADD_PREFIXES)
    def test_the_examples_are_kept_on_their_own_lines(
        self, runner: CliRunner, prefix: list[str]
    ) -> None:
        """Click rewraps prose; the example table must come through verbatim."""
        out = _help(runner, [*prefix, "-h"])
        examples = out[out.index("Examples:"):].splitlines()[1:]
        commands = [line for line in examples if line.strip()]
        assert len(commands) == 6
        assert all(line.lstrip().startswith("ilan add ") for line in commands)

    @pytest.mark.parametrize("prefix", _ADD_PREFIXES)
    def test_no_flag_name_is_split_across_lines(
        self, runner: CliRunner, prefix: list[str]
    ) -> None:
        """Click breaks on hyphens when wrapping, which would leave `--` at one
        line end and the flag's name at the next; the prose and the examples
        are worded so no flag lands there at the default width. (The Options
        column is Click's own layout and is not checked.)"""
        out = _help(runner, [*prefix, "-h"])
        prose = out[:out.index("Options:")]
        examples = out[out.index("Examples:"):]
        for line in (prose + examples).splitlines():
            assert not line.rstrip().endswith("-"), line

    @pytest.mark.parametrize("prefix", _ADD_PREFIXES)
    def test_it_fits_in_eighty_columns(
        self, runner: CliRunner, prefix: list[str]
    ) -> None:
        for line in _help(runner, [*prefix, "-h"]).splitlines():
            assert len(line) <= 80, line


class TestTheCommandListingsAreUnchanged:
    def test_the_shorthand_still_reads_as_a_shorthand(self, runner: CliRunner) -> None:
        """The long help must not leak into `ilan --help`'s one-line summaries."""
        out = _help(runner, ["--help"])
        assert out.count("Shorthand for 'ilan task add'.") == 1
        assert "quick form" not in out

    def test_task_add_keeps_its_summary(self, runner: CliRunner) -> None:
        out = " ".join(_help(runner, ["task", "--help"]).split())
        assert "Add a new task." in out
        assert "quick form" not in out
