"""Tests for the ``ilan task tap`` / ``ilan tap`` CLI command."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from click.testing import CliRunner

from ilan.cli import main, TAP_MESSAGE
from ilan.models import TaskStatus


@pytest.fixture()
def runner() -> CliRunner:
    return CliRunner()


def _mock_client(**overrides):
    """Return a mock Client whose methods can be overridden per test."""
    defaults = {
        "ensure_server": lambda: None,
        "version_mismatch": None,
    }
    defaults.update(overrides)

    class _FakeClient:
        version_mismatch = defaults["version_mismatch"]

        def ensure_server(self):
            return defaults["ensure_server"]()

        def get_task(self, name: str) -> dict:
            return defaults["get_task"](name)

        def reply(self, name: str, message: str) -> dict:
            return defaults["reply"](name, message)

    return _FakeClient()


# ── ilan task tap ──────────────────────────────────────────────────────


class TestTaskTap:
    """Tests for ``ilan task tap NAME``."""

    def test_tap_working_sends_reply(self, runner: CliRunner) -> None:
        """Tap on a WORKING task should send the fixed prompt via reply."""
        reply_calls: list[tuple[str, str]] = []

        def fake_get_task(name: str) -> dict:
            return {"task": {"name": name, "status": TaskStatus.WORKING.value}}

        def fake_reply(name: str, message: str) -> dict:
            reply_calls.append((name, message))
            return {"ok": True, "message": "Interrupted agent and resumed with reply."}

        client = _mock_client(get_task=fake_get_task, reply=fake_reply)

        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["task", "tap", "my-task"])

        assert result.exit_code == 0
        assert len(reply_calls) == 1
        assert reply_calls[0] == ("my-task", TAP_MESSAGE)

    def test_tap_unclaimed_warns(self, runner: CliRunner) -> None:
        """Tap on an UNCLAIMED task should print a warning and not reply."""
        def fake_get_task(name: str) -> dict:
            return {"task": {"name": name, "status": TaskStatus.UNCLAIMED.value}}

        client = _mock_client(
            get_task=fake_get_task,
            reply=lambda n, m: pytest.fail("reply should not be called"),
        )

        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["task", "tap", "idle-task"])

        assert result.exit_code == 0
        assert "UNCLAIMED" in result.output
        assert "not WORKING" in result.output

    def test_tap_needs_attention_warns(self, runner: CliRunner) -> None:
        """Tap on a NEEDS_ATTENTION task should warn."""
        def fake_get_task(name: str) -> dict:
            return {"task": {"name": name, "status": TaskStatus.NEEDS_ATTENTION.value}}

        client = _mock_client(
            get_task=fake_get_task,
            reply=lambda n, m: pytest.fail("reply should not be called"),
        )

        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["task", "tap", "blocked-task"])

        assert result.exit_code == 0
        assert "NEEDS_ATTENTION" in result.output
        assert "not WORKING" in result.output

    def test_tap_agent_finished_warns(self, runner: CliRunner) -> None:
        """Tap on an AGENT_FINISHED task should warn."""
        def fake_get_task(name: str) -> dict:
            return {"task": {"name": name, "status": TaskStatus.AGENT_FINISHED.value}}

        client = _mock_client(
            get_task=fake_get_task,
            reply=lambda n, m: pytest.fail("reply should not be called"),
        )

        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["task", "tap", "finished-task"])

        assert result.exit_code == 0
        assert "AGENT_FINISHED" in result.output

    def test_tap_done_warns(self, runner: CliRunner) -> None:
        """Tap on a DONE task should warn."""
        def fake_get_task(name: str) -> dict:
            return {"task": {"name": name, "status": TaskStatus.DONE.value}}

        client = _mock_client(
            get_task=fake_get_task,
            reply=lambda n, m: pytest.fail("reply should not be called"),
        )

        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["task", "tap", "done-task"])

        assert result.exit_code == 0
        assert "DONE" in result.output
        assert "not WORKING" in result.output

    def test_tap_discarded_warns(self, runner: CliRunner) -> None:
        """Tap on a DISCARDED task should warn."""
        def fake_get_task(name: str) -> dict:
            return {"task": {"name": name, "status": TaskStatus.DISCARDED.value}}

        client = _mock_client(
            get_task=fake_get_task,
            reply=lambda n, m: pytest.fail("reply should not be called"),
        )

        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["task", "tap", "disc-task"])

        assert result.exit_code == 0
        assert "DISCARDED" in result.output

    def test_tap_error_warns(self, runner: CliRunner) -> None:
        """Tap on an ERROR task should warn."""
        def fake_get_task(name: str) -> dict:
            return {"task": {"name": name, "status": TaskStatus.ERROR.value}}

        client = _mock_client(
            get_task=fake_get_task,
            reply=lambda n, m: pytest.fail("reply should not be called"),
        )

        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["task", "tap", "err-task"])

        assert result.exit_code == 0
        assert "ERROR" in result.output

    def test_tap_nonexistent_task(self, runner: CliRunner) -> None:
        """Tap on a task that doesn't exist should show an error."""
        def fake_get_task(name: str) -> dict:
            return {"error": f"Task '{name}' not found"}

        client = _mock_client(
            get_task=fake_get_task,
            reply=lambda n, m: pytest.fail("reply should not be called"),
        )

        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["task", "tap", "ghost"])

        assert result.exit_code == 1

    def test_tap_no_name_arg(self, runner: CliRunner) -> None:
        """Invoking tap without a task name should fail."""
        result = runner.invoke(main, ["task", "tap"])
        assert result.exit_code != 0


# ── ilan tap (shorthand) ──────────────────────────────────────────────


class TestTapShorthand:
    """Tests for the top-level ``ilan tap NAME`` shorthand."""

    def test_shorthand_tap_working(self, runner: CliRunner) -> None:
        """Top-level 'ilan tap' should behave identically to 'ilan task tap'."""
        reply_calls: list[tuple[str, str]] = []

        def fake_get_task(name: str) -> dict:
            return {"task": {"name": name, "status": TaskStatus.WORKING.value}}

        def fake_reply(name: str, message: str) -> dict:
            reply_calls.append((name, message))
            return {"ok": True, "message": "Interrupted agent and resumed with reply."}

        client = _mock_client(get_task=fake_get_task, reply=fake_reply)

        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["tap", "my-task"])

        assert result.exit_code == 0
        assert len(reply_calls) == 1
        assert reply_calls[0] == ("my-task", TAP_MESSAGE)

    def test_shorthand_tap_not_working_warns(self, runner: CliRunner) -> None:
        """Top-level 'ilan tap' should also warn for non-WORKING tasks."""
        def fake_get_task(name: str) -> dict:
            return {"task": {"name": name, "status": TaskStatus.NEEDS_ATTENTION.value}}

        client = _mock_client(
            get_task=fake_get_task,
            reply=lambda n, m: pytest.fail("reply should not be called"),
        )

        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["tap", "blocked-task"])

        assert result.exit_code == 0
        assert "not WORKING" in result.output
