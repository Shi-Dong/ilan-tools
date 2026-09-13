"""Tests for ``-t/--every`` without a message: re-timing a looping task."""

from __future__ import annotations

import re
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


def _looping_task(**overrides) -> dict:
    task = {
        "name": "my-task",
        "status": "AGENT_FINISHED",
        "reply_every_seconds": 1200,
        "reply_every_message": "Report the status.",
        "reply_every_next_at": "2030-01-01T00:00:00+00:00",
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
    client.retime_reply_every.return_value = {
        "ok": True,
        "name": task["name"],
        "message": f"Sent the looping prompt to {task['name']}. Agent resumed.",
        "reply_every_seconds": 1800,
        "previous_every_seconds": task.get("reply_every_seconds"),
        "reply_every_next_at": "2030-01-01T00:30:00+00:00",
    }
    return client


def _invoke(runner: CliRunner, argv: list[str], client: MagicMock | None = None):
    client = client or _make_client()
    with patch("ilan.cli._client", return_value=client):
        result = runner.invoke(main, argv)
    return result, client


@pytest.fixture()
def runner() -> CliRunner:
    return CliRunner()


class TestEveryAloneRetimesALoopingTask:
    @pytest.mark.parametrize("prefix", _REPLY_PREFIXES)
    def test_it_retimes_through_the_cycle_route(
        self, runner: CliRunner, tmp_config, prefix: list[str]
    ) -> None:
        result, client = _invoke(runner, [*prefix, "my-task", "-t", "30m"])
        assert result.exit_code == 0, result.output
        client.retime_reply_every.assert_called_once_with("my-task", 1800)
        # The server re-sends the cycle's own message; the client never
        # retypes it as a reply.
        client.reply.assert_not_called()

    def test_long_flag_spelling(self, runner: CliRunner, tmp_config) -> None:
        _, client = _invoke(runner, ["re", "my-task", "--every", "1h"])
        client.retime_reply_every.assert_called_once_with("my-task", 3600)

    @pytest.mark.parametrize(
        ("arg", "expected_seconds"),
        [("1200", 1200), ("1200s", 1200), ("20m", 1200), ("0.5h", 1800),
         ("2h", 7200), ("1.5h", 5400)],
    )
    def test_it_accepts_sleep_durations(
        self, runner: CliRunner, tmp_config, arg: str, expected_seconds: int
    ) -> None:
        _, client = _invoke(runner, ["re", "my-task", "-t", arg])
        client.retime_reply_every.assert_called_once_with("my-task", expected_seconds)

    def test_it_reports_the_delivery_and_the_new_cadence(
        self, runner: CliRunner, tmp_config
    ) -> None:
        result, _ = _invoke(runner, ["re", "my-task", "-t", "30m"])
        out = _squash(result.output)
        assert "Sent the looping prompt to my-task. Agent resumed." in out
        # 1800s renders as 0.5h, as it does in the `-t MESSAGE` confirmation.
        assert "Will re-send it every 0.5h (was 20m) until the next human reply." in out

    def test_the_same_cadence_is_not_called_a_change(
        self, runner: CliRunner, tmp_config
    ) -> None:
        client = _make_client()
        client.retime_reply_every.return_value["reply_every_seconds"] = 1200
        client.retime_reply_every.return_value["previous_every_seconds"] = 1200
        result, _ = _invoke(runner, ["re", "my-task", "-t", "20m"], client=client)
        out = _squash(result.output)
        assert "Will re-send it every 20m until the next human reply." in out
        assert "(was" not in out

    def test_a_working_looping_task_qualifies(
        self, runner: CliRunner, tmp_config
    ) -> None:
        """Looping is about the cycle, not the status the agent is in."""
        client = _make_client(_looping_task(status="WORKING"))
        client.retime_reply_every.return_value["message"] = (
            "Interrupted my-task and resumed it with the looping prompt."
        )
        result, client = _invoke(runner, ["re", "my-task", "-t", "30m"], client=client)
        assert result.exit_code == 0, result.output
        client.retime_reply_every.assert_called_once_with("my-task", 1800)
        assert "Interrupted my-task and resumed it" in _squash(result.output)

    def test_the_task_is_addressed_as_typed(self, runner: CliRunner, tmp_config) -> None:
        """An alias resolves on the server, so the request goes by what was typed."""
        client = _make_client(_looping_task(name="watch-the-run"))
        _, client = _invoke(runner, ["re", "wr", "-t", "30m"], client=client)
        client.retime_reply_every.assert_called_once_with("wr", 1800)


class TestEveryAloneIsRefused:
    """Every refusal must leave the cycle, and the agent, untouched."""

    @pytest.mark.parametrize("prefix", _REPLY_PREFIXES)
    def test_a_task_that_is_not_looping(
        self, runner: CliRunner, tmp_config, prefix: list[str]
    ) -> None:
        client = _make_client(
            {"name": "my-task", "status": "AGENT_FINISHED", "reply_every_seconds": None}
        )
        result, client = _invoke(runner, [*prefix, "my-task", "-t", "30m"], client=client)
        assert result.exit_code == 1
        out = _squash(result.output)
        assert "Task my-task is not looping" in out
        assert "Give a message to start one." in out
        client.retime_reply_every.assert_not_called()
        client.reply.assert_not_called()

    def test_a_task_without_the_field_at_all(self, runner: CliRunner, tmp_config) -> None:
        client = _make_client({"name": "my-task", "status": "AGENT_FINISHED"})
        result, client = _invoke(runner, ["re", "my-task", "-t", "30m"], client=client)
        assert result.exit_code == 1
        assert "is not looping" in _squash(result.output)
        client.retime_reply_every.assert_not_called()

    @pytest.mark.parametrize("arg", ["0", "abc", "5d", "5 m", "-5m"])
    def test_a_bad_duration_is_refused_before_the_task_is_fetched(
        self, runner: CliRunner, tmp_config, arg: str
    ) -> None:
        result, client = _invoke(runner, ["re", "my-task", "-t", arg])
        assert result.exit_code != 0
        client.get_task.assert_not_called()
        client.retime_reply_every.assert_not_called()

    @pytest.mark.parametrize("arg", ["1199", "19m", "5m", "300s", "0.3h"])
    def test_a_too_short_duration(
        self, runner: CliRunner, tmp_config, arg: str
    ) -> None:
        result, client = _invoke(runner, ["re", "my-task", "-t", arg])
        assert result.exit_code == 1
        assert "-t/--every must be at least 20m" in _squash(result.output)
        client.get_task.assert_not_called()
        client.retime_reply_every.assert_not_called()

    def test_an_unknown_task(self, runner: CliRunner, tmp_config) -> None:
        client = _make_client()
        client.get_task.return_value = {"error": "Task nope not found"}
        result, client = _invoke(runner, ["re", "nope", "-t", "30m"], client=client)
        assert result.exit_code == 1
        assert "Task nope not found" in _squash(result.output)
        client.retime_reply_every.assert_not_called()

    def test_a_cycle_that_ended_meanwhile_is_reported(
        self, runner: CliRunner, tmp_config
    ) -> None:
        """The server re-checks on landing; its refusal is what the user sees."""
        client = _make_client()
        client.retime_reply_every.return_value = {
            "error": "Task my-task is not looping: there is no reply -t cycle to change."
        }
        result, _ = _invoke(runner, ["re", "my-task", "-t", "30m"], client=client)
        assert result.exit_code == 1
        assert "Task my-task is not looping" in _squash(result.output)
        assert "Will re-send" not in result.output

    @pytest.mark.parametrize("flag", ["--line-number", "--no-line-number"])
    def test_the_tail_flags_are_rejected(
        self, runner: CliRunner, tmp_config, flag: str
    ) -> None:
        """They tune the tail, and `-t` alone shows no tail."""
        result, client = _invoke(runner, ["re", "my-task", "-t", "30m", flag])
        assert result.exit_code == 1
        assert "apply to the tail" in _squash(result.output)
        client.get_task.assert_not_called()
        client.retime_reply_every.assert_not_called()

    @pytest.mark.parametrize("flag", ["--max", "--unmax"])
    def test_the_model_flags_still_need_a_message(
        self, runner: CliRunner, tmp_config, flag: str
    ) -> None:
        result, client = _invoke(runner, ["re", "my-task", "-t", "30m", flag])
        assert result.exit_code == 1
        assert "require a response message" in _squash(result.output)
        client.retime_reply_every.assert_not_called()

    def test_update_still_refuses_every(self, runner: CliRunner, tmp_config) -> None:
        """`-u` edits the prompt and leaves the cadence alone; the two stay apart."""
        result, client = _invoke(runner, ["re", "my-task", "-u", "-t", "30m"])
        assert result.exit_code == 1
        assert "-u only edits the looping prompt" in _squash(result.output)
        client.retime_reply_every.assert_not_called()
        client.set_reply_every_message.assert_not_called()


class TestEveryWithAMessageIsUnchanged:
    """Adding the message-free form must not change what `-t MESSAGE` does."""

    def test_a_message_with_every_starts_a_cycle_through_reply(
        self, runner: CliRunner, tmp_config
    ) -> None:
        result, client = _invoke(runner, ["re", "my-task", "go on", "-t", "1h"])
        assert result.exit_code == 0, result.output
        client.reply.assert_called_once_with("my-task", "go on", every_seconds=3600)
        client.retime_reply_every.assert_not_called()

    def test_the_editor_flag_with_every_still_writes_a_reply(
        self, runner: CliRunner, tmp_config
    ) -> None:
        client = _make_client()
        with patch("ilan.cli._client", return_value=client), \
                patch("ilan.cli._collect_editor_reply", return_value="poke"):
            result = runner.invoke(main, ["re", "my-task", "-e", "-t", "1h"])
        assert result.exit_code == 0, result.output
        client.reply.assert_called_once_with("my-task", "poke", every_seconds=3600)
        client.retime_reply_every.assert_not_called()

    def test_a_bare_reply_still_shows_the_tail(self, runner: CliRunner, tmp_config) -> None:
        client = _make_client()
        with patch("ilan.cli._client", return_value=client), \
                patch("ilan.cli._do_tail") as do_tail:
            result = runner.invoke(main, ["re", "my-task"])
        assert result.exit_code == 0, result.output
        do_tail.assert_called_once()
        client.retime_reply_every.assert_not_called()

    @pytest.mark.parametrize("prefix", _REPLY_PREFIXES)
    def test_help_describes_the_message_free_form(
        self, runner: CliRunner, tmp_config, prefix: list[str]
    ) -> None:
        out = _ANSI_RE.sub("", runner.invoke(main, [*prefix, "--help"]).output)
        assert "Without a message" in out
