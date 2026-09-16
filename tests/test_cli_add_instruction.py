"""Tests for the bare-instruction form of ``ilan add``.

``ilan add "…"`` is ``ilan add -d "…"`` with no other flag allowed: a burnable
task on the default backend and its default model. Any flag typed next to the
bare instruction is refused before a request goes out.
"""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner, Result

from ilan.cli import main

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

# The bare form is on the shorthand and the canonical command alike.
_ADD_PREFIXES = [["add"], ["task", "add"]]

_INSTRUCTION = "Check whether the flaky test still flakes"


@pytest.fixture()
def runner() -> CliRunner:
    return CliRunner()


def _unwrap(s: str) -> str:
    """Drop colour codes and collapse whitespace, so asserts survive Rich
    wrapping the line."""
    return " ".join(_ANSI_RE.sub("", s).split())


def _make_client() -> MagicMock:
    client = MagicMock()
    client.add_task.return_value = {"ok": True, "name": "xxx-cat-likes-fin"}
    return client


def _invoke(runner: CliRunner, argv: list[str], client: MagicMock) -> Result:
    with patch("ilan.cli._client", return_value=client), \
         patch("ilan.cli.shutil.which", return_value="/usr/bin/tmux"):
        return runner.invoke(main, argv)


class TestBareInstruction:
    @pytest.mark.parametrize("prefix", _ADD_PREFIXES)
    def test_it_adds_a_burnable_task_on_the_defaults(
        self, runner: CliRunner, tmp_config: Path, prefix: list[str]
    ) -> None:
        client = _make_client()
        result = _invoke(runner, [*prefix, _INSTRUCTION], client)
        assert result.exit_code == 0, result.output
        # No name, no backend, no max model: the server mints a burnable name
        # and applies `default-backend` and its default model.
        client.add_task.assert_called_once_with(
            None, _INSTRUCTION, None, max_model=False
        )
        out = _unwrap(result.output)
        assert "Task xxx-cat-likes-fin added." in out
        assert "Burnable" in out

    def test_it_sends_exactly_what_dash_d_would(
        self, runner: CliRunner, tmp_config: Path
    ) -> None:
        bare, flagged = _make_client(), _make_client()
        _invoke(runner, ["add", _INSTRUCTION], bare)
        _invoke(runner, ["add", "-d", _INSTRUCTION], flagged)
        assert bare.add_task.call_args == flagged.add_task.call_args

    def test_the_instruction_is_passed_verbatim(
        self, runner: CliRunner, tmp_config: Path
    ) -> None:
        """Like -d, the bare form neither strips nor reflows the text."""
        client = _make_client()
        text = "  two words\nand a second line  "
        result = _invoke(runner, ["add", text], client)
        assert result.exit_code == 0, result.output
        client.add_task.assert_called_once_with(None, text, None, max_model=False)

    def test_a_double_dash_lets_an_instruction_start_with_a_dash(
        self, runner: CliRunner, tmp_config: Path
    ) -> None:
        client = _make_client()
        result = _invoke(runner, ["add", "--", "--dry-run the deploy script"], client)
        assert result.exit_code == 0, result.output
        client.add_task.assert_called_once_with(
            None, "--dry-run the deploy script", None, max_model=False
        )

    def test_a_second_bare_argument_is_a_usage_error(
        self, runner: CliRunner, tmp_config: Path
    ) -> None:
        """An unquoted instruction is not silently joined back together."""
        client = _make_client()
        result = _invoke(runner, ["add", "Check", "the", "flaky", "test"], client)
        assert result.exit_code == 2
        assert "unexpected extra argument" in result.output.lower()
        client.add_task.assert_not_called()

    def test_the_flag_forms_are_untouched(
        self, runner: CliRunner, tmp_config: Path
    ) -> None:
        client = _make_client()
        result = _invoke(
            runner, ["add", "-n", "fix-bug", "-d", "p", "--codex", "--max"], client
        )
        assert result.exit_code == 0, result.output
        client.add_task.assert_called_once_with("fix-bug", "p", "codex", max_model=True)

    def test_nothing_at_all_is_still_an_error(
        self, runner: CliRunner, tmp_config: Path
    ) -> None:
        """Making the argument optional must not make the prompt optional."""
        client = _make_client()
        result = _invoke(runner, ["add"], client)
        assert result.exit_code == 1
        client.add_task.assert_not_called()


# Each refused flag set, with how the error echoes it back.
_REFUSED = [
    (["-n", "fix-bug"], "-n fix-bug"),
    (["--claude"], "--claude"),
    (["--codex"], "--codex"),
    (["--max"], "--max"),
    (["--codex", "--max"], "--codex --max"),
    (["-n", "fix-bug", "--claude", "--max"], "-n fix-bug --claude --max"),
]


class TestFlagsAreRefusedNextToIt:
    @pytest.mark.parametrize("prefix", _ADD_PREFIXES)
    @pytest.mark.parametrize("flags, echoed", _REFUSED)
    def test_after_the_instruction(
        self, runner: CliRunner, tmp_config: Path, prefix: list[str],
        flags: list[str], echoed: str,
    ) -> None:
        client = _make_client()
        result = _invoke(runner, [*prefix, _INSTRUCTION, *flags], client)
        assert result.exit_code == 1
        client.add_task.assert_not_called()
        out = _unwrap(result.output)
        assert f"takes no flags, but got {echoed}" in out
        # The fix is spelled out with the same flags, ready to reuse.
        assert f'ilan add -d "…" {echoed}' in out

    @pytest.mark.parametrize("flags, echoed", _REFUSED)
    def test_before_the_instruction_too(
        self, runner: CliRunner, tmp_config: Path, flags: list[str], echoed: str
    ) -> None:
        """Order does not make it a different command: `--max "…"` is refused
        exactly as `"…" --max` is."""
        client = _make_client()
        result = _invoke(runner, ["add", *flags, _INSTRUCTION], client)
        assert result.exit_code == 1
        client.add_task.assert_not_called()
        assert f"got {echoed}" in _unwrap(result.output)

    def test_dash_d_as_well_is_the_instruction_twice(
        self, runner: CliRunner, tmp_config: Path
    ) -> None:
        client = _make_client()
        result = _invoke(runner, ["add", "one", "-d", "two"], client)
        assert result.exit_code == 1
        client.add_task.assert_not_called()
        assert "given twice" in _unwrap(result.output)

    def test_dash_f_as_well_is_one_prompt_too_many(
        self, runner: CliRunner, tmp_config: Path, tmp_path: Path
    ) -> None:
        prompt = tmp_path / "prompt.md"
        prompt.write_text("from a file")
        client = _make_client()
        result = _invoke(runner, ["add", "bare", "-f", str(prompt)], client)
        assert result.exit_code == 1
        client.add_task.assert_not_called()
        assert "one too many" in _unwrap(result.output)

    def test_it_is_refused_before_the_tmux_check(
        self, runner: CliRunner, tmp_config: Path
    ) -> None:
        """The message is about the flags, not about a missing tmux."""
        client = _make_client()
        with patch("ilan.cli._client", return_value=client), \
             patch("ilan.cli.shutil.which", return_value=None):
            result = runner.invoke(main, ["add", _INSTRUCTION, "--max"])
        assert result.exit_code == 1
        assert "tmux" not in result.output
        client.add_task.assert_not_called()


class TestHelp:
    @pytest.mark.parametrize("prefix", _ADD_PREFIXES)
    def test_usage_shows_the_optional_instruction(
        self, runner: CliRunner, prefix: list[str]
    ) -> None:
        result = runner.invoke(main, [*prefix, "--help"])
        assert result.exit_code == 0, result.output
        assert "[OPTIONS] [INSTRUCTION]" in result.output
