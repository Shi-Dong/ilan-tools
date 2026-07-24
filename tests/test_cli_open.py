"""Tests for the ``ilan task open`` / ``ilan open`` CLI commands."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from ilan.cli import main


@pytest.fixture()
def runner() -> CliRunner:
    return CliRunner()


def _make_client(history_resp: dict) -> MagicMock:
    client = MagicMock()
    client.ensure_server.return_value = {}
    client.version_mismatch = None
    client.get_history_url.return_value = history_resp
    return client


DEEP_URL = "https://gist.github.com/u/gid1?permalink_comment_id=7#gistcomment-7"


class TestOpenSuccess:
    def test_opens_browser_with_url(self, runner: CliRunner, tmp_config) -> None:
        client = _make_client({"url": DEEP_URL})
        with patch("ilan.cli._client", return_value=client), \
                patch("ilan.cli.webbrowser.open") as mock_open:
            result = runner.invoke(main, ["task", "open", "my-task"])
        assert result.exit_code == 0
        assert DEEP_URL in result.output
        mock_open.assert_called_once_with(DEEP_URL)
        client.get_history_url.assert_called_once_with("my-task")

    def test_shorthand_opens_browser(self, runner: CliRunner, tmp_config) -> None:
        client = _make_client({"url": DEEP_URL})
        with patch("ilan.cli._client", return_value=client), \
                patch("ilan.cli.webbrowser.open") as mock_open:
            result = runner.invoke(main, ["open", "my-task"])
        assert result.exit_code == 0
        mock_open.assert_called_once_with(DEEP_URL)


class TestOpenNoHistory:
    def test_warns_and_does_nothing(self, runner: CliRunner, tmp_config) -> None:
        client = _make_client({"url": None})
        with patch("ilan.cli._client", return_value=client), \
                patch("ilan.cli.webbrowser.open") as mock_open:
            result = runner.invoke(main, ["task", "open", "my-task"])
        assert result.exit_code == 0
        assert "no history page" in result.output.lower()
        mock_open.assert_not_called()

    def test_shorthand_warns(self, runner: CliRunner, tmp_config) -> None:
        client = _make_client({"url": None})
        with patch("ilan.cli._client", return_value=client), \
                patch("ilan.cli.webbrowser.open") as mock_open:
            result = runner.invoke(main, ["open", "my-task"])
        assert result.exit_code == 0
        assert "no history page" in result.output.lower()
        mock_open.assert_not_called()


class TestOpenErrors:
    def test_task_not_found(self, runner: CliRunner, tmp_config) -> None:
        client = _make_client({"error": "Task not found"})
        with patch("ilan.cli._client", return_value=client), \
                patch("ilan.cli.webbrowser.open") as mock_open:
            result = runner.invoke(main, ["task", "open", "no-such"])
        assert result.exit_code != 0
        assert "not found" in result.output.lower()
        mock_open.assert_not_called()
