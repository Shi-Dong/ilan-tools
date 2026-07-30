"""Tests for the ``-t/--every`` reply flag and the reply-every cycle UX."""

from __future__ import annotations

from unittest.mock import MagicMock, call, patch

import pytest
from click.testing import CliRunner

from ilan.cli import (
    REPLY_EVERY_BG,
    REPLY_EVERY_STYLE,
    SLEEP_STYLE,
    _build_name_cell,
    _format_reply_every_suffix,
    main,
)


@pytest.fixture()
def runner() -> CliRunner:
    return CliRunner()


def _make_client() -> MagicMock:
    client = MagicMock()
    client.ensure_server.return_value = {}
    client.version_mismatch = None
    client.is_remote = False
    client.reply.return_value = {
        "ok": True,
        "name": "my-task",
        "message": "Reply sent to my-task. Agent resumed.",
    }
    client.sleep_task.return_value = {"ok": True, "name": "my-task"}
    return client


def _challenge(seconds: int = 3600) -> dict:
    return {
        "confirm_reply_every": True,
        "name": "my-task",
        "reply_every_seconds": seconds,
    }


# ── -t flag parsing ──────────────────────────────────────────────────


class TestEveryFlag:
    @pytest.mark.parametrize("base", [["task", "reply"], ["reply"], ["re"]])
    def test_every_passed_to_client(
        self, runner: CliRunner, tmp_config, base: list[str]
    ) -> None:
        client = _make_client()
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, [*base, "my-task", "go on", "-t", "1h"])
        assert result.exit_code == 0, result.output
        client.reply.assert_called_once_with("my-task", "go on", every_seconds=3600)

    @pytest.mark.parametrize(
        ("arg", "expected_seconds"),
        [
            ("1200", 1200),
            ("1200s", 1200),
            ("20m", 1200),
            ("0.5h", 1800),
            ("2h", 7200),
            ("1.5h", 5400),
        ],
    )
    def test_every_accepts_sleep_durations(
        self, runner: CliRunner, tmp_config, arg: str, expected_seconds: int
    ) -> None:
        client = _make_client()
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["re", "my-task", "go on", "--every", arg])
        assert result.exit_code == 0, result.output
        client.reply.assert_called_once_with(
            "my-task", "go on", every_seconds=expected_seconds
        )

    def test_every_prints_cycle_note(self, runner: CliRunner, tmp_config) -> None:
        client = _make_client()
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["re", "my-task", "go on", "-t", "1h"])
        assert result.exit_code == 0
        assert "every 1h" in result.output

    def test_every_without_message_rejected(
        self, runner: CliRunner, tmp_config
    ) -> None:
        client = _make_client()
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["re", "my-task", "-t", "1h"])
        assert result.exit_code == 1
        assert "requires a response message" in result.output
        client.reply.assert_not_called()

    @pytest.mark.parametrize("arg", ["0", "abc", "5d", "5 m", "-5m"])
    def test_every_rejects_bad_duration(
        self, runner: CliRunner, tmp_config, arg: str
    ) -> None:
        client = _make_client()
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["re", "my-task", "go on", "-t", arg])
        assert result.exit_code != 0
        client.reply.assert_not_called()

    @pytest.mark.parametrize("arg", ["1199", "19m", "5m", "300s", "0.3h"])
    def test_every_rejects_too_short_duration(
        self, runner: CliRunner, tmp_config, arg: str
    ) -> None:
        client = _make_client()
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["re", "my-task", "go on", "-t", arg])
        assert result.exit_code == 1
        assert "must be at least 20m" in result.output
        client.reply.assert_not_called()

    def test_plain_reply_stays_positional(
        self, runner: CliRunner, tmp_config
    ) -> None:
        """Without -t the client call keeps its historical two-arg shape."""
        client = _make_client()
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["re", "my-task", "go on"])
        assert result.exit_code == 0
        client.reply.assert_called_once_with("my-task", "go on")
        assert "every" not in result.output


# ── [y/n] confirmation when a human reply ends a cycle ───────────────


class TestReplyEveryConfirmation:
    def test_reply_confirm_yes_retries_with_override(
        self, runner: CliRunner, tmp_config
    ) -> None:
        client = _make_client()
        success = {"ok": True, "name": "my-task",
                   "message": "Reply sent to my-task. Agent resumed."}
        client.reply.side_effect = [_challenge(), success]
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["re", "my-task", "go on"], input="y\n")
        assert result.exit_code == 0, result.output
        assert "every 1h" in result.output
        assert client.reply.call_args_list == [
            call("my-task", "go on"),
            call("my-task", "go on", override_reply_every=True),
        ]

    def test_reply_confirm_no_aborts(self, runner: CliRunner, tmp_config) -> None:
        client = _make_client()
        client.reply.side_effect = [_challenge()]
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["re", "my-task", "go on"], input="n\n")
        assert result.exit_code == 1
        assert "Not sent." in result.output
        client.reply.assert_called_once_with("my-task", "go on")

    def test_new_every_confirm_yes_replaces_cycle(
        self, runner: CliRunner, tmp_config
    ) -> None:
        client = _make_client()
        success = {"ok": True, "name": "my-task",
                   "message": "Reply sent to my-task. Agent resumed."}
        client.reply.side_effect = [_challenge(3600), success]
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(
                main, ["re", "my-task", "go on", "-t", "30m"], input="y\n"
            )
        assert result.exit_code == 0, result.output
        assert client.reply.call_args_list == [
            call("my-task", "go on", every_seconds=1800),
            call("my-task", "go on", override_reply_every=True, every_seconds=1800),
        ]

    def test_tap_confirm_no_aborts(self, runner: CliRunner, tmp_config) -> None:
        client = _make_client()
        client.get_task.return_value = {
            "task": {"name": "my-task", "status": "WORKING"}
        }
        client.reply.side_effect = [_challenge()]
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["tap", "my-task"], input="n\n")
        assert result.exit_code == 1
        assert "Not sent." in result.output
        client.reply.assert_called_once()

    def test_cancel_confirm_yes_retries_with_override(
        self, runner: CliRunner, tmp_config
    ) -> None:
        client = _make_client()
        client.get_task.return_value = {
            "task": {"name": "my-task", "status": "WORKING"}
        }
        success = {"ok": True, "message": "Interrupted agent and resumed with reply."}
        client.reply.side_effect = [_challenge(), success]
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["cancel", "my-task"], input="y\n")
        assert result.exit_code == 0, result.output
        assert client.reply.call_count == 2
        assert client.reply.call_args.kwargs == {"override_reply_every": True}

    def test_sleep_confirm_yes_retries_with_override(
        self, runner: CliRunner, tmp_config
    ) -> None:
        client = _make_client()
        client.sleep_task.side_effect = [_challenge(), {"ok": True, "name": "my-task"}]
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["sleep", "my-task", "5m"], input="y\n")
        assert result.exit_code == 0, result.output
        assert client.sleep_task.call_args_list == [
            call("my-task", 300),
            call("my-task", 300, override_reply_every=True),
        ]

    def test_sleep_confirm_no_aborts(self, runner: CliRunner, tmp_config) -> None:
        client = _make_client()
        client.sleep_task.side_effect = [_challenge()]
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["sleep", "my-task", "5m"], input="n\n")
        assert result.exit_code == 1
        assert "Not sent." in result.output
        client.sleep_task.assert_called_once_with("my-task", 300)


# ── suffix rendering in ls / dashboard ───────────────────────────────


class TestFormatReplyEverySuffix:
    def test_none_returns_none(self) -> None:
        assert _format_reply_every_suffix(None) is None

    def test_zero_returns_none(self) -> None:
        assert _format_reply_every_suffix(0) is None

    @pytest.mark.parametrize(
        ("seconds", "expected"),
        [
            (300, " (responding every 5m)"),
            (1800, " (responding every 0.5h)"),
            (3600, " (responding every 1h)"),
            (5400, " (responding every 1.5h)"),
        ],
    )
    def test_positive_durations(self, seconds: int, expected: str) -> None:
        assert _format_reply_every_suffix(seconds) == expected


class TestNameCellReplyEvery:
    def _row(self, **overrides) -> dict:
        row = {
            "name": "alpha",
            "alias": "",
            "status": "WORKING",
            "needs_review": False,
            "model": None,
        }
        row.update(overrides)
        return row

    def test_suffix_shown_in_reply_every_style(self) -> None:
        cell = _build_name_cell(self._row(reply_every_seconds=3600))
        assert "(responding every 1h)" in cell.plain
        assert any(span.style == REPLY_EVERY_STYLE for span in cell.spans)

    def test_suffix_shown_regardless_of_status(self) -> None:
        cell = _build_name_cell(
            self._row(status="NEEDS_ATTENTION", reply_every_seconds=300)
        )
        assert "(responding every 5m)" in cell.plain

    def test_no_suffix_without_cycle(self) -> None:
        cell = _build_name_cell(self._row())
        assert "responding every" not in cell.plain

    def test_background_covers_alias_and_name(self) -> None:
        cell = _build_name_cell(self._row(alias="aa", reply_every_seconds=3600))
        bg_spans = [s for s in cell.spans if s.style == f"on {REPLY_EVERY_BG}"]
        assert len(bg_spans) == 1
        assert bg_spans[0].start == 0
        assert bg_spans[0].end == len(cell.plain)
        assert cell.plain.startswith("(aa) alpha")

    def test_no_background_without_cycle(self) -> None:
        cell = _build_name_cell(self._row(alias="aa"))
        assert not any(
            span.style == f"on {REPLY_EVERY_BG}" for span in cell.spans
        )

    def test_style_differs_from_sleep_suffix(self) -> None:
        cell = _build_name_cell(
            self._row(sleep_seconds=300, reply_every_seconds=3600)
        )
        assert "(sleeping for 5m)" in cell.plain
        assert "(responding every 1h)" in cell.plain
        styles = {span.style for span in cell.spans}
        assert {SLEEP_STYLE, REPLY_EVERY_STYLE} <= styles
        assert SLEEP_STYLE != REPLY_EVERY_STYLE
