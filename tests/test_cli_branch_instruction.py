"""Quick branches use the existing HTTP branch path, including alias resolution."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from ilan import config as cfg
from ilan.cli import _BTW_SUFFIX, main
from ilan.models import Task, is_burnable_name
from ilan.server import IlanServer


@pytest.fixture()
def client(tmp_config: Path) -> MagicMock:
    client = MagicMock()
    client.branch_task.return_value = {
        "ok": True, "name": "xxx-cat-likes-fin", "parent_name": "parent-task",
    }
    return client


@pytest.mark.parametrize("prefix", [["branch"], ["task", "branch"]])
@pytest.mark.parametrize("parent", ["parent-task", "aa"])
def test_bare_instruction_matches_description(
    client: MagicMock, prefix: list[str], parent: str,
) -> None:
    runner = CliRunner()
    instruction = "  Explain the failure\nand suggest a fix.  "
    with patch("ilan.cli._client", return_value=client):
        result = runner.invoke(main, [*prefix, parent, instruction])
        assert result.exit_code == 0, result.output
        client.branch_task.assert_called_once_with(parent, None, instruction)
        client.get_task.assert_not_called()
        assert "xxx-cat-likes-fin" in result.output
        assert "Burnable" in result.output
        bare_call = client.branch_task.call_args
        client.reset_mock()
        result = runner.invoke(main, [*prefix, parent, "-d", instruction])
    assert result.exit_code == 0, result.output
    assert client.branch_task.call_args == bare_call


@pytest.mark.parametrize("flag", ["-n", "-d", "-f"])
@pytest.mark.parametrize("before", [True, False])
def test_bare_instruction_rejects_flags_before_request(
    client: MagicMock, tmp_path: Path, flag: str, before: bool,
) -> None:
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("from file")
    flags = [flag, str(prompt) if flag == "-f" else "child"]
    args = [*flags, "question"] if before else ["question", *flags]
    with patch("ilan.cli._client", return_value=client):
        result = CliRunner().invoke(main, ["branch", "aa", *args])
    assert result.exit_code == 1
    assert "takes no flags" in result.output
    client.branch_task.assert_not_called()


@pytest.mark.parametrize("source", ["bare", "description", "file"])
@pytest.mark.parametrize("instruction", ["", " \n\t "])
def test_empty_assignment_is_rejected(
    client: MagicMock, tmp_path: Path, source: str, instruction: str,
) -> None:
    prompt = tmp_path / "prompt.txt"
    prompt.write_text(instruction)
    args = {
        "bare": [instruction], "description": ["-d", instruction],
        "file": ["-f", str(prompt)],
    }[source]
    with patch("ilan.cli._client", return_value=client):
        result = CliRunner().invoke(main, ["branch", "aa", *args])
    assert result.exit_code == 1
    assert "must not be empty" in result.output
    client.branch_task.assert_not_called()


def test_double_dash_preserves_leading_dashes(client: MagicMock) -> None:
    with patch("ilan.cli._client", return_value=client):
        result = CliRunner().invoke(main, ["branch", "aa", "--", "--dry-run only"])
    assert result.exit_code == 0, result.output
    client.branch_task.assert_called_once_with("aa", None, "--dry-run only")


def test_extra_argument_does_not_create_a_task(client: MagicMock) -> None:
    with patch("ilan.cli._client", return_value=client):
        result = CliRunner().invoke(main, ["branch", "aa", "two", "arguments"])
    assert result.exit_code == 2
    client.branch_task.assert_not_called()


def test_bare_instruction_expands_parent_tail_refs(client: MagicMock) -> None:
    cfg.save({**cfg.DEFAULTS, "line-number": True})
    cfg.save_last_tail("aa", ["the failure"])
    with patch("ilan.cli._client", return_value=client):
        result = CliRunner().invoke(main, ["branch", "aa", "Explain @1"])
    assert result.exit_code == 0, result.output
    client.branch_task.assert_called_once_with("aa", None, "Explain\n\n> the failure")


def test_server_error_is_reported(client: MagicMock) -> None:
    client.branch_task.return_value = {"error": "Task missing not found"}
    with patch("ilan.cli._client", return_value=client):
        result = CliRunner().invoke(main, ["branch", "missing", "Explain this"])
    assert result.exit_code == 1
    assert "not found" in result.output
    assert "Burnable" not in result.output


@pytest.mark.parametrize("prefix", [["branch"], ["task", "branch"]])
def test_help_explains_quick_form(prefix: list[str]) -> None:
    result = CliRunner().invoke(main, [*prefix, "--help"])
    assert result.exit_code == 0, result.output
    assert "OLD_NAME [INSTRUCTION]" in result.output
    assert "quick form" in result.output


@pytest.mark.parametrize("engine", ["claude", "codex"])
@pytest.mark.parametrize("parent_ref", ["parent-task", "aa"])
@pytest.mark.parametrize("command", ["branch", "btw"])
def test_quick_branch_through_real_client_and_server(
    ilan_server: IlanServer, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    engine: str, parent_ref: str, command: str,
) -> None:
    """Exercise HTTP, inheritance and burning while agent spawning is stubbed."""
    session_log = tmp_path / "parent-session.jsonl"
    session_log.write_text("{}\n")
    parent = Task(
        name="parent-task", prompt="Keep watching the existing job",
        created_at="2026-01-01T00:00:00+00:00",
        status_changed_at="2026-01-01T00:00:00+00:00",
        engine=engine, session_id="parent-session", alias="aa",
        sessions={engine: "parent-session"}, session_log_path=str(session_log),
        task_hash="abcd1234",
    )
    with ilan_server.lock:
        ilan_server.store.put_task(parent)
        ilan_server.store.append_log(parent.name, "user", parent.prompt)
        ilan_server.store.append_log(parent.name, "assistant", "The job is progressing")
    monkeypatch.setenv("ILAN_SERVER_URL", ilan_server._test_url)
    with patch.object(
        ilan_server.runner, "find_session_log", return_value=session_log,
    ):
        result = CliRunner().invoke(main, [command, parent_ref, "Explain the last result"])
    assert result.exit_code == 0, result.output
    assignment = "Explain the last result" + (_BTW_SUFFIX if command == "btw" else "")
    with ilan_server.lock:
        tasks = ilan_server.store.load_tasks()
        child = next(task for task in tasks.values() if task.name != parent.name)
        assert is_burnable_name(child.name)
        if command == "btw":
            assert child.name == "xxx-parent-task-btw"
        assert child.parent_name == parent.name
        assert child.engine == engine
        assert child.prompt == parent.prompt
        assert child.cached_replies == [assignment]
        assert child.awaiting_branch_notice
        if engine == "claude":
            assert child.session_id != parent.session_id
            assert Path(child.session_log_path).read_text() == session_log.read_text()
        else:
            assert child.session_id is None
            assert child.awaiting_catchup
        assert [entry.content for entry in ilan_server.store.read_logs(child.name)] == [
            parent.prompt, "The job is progressing", assignment,
        ]
        assert ilan_server.store.get_task(parent.name) == parent
        # Check the actual Claude resume / Codex catch-up consumer, not just
        # the HTTP payload. Building the prompt does not launch an agent.
        with patch.object(ilan_server.runner, "find_session_log", return_value=session_log):
            prompt, resume = ilan_server.runner._build_prompt(child)
        assert assignment in prompt
        assert resume == (engine == "claude")
        assert prompt.count(_BTW_SUFFIX) == (1 if command == "btw" else 0)
    if command == "btw":
        with ilan_server.lock:
            before_tasks = ilan_server.store.load_tasks()
            before_logs = ilan_server.store.read_logs(child.name)
        result = CliRunner().invoke(main, [command, parent_ref, "Another question"])
        assert result.exit_code == 1
        assert "xxx-parent-task-btw already exists" in result.output
        with ilan_server.lock:
            assert ilan_server.store.load_tasks() == before_tasks
            assert ilan_server.store.read_logs(child.name) == before_logs
    result = CliRunner().invoke(main, ["done", child.name])
    assert result.exit_code == 0, result.output
    with ilan_server.lock:
        assert ilan_server.store.get_task(child.name) is None
        assert ilan_server.store.get_task(parent.name) == parent
    if command == "btw":
        with patch.object(ilan_server.runner, "find_session_log", return_value=session_log):
            result = CliRunner().invoke(main, [command, parent_ref, "A new question"])
        assert result.exit_code == 0, result.output
        with ilan_server.lock:
            recreated = ilan_server.store.get_task("xxx-parent-task-btw")
            assert recreated is not None
            assert recreated.cached_replies == ["A new question" + _BTW_SUFFIX]
            assert ilan_server.store.get_task(parent.name) == parent
