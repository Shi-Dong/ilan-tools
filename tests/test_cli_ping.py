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
            result = runner.invoke(main, ["ping", "-c", "4"])
        assert result.exit_code == 0
        assert client.health.call_count == 4
        assert "Average of 4 pings" in result.output
        assert "ms" in result.output

    def test_remote_server_default_count_is_three(self) -> None:
        runner = CliRunner()
        client = self._remote_client()
        with patch("ilan.cli.Client", return_value=client):
            result = runner.invoke(main, ["ping"])
        assert result.exit_code == 0
        assert client.health.call_count == 3
        assert "Average of 3 pings" in result.output

    def test_remote_server_prints_only_average_line(self) -> None:
        runner = CliRunner()
        client = self._remote_client()
        with patch("ilan.cli.Client", return_value=client):
            result = runner.invoke(main, ["ping"])
        assert result.exit_code == 0
        non_empty = [line for line in result.output.splitlines() if line.strip()]
        assert len(non_empty) == 1
        assert non_empty[0].startswith("Average of ")

    def test_average_is_rounded_integer(self) -> None:
        """The reported avg must be a bare integer (no decimal point)."""
        runner = CliRunner()
        client = self._remote_client()
        # perf_counter is called twice per ping (start + end); fabricate
        # deltas so the three pings come out to 12.4, 13.6, 14.0 ms → avg
        # 13.33 → rounded 13.
        ticks = iter([
            0.0, 0.0124,
            1.0, 1.0136,
            2.0, 2.0140,
        ])
        with (
            patch("ilan.cli.Client", return_value=client),
            patch("ilan.cli.time.perf_counter", side_effect=lambda: next(ticks)),
        ):
            result = runner.invoke(main, ["ping"])
        assert result.exit_code == 0
        assert "13 ms" in result.output
        assert "13.3" not in result.output

    def test_remote_server_all_fail_exits_nonzero(self) -> None:
        runner = CliRunner()
        client = self._remote_client()
        client.health.side_effect = ConnectionError("unreachable")
        with patch("ilan.cli.Client", return_value=client):
            result = runner.invoke(main, ["ping", "-c", "2"])
        assert result.exit_code != 0
        assert "failed" in result.output.lower()

    def test_remote_server_partial_failures_use_only_successes(self) -> None:
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
        assert "Average of 2 pings" in result.output

    def test_count_must_be_positive(self) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["ping", "-c", "0"])
        assert result.exit_code != 0
