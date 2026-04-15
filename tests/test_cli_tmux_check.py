"""Tests for the tmux availability check on ``ilan add``."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from ilan.cli import main


def _make_client(**overrides) -> MagicMock:
    client = MagicMock()
    client.ensure_server.return_value = {}
    client.version_mismatch = None
    client.is_remote = False
    for k, v in overrides.items():
        setattr(client, k, v)
    return client


class TestTmuxRequired:
    def test_add_fails_without_tmux(self, tmp_config) -> None:
        runner = CliRunner()
        with patch("ilan.cli.shutil.which", return_value=None):
            result = runner.invoke(main, ["add", "-n", "my-task", "-d", "do stuff"])
        assert result.exit_code != 0
        assert "tmux" in result.output.lower()
        assert "required" in result.output.lower()

    def test_task_add_fails_without_tmux(self, tmp_config) -> None:
        runner = CliRunner()
        with patch("ilan.cli.shutil.which", return_value=None):
            result = runner.invoke(main, ["task", "add", "-n", "my-task", "-d", "do stuff"])
        assert result.exit_code != 0
        assert "tmux" in result.output.lower()

    def test_add_succeeds_with_tmux(self, tmp_config) -> None:
        runner = CliRunner()
        client = _make_client()
        client.add_task.return_value = {"ok": True}
        with (
            patch("ilan.cli.shutil.which", return_value="/usr/bin/tmux"),
            patch("ilan.cli._client", return_value=client),
        ):
            result = runner.invoke(main, ["add", "-n", "my-task", "-d", "do stuff"])
        assert result.exit_code == 0
        client.add_task.assert_called_once()
