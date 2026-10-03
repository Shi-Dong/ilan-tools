"""Top-level help must explain the shortcuts without contacting the server."""

from unittest.mock import patch

import pytest
from click.testing import CliRunner

from ilan.cli import main


def test_short_help_matches_long_help() -> None:
    runner = CliRunner()
    with patch("ilan.cli._client") as client:
        short = runner.invoke(main, ["-h"])
        long = runner.invoke(main, ["--help"])
    assert short.exit_code == long.exit_code == 0
    assert short.output == long.output
    assert "-h, --help" in short.output
    client.assert_not_called()


@pytest.mark.parametrize("width", [80, 120])
def test_rendered_shortcuts_have_descriptions_before_targets(width: int) -> None:
    result = CliRunner().invoke(main, ["-h"], terminal_width=width)
    assert result.exit_code == 0, result.output
    lines = result.output.split("Commands:\n", 1)[1].splitlines()
    rows: dict[str, str] = {}
    current = ""
    for line in lines:
        if not line.strip():
            continue
        if line.startswith("  ") and not line.startswith("   "):
            current, text = line.strip().split(None, 1)
            rows[current] = text
        else:
            rows[current] += " " + line.strip()
    shortcuts = [
        "info", "tail", "done", "discard", "undone", "undiscard", "unread",
        "pin", "unpin", "max", "unmax", "level", "switch-backend", "rename",
        "alias", "tap", "cancel", "sleep", "attach", "log", "logs", "open",
        "check-model", "ls", "add", "branch", "btw", "reply", "re", "notes", "note",
    ]
    for name in shortcuts:
        target = {"re": "reply", "note": "notes"}.get(name, name)
        description, reference = rows[name].split(" Shorthand for ", 1)
        assert description and description.endswith("."), rows[name]
        assert reference == f"'ilan task {target}'.", rows[name]
        assert "..." not in rows[name]
    assert rows["add"].startswith("Add a new task.")
    assert rows["reply"].startswith("Send a response to a task.")
    assert rows["ls"].startswith("List tasks, or tail a specific task.")


@pytest.mark.parametrize("name,description", [
    ("tail", "Show the latest task exchange."),
    ("ls", "List tasks, or tail a specific task."),
    ("notes", "Edit a task's note."),
    ("logs", "Open task logs in your editor."),
])
def test_shortcut_help_starts_with_description(name: str, description: str) -> None:
    result = CliRunner().invoke(main, [name, "--help"])
    assert result.exit_code == 0, result.output
    body = " ".join(result.output.split("Options:", 1)[0].split())
    assert f"{description} Shorthand for 'ilan task {name}'." in body
