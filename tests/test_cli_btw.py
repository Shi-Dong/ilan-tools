"""Side questions share branching behavior and add a scope instruction."""

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
    client.branch_task.return_value = {
        "name": "xxx-cat-likes-fin", "parent_name": "parent-task",
    }
    return client


@pytest.mark.parametrize("prefix", [["btw"], ["task", "btw"]])
@pytest.mark.parametrize("source", ["bare", "description", "file"])
def test_only_adds_suffix_to_branch_assignment(
    client: MagicMock, tmp_path: Path, prefix: list[str], source: str,
) -> None:
    instruction = "  Why this choice?\nExplain briefly.  "
    prompt = tmp_path / "question.txt"
    prompt.write_text(instruction)
    args = {
        "bare": [instruction],
        "description": ["-n", "saved-answer", "-d", instruction],
        "file": ["-n", "saved-answer", "-f", str(prompt)],
    }[source]
    expected_name = None if source == "bare" else "saved-answer"
    client.branch_task.return_value["name"] = expected_name or "xxx-cat-likes-fin"
    runner = CliRunner()
    with patch("ilan.cli._client", return_value=client):
        branch_result = runner.invoke(main, ["branch", "aa", *args])
        branch_call = client.branch_task.call_args
        client.reset_mock()
        result = runner.invoke(main, [*prefix, "aa", *args])
    assert branch_result.exit_code == 0, branch_result.output
    assert result.exit_code == 0, result.output
    assert branch_call.args == ("aa", expected_name, instruction)
    client.branch_task.assert_called_once_with(
        "aa", expected_name, instruction + _BTW_SUFFIX,
    )
    assert ("Burnable" in result.output) == (expected_name is None)


@pytest.mark.parametrize("args", [
    [], [""], [" \n\t "], ["-d", ""], ["-d", " \n\t "],
    ["question", "-d", "another"], ["question", "-n", "child"],
    ["-n", "child", "question"], ["question", "extra"],
])
def test_no_branch_without_an_unambiguous_assignment(
    client: MagicMock, args: list[str],
) -> None:
    with patch("ilan.cli._client", return_value=client):
        result = CliRunner().invoke(main, ["btw", "aa", *args])
    assert result.exit_code != 0
    client.branch_task.assert_not_called()


def test_empty_file_cannot_become_a_suffix_only_assignment(
    client: MagicMock, tmp_path: Path,
) -> None:
    prompt = tmp_path / "empty.txt"
    prompt.write_text(" \n")
    with patch("ilan.cli._client", return_value=client):
        result = CliRunner().invoke(main, ["btw", "aa", "-f", str(prompt)])
    assert result.exit_code == 1
    assert "must not be empty" in result.output
    client.branch_task.assert_not_called()


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
    client.branch_task.assert_called_once_with("aa", None, instruction + _BTW_SUFFIX)


def test_server_refusal_does_not_print_success(client: MagicMock) -> None:
    client.branch_task.return_value = {"error": "Parent has no established session"}
    with patch("ilan.cli._client", return_value=client):
        result = CliRunner().invoke(main, ["btw", "aa", "Why?"])
    assert result.exit_code == 1
    assert "no established session" in result.output
    assert "Branched" not in result.output


@pytest.mark.parametrize("prefix", [["btw"], ["task", "btw"]])
def test_help_describes_scope_and_options(prefix: list[str]) -> None:
    result = CliRunner().invoke(main, [*prefix, "--help"])
    assert result.exit_code == 0, result.output
    assert "OLD_NAME [INSTRUCTION]" in result.output
    assert "ongoing jobs" in result.output
    assert "--name" in result.output
    assert "--file" in result.output
    assert "--description" in result.output
