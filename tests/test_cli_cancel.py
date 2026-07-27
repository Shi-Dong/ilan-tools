"""Tests for ``ilan cancel`` across allowed task statuses."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from ilan.cli import CANCEL_MESSAGE, main


@pytest.fixture()
def runner() -> CliRunner:
    return CliRunner()


def _make_client(status: str) -> MagicMock:
    client = MagicMock()
    client.ensure_server.return_value = {}
    client.version_mismatch = None
    client.is_remote = False
    client.get_task.return_value = {
        "task": {"name": "my-task", "status": status},
    }
    client.reply.return_value = {"ok": True, "message": "Reply cached."}
    return client


class TestCancelAllowedStatuses:
    @pytest.mark.parametrize("status", ["WORKING", "AGENT_FINISHED", "NEEDS_ATTENTION", "ERROR"])
    def test_cancel_replies_with_cancel_message(
        self, runner: CliRunner, tmp_config, status: str
    ) -> None:
        client = _make_client(status)
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["cancel", "my-task"])
        assert result.exit_code == 0
        client.reply.assert_called_once_with("my-task", CANCEL_MESSAGE)

    @pytest.mark.parametrize("status", ["WORKING", "AGENT_FINISHED", "NEEDS_ATTENTION", "ERROR"])
    def test_task_cancel_replies_with_cancel_message(
        self, runner: CliRunner, tmp_config, status: str
    ) -> None:
        client = _make_client(status)
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["task", "cancel", "my-task"])
        assert result.exit_code == 0
        client.reply.assert_called_once_with("my-task", CANCEL_MESSAGE)


class TestCancelDisallowedStatuses:
    @pytest.mark.parametrize("status", ["DONE", "DISCARDED"])
    def test_cancel_rejects_other_statuses(
        self, runner: CliRunner, tmp_config, status: str
    ) -> None:
        client = _make_client(status)
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["cancel", "my-task"])
        assert result.exit_code == 0
        client.reply.assert_not_called()
        assert status in result.output
