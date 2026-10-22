"""Tests for the ``ilan task copy`` / ``ilan copy`` CLI commands."""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from ilan.cli import _copy_to_clipboard, console, main

GIST_URL = "https://gist.github.com/u/gid1"


@pytest.fixture()
def runner() -> CliRunner:
    return CliRunner()


def _make_client(task_resp: dict) -> MagicMock:
    client = MagicMock()
    client.ensure_server.return_value = {}
    client.version_mismatch = None
    client.get_task.return_value = task_resp
    return client


def _printed(console_print: MagicMock) -> str:
    return "\n".join(str(c.args[0]) for c in console_print.call_args_list)


def _task(gist_url: str | None) -> dict:
    return {"task": {"name": "my-task", "gist_url": gist_url}}


class TestCopySuccess:
    @pytest.mark.parametrize("argv", [["task", "copy", "aa"], ["copy", "aa"]])
    def test_copies_link_in_green(self, runner: CliRunner, tmp_config, argv) -> None:
        client = _make_client(_task(GIST_URL))
        with patch("ilan.cli._client", return_value=client), \
                patch("ilan.cli._copy_to_clipboard", return_value=True) as copy, \
                patch("ilan.cli.console.print", wraps=console.print) as console_print:
            result = runner.invoke(main, argv)
        assert result.exit_code == 0
        copy.assert_called_once_with(GIST_URL)
        client.get_task.assert_called_once_with("aa")
        assert GIST_URL in result.output
        assert "[green]" in _printed(console_print)


class TestCopyFailures:
    @pytest.mark.parametrize("gist_url", [None, "", "  "])
    def test_no_gist_is_red_error(self, runner: CliRunner, tmp_config, gist_url) -> None:
        client = _make_client(_task(gist_url))
        with patch("ilan.cli._client", return_value=client), \
                patch("ilan.cli._copy_to_clipboard") as copy, \
                patch("ilan.cli.console.print", wraps=console.print) as console_print:
            result = runner.invoke(main, ["copy", "aa"])
        assert result.exit_code == 1
        assert "no github gist" in result.output.lower()
        assert "[red]" in _printed(console_print)
        copy.assert_not_called()

    def test_task_not_found(self, runner: CliRunner, tmp_config) -> None:
        client = _make_client({"error": "Task not found"})
        with patch("ilan.cli._client", return_value=client), \
                patch("ilan.cli._copy_to_clipboard") as copy:
            result = runner.invoke(main, ["copy", "nope"])
        assert result.exit_code == 1
        assert "not found" in result.output.lower()
        copy.assert_not_called()

    def test_no_clipboard_is_red_error(self, runner: CliRunner, tmp_config) -> None:
        client = _make_client(_task(GIST_URL))
        with patch("ilan.cli._client", return_value=client), \
                patch("ilan.cli._copy_to_clipboard", return_value=False), \
                patch("ilan.cli.console.print", wraps=console.print) as console_print:
            result = runner.invoke(main, ["copy", "aa"])
        assert result.exit_code == 1
        assert "clipboard" in result.output.lower()
        assert "[red]" in _printed(console_print)


class TestCopyToClipboard:
    def test_uses_first_available_writer(self) -> None:
        def which(cmd: str) -> str | None:
            return "/usr/bin/xclip" if cmd == "xclip" else None

        with patch("ilan.cli.shutil.which", side_effect=which), \
                patch("ilan.cli.subprocess.run") as run:
            assert _copy_to_clipboard(GIST_URL) is True
        run.assert_called_once()
        assert run.call_args.args[0] == ("xclip", "-selection", "clipboard")
        assert run.call_args.kwargs["input"] == GIST_URL

    def test_falls_through_a_failing_writer(self) -> None:
        with patch("ilan.cli.shutil.which", return_value="/bin/x"), \
                patch("ilan.cli.subprocess.run", side_effect=[
                    subprocess.CalledProcessError(1, "pbcopy"), None,
                ]) as run:
            assert _copy_to_clipboard(GIST_URL) is True
        assert run.call_args.args[0] == ("wl-copy",)

    def test_no_writer_returns_false(self) -> None:
        with patch("ilan.cli.shutil.which", return_value=None), \
                patch("ilan.cli.subprocess.run") as run:
            assert _copy_to_clipboard(GIST_URL) is False
        run.assert_not_called()
