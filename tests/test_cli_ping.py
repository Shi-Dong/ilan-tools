"""Tests for ``ilan ping``."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from ilan.cli import main


class TestPingLocal:
    def test_local_server_skips_network_ping(self) -> None:
        runner = CliRunner()
        client = MagicMock()
        client.is_remote = False
        with patch("ilan.cli.Client", return_value=client):
            result = runner.invoke(main, ["ping"])
        assert result.exit_code == 0
        assert "local" in result.output.lower()
        client.health.assert_not_called()


class TestPingRemote:
    def _remote_client(self) -> MagicMock:
        client = MagicMock()
        client.is_remote = True
        client._base_url = "http://example.invalid:4526"
        client.health.return_value = {"status": "ok"}
        return client

    def test_remote_server_pings_count_times(self) -> None:
        runner = CliRunner()
        client = self._remote_client()
        with patch("ilan.cli.Client", return_value=client):
            result = runner.invoke(main, ["ping", "-c", "3"])
        assert result.exit_code == 0
        assert client.health.call_count == 3
        assert "ms" in result.output
        assert "min" in result.output and "avg" in result.output and "max" in result.output

    def test_remote_server_default_count(self) -> None:
        runner = CliRunner()
        client = self._remote_client()
        with patch("ilan.cli.Client", return_value=client):
            result = runner.invoke(main, ["ping"])
        assert result.exit_code == 0
        assert client.health.call_count == 5

    def test_remote_server_all_fail_exits_nonzero(self) -> None:
        runner = CliRunner()
        client = self._remote_client()
        client.health.side_effect = ConnectionError("unreachable")
        with patch("ilan.cli.Client", return_value=client):
            result = runner.invoke(main, ["ping", "-c", "2"])
        assert result.exit_code != 0
        assert "all pings failed" in result.output.lower()

    def test_remote_server_partial_failures_still_summarize(self) -> None:
        runner = CliRunner()
        client = self._remote_client()
        client.health.side_effect = [
            {"status": "ok"},
            ConnectionError("transient"),
            {"status": "ok"},
        ]
        with patch("ilan.cli.Client", return_value=client):
            result = runner.invoke(main, ["ping", "-c", "3"])
        assert result.exit_code == 0
        assert "2/3 ok" in result.output

    def test_count_must_be_positive(self) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["ping", "-c", "0"])
        assert result.exit_code != 0
