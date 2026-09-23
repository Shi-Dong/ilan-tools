"""Tests for ``ilan dashboard`` — full-screen real-time task dashboard."""

from __future__ import annotations

import io
import re
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest
from click.testing import CliRunner
from rich.console import Console
from rich.table import Table
from rich.text import Span, Text

from ilan.cli import (
    _build_dashboard_table,
    _maybe_warn_one_liner_unconfigured,
    main,
)
from ilan.models import (
    DEFAULT_ENGINE,
    ENGINE_NAME_STYLE,
    STYLE_FOR_STATUS,
    TaskStatus,
)
from ilan.time_format import (
    _format_ts,
)
from ilan.task_display import (
    ALIAS_STYLE,
    REASONING_COLUMN_WIDTH,
    SLEEP_PROGRESS_EMPTY_STYLE,
    SLEEP_PROGRESS_STYLE,
    SLEEP_SUFFIX_STYLE,
    _build_concise_task_line,
    _build_name_cell,
    _build_reasoning_cell,
    _name_style,
)


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
    needs_review: bool = False,
    created_at: str = _EARLIER_ISO,
    status_changed_at: str = _NOW_ISO,
    summary_one_liner: str | None = None,
    sleep_seconds: int | None = None,
) -> dict:
    return {
        "name": name,
        "status": status,
        "alias": alias,
        "needs_review": needs_review,
        "created_at": created_at,
        "status_changed_at": status_changed_at,
        "summary_one_liner": summary_one_liner,
        "sleep_seconds": sleep_seconds,
    }


def _render_table_text(rows: list[dict]) -> str:
    """Render a dashboard table to plain text for assertion."""
    table = _build_dashboard_table(rows, _TZ)
    buf = io.StringIO()
    console = Console(file=buf, width=120, force_terminal=True)
    console.print(table)
    return buf.getvalue()


def _rendered_widths(table, width: int) -> list[int]:
    """Content width of each column, read off the rendered top border rule."""
    buf = io.StringIO()
    Console(file=buf, width=width).print(table)
    rule = next(l for l in buf.getvalue().splitlines() if l.startswith("┏"))
    return [len(seg) - 2 for seg in rule.strip("┏┓").split("┳")]


def _render_small(table) -> str:
    """Render an already-built table on a small window for assertion."""
    buf = io.StringIO()
    Console(file=buf, width=80, force_terminal=True).print(table)
    return buf.getvalue()


def _rendered_widths(table: Table, width: int) -> list[int]:
    """Content width of each column as drawn, read off the top border rule."""
    buf = io.StringIO()
    Console(file=buf, width=width).print(table)
    rule = next(l for l in buf.getvalue().splitlines() if l.startswith("┏"))
    return [len(seg) - 2 for seg in rule.strip("┏┓").split("┳")]


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
        assert col_names == ["(Alias) Name", "Status", "Reasoning"]


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
        """Alias uses ALIAS_STYLE ('bold magenta'), name uses 'bold' + engine colour."""
        row = _task_row(name="my-task", alias="ab", needs_review=False)
        table = _build_dashboard_table([row], _TZ)
        name_cell = table.columns[0]._cells[0]
        assert isinstance(name_cell, Text)
        spans = name_cell._spans
        # First span should be the alias with ALIAS_STYLE.
        alias_span = spans[0]
        assert alias_span.style == ALIAS_STYLE
        # Second span is the task name, styled bold plus the engine colour.
        name_span = spans[1]
        assert name_span.style == f"bold {ENGINE_NAME_STYLE[DEFAULT_ENGINE]}"

    def test_marker_is_ascii_safe(self) -> None:
        """The review marker must be pure ASCII for predictable terminal width."""
        row = _task_row(name="my-task", needs_review=True)
        table = _build_dashboard_table([row], _TZ)
        name_cell = table.columns[0]._cells[0]
        assert isinstance(name_cell, Text)
        assert name_cell.plain.isascii()

    def test_status_styling_applied(self) -> None:
        """Each status's label span should get the correct Rich style from STYLE_FOR_STATUS."""
        for status in TaskStatus:
            expected_style = STYLE_FOR_STATUS.get(status, "")
            row = _task_row(status=status.value)
            table = _build_dashboard_table([row], _TZ)
            status_cell = table.columns[1]._cells[0]
            assert isinstance(status_cell, Text)
            assert status_cell.plain.startswith(status.value)
            # The base Text carries no style; the status style is attached only
            # to the status-label span. Otherwise a `dim` base style (DONE /
            # DISCARDED) would bleed onto appended spans like the one-liner.
            assert str(status_cell.style) == ""
            label_spans = [
                s for s in status_cell._spans
                if s.start == 0 and s.end == len(status.value)
            ]
            assert label_spans, (
                f"Expected a span covering the status label, got {status_cell._spans}"
            )
            assert str(label_spans[0].style) == expected_style


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


class TestSleepingProgress:
    NOW = datetime(2026, 9, 14, 12, tzinfo=timezone.utc)

    def _cell(self, elapsed: int, total: int = 300) -> Text:
        row = _task_row(
            status="SLEEPING",
            status_changed_at=(self.NOW - timedelta(seconds=elapsed)).isoformat(),
            sleep_seconds=total,
        )
        with patch("ilan.time_format.datetime", wraps=datetime) as clock:
            clock.now.return_value = self.NOW
            table = _build_dashboard_table([row], _TZ)
        cell = table.columns[1]._cells[0]
        assert isinstance(cell, Text)
        return cell

    def test_status_column_shows_a_half_full_bar_and_elapsed_total(self) -> None:
        cell = self._cell(150)
        assert cell.plain == "SLEEPING █████░░░░░ 2m30s / 5m"

    def test_full_bar_keeps_counting_elapsed_time(self) -> None:
        cell = self._cell(600)
        assert cell.plain == "SLEEPING ██████████ 10m / 5m"

    def test_bar_has_distinct_filled_and_empty_styles(self) -> None:
        cell = self._cell(150)
        styles = {span.style for span in cell.spans}
        assert SLEEP_PROGRESS_STYLE in styles
        assert SLEEP_PROGRESS_EMPTY_STYLE in styles

    def test_name_cell_keeps_the_requested_sleep_duration(self) -> None:
        row = _task_row(status="SLEEPING", sleep_seconds=300)
        table = _build_dashboard_table([row], _TZ)
        cell = table.columns[0]._cells[0]
        assert isinstance(cell, Text)
        assert cell.plain == "my-task (sleeping for 5m)"

    def test_name_suffix_uses_the_same_dark_blue_as_sleeping_status(self) -> None:
        row = _task_row(status="SLEEPING", sleep_seconds=300)
        cell = _build_name_cell(row)
        suffix_start = cell.plain.index(" (sleeping for 5m)")
        suffix_span = next(
            span for span in cell.spans if span.start == suffix_start
        )
        assert suffix_span.style == SLEEP_SUFFIX_STYLE
        assert SLEEP_SUFFIX_STYLE in STYLE_FOR_STATUS[TaskStatus.SLEEPING]

    @pytest.mark.parametrize("status", ["WORKING", "AGENT_FINISHED"])
    def test_stale_sleep_metadata_has_no_name_suffix(self, status: str) -> None:
        row = _task_row(status=status, sleep_seconds=300)
        assert "sleeping for" not in _build_name_cell(row).plain

    @pytest.mark.parametrize("seconds", [None, 0, -5])
    def test_invalid_sleep_duration_has_no_name_suffix(
        self, seconds: int | None,
    ) -> None:
        row = _task_row(status="SLEEPING", sleep_seconds=seconds)
        assert "sleeping for" not in _build_name_cell(row).plain


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

    def test_dashboard_reloads_timezone_per_render(
        self, tmp_config, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``_do_dashboard`` should re-read ``time-zone`` on every render.

        Otherwise, editing ``time-zone`` while the dashboard is running leaves
        the header stuck on whatever zone was loaded at startup, even though
        the per-row ``Last Changed`` cell (rendered via ``_format_ts``)
        follows the new value because it reloads on each call.
        """
        import sys as _sys

        import ilan.cli as cli_mod
        import ilan.config as cfg_mod

        cfg_mod.save({**cfg_mod.DEFAULTS, "time-zone": "US/Pacific"})

        captured: list[str] = []
        real_build = cli_mod._build_dashboard_table

        def spy_build(rows, tz, **kw):
            captured.append(str(tz))
            # Switch the configured zone once the first render has captured
            # the original. The next render must pick up the new value.
            if len(captured) == 1:
                cfg_mod.save({**cfg_mod.DEFAULTS, "time-zone": "Europe/London"})
            return real_build(rows, tz, **kw)

        class _FakeLive:
            def __init__(self, renderable, **_kw):
                self.renderable = renderable

            def __enter__(self):
                return self

            def __exit__(self, *_a):
                return False

            def update(self, renderable):
                self.renderable = renderable

        client = _make_client()
        client.list_tasks.return_value = {"tasks": []}

        monkeypatch.setattr(cli_mod, "_build_dashboard_table", spy_build)
        monkeypatch.setattr(cli_mod, "Live", _FakeLive)
        monkeypatch.setattr(cli_mod, "Client", lambda: client)
        monkeypatch.setattr(cli_mod.termios, "tcgetattr", lambda _fd: [])
        monkeypatch.setattr(cli_mod.termios, "tcsetattr", lambda *_a, **_kw: None)
        monkeypatch.setattr(cli_mod.tty, "setcbreak", lambda _fd: None)

        fake_stdin = MagicMock()
        fake_stdin.fileno.return_value = 0
        fake_stdin.read.return_value = "q"
        monkeypatch.setattr(cli_mod.sys, "stdin", fake_stdin)
        # Avoid touching the real fileno when the test runs under pytest's
        # captured stdout.
        del _sys

        # First select call: no key (drives the auto-refresh branch).
        # Second select call: signal a keypress so the loop exits via 'q'.
        select_returns = iter([([], [], []), ([fake_stdin], [], [])])
        monkeypatch.setattr(
            cli_mod.select,
            "select",
            lambda *_a, **_kw: next(select_returns),
        )

        # Make every monotonic() call advance well past the 1-second
        # interval so the auto-refresh fires immediately on the first loop.
        ticks = iter([0.0, 100.0, 100.0, 200.0])
        monkeypatch.setattr(
            cli_mod.time, "monotonic", lambda: next(ticks)
        )

        cli_mod._do_dashboard()

        # First render captured at startup, second after the auto-refresh.
        assert len(captured) >= 2, captured
        assert captured[0] == "US/Pacific"
        assert captured[-1] == "Europe/London"


# ── _format_ts seconds toggle ────────────────────────────────────────


class TestFormatTsSeconds:
    def test_default_includes_seconds(self, tmp_config) -> None:
        import ilan.config as cfg_mod

        cfg_mod.save({**cfg_mod.DEFAULTS, "time-zone": "US/Pacific"})
        out = _format_ts("2026-07-02T18:05:09+00:00")
        assert "11:05:09" in out

    def test_seconds_false_drops_seconds(self, tmp_config) -> None:
        import ilan.config as cfg_mod

        cfg_mod.save({**cfg_mod.DEFAULTS, "time-zone": "US/Pacific"})
        out = _format_ts("2026-07-02T18:05:09+00:00", seconds=False)
        assert "11:05" in out
        assert "11:05:09" not in out


# ── table expand property ────────────────────────────────────────────


class TestDashboardTableProperties:
    def test_table_expands_with_name_and_status_as_equal_ratio_columns(self) -> None:
        """Name and Status flex equally; ``Last Changed`` is pinned.

        Both are prose columns now — the note under the name, the summary
        under the status label — so neither has a claim on more room.
        """
        table = _build_dashboard_table([], _TZ, show_one_liner=True)
        assert table.expand is True
        ratios = [c.ratio for c in table.columns]
        assert ratios == [1, 1, None]

    def test_the_split_does_not_depend_on_the_one_liner(self) -> None:
        on = _build_dashboard_table([], _TZ, show_one_liner=True)
        off = _build_dashboard_table([], _TZ, show_one_liner=False)
        assert [c.ratio for c in on.columns[:2]] == [c.ratio for c in off.columns[:2]] == [1, 1]

    def test_table_draws_separator_between_rows(self) -> None:
        """``show_lines=True`` draws a horizontal rule between every task row."""
        table = _build_dashboard_table([], _TZ)
        assert table.show_lines is True


# ── the Created column is gone ───────────────────────────────────────


class TestNoCreatedColumn:
    """``Created`` is not a column of either listing at any terminal width."""

    @pytest.mark.parametrize("show_one_liner", [True, False])
    def test_the_dashboard_never_builds_one(self, show_one_liner: bool) -> None:
        table = _build_dashboard_table([], _TZ, show_one_liner=show_one_liner)
        assert [c.header for c in table.columns] == [
            "(Alias) Name", "Status", "Reasoning",
        ]

    def test_the_placeholder_row_fills_every_column(self) -> None:
        """The 'No active tasks.' row must not over- or under-fill cells."""
        table = _build_dashboard_table([], _TZ)
        assert len(table.columns) == 3
        assert all(len(c._cells) == 1 for c in table.columns)

    def test_a_task_row_fills_every_column(self) -> None:
        table = _build_dashboard_table([_task_row(name="a-task")], _TZ)
        assert len(table.columns) == 3
        assert all(len(c._cells) == 1 for c in table.columns)

    def test_no_creation_stamp_reaches_the_table(self) -> None:
        """The row still carries ``created_at``; nothing renders it."""
        row = _task_row(name="a-task", status="WORKING")
        row["created_at"] = "2026-01-02T03:04:05+00:00"
        table = _build_dashboard_table([row], _TZ)
        rendered = _render_table_text([row])
        assert _format_ts(row["created_at"], seconds=False) not in rendered
        assert len(table.columns) == 3

    def test_a_small_window_still_shows_name_and_status(self) -> None:
        table = _build_dashboard_table([_task_row(name="keep-me")], _TZ)
        text = _render_small(table)
        assert "keep-me" in text
        assert "WORKING" in text


class TestDashboardProseWidths:
    """Name and Status share the space left by the fixed timestamp column."""

    @pytest.mark.parametrize("width", [140, 160, 200, 240])
    def test_the_dashboard_fills_the_window(self, width: int) -> None:
        """Its prose columns are ratios under ``expand=True``, so what the
        dropped column stopped taking is already theirs — no code required.
        """
        name, status, changed = _rendered_widths(
            _build_dashboard_table([_task_row(name="a-task")], _TZ), width,
        )
        assert changed == REASONING_COLUMN_WIDTH
        # Three columns of Rich chrome, then a closing rule.
        assert name + status + changed == width - (3 * 3 + 1)
        assert abs(name - status) <= 1


# ── task name as Gist hyperlink ──────────────────────────────────────


_GIST_URL = "https://gist.github.com/u/abc123"
# The SGR run that styles the task name, plus the text it covers, so a test
# can assert on the run's *contents* instead of hard-coding a color code.
_NAME_SGR_RUN = re.compile(r"\x1b\[([0-9;]*)m(my-task[^\x1b]*)")


def _name_span(row: dict) -> tuple[Text, Span]:
    """Return the dashboard row's name cell and the span covering the name."""
    table = _build_dashboard_table([row], _TZ)
    name_cell = table.columns[0]._cells[0]
    assert isinstance(name_cell, Text)
    # A plain row carries the name as its only span, so it is the last one.
    return name_cell, name_cell.spans[-1]


def _render_name_cell(row: dict) -> str:
    """Render the row's name cell in a column far wider than the name."""
    table = Table()
    table.add_column("(Alias) Name", style="bold", width=30)
    table.add_column("Status")
    table.add_row(_build_name_cell(row), "WORKING")
    buf = io.StringIO()
    # no_color=False: Rich honors the NO_COLOR env var by dropping color SGR
    # codes, which would change the byte runs asserted below whenever the
    # test runs under NO_COLOR (CI, agent shells).
    Console(
        file=buf, force_terminal=True, width=60, color_system="standard",
        no_color=False,
    ).print(table)
    return buf.getvalue()


class TestNameGistLink:
    """The task name doubles as a link to its Gist conversation mirror.

    This replaced the old ``History`` column, which spent a whole column of
    both listings on a single short ``history`` label.
    """

    def test_name_span_is_linked_when_gist_url(self) -> None:
        row = _task_row(name="my-task")
        row["gist_url"] = _GIST_URL
        cell, span = _name_span(row)
        assert cell.plain == "my-task"
        # The link and underline live on a span over just the name, NOT as a
        # base Text style: a base style bleeds across the cell's padding.
        assert str(cell.style) in ("", "none")
        assert (span.start, span.end) == (0, len("my-task"))
        assert f"link {_GIST_URL}" in str(span.style)
        assert "underline" in str(span.style)

    def test_engine_color_and_bold_survive_the_link(self) -> None:
        """Linking must not cost the name its engine color or its weight."""
        row = _task_row(name="my-task")
        row["engine"] = DEFAULT_ENGINE
        row["gist_url"] = _GIST_URL
        _, span = _name_span(row)
        assert "bold" in str(span.style)
        assert ENGINE_NAME_STYLE[DEFAULT_ENGINE] in str(span.style)

    def test_url_is_the_last_word_of_the_style(self) -> None:
        """Rich reads the word after ``link`` as the URL.

        Anything written after the URL would be swallowed into it, so the URL
        has to stay last and every attribute has to precede it.
        """
        row = _task_row()
        row["gist_url"] = _GIST_URL
        assert _name_style(row).endswith(f"link {_GIST_URL}")

    def test_no_link_without_gist(self) -> None:
        row = _task_row(name="my-task")
        _, span = _name_span(row)
        assert "link" not in str(span.style)
        assert "underline" not in str(span.style)

    def test_no_link_when_blank_url(self) -> None:
        row = _task_row(name="my-task")
        row["gist_url"] = "   "
        _, span = _name_span(row)
        assert "link" not in str(span.style)
        assert "underline" not in str(span.style)

    def test_rendered_hyperlink_wraps_exactly_the_name(self) -> None:
        r"""The OSC 8 hyperlink must open and close around the name alone.

        Rich opens a terminal hyperlink with ``ESC ] 8 ; id=<n> ; <url> ESC \``
        and closes it with ``ESC ] 8 ; ; ESC \``. The id is derived from the
        URL, so only the open sequence's tail and the close are asserted.
        """
        row = _task_row(name="my-task")
        row["gist_url"] = _GIST_URL
        out = _render_name_cell(row)
        assert f";{_GIST_URL}\x1b\\" in out
        # The link closes immediately after the name, so the cell's trailing
        # padding is not part of the clickable region.
        assert "my-task\x1b[0m\x1b]8;;\x1b\\" in out

    def test_underline_does_not_bleed_into_padding(self) -> None:
        """The underline must cover only the name, never the cell padding.

        Regression test: the name column is far wider than most names, so a
        base Text style would extend the underline SGR across the trailing
        padding spaces and draw a rule out to the column edge.
        """
        row = _task_row(name="my-task")
        row["gist_url"] = _GIST_URL
        match = _NAME_SGR_RUN.search(_render_name_cell(row))
        assert match is not None
        # SGR 4 is underline; it must open for the name and for nothing else.
        assert "4" in match.group(1).split(";")
        assert match.group(2) == "my-task"

    def test_unlinked_name_is_neither_hyperlinked_nor_underlined(self) -> None:
        row = _task_row(name="my-task")
        match = _NAME_SGR_RUN.search(_render_name_cell(row))
        assert match is not None
        assert "4" not in match.group(1).split(";")
        assert "\x1b]8;" not in _render_name_cell(row)


# ── one-liner summary rendering ──────────────────────────────────────


class TestOneLinerSummary:
    def test_one_liner_shown_under_status(self) -> None:
        row = _task_row(
            name="t-ol",
            status="AGENT_FINISHED",
            summary_one_liner="Opened PR with the feature flag.",
        )
        table = _build_dashboard_table([row], _TZ)
        status_cell = table.columns[1]._cells[0]
        assert isinstance(status_cell, Text)
        plain = status_cell.plain
        assert plain.startswith("AGENT_FINISHED")
        assert "\n" in plain
        assert "Opened PR with the feature flag." in plain

    def test_no_one_liner_when_missing(self) -> None:
        row = _task_row(name="t-no-ol", status="AGENT_FINISHED", summary_one_liner=None)
        table = _build_dashboard_table([row], _TZ)
        status_cell = table.columns[1]._cells[0]
        assert isinstance(status_cell, Text)
        assert "\n" not in status_cell.plain

    def test_blank_one_liner_treated_as_missing(self) -> None:
        row = _task_row(status="AGENT_FINISHED", summary_one_liner="   ")
        table = _build_dashboard_table([row], _TZ)
        status_cell = table.columns[1]._cells[0]
        assert isinstance(status_cell, Text)
        assert "\n" not in status_cell.plain

    def test_one_liner_styled_yellow_italic(self) -> None:
        row = _task_row(status="AGENT_FINISHED", summary_one_liner="Did the thing.")
        table = _build_dashboard_table([row], _TZ)
        status_cell = table.columns[1]._cells[0]
        assert isinstance(status_cell, Text)
        liner_start = status_cell.plain.index("Did the thing.")
        spans = status_cell._spans
        matched = [
            s for s in spans
            if s.start <= liner_start < s.end and s.style == "yellow italic"
        ]
        assert matched, "Expected a yellow italic span over the one-liner"

    def test_one_liner_brightness_uniform_across_statuses(self) -> None:
        """The one-liner must render at the same brightness for every status.

        The parent Text must have no base style and the one-liner span must
        carry exactly `yellow italic` (no `dim`) — so DONE / DISCARDED rows
        don't get a `dim`-bleed from the status style and every row's
        one-liner looks identical regardless of status."""
        for status in TaskStatus:
            row = _task_row(status=status.value, summary_one_liner="visible summary")
            table = _build_dashboard_table([row], _TZ)
            status_cell = table.columns[1]._cells[0]
            assert isinstance(status_cell, Text)
            assert str(status_cell.style) == "", (
                f"{status.value}: base Text style must be empty, got {status_cell.style!r}"
            )
            liner_start = status_cell.plain.index("visible summary")
            covering = [
                s for s in status_cell._spans if s.start <= liner_start < s.end
            ]
            yellow_italic = [s for s in covering if s.style == "yellow italic"]
            assert yellow_italic, (
                f"{status.value}: expected a yellow italic span over the one-liner, "
                f"got {covering!r}"
            )
            for span in covering:
                assert "dim" not in str(span.style), (
                    f"{status.value}: one-liner covered by dim span {span!r}"
                )

    def test_one_liner_hidden_when_disabled(self) -> None:
        row = _task_row(status="AGENT_FINISHED", summary_one_liner="should not show")
        table = _build_dashboard_table([row], _TZ, show_one_liner=False)
        status_cell = table.columns[1]._cells[0]
        assert isinstance(status_cell, Text)
        assert "should not show" not in status_cell.plain
        assert "\n" not in status_cell.plain

    def test_one_liner_shown_when_enabled_explicit(self) -> None:
        row = _task_row(status="AGENT_FINISHED", summary_one_liner="visible line")
        table = _build_dashboard_table([row], _TZ, show_one_liner=True)
        status_cell = table.columns[1]._cells[0]
        assert isinstance(status_cell, Text)
        assert "visible line" in status_cell.plain


# ── one-line-summary client toggle warning ───────────────────────────


class TestOneLinerWarning:
    def _capture(self) -> tuple[Console, io.StringIO]:
        buf = io.StringIO()
        return Console(file=buf, width=120, force_terminal=False), buf

    def test_warning_when_enabled_but_no_api_key(
        self, tmp_config, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import ilan.cli as cli_mod
        import ilan.config as cfg_mod

        cfg_mod.save({**cfg_mod.DEFAULTS, "one-line-summary": True})

        console_, buf = self._capture()
        monkeypatch.setattr(cli_mod, "console", console_)

        client = _make_client()
        client.get_config.return_value = {"config": {"api-key-codex": ""}}

        _maybe_warn_one_liner_unconfigured(client)
        out = buf.getvalue()
        assert "Note" in out
        assert "one-line-summary" in out
        # The note should explain the local `codex` CLI fallback.
        assert "codex" in out

    def test_no_warning_when_api_key_is_set(
        self, tmp_config, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import ilan.cli as cli_mod
        import ilan.config as cfg_mod

        cfg_mod.save({**cfg_mod.DEFAULTS, "one-line-summary": True})

        console_, buf = self._capture()
        monkeypatch.setattr(cli_mod, "console", console_)

        client = _make_client()
        client.get_config.return_value = {"config": {"api-key-codex": "sk-secret"}}

        _maybe_warn_one_liner_unconfigured(client)
        assert buf.getvalue() == ""

    def test_no_warning_when_one_liner_disabled(
        self, tmp_config, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import ilan.cli as cli_mod
        import ilan.config as cfg_mod

        cfg_mod.save({**cfg_mod.DEFAULTS, "one-line-summary": False})

        console_, buf = self._capture()
        monkeypatch.setattr(cli_mod, "console", console_)

        client = _make_client()
        client.get_config.return_value = {"config": {"api-key-claude": ""}}

        _maybe_warn_one_liner_unconfigured(client)
        assert buf.getvalue() == ""

    def test_silent_when_get_config_fails(
        self, tmp_config, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A failing get_config must not raise — the warning is best-effort."""
        import ilan.cli as cli_mod
        import ilan.config as cfg_mod

        cfg_mod.save({**cfg_mod.DEFAULTS, "one-line-summary": True})

        console_, buf = self._capture()
        monkeypatch.setattr(cli_mod, "console", console_)

        client = _make_client()
        client.get_config.side_effect = RuntimeError("boom")

        # Should not raise.
        _maybe_warn_one_liner_unconfigured(client)
        assert buf.getvalue() == ""


# ── Reasoning column ─────────────────────────────────────────────────


class TestReasoningCell:
    @staticmethod
    def _styles(cell: Text) -> dict[str, str]:
        return {cell.plain[sp.start:sp.end]: str(sp.style) for sp in cell.spans}

    @pytest.mark.parametrize(("level", "style"), [
        ("low", "green"), ("medium", "yellow"), ("max", "red"),
    ])
    def test_the_active_level_is_coloured_and_the_rest_grey(
        self, level: str, style: str,
    ) -> None:
        cell = _build_reasoning_cell({"reasoning": level})
        assert cell.plain == "low ⋅ medium ⋅ max"
        styles = self._styles(cell)
        assert styles[level] == style
        for other in {"low", "medium", "max"} - {level}:
            assert styles[other] == "grey50"
        assert styles[" ⋅ "] == "grey50"

    def test_a_row_without_a_level_is_all_grey(self) -> None:
        cell = _build_reasoning_cell({})
        assert cell.plain == "low ⋅ medium ⋅ max"
        assert set(self._styles(cell).values()) == {"grey50"}

    def test_the_column_fits_the_ladder(self) -> None:
        assert REASONING_COLUMN_WIDTH == len("low ⋅ medium ⋅ max")

    @pytest.mark.parametrize(("level", "style"), [
        ("low", "green"), ("medium", "yellow"), ("max", "red"),
    ])
    def test_the_concise_line_shows_only_the_active_level(
        self, level: str, style: str,
    ) -> None:
        line = _build_concise_task_line(
            {"name": "t", "status": "WORKING", "reasoning": level},
        )
        assert line.plain.endswith(f" {level}")
        assert "⋅" not in line.plain
        assert any(
            line.plain[sp.start:sp.end] == f" {level}" and str(sp.style) == style
            for sp in line.spans
        )
