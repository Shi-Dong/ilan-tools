"""Side questions accept inline instructions or the editor flag."""

from __future__ import annotations

import shlex
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from ilan import config as cfg
from ilan.cli import _BTW_SUFFIX, main


@pytest.fixture()
def client(tmp_config: Path) -> MagicMock:
    client = MagicMock()
    client.get_task.return_value = {"task": {"name": "parent-task"}}
    client.btw_task.return_value = {
        "name": "xxx-parent-task-btw", "parent_name": "parent-task", "reasoning": "low",
    }
    return client


@pytest.mark.parametrize("prefix", [["btw"], ["task", "btw"]])
@pytest.mark.parametrize("flag", ["-e", "--editor"])
@pytest.mark.parametrize("content,returncode", [
    ("  Why?\n\nExplain @1.\n", 0), (" \n\t", 0), ("discard me", 1),
])
def test_editor_round_trip(
    client: MagicMock, prefix: list[str], flag: str, content: str,
    returncode: int, tmp_path: Path,
) -> None:
    """Run a real editor process and verify the outgoing message and cleanup."""
    script = tmp_path / "editor.py"
    path_record = tmp_path / "editor-path.txt"
    script.write_text(
        "import sys\nfrom pathlib import Path\n"
        "path = Path(sys.argv[1])\nassert path.read_text() == ''\n"
        f"Path({str(path_record)!r}).write_text(str(path))\n"
        f"path.write_text({content!r})\nsys.exit({returncode})\n"
    )
    cfg.save({**cfg.DEFAULTS, "editor": shlex.join([sys.executable, str(script)]),
              "line-number": True})
    cfg.save_last_tail("aa", ["previous answer"])
    with patch("ilan.cli._client", return_value=client):
        result = CliRunner().invoke(main, [*prefix, "aa", flag])
    assert result.exit_code == (1 if returncode else 0), result.output
    assert not Path(path_record.read_text()).exists()
    client.get_task.assert_called_once_with("aa")
    if returncode or not content.strip():
        client.btw_task.assert_not_called()
        assert "Branched" not in result.output
    else:
        client.btw_task.assert_called_once_with(
            "aa", "Why?\n\nExplain\n\n> previous answer\n\n." + _BTW_SUFFIX,
        )


@pytest.mark.parametrize("prefix", [["btw"], ["task", "btw"]])
@pytest.mark.parametrize("instruction", ["Why?", ""])
def test_editor_conflicts_before_requests(
    client: MagicMock, prefix: list[str], instruction: str,
) -> None:
    with patch("ilan.cli._client", return_value=client), \
         patch("ilan.cli.subprocess.run") as editor:
        result = CliRunner().invoke(main, [*prefix, "aa", instruction, "-e"])
    assert result.exit_code == 1
    assert "-e takes no message" in result.output
    editor.assert_not_called()
    assert client.method_calls == []


@pytest.mark.parametrize("configured", ["", "vim '", "missing-editor"])
def test_unusable_editor_makes_no_requests(client: MagicMock, configured: str) -> None:
    cfg.save({**cfg.DEFAULTS, "editor": configured})
    with patch("ilan.cli._client", return_value=client), \
         patch("ilan.cli.shutil.which", return_value=None), \
         patch("ilan.cli.subprocess.run") as editor:
        result = CliRunner().invoke(main, ["btw", "aa", "-e"])
    assert result.exit_code == 1
    editor.assert_not_called()
    assert client.method_calls == []


def test_missing_parent_rejected_before_editor(client: MagicMock) -> None:
    client.get_task.return_value = {"error": "Task aa not found"}
    with patch("ilan.cli._client", return_value=client), \
         patch("ilan.cli._resolve_editor", return_value=["vim"]), \
         patch("ilan.cli.subprocess.run") as editor:
        result = CliRunner().invoke(main, ["btw", "aa", "-e"])
    assert result.exit_code == 1
    assert "Task aa not found" in result.output
    editor.assert_not_called()
    client.btw_task.assert_not_called()


def test_editor_launch_failure_creates_nothing(client: MagicMock) -> None:
    with patch("ilan.cli._client", return_value=client), \
         patch("ilan.cli._resolve_editor", return_value=["vim"]), \
         patch("ilan.cli.subprocess.run", side_effect=OSError("cannot launch")):
        result = CliRunner().invoke(main, ["btw", "aa", "-e"])
    assert result.exit_code == 1
    assert "Cannot run" in result.output
    client.btw_task.assert_not_called()


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
def test_usage_shows_optional_instruction(prefix: list[str]) -> None:
    result = CliRunner().invoke(main, prefix)
    assert result.exit_code == 2
    assert "OLD_NAME [INSTRUCTION]" in result.output
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
