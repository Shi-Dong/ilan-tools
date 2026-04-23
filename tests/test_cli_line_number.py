"""Tests for line-number mode: numbered tail output + ``@N`` expansion on reply."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

import ilan.config as cfg
from ilan.cli import _expand_at_refs, main


@pytest.fixture()
def runner() -> CliRunner:
    return CliRunner()


def _make_client() -> MagicMock:
    client = MagicMock()
    client.ensure_server.return_value = {}
    client.version_mismatch = None
    client.is_remote = False
    return client


def _enable_line_number(tmp_config: Path) -> None:
    tmp_config.parent.mkdir(parents=True, exist_ok=True)
    with open(tmp_config, "w") as f:
        json.dump({"line-number": True}, f)


# ── tail rendering ───────────────────────────────────────────────────


class TestTailLineNumbers:
    def _logs(self) -> dict:
        return {
            "logs": [
                {"role": "assistant", "content": "hello\nworld",
                 "timestamp": "2026-04-13T00:00:00+00:00"},
                {"role": "user", "content": "thanks",
                 "timestamp": "2026-04-13T00:00:01+00:00"},
                {"role": "assistant", "content": "second msg\nsecond line",
                 "timestamp": "2026-04-13T00:00:02+00:00"},
            ],
        }

    def test_off_by_default_no_numbers(self, runner: CliRunner, tmp_config: Path) -> None:
        client = _make_client()
        client.get_logs.return_value = self._logs()
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["tail", "my-task", "-n", "3"])
        assert result.exit_code == 0
        assert "[1]" not in result.output
        assert "[2]" not in result.output

    def test_on_numbers_assistant_lines_only(self, runner: CliRunner, tmp_config: Path) -> None:
        _enable_line_number(tmp_config)
        client = _make_client()
        client.get_logs.return_value = self._logs()
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["tail", "my-task", "-n", "3"])
        assert result.exit_code == 0
        # Continuous numbering across the two assistant messages.
        assert "[1]" in result.output
        assert "[2]" in result.output
        assert "[3]" in result.output
        assert "[4]" in result.output
        assert "hello" in result.output
        assert "world" in result.output
        assert "second msg" in result.output
        # User lines must not be numbered.
        thanks_line = [l for l in result.output.splitlines() if "thanks" in l][0]
        assert "[" not in thanks_line or "[bold green]" in thanks_line  # only the User label bracket

    def test_on_caches_lines_for_reply(self, runner: CliRunner, tmp_config: Path) -> None:
        _enable_line_number(tmp_config)
        client = _make_client()
        client.get_logs.return_value = self._logs()
        with patch("ilan.cli._client", return_value=client):
            runner.invoke(main, ["tail", "my-task", "-n", "3"])
        cached = cfg.load_last_tail("my-task")
        assert cached == ["hello", "world", "second msg", "second line"]

    def test_on_with_no_n_uses_tail_endpoint(self, runner: CliRunner, tmp_config: Path) -> None:
        _enable_line_number(tmp_config)
        client = _make_client()
        client.get_tail.return_value = {
            "entries": [
                {"role": "assistant", "content": "only\nmsg",
                 "timestamp": "2026-04-13T00:00:00+00:00"},
            ],
        }
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["tail", "my-task"])
        assert result.exit_code == 0
        assert "[1]" in result.output
        assert "[2]" in result.output
        assert cfg.load_last_tail("my-task") == ["only", "msg"]


# ── @N expansion ─────────────────────────────────────────────────────


class TestExpandAtRefs:
    def test_replaces_in_range(self) -> None:
        lines = ["first", "second", "third"]
        out = _expand_at_refs("see @2 please", lines)
        assert out == 'see "second" please'

    def test_multiple_refs(self) -> None:
        lines = ["a", "b", "c"]
        out = _expand_at_refs("@1 and @3", lines)
        assert out == '"a" and "c"'

    def test_out_of_range_left_alone(self) -> None:
        lines = ["only"]
        out = _expand_at_refs("@5 doesn't exist", lines)
        assert out == "@5 doesn't exist"

    def test_no_cache_passes_through(self) -> None:
        assert _expand_at_refs("@1 x", []) == "@1 x"

    def test_ignores_email_like_patterns(self) -> None:
        lines = ["the-line"]
        out = _expand_at_refs("reach user@1 via dm", lines)
        # The `@1` here is attached to an identifier on the left, so no replacement.
        assert out == "reach user@1 via dm"

    def test_no_digits_after_at_not_replaced(self) -> None:
        lines = ["x"]
        assert _expand_at_refs("just @word not a ref", lines) == "just @word not a ref"


class TestReplyExpansion:
    def test_off_no_expansion(self, runner: CliRunner, tmp_config: Path) -> None:
        cfg.save_last_tail("my-task", ["cached-line"])
        client = _make_client()
        client.reply.return_value = {"message": "ok"}
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["reply", "my-task", "see @1"])
        assert result.exit_code == 0
        client.reply.assert_called_once_with("my-task", "see @1")

    def test_on_expands_refs(self, runner: CliRunner, tmp_config: Path) -> None:
        _enable_line_number(tmp_config)
        cfg.save_last_tail("my-task", ["first line", "second line"])
        client = _make_client()
        client.reply.return_value = {"message": "ok"}
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["reply", "my-task", "clarify @2"])
        assert result.exit_code == 0
        client.reply.assert_called_once_with("my-task", 'clarify "second line"')

    def test_on_but_no_cache_passthrough(self, runner: CliRunner, tmp_config: Path) -> None:
        _enable_line_number(tmp_config)
        client = _make_client()
        client.reply.return_value = {"message": "ok"}
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["reply", "never-tailed", "hi @3"])
        assert result.exit_code == 0
        client.reply.assert_called_once_with("never-tailed", "hi @3")

    def test_re_shortcut_also_expands(self, runner: CliRunner, tmp_config: Path) -> None:
        _enable_line_number(tmp_config)
        cfg.save_last_tail("t", ["foo"])
        client = _make_client()
        client.reply.return_value = {"message": "ok"}
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["re", "t", "re: @1"])
        assert result.exit_code == 0
        client.reply.assert_called_once_with("t", 're: "foo"')


# ── config set for line-number ───────────────────────────────────────


class TestSetConfigLineNumber:
    def test_bool_config_roundtrips(self, tmp_config: Path) -> None:
        cfg.save({**cfg.DEFAULTS, "line-number": cfg.parse_bool("true")})
        assert cfg.load()["line-number"] is True
        cfg.save({**cfg.DEFAULTS, "line-number": cfg.parse_bool("false")})
        assert cfg.load()["line-number"] is False
