"""Tests for ``ilan sleep`` / ``ilan task sleep`` and the sleep-suffix renderer."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from ilan.cli import _format_sleep_suffix, main


@pytest.fixture()
def runner() -> CliRunner:
    return CliRunner()


def _make_client() -> MagicMock:
    client = MagicMock()
    client.ensure_server.return_value = {}
    client.version_mismatch = None
    client.is_remote = False
    client.sleep_task.return_value = {"ok": True, "name": "my-task"}
    return client


class TestSleepCommand:
    def test_task_sleep_calls_client(self, runner: CliRunner, tmp_config) -> None:
        client = _make_client()
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["task", "sleep", "my-task", "300"])
        assert result.exit_code == 0
        client.sleep_task.assert_called_once_with("my-task", 300)

    def test_shorthand_sleep_calls_client(self, runner: CliRunner, tmp_config) -> None:
        client = _make_client()
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["sleep", "my-task", "300"])
        assert result.exit_code == 0
        client.sleep_task.assert_called_once_with("my-task", 300)

    def test_sleep_rejects_non_positive_seconds(
        self, runner: CliRunner, tmp_config
    ) -> None:
        client = _make_client()
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["sleep", "my-task", "0"])
        assert result.exit_code == 1
        client.sleep_task.assert_not_called()

    def test_sleep_surfaces_server_error(
        self, runner: CliRunner, tmp_config
    ) -> None:
        client = _make_client()
        client.sleep_task.return_value = {"error": "Task is UNCLAIMED, not WORKING."}
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["sleep", "my-task", "300"])
        assert result.exit_code == 1
        assert "UNCLAIMED" in result.output


class TestFormatSleepSuffix:
    def test_none_returns_none(self) -> None:
        assert _format_sleep_suffix(None) is None

    def test_empty_returns_none(self) -> None:
        assert _format_sleep_suffix("") is None

    def test_past_timestamp_returns_none(self) -> None:
        past = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat()
        assert _format_sleep_suffix(past) is None

    def test_future_timestamp_shows_remaining(self) -> None:
        future = (datetime.now(timezone.utc) + timedelta(seconds=300)).isoformat()
        suffix = _format_sleep_suffix(future)
        assert suffix is not None
        assert "sleeping for" in suffix
        assert "s)" in suffix

    def test_malformed_timestamp_returns_none(self) -> None:
        assert _format_sleep_suffix("not-a-timestamp") is None
