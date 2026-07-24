"""Tests for ``ilan sleep`` / ``ilan task sleep`` and the sleep-suffix renderer."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from ilan.cli import _format_sleep_suffix, _parse_sleep_duration, main


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


class TestFormatSleepSuffix:
    def test_none_returns_none(self) -> None:
        assert _format_sleep_suffix(None) is None

    def test_zero_returns_none(self) -> None:
        assert _format_sleep_suffix(0) is None

    def test_negative_returns_none(self) -> None:
        assert _format_sleep_suffix(-5) is None

    def test_positive_shows_fixed_string(self) -> None:
        assert _format_sleep_suffix(300) == " (sleeping for 5m)"

    @pytest.mark.parametrize(
        ("seconds", "expected"),
        [
            (42, " (sleeping for 0.7m)"),
            (60, " (sleeping for 1m)"),
            (300, " (sleeping for 5m)"),
            (630, " (sleeping for 10.5m)"),
            (1799, " (sleeping for 29.9m)"),
            (1800, " (sleeping for 0.5h)"),
            (3599, " (sleeping for 0.9h)"),
            (3600, " (sleeping for 1h)"),
            (4680, " (sleeping for 1.3h)"),
            (7199, " (sleeping for 1.9h)"),
            (7200, " (sleeping for 2h)"),
            (86400, " (sleeping for 24h)"),
        ],
    )
    def test_unit_switches_at_threshold(self, seconds: int, expected: str) -> None:
        assert _format_sleep_suffix(seconds) == expected

    @pytest.mark.parametrize("seconds", range(1, 6))
    def test_sub_tenth_sleeps_clamp_up(self, seconds: int) -> None:
        assert _format_sleep_suffix(seconds) == " (sleeping for 0.1m)"

    @pytest.mark.parametrize("seconds", range(6, 3 * 3600, 7))
    def test_never_rounds_up(self, seconds: int) -> None:
        suffix = _format_sleep_suffix(seconds)
        assert suffix is not None
        shown = float(suffix.removeprefix(" (sleeping for ").removesuffix(")")[:-1])
        unit_seconds = 3600 if seconds >= 1800 else 60
        assert shown <= seconds / unit_seconds

    @pytest.mark.parametrize("seconds", range(1, 3 * 3600, 7))
    def test_no_trailing_zero_decimal(self, seconds: int) -> None:
        suffix = _format_sleep_suffix(seconds)
        assert suffix is not None
        assert not suffix.removesuffix(")").endswith((".0m", ".0h"))
