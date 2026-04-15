"""Tests for ``ilan dashboard`` — full-screen real-time task dashboard."""

from __future__ import annotations

import io
import sys
from datetime import datetime, timezone
from unittest.mock import MagicMock, call, patch
from zoneinfo import ZoneInfo

import pytest
from click.testing import CliRunner
from rich.console import Console
from rich.text import Text

from ilan.cli import ALIAS_STYLE, _build_dashboard_table, main
from ilan.models import STYLE_FOR_STATUS, TaskStatus


# ── helpers ──────────────────────────────────────────────────────────


def _make_client(**overrides) -> MagicMock:
    """Build a mock Client with sensible defaults."""
    client = MagicMock()
    client.ensure_server.return_value = {}
    client.version_mismatch = None
    client.is_remote = False
    for k, v in overrides.items():
        setattr(client, k, v)
    return client


_TZ = ZoneInfo("US/Pacific")

_NOW_ISO = "2026-04-15T12:00:00+00:00"
_EARLIER_ISO = "2026-04-15T10:00:00+00:00"


def _task_row(
    name: str = "my-task",
    status: str = "WORKING",
    alias: str | None = None,
    cost_usd: float = 0.0,
    needs_review: bool = False,
    created_at: str = _EARLIER_ISO,
    status_changed_at: str = _NOW_ISO,
) -> dict:
    return {
        "name": name,
        "status": status,
        "alias": alias,
        "cost_usd": cost_usd,
        "needs_review": needs_review,
        "created_at": created_at,
        "status_changed_at": status_changed_at,
    }


def _render_table_text(rows: list[dict]) -> str:
    """Render a dashboard table to plain text for assertion."""
    table = _build_dashboard_table(rows, _TZ)
    buf = io.StringIO()
    console = Console(file=buf, width=120, force_terminal=True)
    console.print(table)
    return buf.getvalue()


# ── _build_dashboard_table unit tests ────────────────────────────────


class TestBuildDashboardTable:
    def test_empty_rows(self) -> None:
        text = _render_table_text([])
        assert "No active tasks" in text

    def test_header_contains_refresh_timestamp(self) -> None:
        text = _render_table_text([])
        assert "refreshed at" in text

    def test_header_contains_keybinding_hints(self) -> None:
        text = _render_table_text([])
        assert "q" in text
        assert "quit" in text
        assert "r" in text
        assert "refresh" in text

    def test_header_contains_title(self) -> None:
        text = _render_table_text([])
        assert "ilan dashboard" in text

    def test_single_task_displayed(self) -> None:
        text = _render_table_text([_task_row(name="build-api")])
        assert "build-api" in text
        assert "WORKING" in text

    def test_all_statuses_displayed(self) -> None:
        """Every TaskStatus should render with its value string."""
        for status in TaskStatus:
            text = _render_table_text([_task_row(status=status.value)])
            assert status.value in text

    def test_alias_displayed(self) -> None:
        text = _render_table_text([_task_row(alias="aa")])
        assert "(aa)" in text

    def test_no_alias_no_parens(self) -> None:
        text = _render_table_text([_task_row(alias=None)])
        assert "()" not in text

    def test_cost_formatted(self) -> None:
        text = _render_table_text([_task_row(cost_usd=1.50)])
        assert "$1.50" in text

    def test_zero_cost_shows_dash(self) -> None:
        text = _render_table_text([_task_row(cost_usd=0.0)])
        assert "-" in text

    def test_multiple_tasks(self) -> None:
        rows = [
            _task_row(name="task-a", status="WORKING"),
            _task_row(name="task-b", status="DONE"),
            _task_row(name="task-c", status="ERROR"),
        ]
        text = _render_table_text(rows)
        assert "task-a" in text
        assert "task-b" in text
        assert "task-c" in text

    def test_table_has_correct_columns(self) -> None:
        table = _build_dashboard_table([], _TZ)
        col_names = [c.header for c in table.columns]
        assert col_names == ["(Alias) Name", "Status", "Cost", "Created", "Last Changed"]


# ── needs_review / ⚠️ marker ────────────────────────────────────────


class TestNeedsReviewMarker:
    """Ensure the dashboard renders the ⚠️ marker identically to ``ilan ls``."""

    def test_needs_review_true_shows_warning(self) -> None:
        text = _render_table_text([_task_row(needs_review=True)])
        assert "\u26a0\ufe0f" in text

    def test_needs_review_false_no_warning(self) -> None:
        text = _render_table_text([_task_row(needs_review=False)])
        assert "\u26a0" not in text

    def test_needs_review_with_alias(self) -> None:
        """⚠️ should appear even when an alias is set."""
        text = _render_table_text([_task_row(alias="sd", needs_review=True)])
        assert "(sd)" in text
        assert "\u26a0\ufe0f" in text

    def test_name_cell_structure_matches_ls(self) -> None:
        """Verify the Rich Text object built for the name cell matches _do_ls."""
        row = _task_row(name="fix-bug", alias="jk", needs_review=True)
        table = _build_dashboard_table([row], _TZ)
        # The first column of the first data row is the name cell.
        name_cell = table.columns[0]._cells[0]
        assert isinstance(name_cell, Text)
        plain = name_cell.plain
        assert plain.startswith("(jk) ")
        assert "fix-bug" in plain
        assert "\u26a0\ufe0f" in plain

    def test_name_cell_without_review(self) -> None:
        """Without needs_review, no ⚠️ in the name cell."""
        row = _task_row(name="fix-bug", alias="jk", needs_review=False)
        table = _build_dashboard_table([row], _TZ)
        name_cell = table.columns[0]._cells[0]
        assert isinstance(name_cell, Text)
        assert "\u26a0" not in name_cell.plain

    def test_name_cell_styling_matches_ls(self) -> None:
        """Alias uses ALIAS_STYLE ('bold magenta'), name uses 'bold'."""
        row = _task_row(name="my-task", alias="ab", needs_review=False)
        table = _build_dashboard_table([row], _TZ)
        name_cell = table.columns[0]._cells[0]
        assert isinstance(name_cell, Text)
        # Check that the alias portion has the correct style.
        spans = name_cell._spans
        # First span should be the alias with ALIAS_STYLE.
        alias_span = spans[0]
        assert alias_span.style == ALIAS_STYLE
        # Second span should be the task name with 'bold'.
        name_span = spans[1]
        assert name_span.style == "bold"

    def test_status_styling_applied(self) -> None:
        """Each status should get the correct Rich style from STYLE_FOR_STATUS."""
        for status in TaskStatus:
            expected_style = STYLE_FOR_STATUS.get(status, "")
            row = _task_row(status=status.value)
            table = _build_dashboard_table([row], _TZ)
            status_cell = table.columns[1]._cells[0]
            assert isinstance(status_cell, Text)
            assert status_cell.plain == status.value
            assert str(status_cell.style) == expected_style


# ── bell / status change detection ───────────────────────────────────


class TestDashboardBell:
    """Test that the terminal bell fires on status transitions."""

    def _run_dashboard_iterations(
        self,
        responses: list[dict],
        *,
        keypress_after: int | None = None,
        key: str = "q",
    ) -> list[str]:
        """Simulate dashboard iterations and capture stdout writes.

        *responses* is a list of server responses for successive ``list_tasks``
        calls.  After *keypress_after* iterations (0-indexed), the given *key*
        is injected; otherwise ``q`` is sent after all responses are consumed.

        Returns a list of strings written to stdout (look for ``\\a``).
        """
        client = _make_client()
        call_count = 0

        def mock_list_tasks(show_all=False):
            nonlocal call_count
            idx = min(call_count, len(responses) - 1)
            call_count += 1
            return responses[idx]

        client.list_tasks.side_effect = mock_list_tasks

        stdout_writes: list[str] = []
        original_write = sys.stdout.write

        iteration = 0
        quit_after = keypress_after if keypress_after is not None else len(responses) - 1

        # We mock the terminal/select machinery so the loop runs purely in
        # Python, without needing an actual tty.
        def fake_select(rlist, wlist, xlist, timeout):
            nonlocal iteration
            iteration += 1
            if iteration > quit_after:
                return ([rlist[0]], [], [])  # simulate keypress ready
            return ([], [], [])  # no keypress

        fake_stdin = MagicMock()
        fake_stdin.fileno.return_value = 0
        fake_stdin.read.return_value = key

        with (
            patch("ilan.cli._client", return_value=client),
            patch("ilan.cli.cfg") as mock_cfg,
            patch("ilan.cli.termios") as mock_termios,
            patch("ilan.cli.tty"),
            patch("ilan.cli.select") as mock_select,
            patch("ilan.cli.sys") as mock_sys,
            patch("ilan.cli.time") as mock_time,
            patch("ilan.cli.Live"),
        ):
            mock_cfg.load.return_value = {"time-zone": "US/Pacific"}
            mock_termios.tcgetattr.return_value = []
            mock_select.select.side_effect = fake_select
            mock_sys.stdin = fake_stdin
            mock_sys.stdout = MagicMock()
            mock_sys.stdout.write.side_effect = lambda s: stdout_writes.append(s)
            # Make time.monotonic advance so auto-refresh triggers.
            monotonic_counter = [0.0]

            def advancing_monotonic():
                monotonic_counter[0] += 3.0  # always past the 2s threshold
                return monotonic_counter[0]

            mock_time.monotonic.side_effect = advancing_monotonic

            from ilan.cli import _do_dashboard

            _do_dashboard()

        return stdout_writes

    def test_no_bell_on_first_render(self) -> None:
        """First poll should never ring the bell (no prior state)."""
        resp = {"tasks": [_task_row(name="t1", status="WORKING")]}
        writes = self._run_dashboard_iterations([resp], keypress_after=0)
        assert "\a" not in writes

    def test_bell_on_status_change(self) -> None:
        """Bell should ring when a task's status changes between polls."""
        resp1 = {"tasks": [_task_row(name="t1", status="WORKING")]}
        resp2 = {"tasks": [_task_row(name="t1", status="AGENT_FINISHED")]}
        writes = self._run_dashboard_iterations([resp1, resp2], keypress_after=1)
        assert "\a" in writes

    def test_bell_on_new_task(self) -> None:
        """Bell should ring when a new task appears."""
        resp1 = {"tasks": [_task_row(name="t1", status="WORKING")]}
        resp2 = {"tasks": [
            _task_row(name="t1", status="WORKING"),
            _task_row(name="t2", status="UNCLAIMED"),
        ]}
        writes = self._run_dashboard_iterations([resp1, resp2], keypress_after=1)
        assert "\a" in writes

    def test_no_bell_when_no_change(self) -> None:
        """No bell when nothing changes between polls."""
        resp = {"tasks": [_task_row(name="t1", status="WORKING")]}
        writes = self._run_dashboard_iterations([resp, resp], keypress_after=1)
        assert "\a" not in writes

    def test_bell_on_task_disappearing_and_status_change(self) -> None:
        """Bell on status change even if another task disappeared."""
        resp1 = {"tasks": [
            _task_row(name="t1", status="WORKING"),
            _task_row(name="t2", status="UNCLAIMED"),
        ]}
        resp2 = {"tasks": [
            _task_row(name="t1", status="DONE"),
        ]}
        writes = self._run_dashboard_iterations([resp1, resp2], keypress_after=1)
        assert "\a" in writes


# ── CLI command registration ─────────────────────────────────────────


class TestDashboardCommand:
    def test_dashboard_help(self) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["dashboard", "--help"])
        assert result.exit_code == 0
        assert "Full-screen" in result.output

    def test_dashboard_registered_in_main_group(self) -> None:
        assert "dashboard" in main.commands

    def test_dashboard_connection_error(self, tmp_config) -> None:
        """Dashboard should exit gracefully when the server is unreachable."""
        runner = CliRunner()
        client = _make_client()
        client.ensure_server.side_effect = RuntimeError("Cannot reach server")
        with patch("ilan.cli.Client", return_value=client):
            result = runner.invoke(main, ["dashboard"])
        assert result.exit_code != 0
        assert "Cannot reach server" in result.output


# ── timezone handling ────────────────────────────────────────────────


class TestDashboardTimezone:
    def test_default_timezone_pacific(self) -> None:
        """Header timestamp should include Pacific timezone by default."""
        table = _build_dashboard_table([], ZoneInfo("US/Pacific"))
        assert isinstance(table.title, Text)
        # The title should contain a timezone abbreviation.
        plain = table.title.plain
        assert "refreshed at" in plain
        # Should contain PDT or PST depending on time of year.
        assert "PT" in plain or "PDT" in plain or "PST" in plain

    def test_custom_timezone(self) -> None:
        """Header should reflect a custom timezone."""
        table = _build_dashboard_table([], ZoneInfo("Europe/London"))
        assert isinstance(table.title, Text)
        plain = table.title.plain
        assert "refreshed at" in plain
        # Should contain BST or GMT depending on time of year.
        assert "BST" in plain or "GMT" in plain


# ── table expand property ────────────────────────────────────────────


class TestDashboardTableProperties:
    def test_table_expands_to_full_width(self) -> None:
        """Dashboard table should expand=True for full-screen display."""
        table = _build_dashboard_table([], _TZ)
        assert table.expand is True
