"""Tests for ``ilan reply --level``: the reasoning level from this reply on."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from ilan.cli import main
from ilan.models import ENGINE_CLAUDE, ENGINE_CODEX, Task, TaskStatus
from ilan.server import IlanServer
from tests.helpers import post_json


# ── server: POST /tasks/<name>/reply with a level ───────────────────────


def _seed(server: IlanServer, *, status: TaskStatus = TaskStatus.AGENT_FINISHED,
          engine: str = ENGINE_CLAUDE, reasoning: str = "low", **extra) -> Task:
    task = Task(
        name="my-task", prompt="p", status=status, engine=engine, reasoning=reasoning,
        created_at="2026-01-01T00:00:00+00:00",
        status_changed_at="2026-01-01T00:00:00+00:00",
        session_id="sid-1", **extra,
    )
    with server.lock:
        server.store.put_task(task)
    return task


def _stored(server: IlanServer) -> Task:
    with server.lock:
        task = server.store.get_task("my-task")
    assert task is not None
    return task


@pytest.mark.parametrize(("engine", "level", "effort"), [
    (ENGINE_CLAUDE, "max", "max"),
    (ENGINE_CODEX, "max", "xhigh"),
    (ENGINE_CODEX, "medium", "medium"),
])
def test_the_answer_to_the_reply_is_spawned_at_the_new_level(
    ilan_server: IlanServer, engine: str, level: str, effort: str,
) -> None:
    """The spawn that answers this very message is already told the level."""
    _seed(ilan_server, engine=engine)
    seen: list[str] = []
    fake_start = ilan_server.runner.start

    def start(task: Task) -> bool:
        seen.append(task.effort)
        return fake_start(task)

    with patch.object(ilan_server.runner, "start", side_effect=start):
        resp = post_json(ilan_server, "/tasks/my-task/reply", {"message": "Go", "level": level})
    assert resp["ok"] is True, resp
    assert (resp["reasoning"], resp["effort"]) == (level, effort)
    assert seen == [effort]
    # And it stays: every turn after this one runs at it too.
    assert _stored(ilan_server).reasoning == level


def test_a_working_task_is_resumed_at_the_new_level(ilan_server: IlanServer) -> None:
    """A WORKING task is interrupted and resumed with the reply; the resumed
    spawn is the one that answers it, so it must see the new level."""
    _seed(ilan_server, status=TaskStatus.WORKING, engine=ENGINE_CODEX)
    seen: list[str] = []

    def reply_to_working(task: Task, message: str) -> None:
        seen.append(task.effort)

    with patch.object(ilan_server.runner, "reply_to_working", side_effect=reply_to_working):
        resp = post_json(ilan_server, "/tasks/my-task/reply", {"message": "Go", "level": "max"})
    assert resp["ok"] is True, resp
    assert seen == ["xhigh"]
    assert (resp["reasoning"], resp["effort"]) == ("max", "xhigh")


def test_the_level_is_case_insensitive(ilan_server: IlanServer) -> None:
    _seed(ilan_server)
    resp = post_json(ilan_server, "/tasks/my-task/reply", {"message": "Go", "level": " MAX "})
    assert resp["reasoning"] == "max"


@pytest.mark.parametrize("bad", ["high", "xhigh", "none", "", 3])
def test_a_bad_level_refuses_the_reply_and_changes_nothing(
    ilan_server: IlanServer, bad: object,
) -> None:
    """Refused up front, so the message is not posted at the old level either."""
    _seed(ilan_server)
    with patch.object(ilan_server.runner, "start") as start:
        resp = post_json(ilan_server, "/tasks/my-task/reply", {"message": "Go", "level": bad})
    assert "Invalid reasoning level" in resp["error"]
    start.assert_not_called()
    task = _stored(ilan_server)
    assert task.reasoning == "low"
    assert task.status == TaskStatus.AGENT_FINISHED
    with ilan_server.lock:
        assert ilan_server.store.read_logs("my-task") == []


def test_a_declined_cycle_confirmation_leaves_the_level_alone(ilan_server: IlanServer) -> None:
    """A reply that ends a reply -t cycle is first refused with a question; the
    level must not move until the reply is actually accepted."""
    _seed(ilan_server, reply_every_seconds=3600, reply_every_message="poke")
    resp = post_json(ilan_server, "/tasks/my-task/reply", {"message": "Go", "level": "max"})
    assert resp.get("confirm_reply_every") is True, resp
    assert _stored(ilan_server).reasoning == "low"

    resp = post_json(ilan_server, "/tasks/my-task/reply", {
        "message": "Go", "level": "max", "override_reply_every": True,
    })
    assert resp["ok"] is True, resp
    assert _stored(ilan_server).reasoning == "max"


def test_a_reply_without_a_level_keeps_the_tasks_level(ilan_server: IlanServer) -> None:
    _seed(ilan_server, reasoning="medium")
    resp = post_json(ilan_server, "/tasks/my-task/reply", {"message": "Go"})
    assert resp["ok"] is True, resp
    assert resp["reasoning"] == "medium"
    assert _stored(ilan_server).reasoning == "medium"


def test_a_closed_task_refuses_the_reply_and_keeps_its_level(ilan_server: IlanServer) -> None:
    _seed(ilan_server, status=TaskStatus.DONE)
    resp = post_json(ilan_server, "/tasks/my-task/reply", {"message": "Go", "level": "max"})
    assert "Cannot reply" in resp["error"]
    assert _stored(ilan_server).reasoning == "low"


# ── CLI: ilan reply --level ─────────────────────────────────────────────


@pytest.fixture()
def runner() -> CliRunner:
    return CliRunner()


def _client(reasoning: str | None = "max", effort: str = "xhigh") -> MagicMock:
    client = MagicMock()
    client.ensure_server.return_value = {}
    client.version_mismatch = None
    client.is_remote = False
    reply = {"ok": True, "name": "my-task", "message": "Reply sent to my-task. Agent resumed."}
    if reasoning is not None:
        reply |= {"reasoning": reasoning, "effort": effort}
    client.reply.return_value = reply
    return client


@pytest.mark.parametrize("base", [["task", "reply"], ["reply"], ["re"]])
@pytest.mark.parametrize("spelling", ["max", "MAX"])
def test_the_level_travels_with_the_reply(
    runner: CliRunner, tmp_config, base: list[str], spelling: str,
) -> None:
    client = _client()
    with patch("ilan.cli._client", return_value=client):
        result = runner.invoke(main, [*base, "my-task", "Dig in", "--level", spelling])
    assert result.exit_code == 0, result.output
    client.reply.assert_called_once_with("my-task", "Dig in", level="max")
    # One request, not an `ilan level` call ahead of it.
    client.set_level.assert_not_called()
    assert "Reasoning level set to max (effort: xhigh)" in result.output
    assert "starting with the answer to this reply" in " ".join(result.output.split())


def test_no_level_sends_none(runner: CliRunner, tmp_config) -> None:
    client = _client(reasoning="low", effort="low")
    with patch("ilan.cli._client", return_value=client):
        result = runner.invoke(main, ["reply", "my-task", "Go"])
    assert result.exit_code == 0, result.output
    client.reply.assert_called_once_with("my-task", "Go")
    assert "Reasoning level" not in result.output


@pytest.mark.parametrize("bad", ["high", "xhigh", "minimal"])
def test_only_the_three_levels_are_accepted(runner: CliRunner, tmp_config, bad: str) -> None:
    client = _client()
    with patch("ilan.cli._client", return_value=client):
        result = runner.invoke(main, ["reply", "my-task", "Go", "--level", bad])
    assert result.exit_code != 0
    client.reply.assert_not_called()


def test_a_level_needs_a_message(runner: CliRunner, tmp_config) -> None:
    """Without a message there is no answer to set the level for; ilan level
    is the command for changing it on its own."""
    client = _client()
    with patch("ilan.cli._client", return_value=client):
        result = runner.invoke(main, ["reply", "my-task", "--level", "max"])
    assert result.exit_code != 0
    assert "--level requires a response message" in result.output
    client.reply.assert_not_called()
    client.get_tail.assert_not_called()


def test_a_level_does_not_combine_with_retiming_a_cycle(runner: CliRunner, tmp_config) -> None:
    client = _client()
    with patch("ilan.cli._client", return_value=client):
        result = runner.invoke(main, ["reply", "my-task", "-t", "1h", "--level", "max"])
    assert result.exit_code != 0
    assert "--level requires a response message" in result.output
    client.retime_reply_every.assert_not_called()


def test_a_level_does_not_combine_with_update(runner: CliRunner, tmp_config) -> None:
    client = _client()
    with patch("ilan.cli._client", return_value=client):
        result = runner.invoke(main, ["reply", "my-task", "-u", "--level", "max"])
    assert result.exit_code != 0
    assert "--level" in result.output
    client.set_reply_every_message.assert_not_called()


def test_a_level_combines_with_the_editor(runner: CliRunner, tmp_config) -> None:
    client = _client()
    with patch("ilan.cli._client", return_value=client), \
         patch("ilan.cli._collect_editor_reply", return_value="Written in the editor"):
        result = runner.invoke(main, ["reply", "my-task", "-e", "--level", "max"])
    assert result.exit_code == 0, result.output
    client.reply.assert_called_once_with("my-task", "Written in the editor", level="max")


def test_a_level_combines_with_a_new_cycle(runner: CliRunner, tmp_config) -> None:
    client = _client()
    with patch("ilan.cli._client", return_value=client):
        result = runner.invoke(main, ["reply", "my-task", "Status?", "-t", "1h", "--level", "max"])
    assert result.exit_code == 0, result.output
    client.reply.assert_called_once_with("my-task", "Status?", every_seconds=3600, level="max")


def test_the_level_is_resent_when_the_cycle_confirmation_is_accepted(
    runner: CliRunner, tmp_config,
) -> None:
    client = _client()
    accepted = client.reply.return_value
    client.reply.side_effect = [
        {"confirm_reply_every": True, "name": "my-task", "reply_every_seconds": 3600},
        accepted,
    ]
    with patch("ilan.cli._client", return_value=client):
        result = runner.invoke(main, ["reply", "my-task", "Go", "--level", "max"], input="y\n")
    assert result.exit_code == 0, result.output
    assert client.reply.call_args_list[1].kwargs == {"override_reply_every": True, "level": "max"}
    assert "Reasoning level set to max" in result.output


def test_a_declined_cycle_confirmation_reports_no_level(runner: CliRunner, tmp_config) -> None:
    client = _client()
    client.reply.return_value = {
        "confirm_reply_every": True, "name": "my-task", "reply_every_seconds": 3600,
    }
    with patch("ilan.cli._client", return_value=client):
        result = runner.invoke(main, ["reply", "my-task", "Go", "--level", "max"], input="n\n")
    assert result.exit_code != 0
    assert client.reply.call_count == 1
    assert "Reasoning level set" not in result.output


def test_a_server_that_ignores_the_level_is_reported(runner: CliRunner, tmp_config) -> None:
    """An older server drops the field it does not know and replies at the old
    level; the CLI says so rather than confirming a change that did not happen."""
    client = _client(reasoning=None)
    with patch("ilan.cli._client", return_value=client):
        result = runner.invoke(main, ["reply", "my-task", "Go", "--level", "max"])
    assert result.exit_code == 0, result.output
    out = " ".join(result.output.split())
    assert "did not change the reasoning level" in out
    assert "ilan level my-task max" in out
    assert "Reasoning level set" not in out
