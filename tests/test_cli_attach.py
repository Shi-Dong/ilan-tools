"""Tests for the ``ilan task attach`` / ``ilan attach`` CLI commands."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from ilan.cli import main


@pytest.fixture()
def runner() -> CliRunner:
    return CliRunner()


def _make_client(task_resp: dict, *, remote: bool = False) -> MagicMock:
    """Build a mock Client whose get_task returns *task_resp*."""
    client = MagicMock()
    client.ensure_server.return_value = {}
    client.version_mismatch = None
    client.is_remote = remote
    client.get_task.return_value = task_resp
    return client


# ── remote client refused ───────────────────────────────────────────────


class TestAttachRemoteRefused:
    def test_remote_client_refused(self, runner: CliRunner, tmp_config) -> None:
        client = _make_client({"task": {}}, remote=True)
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["task", "attach", "any-task"])
        assert result.exit_code != 0
        assert "host machine" in result.output.lower()
        client.get_task.assert_not_called()

    def test_shorthand_remote_refused(self, runner: CliRunner, tmp_config) -> None:
        client = _make_client({"task": {}}, remote=True)
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["attach", "any-task"])
        assert result.exit_code != 0
        assert "host machine" in result.output.lower()


# ── task not found ──────────────────────────────────────────────────────


class TestAttachNotFound:
    def test_task_not_found(self, runner: CliRunner, tmp_config) -> None:
        client = _make_client({"error": "Task not found"})
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["task", "attach", "no-such"])
        assert result.exit_code != 0
        assert "not found" in result.output.lower()

    def test_shorthand_not_found(self, runner: CliRunner, tmp_config) -> None:
        client = _make_client({"error": "Task not found"})
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["attach", "no-such"])
        assert result.exit_code != 0


# ── no session yet ──────────────────────────────────────────────────────


class TestAttachNoSession:
    def test_no_session_id(self, runner: CliRunner, tmp_config) -> None:
        client = _make_client({
            "task": {
                "name": "my-task",
                "status": "NEEDS_ATTENTION",
                "session_id": None,
            }
        })
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["task", "attach", "my-task"])
        assert result.exit_code != 0
        assert "no session" in result.output.lower()

    def test_empty_session_id(self, runner: CliRunner, tmp_config) -> None:
        client = _make_client({
            "task": {
                "name": "my-task",
                "status": "NEEDS_ATTENTION",
                "session_id": "",
            }
        })
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["task", "attach", "my-task"])
        assert result.exit_code != 0
        assert "no session" in result.output.lower()


# ── WORKING task refused ────────────────────────────────────────────────


class TestAttachWorkingRefused:
    def test_working_task_refused(self, runner: CliRunner, tmp_config) -> None:
        client = _make_client({
            "task": {
                "name": "busy-task",
                "status": "WORKING",
                "session_id": "sess-123",
            }
        })
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["task", "attach", "busy-task"])
        assert result.exit_code != 0
        assert "WORKING" in result.output
        assert "kill" in result.output.lower()

    def test_shorthand_working_refused(self, runner: CliRunner, tmp_config) -> None:
        client = _make_client({
            "task": {
                "name": "busy-task",
                "status": "WORKING",
                "session_id": "sess-123",
            }
        })
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["attach", "busy-task"])
        assert result.exit_code != 0
        assert "WORKING" in result.output


# ── successful attach ───────────────────────────────────────────────────


class TestAttachSuccess:
    def test_execvp_called_with_resume(self, runner: CliRunner, tmp_config) -> None:
        client = _make_client({
            "task": {
                "name": "good-task",
                "status": "NEEDS_ATTENTION",
                "session_id": "sess-abc",
            }
        })
        with (
            patch("ilan.cli._client", return_value=client),
            patch("ilan.cli.os.chdir") as mock_chdir,
            patch("ilan.cli.os.execvp") as mock_execvp,
        ):
            result = runner.invoke(main, ["task", "attach", "good-task"])

        assert result.exit_code == 0
        mock_chdir.assert_called_once()
        mock_execvp.assert_called_once()
        args = mock_execvp.call_args
        assert args[0][0] == "claude"
        argv = args[0][1]
        assert "--resume" in argv
        assert "sess-abc" in argv

    def test_passes_model_and_effort_flags(self, runner: CliRunner, tmp_config) -> None:
        client = _make_client({
            "task": {
                "name": "flag-task",
                "status": "AGENT_FINISHED",
                "session_id": "sess-xyz",
            }
        })
        with (
            patch("ilan.cli._client", return_value=client),
            patch("ilan.cli.os.chdir"),
            patch("ilan.cli.os.execvp") as mock_execvp,
        ):
            result = runner.invoke(main, ["task", "attach", "flag-task"])

        assert result.exit_code == 0
        argv = mock_execvp.call_args[0][1]
        assert "--dangerously-skip-permissions" in argv
        assert "--model" in argv
        assert "--effort" in argv

    def test_shorthand_attach(self, runner: CliRunner, tmp_config) -> None:
        client = _make_client({
            "task": {
                "name": "short-task",
                "status": "NEEDS_ATTENTION",
                "session_id": "sess-short",
            }
        })
        with (
            patch("ilan.cli._client", return_value=client),
            patch("ilan.cli.os.chdir"),
            patch("ilan.cli.os.execvp") as mock_execvp,
        ):
            result = runner.invoke(main, ["attach", "short-task"])

        assert result.exit_code == 0
        mock_execvp.assert_called_once()
        argv = mock_execvp.call_args[0][1]
        assert "sess-short" in argv

    def test_done_task_can_attach(self, runner: CliRunner, tmp_config) -> None:
        """DONE tasks still have a session — attaching should work."""
        client = _make_client({
            "task": {
                "name": "done-task",
                "status": "DONE",
                "session_id": "sess-done",
            }
        })
        with (
            patch("ilan.cli._client", return_value=client),
            patch("ilan.cli.os.chdir"),
            patch("ilan.cli.os.execvp") as mock_execvp,
        ):
            result = runner.invoke(main, ["task", "attach", "done-task"])

        assert result.exit_code == 0
        mock_execvp.assert_called_once()
