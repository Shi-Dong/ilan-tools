"""Editor reminders survive the real subprocess round trip but never get sent."""

import shlex
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from ilan.cli import _editor_compose_args, main


@pytest.mark.parametrize(("editor", "expected"), [
    (["vim", "-n"], ["+normal! G$", "+startinsert!"]),
    (["/usr/bin/nvim"], ["+normal! G$", "+startinsert!"]),
    (["emacs", "-nw"], ["+3:0"]),
    (["/Applications/Emacs.app/Contents/MacOS/Emacs", "-nw"], ["+3:0"]),
    (["/Applications/Emacs.app/Contents/MacOS/bin/emacs", "-nw"], ["+3:0"]),
    (["emacsclient", "-t"], ["+3:0"]),
    (["nano"], ["+3"]),
    (["pico"], ["+3"]),
    (["custom-editor", "--wait"], []),
])
def test_compose_cursor_args(editor: list[str], expected: list[str]) -> None:
    assert _editor_compose_args(editor, "%% You are replying to `task`.\n\n") == expected


@pytest.mark.parametrize("prefix", [
    ["re"], ["reply"], ["task", "reply"], ["branch"], ["task", "branch"],
])
@pytest.mark.parametrize("alias", ["hk", None])
@pytest.mark.parametrize("body", [
    "", "immediate message", "\n%% extra\n  hello\n\nworld\n%% footer\n",
    "\n  %% indented\ntext %% inline\n",
])
def test_task_editor_reminder(
    tmp_path: Path, prefix: list[str], alias: str | None, body: str,
) -> None:
    branch = prefix[-1] == "branch"
    action = "branching from" if branch else "replying to"
    label = "`your-task-name`" + (f" (alias `{alias}`)" if alias else "")
    header = (
        f"%% You are {action} {label}. All lines starting with `%%` "
        "will be omitted in the message.\n\n"
    )
    script = tmp_path / "editor.py"
    script.write_text(
        "import sys\nfrom pathlib import Path\n"
        "p = Path(sys.argv[1])\n"
        f"assert p.read_text() == {header!r}\n"
        f"p.write_text(p.read_text() + {body!r})\n"
    )
    client = MagicMock()
    client.get_task.return_value = {
        "task": {"name": "your-task-name", "alias": alias},
    }
    client.branch_task.return_value = {"name": "new-task"}
    client.reply.return_value = {"ok": True, "name": "your-task-name"}
    editor = shlex.join([sys.executable, str(script)])
    with patch("ilan.cli._client", return_value=client), \
         patch("ilan.cli.cfg.load", return_value={"editor": editor}), \
         patch("ilan.cli.shutil.which", return_value=sys.executable):
        result = CliRunner().invoke(main, [*prefix, alias or "your-task-name", "-e"])
    expected = "hello\n\nworld" if "hello" in body else body.strip()
    if not expected:
        assert result.exit_code == (0 if branch else 1), result.output
        client.branch_task.assert_not_called()
        client.reply.assert_not_called()
    else:
        assert result.exit_code == 0, result.output
        if branch:
            client.branch_task.assert_called_once_with(
                alias or "your-task-name", None, expected,
            )
        else:
            client.reply.assert_called_once_with(alias or "your-task-name", expected)
