"""Exercise add's editor path through both command spellings."""

import shlex
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from ilan.cli import main


@pytest.mark.parametrize("prefix", [["add"], ["task", "add"]])
@pytest.mark.parametrize("flag", ["-e", "--editor"])
@pytest.mark.parametrize("name", [None, "task-name"])
def test_editor_creates_task(prefix: list[str], flag: str, name: str | None) -> None:
    client = MagicMock()
    client.add_task.return_value = {"name": name or "xxx-cat-likes-fin"}
    paths: list[Path] = []

    def edit(argv: list[str]) -> MagicMock:
        assert argv[:2] == ["emacs", "-nw"]
        path = Path(argv[-1])
        paths.append(path)
        assert path.read_text() == ""
        path.write_text("  first line\n\nsecond line\n")
        return MagicMock(returncode=0)

    args = [*prefix, flag, "--codex", "--max"]
    if name is not None:
        args.extend(["-n", name])
    with patch("ilan.cli._client", return_value=client), \
         patch("ilan.cli.cfg.load", return_value={"editor": "emacs -nw"}), \
         patch("ilan.cli.shutil.which", return_value="/bin/editor"), \
         patch("ilan.cli.subprocess.run", side_effect=edit):
        result = CliRunner().invoke(main, args)
    assert result.exit_code == 0, result.output
    client.add_task.assert_called_once_with(
        name, "first line\n\nsecond line", "codex", max_model=True,
    )
    assert all(not path.exists() for path in paths)


@pytest.mark.parametrize("prefix", [["add"], ["task", "add"]])
@pytest.mark.parametrize("content,returncode", [("", 0), (" \t\n\n ", 0), ("text", 1)])
def test_no_task_on_empty_or_abandoned_edit(
    prefix: list[str], content: str, returncode: int,
) -> None:
    def edit(argv: list[str]) -> MagicMock:
        Path(argv[-1]).write_text(content)
        return MagicMock(returncode=returncode)

    with patch("ilan.cli._client") as client, \
         patch("ilan.cli.cfg.load", return_value={"editor": "vim"}), \
         patch("ilan.cli.shutil.which", return_value="/bin/vim"), \
         patch("ilan.cli.subprocess.run", side_effect=edit):
        result = CliRunner().invoke(main, [*prefix, "-e"])
    assert result.exit_code == (1 if returncode else 0), result.output
    client.assert_not_called()


@pytest.mark.parametrize("prefix", [["add"], ["task", "add"]])
@pytest.mark.parametrize("source", ["description", "file", "bare"])
def test_conflicts_before_editor(
    prefix: list[str], source: str, tmp_path: Path,
) -> None:
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("text")
    args = {"description": ["-d", ""], "file": ["-f", str(prompt)], "bare": ["text"]}
    with patch("ilan.cli._client") as client, \
         patch("ilan.cli.subprocess.run") as editor:
        result = CliRunner().invoke(main, [*prefix, "-e", *args[source]])
    assert result.exit_code == 1
    assert "-e takes no instruction" in result.output
    editor.assert_not_called()
    client.assert_not_called()


@pytest.mark.parametrize("configured", ["", "vim '", "missing-editor"])
def test_unusable_editor_creates_nothing(configured: str) -> None:
    with patch("ilan.cli._client") as client, \
         patch("ilan.cli.cfg.load", return_value={"editor": configured}), \
         patch("ilan.cli.shutil.which", return_value=None), \
         patch("ilan.cli.subprocess.run") as editor:
        result = CliRunner().invoke(main, ["add", "-e"])
    assert result.exit_code == 1
    editor.assert_not_called()
    client.assert_not_called()


def test_editor_launch_error_creates_nothing() -> None:
    with patch("ilan.cli._client") as client, \
         patch("ilan.cli.cfg.load", return_value={"editor": "vim"}), \
         patch("ilan.cli.shutil.which", return_value="/bin/vim"), \
         patch("ilan.cli.subprocess.run", side_effect=OSError("cannot launch")):
        result = CliRunner().invoke(main, ["add", "-e"])
    assert result.exit_code == 1
    assert "Cannot run" in result.output
    client.assert_not_called()


@pytest.mark.parametrize("content", ["# Initial instruction\n\nDo the work\n", " \n\t"])
def test_real_editor_process(tmp_path: Path, content: str) -> None:
    """Use a real external editor process and real temp-file round trip."""
    script = tmp_path / "editor.py"
    script.write_text(
        "import sys\nfrom pathlib import Path\n"
        f"Path(sys.argv[1]).write_text({content!r})\n"
    )
    editor = shlex.join([sys.executable, str(script)])
    client = MagicMock()
    client.add_task.return_value = {"name": "task-name"}
    with patch("ilan.cli._client", return_value=client), \
         patch("ilan.cli.cfg.load", return_value={"editor": editor}), \
         patch("ilan.cli.shutil.which", return_value=sys.executable):
        result = CliRunner().invoke(main, ["add", "-n", "task-name", "-e"])
    assert result.exit_code == 0, result.output
    if content.strip():
        client.add_task.assert_called_once_with(
            "task-name", content.strip(), None, max_model=False,
        )
    else:
        client.add_task.assert_not_called()
