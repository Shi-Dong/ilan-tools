"""Tests for ``ilan dashboard`` — full-screen real-time task dashboard."""

from __future__ import annotations

import io
from unittest.mock import MagicMock, patch
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
    """Ensure the dashboard renders the review marker correctly.

    The dashboard uses an ASCII ``!`` instead of the ⚠️ emoji to avoid
    terminal-width misalignment in Rich's Live display.
    """

    def test_needs_review_true_shows_double_bang(self) -> None:
        text = _render_table_text([_task_row(needs_review=True)])
        assert "!!" in text

    def test_needs_review_false_no_bang(self) -> None:
        row = _task_row(name="clean-task", needs_review=False)
        table = _build_dashboard_table([row], _TZ)
        name_cell = table.columns[0]._cells[0]
        assert isinstance(name_cell, Text)
        assert "!" not in name_cell.plain

    def test_needs_review_with_alias(self) -> None:
        """Review marker should appear even when an alias is set."""
        text = _render_table_text([_task_row(alias="sd", needs_review=True)])
        assert "(sd)" in text
        assert "!!" in text

    def test_name_cell_structure(self) -> None:
        """Verify the Rich Text object: alias + name + review marker."""
        row = _task_row(name="fix-bug", alias="jk", needs_review=True)
        table = _build_dashboard_table([row], _TZ)
        name_cell = table.columns[0]._cells[0]
        assert isinstance(name_cell, Text)
        plain = name_cell.plain
        assert plain.startswith("(jk) ")
        assert "fix-bug" in plain
        assert plain.endswith(" !!")

    def test_name_cell_without_review(self) -> None:
        """Without needs_review, no marker in the name cell."""
        row = _task_row(name="fix-bug", alias="jk", needs_review=False)
        table = _build_dashboard_table([row], _TZ)
        name_cell = table.columns[0]._cells[0]
        assert isinstance(name_cell, Text)
        assert "!" not in name_cell.plain

    def test_review_marker_styled_bold_yellow(self) -> None:
        """The ``!!`` marker should be styled bold yellow for visibility."""
        row = _task_row(name="my-task", needs_review=True)
        table = _build_dashboard_table([row], _TZ)
        name_cell = table.columns[0]._cells[0]
        assert isinstance(name_cell, Text)
        bang_idx = name_cell.plain.index("!!")
        spans = name_cell._spans
        bang_span = [s for s in spans if s.start <= bang_idx < s.end]
        assert bang_span, "No style span found for the '!!' marker"
        assert bang_span[0].style == "bold yellow"

    def test_name_cell_styling(self) -> None:
        """Alias uses ALIAS_STYLE ('bold magenta'), name uses 'bold'."""
        row = _task_row(name="my-task", alias="ab", needs_review=False)
        table = _build_dashboard_table([row], _TZ)
        name_cell = table.columns[0]._cells[0]
        assert isinstance(name_cell, Text)
        spans = name_cell._spans
        # First span should be the alias with ALIAS_STYLE.
        alias_span = spans[0]
        assert alias_span.style == ALIAS_STYLE
        # Second span should be the task name with 'bold'.
        name_span = spans[1]
        assert name_span.style == "bold"

    def test_marker_is_ascii_safe(self) -> None:
        """The review marker must be pure ASCII for predictable terminal width."""
        row = _task_row(name="my-task", needs_review=True)
        table = _build_dashboard_table([row], _TZ)
        name_cell = table.columns[0]._cells[0]
        assert isinstance(name_cell, Text)
        assert name_cell.plain.isascii()

    def test_status_styling_applied(self) -> None:
        """Each status should get the correct Rich style from STYLE_FOR_STATUS."""
        for status in TaskStatus:
            expected_style = STYLE_FOR_STATUS.get(status, "")
            row = _task_row(status=status.value)
            table = _build_dashboard_table([row], _TZ)
            status_cell = table.columns[1]._cells[0]
            assert isinstance(status_cell, Text)
            assert status_cell.plain.startswith(status.value)
            assert str(status_cell.style) == expected_style


class TestWorkingElapsed:
    """Test the elapsed-time annotation on WORKING tasks."""

    def test_working_shows_elapsed(self) -> None:
        row = _task_row(status="WORKING")
        table = _build_dashboard_table([row], _TZ)
        status_cell = table.columns[1]._cells[0]
        assert isinstance(status_cell, Text)
        assert status_cell.plain.startswith("WORKING (for ")
        assert status_cell.plain.endswith("s)")

    def test_non_working_no_elapsed(self) -> None:
        for status in TaskStatus:
            if status == TaskStatus.WORKING:
                continue
            row = _task_row(status=status.value)
            table = _build_dashboard_table([row], _TZ)
            status_cell = table.columns[1]._cells[0]
            assert isinstance(status_cell, Text)
            assert status_cell.plain == status.value

    def test_elapsed_format(self) -> None:
        """Elapsed time should be formatted as NNhNNmNNs."""
        import re
        row = _task_row(status="WORKING")
        table = _build_dashboard_table([row], _TZ)
        status_cell = table.columns[1]._cells[0]
        assert isinstance(status_cell, Text)
        match = re.search(r"\(for (\d+h\d{2}m\d{2}s)\)", status_cell.plain)
        assert match, f"Expected elapsed time pattern, got: {status_cell.plain}"

    def test_elapsed_styled_dim(self) -> None:
        """The elapsed-time portion should be styled dim."""
        row = _task_row(status="WORKING")
        table = _build_dashboard_table([row], _TZ)
        status_cell = table.columns[1]._cells[0]
        assert isinstance(status_cell, Text)
        # The main style is "bold cyan" for WORKING.
        # The appended "(for ...)" has its own "dim" style span.
        elapsed_start = status_cell.plain.index(" (for ")
        spans = status_cell._spans
        dim_spans = [s for s in spans if s.start <= elapsed_start < s.end or s.start >= elapsed_start]
        assert any(s.style == "dim" for s in dim_spans)


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
    def test_table_expands_with_fixed_column_ratios(self) -> None:
        """Dashboard table fills the terminal with fixed column ratios.

        The name column takes 14/40 of the width. Status gets an extra slot
        (ratio=8) for its "(for HHhMMmSSs)" suffix, Cost is narrow
        (ratio=4), and Created / Last Changed stay at ratio=7 each.
        """
        table = _build_dashboard_table([], _TZ)
        assert table.expand is True
        ratios = [c.ratio for c in table.columns]
        assert ratios == [14, 8, 4, 7, 7]
