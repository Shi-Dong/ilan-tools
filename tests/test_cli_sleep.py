"""Tests for ``ilan sleep`` / ``ilan task sleep`` and the sleep-suffix renderer."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from ilan.cli import (
    _parse_sleep_duration,
    main,
)
from ilan.time_format import (
    _format_progress_duration,
    _sleep_progress,
)


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
            result = runner.invoke(main, ["task", "sleep", "my-task", "5"])
        assert result.exit_code == 0
        client.sleep_task.assert_called_once_with("my-task", 5)

    def test_shorthand_sleep_calls_client(self, runner: CliRunner, tmp_config) -> None:
        client = _make_client()
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["sleep", "my-task", "5"])
        assert result.exit_code == 0
        client.sleep_task.assert_called_once_with("my-task", 5)

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
        client.sleep_task.return_value = {"error": "Task is WORKING. Sleep only works on tasks in: NEEDS_ATTENTION, AGENT_FINISHED."}
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["sleep", "my-task", "5"])
        assert result.exit_code == 1
        assert "NEEDS_ATTENTION" in result.output

    @pytest.mark.parametrize(
        ("arg", "expected_seconds"),
        [
            ("300s", 300),
            ("5m", 300),
            ("2h", 7200),
            ("90sec", 90),
            ("3MIN", 180),
            ("1Hour", 3600),
        ],
    )
    def test_sleep_accepts_unit_suffix(
        self,
        runner: CliRunner,
        tmp_config,
        arg: str,
        expected_seconds: int,
    ) -> None:
        client = _make_client()
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["sleep", "my-task", arg])
        assert result.exit_code == 0, result.output
        client.sleep_task.assert_called_once_with("my-task", expected_seconds)

    @pytest.mark.parametrize(
        "arg",
        ["5 m", "5 ", "m5", "5mx", "", "abc", "-5", "1.5.0h", ".", ".m"],
    )
    def test_sleep_rejects_bad_duration(
        self, runner: CliRunner, tmp_config, arg: str
    ) -> None:
        client = _make_client()
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["sleep", "my-task", arg])
        assert result.exit_code != 0
        client.sleep_task.assert_not_called()


class TestParseSleepDuration:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("0", 0),
            ("300", 300),
            ("300s", 300),
            ("300sec", 300),
            ("300second", 300),
            ("300seconds", 300),
            ("5m", 300),
            ("5min", 300),
            ("5mins", 300),
            ("5minute", 300),
            ("5minutes", 300),
            ("2h", 7200),
            ("2hr", 7200),
            ("2hrs", 7200),
            ("2hour", 7200),
            ("2hours", 7200),
            ("1S", 1),
            ("1Min", 60),
            ("1HR", 3600),
            ("1.5h", 5400),
            ("0.5m", 30),
            ("2.5hours", 9000),
            ("5.5", 6),
            ("1.5s", 2),
            ("5.m", 300),
            (".5m", 30),
            ("5.", 5),
            (".5", 0),  # 0.5s rounds to 0 (banker's rounding)
            ("5.h", 18000),
        ],
    )
    def test_valid(self, value: str, expected: int) -> None:
        assert _parse_sleep_duration(value) == expected

    @pytest.mark.parametrize(
        "value",
        [
            "",
            "5 m",
            " 5m",
            "5m ",
            "m5",
            "5mx",
            ".",
            ".m",
            "1..5m",
            "1.5.0h",
            "-5",
            "-5m",
            "-1.5h",
            "5day",
            "5d",
            "abc",
        ],
    )
    def test_invalid(self, value: str) -> None:
        with pytest.raises(ValueError):
            _parse_sleep_duration(value)


class TestSleepProgress:
    NOW = datetime(2026, 9, 14, 12, tzinfo=timezone.utc)

    def _progress(self, elapsed: int, total: int = 300) -> tuple[int, int] | None:
        started = (self.NOW - timedelta(seconds=elapsed)).isoformat()
        with patch("ilan.time_format.datetime", wraps=datetime) as clock:
            clock.now.return_value = self.NOW
            return _sleep_progress(started, total)

    def test_reports_elapsed_against_total(self) -> None:
        assert self._progress(123) == (123, 300)

    def test_clamps_at_the_requested_sleep(self) -> None:
        assert self._progress(600) == (300, 300)

    def test_future_start_clamps_to_zero(self) -> None:
        assert self._progress(-10) == (0, 300)

    @pytest.mark.parametrize(
        ("started_at", "seconds"),
        [(None, 300), ("not-a-date", 300), (NOW.isoformat(), None),
         (NOW.isoformat(), 0), (NOW.isoformat(), -1)],
    )
    def test_invalid_metadata_has_no_progress(
        self, started_at: str | None, seconds: int | None
    ) -> None:
        assert _sleep_progress(started_at, seconds) is None

    @pytest.mark.parametrize(
        ("seconds", "expected"),
        [
            (0, "0s"),
            (42, "42s"),
            (60, "1m"),
            (63, "1m03s"),
            (3599, "59m59s"),
            (3600, "1h00m"),
            (9480, "2h38m"),
        ],
    )
    def test_progress_duration_is_compact(self, seconds: int, expected: str) -> None:
        assert _format_progress_duration(seconds) == expected
