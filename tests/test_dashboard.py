"""Tests for ``ilan dashboard`` — full-screen real-time task dashboard."""

from __future__ import annotations

import io
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest
from click.testing import CliRunner
from rich.console import Console
from rich.text import Text

from ilan.cli import (
    ALIAS_STYLE,
    _NARROW_TERMINAL_WIDTH,
    _build_dashboard_table,
    _build_history_cell,
    _format_ts,
    _maybe_warn_one_liner_unconfigured,
    _terminal_is_narrow,
    main,
)
from ilan.models import (
    DEFAULT_ENGINE,
    ENGINE_NAME_STYLE,
    STYLE_FOR_STATUS,
    TaskStatus,
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
) -> dict:
    return {
        "name": name,
        "status": status,
        "alias": alias,
        "needs_review": needs_review,
        "created_at": created_at,
        "status_changed_at": status_changed_at,
        "summary_one_liner": summary_one_liner,
    }


def _render_table_text(rows: list[dict]) -> str:
    """Render a dashboard table to plain text for assertion."""
    table = _build_dashboard_table(rows, _TZ)
    buf = io.StringIO()
    console = Console(file=buf, width=120, force_terminal=True)
    console.print(table)
    return buf.getvalue()


def _render_narrow(table) -> str:
    """Render an already-built table at a narrow width for assertion."""
    buf = io.StringIO()
    Console(file=buf, width=80, force_terminal=True).print(table)
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
        assert col_names == [
            "(Alias) Name", "Status", "Created", "Last Changed", "History",
        ]


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
        the per-row ``Created`` / ``Last Changed`` cells (rendered via
        ``_format_ts``) follow the new value because they reload on each call.
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
    def test_table_expands_with_one_liner_on(self) -> None:
        """One-liner ON: Status gets the biggest slot (16/34)."""
        table = _build_dashboard_table([], _TZ, show_one_liner=True)
        assert table.expand is True
        ratios = [c.ratio for c in table.columns]
        assert ratios == [10, 16, 6, 6, 4]

    def test_table_expands_with_one_liner_off(self) -> None:
        """One-liner OFF: keep the original 14:8:7:7:4 ratios since Status
        only holds a short label + duration suffix.
        """
        table = _build_dashboard_table([], _TZ, show_one_liner=False)
        assert table.expand is True
        ratios = [c.ratio for c in table.columns]
        assert ratios == [14, 8, 7, 7, 4]

    def test_table_draws_separator_between_rows(self) -> None:
        """``show_lines=True`` draws a horizontal rule between every task row."""
        table = _build_dashboard_table([], _TZ)
        assert table.show_lines is True


# ── narrow-terminal column dropping ──────────────────────────────────


class TestTerminalIsNarrow:
    def test_below_threshold_is_narrow(self) -> None:
        assert _terminal_is_narrow(_NARROW_TERMINAL_WIDTH - 1) is True

    def test_at_threshold_is_not_narrow(self) -> None:
        assert _terminal_is_narrow(_NARROW_TERMINAL_WIDTH) is False

    def test_above_threshold_is_not_narrow(self) -> None:
        assert _terminal_is_narrow(_NARROW_TERMINAL_WIDTH + 40) is False


class TestNarrowDashboardColumns:
    """When ``narrow`` is set, the dashboard drops Created."""

    def test_narrow_drops_created_one_liner_on(self) -> None:
        table = _build_dashboard_table([], _TZ, show_one_liner=True, narrow=True)
        col_names = [c.header for c in table.columns]
        assert col_names == ["(Alias) Name", "Status", "Last Changed", "History"]

    def test_narrow_drops_created_one_liner_off(self) -> None:
        table = _build_dashboard_table([], _TZ, show_one_liner=False, narrow=True)
        col_names = [c.header for c in table.columns]
        assert col_names == ["(Alias) Name", "Status", "Last Changed", "History"]

    def test_wide_keeps_all_columns(self) -> None:
        table = _build_dashboard_table([], _TZ, narrow=False)
        col_names = [c.header for c in table.columns]
        assert col_names == [
            "(Alias) Name", "Status", "Created", "Last Changed", "History",
        ]

    def test_narrow_empty_row_matches_column_count(self) -> None:
        """The 'No active tasks.' placeholder row must not over/under-fill cells."""
        table = _build_dashboard_table([], _TZ, narrow=True)
        assert len(table.columns) == 4
        # Each column has exactly one placeholder cell.
        assert all(len(c._cells) == 1 for c in table.columns)

    def test_narrow_task_row_drops_created(self) -> None:
        row = _task_row(name="narrow-task", status="WORKING")
        table = _build_dashboard_table([row], _TZ, narrow=True)
        assert len(table.columns) == 4
        col_names = [c.header for c in table.columns]
        assert "Created" not in col_names

    def test_narrow_still_shows_name_and_status(self) -> None:
        row = _task_row(name="keep-me", status="WORKING")
        table = _build_dashboard_table([row], _TZ, narrow=True)
        text = _render_narrow(table)
        assert "keep-me" in text
        assert "WORKING" in text


# ── History (gist) column ────────────────────────────────────────────


class TestHistoryColumn:
    def test_history_cell_is_linked_when_gist_url(self) -> None:
        row = _task_row()
        row["gist_url"] = "https://gist.github.com/u/abc123"
        table = _build_dashboard_table([row], _TZ)
        hist_cell = table.columns[4]._cells[0]
        assert isinstance(hist_cell, Text)
        assert hist_cell.plain == "history"
        # The link/underline style is carried by a span over just the label,
        # NOT as a base Text style. A base style bleeds across the cell's right
        # padding, so the underline would run past the word "history".
        assert str(hist_cell.style) in ("", "none")
        assert len(hist_cell.spans) == 1
        span = hist_cell.spans[0]
        assert (span.start, span.end) == (0, len("history"))
        assert "link https://gist.github.com/u/abc123" in str(span.style)
        assert "underline" in str(span.style)

    def test_history_underline_does_not_bleed_into_padding(self) -> None:
        """Rendered underline must cover only "history", never the padding.

        Regression test: when the History column is wider than the label (as it
        is in the expanding dashboard table), a base Text style would extend the
        underline SGR across the trailing padding spaces.
        """
        import io

        from rich.console import Console
        from rich.table import Table

        cell = _build_history_cell({"gist_url": "https://gist/abc"})
        table = Table()
        table.add_column("Name")
        table.add_column("History", width=20)  # far wider than "history"
        table.add_row("x", cell)
        buf = io.StringIO()
        # no_color=False: Rich honors the NO_COLOR env var by dropping color
        # SGR codes (underline survives), which would turn the expected
        # ``4;34`` run into ``4`` and break the byte-exact assertions below
        # whenever the test runs under NO_COLOR (CI, agent shells).
        Console(
            file=buf, force_terminal=True, width=60, color_system="standard",
            no_color=False,
        ).print(table)
        out = buf.getvalue()
        # Underline (SGR 4) + blue (34) opens right before the word and resets
        # immediately after it — the padding spaces stay outside the SGR run.
        assert "\x1b[4;34mhistory\x1b[0m" in out
        assert "\x1b[4;34mhistory " not in out

    def test_history_cell_placeholder_without_gist(self) -> None:
        row = _task_row()
        table = _build_dashboard_table([row], _TZ)
        hist_cell = table.columns[4]._cells[0]
        assert isinstance(hist_cell, Text)
        assert hist_cell.plain == "-"

    def test_history_cell_placeholder_when_blank_url(self) -> None:
        row = _task_row()
        row["gist_url"] = "   "
        table = _build_dashboard_table([row], _TZ)
        hist_cell = table.columns[4]._cells[0]
        assert isinstance(hist_cell, Text)
        assert hist_cell.plain == "-"


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
