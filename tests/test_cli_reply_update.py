"""Tests for the ``-u/--update`` reply flag: editing a looping task's prompt."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from ilan.cli import main

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _squash(s: str) -> str:
    """Strip colour codes and collapse Rich's wrapping so a message matches whole."""
    return " ".join(_ANSI_RE.sub("", s).split())


# One body serves all three spellings; each behaviour is checked through all
# of them rather than trusting that the wiring matches.
_REPLY_PREFIXES = [["task", "reply"], ["reply"], ["re"]]

_LOOP_PROMPT = "Update and report the devbox status."


def _looping_task(**overrides) -> dict:
    """A task on a 20m cycle whose next re-send is 12 minutes out."""
    task = {
        "name": "my-task",
        "status": "AGENT_FINISHED",
        "reply_every_seconds": 1200,
        "reply_every_message": _LOOP_PROMPT,
        "reply_every_next_at": (
            datetime.now(timezone.utc) + timedelta(seconds=720)
        ).isoformat(),
    }
    task.update(overrides)
    return task


def _make_client(task: dict | None = None) -> MagicMock:
    client = MagicMock()
    client.ensure_server.return_value = {}
    client.version_mismatch = None
    client.is_remote = False
    task = _looping_task() if task is None else task
    client.get_task.return_value = {"task": task}
    client.reply.return_value = {
        "ok": True,
        "name": task["name"],
        "message": f"Reply sent to {task['name']}. Agent resumed.",
    }
    client.set_reply_every_message.return_value = {
        "ok": True,
        "name": task["name"],
        "reply_every_message": "whatever was saved",
        "reply_every_seconds": task.get("reply_every_seconds"),
        "reply_every_next_at": task.get("reply_every_next_at"),
    }
    return client


def _invoke_editor(
    runner: CliRunner,
    argv: list[str],
    *,
    written: str | None = None,
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


class TestUpdateFlagEditsTheLoopingPrompt:
    @pytest.mark.parametrize("prefix", _REPLY_PREFIXES)
    def test_it_saves_what_was_written(
        self, runner: CliRunner, prefix: list[str]
    ) -> None:
        result, client, _ = _invoke_editor(
            runner, [*prefix, "my-task", "-u"], written="Report only the H200s.",
        )
        assert result.exit_code == 0, result.output
        client.set_reply_every_message.assert_called_once_with(
            "my-task", "Report only the H200s."
        )

    def test_long_flag_spelling(self, runner: CliRunner) -> None:
        _, client, _ = _invoke_editor(
            runner, ["re", "my-task", "--update"], written="Report only the H200s.",
        )
        client.set_reply_every_message.assert_called_once_with(
            "my-task", "Report only the H200s."
        )

    def test_it_is_not_a_reply(self, runner: CliRunner) -> None:
        """Nothing goes to the agent now; the timer delivers the new text."""
        _, client, _ = _invoke_editor(
            runner, ["re", "my-task", "-u"], written="Report only the H200s.",
        )
        client.reply.assert_not_called()

    def test_it_runs_the_configured_editor(self, runner: CliRunner) -> None:
        _, _, seen = _invoke_editor(
            runner, ["re", "my-task", "-u"], written="x", editor="nano",
        )
        assert seen["cmd"][0] == "nano"

    def test_the_buffer_is_prefilled_with_the_current_prompt(
        self, runner: CliRunner
    ) -> None:
        """Unlike `-e`, this edits something the task already carries."""
        _, _, seen = _invoke_editor(runner, ["re", "my-task", "-u"], written="x")
        assert seen["prefill"] == _LOOP_PROMPT

    def test_the_temp_file_is_labelled_a_loop_prompt(
        self, runner: CliRunner
    ) -> None:
        _, _, seen = _invoke_editor(runner, ["re", "my-task", "-u"], written="x")
        assert seen["cmd"][-1].endswith("-my-task-loop-prompt.txt")

    def test_surrounding_whitespace_is_stripped(self, runner: CliRunner) -> None:
        """Editors end a buffer with a newline; that is not part of the prompt."""
        _, client, _ = _invoke_editor(
            runner, ["re", "my-task", "-u"], written="  new prompt\n\n",
        )
        client.set_reply_every_message.assert_called_once_with("my-task", "new prompt")

    def test_inner_layout_is_preserved(self, runner: CliRunner) -> None:
        _, client, _ = _invoke_editor(
            runner, ["re", "my-task", "-u"], written="line one\n\nline two\n",
        )
        client.set_reply_every_message.assert_called_once_with(
            "my-task", "line one\n\nline two"
        )

    def test_it_reports_the_next_resend_and_the_unchanged_cadence(
        self, runner: CliRunner
    ) -> None:
        result, _, _ = _invoke_editor(
            runner, ["re", "my-task", "-u"], written="new prompt",
        )
        out = _squash(result.output)
        assert "Looping prompt for my-task updated." in out
        assert "Next re-send in 12m, then every 20m as before." in out

    def test_a_resend_that_is_due_is_said_to_be_due(self, runner: CliRunner) -> None:
        client = _make_client(
            _looping_task(reply_every_next_at=datetime.now(timezone.utc).isoformat())
        )
        client.set_reply_every_message.return_value["reply_every_next_at"] = (
            client.get_task.return_value["task"]["reply_every_next_at"]
        )
        result, _, _ = _invoke_editor(
            runner, ["re", "my-task", "-u"], written="new prompt", client=client,
        )
        assert "The next re-send is due now, then every 20m as before." in _squash(
            result.output
        )

    def test_an_unreadable_timer_still_confirms(self, runner: CliRunner) -> None:
        client = _make_client()
        client.set_reply_every_message.return_value["reply_every_next_at"] = None
        result, _, _ = _invoke_editor(
            runner, ["re", "my-task", "-u"], written="new prompt", client=client,
        )
        assert result.exit_code == 0, result.output
        assert "It goes out with the next re-send, every 20m as before." in _squash(
            result.output
        )

    def test_the_task_is_addressed_as_typed(self, runner: CliRunner) -> None:
        """An alias resolves on the server, so the write goes by what was typed."""
        client = _make_client(_looping_task(name="watch-the-run"))
        _, client, _ = _invoke_editor(
            runner, ["re", "wr", "-u"], written="new prompt", client=client,
        )
        client.set_reply_every_message.assert_called_once_with("wr", "new prompt")

    def test_a_working_looping_task_can_be_edited(self, runner: CliRunner) -> None:
        """Looping is about the cycle, not the status the agent is in."""
        client = _make_client(_looping_task(status="WORKING"))
        result, client, _ = _invoke_editor(
            runner, ["re", "my-task", "-u"], written="new prompt", client=client,
        )
        assert result.exit_code == 0, result.output
        client.set_reply_every_message.assert_called_once()


class TestUpdateFlagWritesNothing:
    """Every way the edit can fail must leave the cycle exactly as it was."""

    @pytest.mark.parametrize("prefix", _REPLY_PREFIXES)
    def test_a_task_that_is_not_looping_is_refused(
        self, runner: CliRunner, prefix: list[str]
    ) -> None:
        client = _make_client(
            {"name": "my-task", "status": "AGENT_FINISHED", "reply_every_seconds": None}
        )
        result, client, seen = _invoke_editor(
            runner, [*prefix, "my-task", "-u"], written="x", client=client,
        )
        assert result.exit_code == 1
        assert "Task my-task is not looping" in _squash(result.output)
        client.set_reply_every_message.assert_not_called()
        client.reply.assert_not_called()
        assert "cmd" not in seen  # the editor never opened

    def test_a_task_without_the_field_at_all_is_refused(
        self, runner: CliRunner
    ) -> None:
        """A server that predates cycles reports no field; that is not looping."""
        client = _make_client({"name": "my-task", "status": "AGENT_FINISHED"})
        result, client, seen = _invoke_editor(
            runner, ["re", "my-task", "-u"], written="x", client=client,
        )
        assert result.exit_code == 1
        assert "is not looping" in _squash(result.output)
        assert "cmd" not in seen

    @pytest.mark.parametrize("prefix", _REPLY_PREFIXES)
    def test_a_message_as_well_is_rejected(
        self, runner: CliRunner, prefix: list[str]
    ) -> None:
        result, client, seen = _invoke_editor(
            runner, [*prefix, "my-task", "hello", "-u"],
        )
        assert result.exit_code == 1
        assert "-u takes no message" in _squash(result.output)
        client.set_reply_every_message.assert_not_called()
        client.reply.assert_not_called()
        assert "cmd" not in seen

    @pytest.mark.parametrize(
        "extra",
        [["-e"], ["-t", "30m"], ["--max"], ["--unmax"], ["--line-number"],
         ["--no-line-number"]],
    )
    def test_the_other_reply_flags_are_rejected(
        self, runner: CliRunner, extra: list[str]
    ) -> None:
        """`-u` sends nothing, so flags that shape a reply have nothing to do."""
        result, client, seen = _invoke_editor(
            runner, ["re", "my-task", "-u", *extra],
        )
        assert result.exit_code == 1
        assert "-u only edits the looping prompt" in _squash(result.output)
        client.set_reply_every_message.assert_not_called()
        client.reply.assert_not_called()
        client.get_task.assert_not_called()
        assert "cmd" not in seen

    def test_an_unchanged_buffer_writes_nothing(self, runner: CliRunner) -> None:
        result, client, _ = _invoke_editor(runner, ["re", "my-task", "-u"], written=None)
        assert result.exit_code == 0, result.output
        assert "Looping prompt for my-task unchanged." in _squash(result.output)
        client.set_reply_every_message.assert_not_called()

    def test_a_buffer_that_only_gained_whitespace_is_unchanged(
        self, runner: CliRunner
    ) -> None:
        result, client, _ = _invoke_editor(
            runner, ["re", "my-task", "-u"], written=f"\n{_LOOP_PROMPT}\n\n",
        )
        assert result.exit_code == 0, result.output
        assert "unchanged" in _squash(result.output)
        client.set_reply_every_message.assert_not_called()

    @pytest.mark.parametrize("written", ["", "   \n\n  "])
    def test_an_empty_buffer_keeps_the_current_prompt(
        self, runner: CliRunner, written: str
    ) -> None:
        """A cycle with nothing to send is not a cycle; ending one is a reply's job."""
        result, client, _ = _invoke_editor(
            runner, ["re", "my-task", "-u"], written=written,
        )
        assert result.exit_code == 1
        assert "Empty looping prompt; the current one is kept." in _squash(
            result.output
        )
        client.set_reply_every_message.assert_not_called()

    def test_a_nonzero_exit_keeps_the_current_prompt(self, runner: CliRunner) -> None:
        """`:cq` in vim, or a crash, must not save what is in the buffer."""
        result, client, _ = _invoke_editor(
            runner, ["re", "my-task", "-u"], written="half a thought", returncode=1,
        )
        assert result.exit_code == 1
        assert "vim exited with 1; the looping prompt is unchanged." in _squash(
            result.output
        )
        client.set_reply_every_message.assert_not_called()

    def test_no_editor_configured(self, runner: CliRunner) -> None:
        result, client, seen = _invoke_editor(
            runner, ["re", "my-task", "-u"], configured_editor="",
        )
        assert result.exit_code == 1
        assert "No editor configured" in _squash(result.output)
        client.get_task.assert_not_called()
        client.set_reply_every_message.assert_not_called()
        assert "cmd" not in seen

    def test_editor_not_on_path(self, runner: CliRunner) -> None:
        client = _make_client()
        with patch("ilan.cli._client", return_value=client), \
                patch("ilan.cli.subprocess.run") as run, \
                patch("ilan.cli.shutil.which", return_value=None), \
                patch("ilan.cli.cfg.load", return_value={"editor": "vim"}):
            result = runner.invoke(main, ["re", "my-task", "-u"])
        assert result.exit_code == 1
        assert "is not installed, or not on your PATH" in _squash(result.output)
        client.set_reply_every_message.assert_not_called()
        run.assert_not_called()

    def test_an_unknown_task_is_caught_before_the_editor_opens(
        self, runner: CliRunner
    ) -> None:
        client = _make_client()
        client.get_task.return_value = {"error": "Task nope not found"}
        result, client, seen = _invoke_editor(
            runner, ["re", "nope", "-u"], written="x", client=client,
        )
        assert result.exit_code == 1
        assert "Task nope not found" in _squash(result.output)
        client.set_reply_every_message.assert_not_called()
        assert "cmd" not in seen

    def test_a_cycle_that_ended_while_editing_is_reported(
        self, runner: CliRunner
    ) -> None:
        """The server re-checks on landing; its refusal is what the user sees."""
        client = _make_client()
        client.set_reply_every_message.return_value = {
            "error": "Task my-task is not looping: there is no reply -t cycle "
            "whose message could be changed."
        }
        result, _, _ = _invoke_editor(
            runner, ["re", "my-task", "-u"], written="new prompt", client=client,
        )
        assert result.exit_code == 1
        assert "Task my-task is not looping" in _squash(result.output)


class TestTheOtherReplyModesAreUnchanged:
    """Adding `-u` must not change what the command does without it."""

    def test_a_bare_reply_still_shows_the_tail(self, runner: CliRunner) -> None:
        client = _make_client()
        with patch("ilan.cli._client", return_value=client), \
                patch("ilan.cli.subprocess.run") as run, \
                patch("ilan.cli._do_tail") as do_tail, \
                patch("ilan.cli.cfg.load", return_value={"editor": "vim"}):
            result = runner.invoke(main, ["re", "my-task"])
        assert result.exit_code == 0, result.output
        do_tail.assert_called_once()
        run.assert_not_called()
        client.set_reply_every_message.assert_not_called()

    def test_the_editor_flag_still_writes_a_fresh_reply(
        self, runner: CliRunner
    ) -> None:
        """`-e` keeps its empty buffer and still posts a reply."""
        result, client, seen = _invoke_editor(
            runner, ["re", "my-task", "-e"], written="ship it",
        )
        assert result.exit_code == 0, result.output
        assert seen["prefill"] == ""
        client.reply.assert_called_once_with("my-task", "ship it")
        client.set_reply_every_message.assert_not_called()

    def test_a_command_line_message_still_sends(self, runner: CliRunner) -> None:
        client = _make_client()
        with patch("ilan.cli._client", return_value=client), \
                patch("ilan.cli.cfg.load", return_value={"editor": "vim"}):
            result = runner.invoke(main, ["re", "my-task", "ship it"])
        assert result.exit_code == 0, result.output
        client.reply.assert_called_once_with("my-task", "ship it")
        client.set_reply_every_message.assert_not_called()

    @pytest.mark.parametrize("prefix", _REPLY_PREFIXES)
    def test_help_lists_the_flag(self, runner: CliRunner, prefix: list[str]) -> None:
        result = runner.invoke(main, [*prefix, "--help"])
        assert result.exit_code == 0
        assert "-u, --update" in _ANSI_RE.sub("", result.output)
