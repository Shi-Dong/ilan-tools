"""Side questions accept exactly two positional arguments and no flags."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from ilan import config as cfg
from ilan.cli import _BTW_SUFFIX, main


@pytest.fixture()
def client(tmp_config: Path) -> MagicMock:
    client = MagicMock()
    client.btw_task.return_value = {
        "name": "xxx-parent-task-btw", "parent_name": "parent-task", "reasoning": "low",
    }
    return client


@pytest.mark.parametrize("prefix", [["btw"], ["task", "btw"]])
@pytest.mark.parametrize("parent_ref", ["parent-task", "aa"])
def test_positional_instruction_and_scope_suffix(
    client: MagicMock, prefix: list[str], parent_ref: str,
) -> None:
    instruction = "  Why this choice?\nExplain briefly.  "
    with patch("ilan.cli._client", return_value=client):
        result = CliRunner().invoke(main, [*prefix, parent_ref, instruction])
    assert result.exit_code == 0, result.output
    client.btw_task.assert_called_once_with(parent_ref, instruction + _BTW_SUFFIX)
    client.get_task.assert_not_called()
    client.branch_task.assert_not_called()
    assert "xxx-parent-task-btw" in result.output
    assert "Burnable" in result.output


@pytest.mark.parametrize("prefix", [["btw"], ["task", "btw"]])
@pytest.mark.parametrize("before", [False, True])
@pytest.mark.parametrize("flags", [
    ["-n", "child"], ["--name", "child"],
    ["-d", "question"], ["--description", "question"],
    ["-f", "question.txt"], ["--file", "question.txt"],
    ["--max"], ["--claude"], ["--codex"], ["--help"], ["-h"],
])
def test_every_flag_is_rejected_before_requests(
    client: MagicMock, prefix: list[str], before: bool, flags: list[str],
) -> None:
    args = [*flags, "aa", "Why?"] if before else ["aa", "Why?", *flags]
    with patch("ilan.cli._client", return_value=client):
        result = CliRunner().invoke(main, [*prefix, *args])
    assert result.exit_code == 2
    assert "No such option" in result.output
    assert client.method_calls == []


@pytest.mark.parametrize("prefix", [["btw"], ["task", "btw"]])
@pytest.mark.parametrize("args", [[], ["aa"], ["aa", ""], ["aa", " \n\t "],
                                  ["aa", "question", "extra"]])
def test_invalid_positionals_do_not_create_tasks(
    client: MagicMock, prefix: list[str], args: list[str],
) -> None:
    with patch("ilan.cli._client", return_value=client):
        result = CliRunner().invoke(main, [*prefix, *args])
    assert result.exit_code != 0
    assert client.method_calls == []


def test_double_dash_allows_an_instruction_starting_with_a_dash(client: MagicMock) -> None:
    with patch("ilan.cli._client", return_value=client):
        result = CliRunner().invoke(main, ["btw", "aa", "--", "--dry-run only"])
    assert result.exit_code == 0, result.output
    client.btw_task.assert_called_once_with("aa", "--dry-run only" + _BTW_SUFFIX)


@pytest.mark.parametrize("line_number", [False, True])
def test_suffix_stays_outside_the_expanded_reference(
    client: MagicMock, line_number: bool,
) -> None:
    cfg.save({**cfg.DEFAULTS, "line-number": line_number})
    cfg.save_last_tail("aa", ["the previous answer"])
    with patch("ilan.cli._client", return_value=client):
        result = CliRunner().invoke(main, ["btw", "aa", "Explain @1"])
    assert result.exit_code == 0, result.output
    instruction = "Explain\n\n> the previous answer" if line_number else "Explain @1"
    client.btw_task.assert_called_once_with("aa", instruction + _BTW_SUFFIX)


@pytest.mark.parametrize("error", ["Task missing not found", "Parent has no established session"])
def test_server_refusal_does_not_print_success(client: MagicMock, error: str) -> None:
    client.btw_task.return_value = {"error": error}
    with patch("ilan.cli._client", return_value=client):
        result = CliRunner().invoke(main, ["btw", "aa", "Why?"])
    assert result.exit_code == 1
    assert error in result.output
    assert "Branched" not in result.output


def test_server_chosen_numbered_name_is_reported(client: MagicMock) -> None:
    client.btw_task.return_value["name"] = "xxx-parent-task-btw-2"
    with patch("ilan.cli._client", return_value=client):
        result = CliRunner().invoke(main, ["btw", "aa", "Why?"])
    assert result.exit_code == 0, result.output
    assert "xxx-parent-task-btw-2" in result.output
    assert "Burnable" in result.output


@pytest.mark.parametrize("prefix", [["btw"], ["task", "btw"]])
def test_usage_shows_only_two_required_positionals(prefix: list[str]) -> None:
    result = CliRunner().invoke(main, prefix)
    assert result.exit_code == 2
    assert "OLD_NAME INSTRUCTION" in result.output
    assert "[OPTIONS]" not in result.output


def test_the_confirmation_names_the_childs_level(client: MagicMock) -> None:
    """A side question starts at low whatever its parent runs at, so the level
    is said out loud rather than left for the user to assume."""
    with patch("ilan.cli._client", return_value=client):
        result = CliRunner().invoke(main, ["btw", "aa", "Why?"])
    assert result.exit_code == 0, result.output
    assert "Branched xxx-parent-task-btw from parent-task. Reasoning level: low." in result.output


def test_a_server_without_levels_gets_the_plain_confirmation(client: MagicMock) -> None:
    del client.btw_task.return_value["reasoning"]
    with patch("ilan.cli._client", return_value=client):
        result = CliRunner().invoke(main, ["btw", "aa", "Why?"])
    assert result.exit_code == 0, result.output
    assert "Branched xxx-parent-task-btw from parent-task." in result.output
    assert "Reasoning level" not in result.output
