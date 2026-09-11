"""Tests for the ``-e/--editor`` reply flag on reply, re and task reply."""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from ilan.cli import main

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _strip_ansi(s: str) -> str:
    return _ANSI_RE.sub("", s)


def _squash(s: str) -> str:
    """Collapse the wrapping Rich adds so a message can be matched whole."""
    return " ".join(_strip_ansi(s).split())


# Every spelling of the command shares one body, so each behaviour is checked
# through all three rather than trusting that the wiring matches.
_REPLY_PREFIXES = [["task", "reply"], ["reply"], ["re"]]


def _make_client() -> MagicMock:
    client = MagicMock()
    client.ensure_server.return_value = {}
    client.version_mismatch = None
    client.is_remote = False
    client.get_task.return_value = {"task": {"name": "my-task"}}
    client.reply.return_value = {
        "ok": True,
        "name": "my-task",
        "message": "Reply sent to my-task. Agent resumed.",
    }
    return client


def _invoke_editor(
    runner: CliRunner,
    argv: list[str],
    *,
    written: str | None = "from the editor",
    returncode: int = 0,
    editor: str = "vim",
    client: MagicMock | None = None,
    configured_editor: str | None = None,
):
    """Run a reply command against a mock client and a fake editor.

    ``written=None`` leaves the buffer exactly as ilan wrote it, which is what
    an editor opened and closed without typing anything looks like.
    """
    client = client or _make_client()
    seen: dict = {}

    def run(cmd, *args, **kwargs):
        seen["cmd"] = list(cmd)
        seen["prefill"] = Path(cmd[-1]).read_text()
        if written is not None:
            Path(cmd[-1]).write_text(written)
        return MagicMock(returncode=returncode)

    # `shutil.which` is patched rather than left to the host: CI runners do
    # not all ship vim, and these tests are about ilan's behaviour.
    cfg = {"editor": editor if configured_editor is None else configured_editor}
    with patch("ilan.cli._client", return_value=client), \
            patch("ilan.cli.subprocess.run", side_effect=run), \
            patch("ilan.cli.shutil.which", return_value=f"/usr/bin/{editor}"), \
            patch("ilan.cli.cfg.load", return_value=cfg):
        result = runner.invoke(main, argv)
    return result, client, seen


@pytest.fixture()
def runner() -> CliRunner:
    return CliRunner()


class TestEditorFlagSendsWhatWasWritten:
    @pytest.mark.parametrize("prefix", _REPLY_PREFIXES)
    def test_it_sends_the_buffer(
        self, runner: CliRunner, prefix: list[str]
    ) -> None:
        result, client, _ = _invoke_editor(
            runner, [*prefix, "my-task", "-e"], written="ship it",
        )
        assert result.exit_code == 0, result.output
        client.reply.assert_called_once_with("my-task", "ship it")

    def test_long_flag_spelling(self, runner: CliRunner) -> None:
        _, client, _ = _invoke_editor(
            runner, ["re", "my-task", "--editor"], written="ship it",
        )
        client.reply.assert_called_once_with("my-task", "ship it")

    def test_it_runs_the_configured_editor(self, runner: CliRunner) -> None:
        _, _, seen = _invoke_editor(
            runner, ["re", "my-task", "-e"], written="x", editor="nano",
        )
        assert seen["cmd"][0] == "nano"

    def test_the_buffer_starts_empty(self, runner: CliRunner) -> None:
        """A reply is new text, so nothing is prefilled for the user to delete."""
        _, _, seen = _invoke_editor(runner, ["re", "my-task", "-e"], written="x")
        assert seen["prefill"] == ""

    def test_the_temp_file_is_labelled_a_reply(self, runner: CliRunner) -> None:
        _, _, seen = _invoke_editor(runner, ["re", "my-task", "-e"], written="x")
        assert seen["cmd"][-1].endswith("-my-task-reply.txt")

    def test_surrounding_whitespace_is_stripped(self, runner: CliRunner) -> None:
        """Editors end a buffer with a newline; that is not part of the reply."""
        _, client, _ = _invoke_editor(
            runner, ["re", "my-task", "-e"], written="  ship it\n\n",
        )
        client.reply.assert_called_once_with("my-task", "ship it")

    def test_inner_layout_is_preserved(self, runner: CliRunner) -> None:
        _, client, _ = _invoke_editor(
            runner, ["re", "my-task", "-e"], written="line one\n\nline two\n",
        )
        client.reply.assert_called_once_with("my-task", "line one\n\nline two")


class TestEditorFlagSendsNothing:
    """Every way the edit can fail must leave the task unwritten to."""

    @pytest.mark.parametrize("prefix", _REPLY_PREFIXES)
    def test_a_message_as_well_is_rejected(
        self, runner: CliRunner, prefix: list[str]
    ) -> None:
        result, client, seen = _invoke_editor(
            runner, [*prefix, "my-task", "hello", "-e"],
        )
        assert result.exit_code == 1
        assert "-e takes no message" in _squash(result.output)
        client.reply.assert_not_called()
        assert "cmd" not in seen  # no editor was launched

    def test_an_empty_buffer_sends_nothing(self, runner: CliRunner) -> None:
        result, client, _ = _invoke_editor(
            runner, ["re", "my-task", "-e"], written=None,
        )
        assert result.exit_code == 1
        assert "Empty reply; nothing sent." in _squash(result.output)
        client.reply.assert_not_called()

    def test_a_whitespace_only_buffer_sends_nothing(self, runner: CliRunner) -> None:
        result, client, _ = _invoke_editor(
            runner, ["re", "my-task", "-e"], written="   \n\n  ",
        )
        assert result.exit_code == 1
        assert "Empty reply; nothing sent." in _squash(result.output)
        client.reply.assert_not_called()

    def test_a_nonzero_exit_sends_nothing(self, runner: CliRunner) -> None:
        """`:cq` in vim, or a crash, must not post what is in the buffer."""
        result, client, _ = _invoke_editor(
            runner, ["re", "my-task", "-e"], written="half a thought", returncode=1,
        )
        assert result.exit_code == 1
        assert "vim exited with 1; nothing was sent." in _squash(result.output)
        client.reply.assert_not_called()

    def test_no_editor_configured(self, runner: CliRunner) -> None:
        result, client, seen = _invoke_editor(
            runner, ["re", "my-task", "-e"], configured_editor="",
        )
        assert result.exit_code == 1
        assert "No editor configured" in _squash(result.output)
        client.reply.assert_not_called()
        assert "cmd" not in seen

    def test_editor_not_on_path(self, runner: CliRunner) -> None:
        client = _make_client()
        with patch("ilan.cli._client", return_value=client), \
                patch("ilan.cli.subprocess.run") as run, \
                patch("ilan.cli.shutil.which", return_value=None), \
                patch("ilan.cli.cfg.load", return_value={"editor": "vim"}):
            result = runner.invoke(main, ["re", "my-task", "-e"])
        assert result.exit_code == 1
        assert "is not installed, or not on your PATH" in _squash(result.output)
        client.reply.assert_not_called()
        run.assert_not_called()

    def test_an_unknown_task_is_caught_before_the_editor_opens(
        self, runner: CliRunner
    ) -> None:
        """Nothing typed is worth losing to a name that was never going to work."""
        client = _make_client()
        client.get_task.return_value = {"error": "No such task: nope"}
        result, _, seen = _invoke_editor(
            runner, ["re", "nope", "-e"], client=client,
        )
        assert result.exit_code == 1
        assert "No such task: nope" in _squash(result.output)
        client.reply.assert_not_called()
        assert "cmd" not in seen


class TestEditorFlagWithTheOtherReplyFlags:
    def test_every_is_parsed_before_the_editor_opens(
        self, runner: CliRunner
    ) -> None:
        """A duration ilan will refuse must not cost the user a typed reply."""
        result, client, seen = _invoke_editor(
            runner, ["re", "my-task", "-e", "-t", "5m"],
        )
        assert result.exit_code == 1
        assert "-t/--every must be at least 20m" in _squash(result.output)
        client.reply.assert_not_called()
        assert "cmd" not in seen

    def test_every_applies_to_the_written_reply(self, runner: CliRunner) -> None:
        _, client, _ = _invoke_editor(
            runner, ["re", "my-task", "-e", "-t", "1h"], written="poke",
        )
        client.reply.assert_called_once_with("my-task", "poke", every_seconds=3600)

    def test_max_no_longer_demands_a_command_line_message(
        self, runner: CliRunner
    ) -> None:
        """`-e` is the message, so `--max` has one even before it is written."""
        client = _make_client()
        with patch("ilan.cli._model_switch_needed", return_value=False):
            result, _, _ = _invoke_editor(
                runner, ["re", "my-task", "-e", "--max"],
                written="ship it", client=client,
            )
        assert result.exit_code == 0, result.output
        client.reply.assert_called_once_with("my-task", "ship it")

    def test_max_and_unmax_together_still_lose_before_the_editor(
        self, runner: CliRunner
    ) -> None:
        result, client, seen = _invoke_editor(
            runner, ["re", "my-task", "-e", "--max", "--unmax"],
        )
        assert result.exit_code == 1
        assert "--max and --unmax cannot be used together." in _squash(result.output)
        client.reply.assert_not_called()
        assert "cmd" not in seen

    def test_line_number_is_rejected(self, runner: CliRunner) -> None:
        """`--line-number` tunes the tail, and `-e` means there is no tail."""
        result, client, seen = _invoke_editor(
            runner, ["re", "my-task", "-e", "--line-number"],
        )
        assert result.exit_code == 1
        assert "cannot be used when a response message is provided" in _squash(
            result.output
        )
        client.reply.assert_not_called()
        assert "cmd" not in seen


class TestBareReplyIsUnchanged:
    """Adding `-e` must not change what the command does without it."""

    @pytest.mark.parametrize("prefix", _REPLY_PREFIXES)
    def test_no_message_still_shows_the_tail(
        self, runner: CliRunner, prefix: list[str]
    ) -> None:
        """Unlike `ilan notes`, a bare reply shows the tail rather than editing."""
        client = _make_client()
        with patch("ilan.cli._client", return_value=client), \
                patch("ilan.cli.subprocess.run") as run, \
                patch("ilan.cli._do_tail") as do_tail, \
                patch("ilan.cli.cfg.load", return_value={"editor": "vim"}):
            result = runner.invoke(main, [*prefix, "my-task"])
        assert result.exit_code == 0, result.output
        do_tail.assert_called_once()
        run.assert_not_called()
        client.reply.assert_not_called()

    def test_every_without_a_message_or_editor_is_still_rejected(
        self, runner: CliRunner
    ) -> None:
        client = _make_client()
        with patch("ilan.cli._client", return_value=client), \
                patch("ilan.cli.cfg.load", return_value={"editor": "vim"}):
            result = runner.invoke(main, ["re", "my-task", "-t", "1h"])
        assert result.exit_code == 1
        assert "-t/--every requires a response message." in _squash(result.output)
        client.reply.assert_not_called()

    def test_a_command_line_message_still_sends(self, runner: CliRunner) -> None:
        client = _make_client()
        with patch("ilan.cli._client", return_value=client), \
                patch("ilan.cli.cfg.load", return_value={"editor": "vim"}):
            result = runner.invoke(main, ["re", "my-task", "ship it"])
        assert result.exit_code == 0, result.output
        client.reply.assert_called_once_with("my-task", "ship it")
