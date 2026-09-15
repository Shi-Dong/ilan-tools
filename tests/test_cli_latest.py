"""Tests for ``ilan latest [-n N]``."""

from __future__ import annotations

import re
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner
from rich.console import Console
from rich.text import Text

import ilan.cli as cli_mod
from ilan.cli import (
    LATEST_COUNT_DEFAULT,
    LATEST_NOTES_CLOSE,
    LATEST_NOTES_LABEL_STYLE,
    LATEST_NOTES_OPEN,
    main,
    _build_latest_line,
    _latest_done_rows,
)
from ilan.models import ENGINE_CLAUDE, ENGINE_CODEX, ENGINE_NAME_STYLE
from ilan.task_display import NOTES_STYLE, NUMBER_STYLE, PIN_MARKER


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _strip_ansi(s: str) -> str:
    return _ANSI_RE.sub("", s)


@pytest.fixture()
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture()
def wide_console(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force a terminal-like console wide enough that no line is cut."""
    monkeypatch.setattr(cli_mod, "console", Console(width=200, force_terminal=True))


def _row(
    name: str,
    *,
    status: str = "DONE",
    hour: int = 0,
    number: int | None = 1,
    notes: str | None = None,
    engine: str = ENGINE_CLAUDE,
    pinned: bool = False,
) -> dict:
    return {
        "name": name,
        "alias": None,
        "status": status,
        "status_changed_at": f"2026-09-15T{hour:02d}:00:00+00:00",
        "number": number,
        "notes": notes,
        "engine": engine,
        "pinned": pinned,
    }


def _make_client(rows: list[dict]) -> MagicMock:
    client = MagicMock()
    client.ensure_server.return_value = {}
    client.version_mismatch = None
    client.is_remote = False
    client.list_tasks.return_value = {"tasks": rows}
    return client


def _invoke(runner: CliRunner, rows: list[dict], *args: str):
    client = _make_client(rows)
    with patch("ilan.cli._client", return_value=client):
        result = runner.invoke(main, ["latest", *args])
    return result, client


def _lines(output: str) -> list[str]:
    return _strip_ansi(output).splitlines()


def _name_of(line: str) -> str:
    """The task name in a rendered line: last word before the note, if any."""
    return line.split(LATEST_NOTES_OPEN, 1)[0].split()[-1]


def _names_in_order(output: str) -> list[str]:
    return [_name_of(line) for line in _lines(output)]


class TestLatestOrdering:
    def test_most_recently_done_first(
        self, runner: CliRunner, tmp_config, wide_console,
    ) -> None:
        rows = [
            _row("closed-first", hour=1, number=1),
            _row("closed-last", hour=3, number=3),
            _row("closed-second", hour=2, number=2),
        ]
        result, _ = _invoke(runner, rows)
        assert result.exit_code == 0
        assert _names_in_order(result.output) == [
            "closed-last", "closed-second", "closed-first",
        ]

    def test_order_is_by_when_it_was_done_not_by_number(
        self, runner: CliRunner, tmp_config, wide_console,
    ) -> None:
        """A task revived and closed again keeps its old number but is newest."""
        rows = [
            _row("revived", hour=5, number=1),
            _row("newer-number", hour=4, number=9),
        ]
        result, _ = _invoke(runner, rows)
        assert _names_in_order(result.output) == ["revived", "newer-number"]

    def test_a_row_with_no_stamp_sorts_last_but_is_kept(
        self, runner: CliRunner, tmp_config, wide_console,
    ) -> None:
        stampless = _row("no-stamp", number=2)
        stampless["status_changed_at"] = None
        result, _ = _invoke(runner, [stampless, _row("stamped", hour=1, number=1)])
        assert _names_in_order(result.output) == ["stamped", "no-stamp"]


class TestLatestFiltering:
    def test_only_done_tasks_are_listed(
        self, runner: CliRunner, tmp_config, wide_console,
    ) -> None:
        rows = [
            _row("finished", hour=3, number=1),
            _row("thrown-away", status="DISCARDED", hour=4, number=2),
            _row("still-going", status="WORKING", hour=5, number=None),
            _row("waiting", status="NEEDS_ATTENTION", hour=6, number=None),
        ]
        result, _ = _invoke(runner, rows)
        assert _names_in_order(result.output) == ["finished"]

    def test_closed_tasks_are_fetched_at_all(
        self, runner: CliRunner, tmp_config, wide_console,
    ) -> None:
        """DONE rows only reach the client when the listing asks for all."""
        _, client = _invoke(runner, [_row("finished")])
        client.list_tasks.assert_called_once_with(show_all=True)


class TestLatestLine:
    def test_number_name_and_note_on_one_line(
        self, runner: CliRunner, tmp_config, wide_console,
    ) -> None:
        result, _ = _invoke(
            runner, [_row("ship-the-fix", number=12, notes="follow up on the flake")],
        )
        assert _lines(result.output) == [
            "12 ship-the-fix (Notes: follow up on the flake)",
        ]

    def test_one_line_per_task_and_no_table_chrome(
        self, runner: CliRunner, tmp_config, wide_console,
    ) -> None:
        rows = [
            _row("first", hour=1, number=1, notes="a note"),
            _row("second", hour=2, number=2),
            _row("third", hour=3, number=3, notes="another note"),
        ]
        result, _ = _invoke(runner, rows)
        lines = _lines(result.output)
        assert len(lines) == len(rows)
        assert not any(char in result.output for char in "┏┃┡│└")

    def test_a_task_with_no_note_ends_at_its_name(
        self, runner: CliRunner, tmp_config, wide_console,
    ) -> None:
        """No note means no dangling separator."""
        result, _ = _invoke(runner, [_row("no-note", number=4)])
        assert _lines(result.output) == ["4 no-note"]

    def test_a_whitespace_only_note_counts_as_no_note(
        self, runner: CliRunner, tmp_config, wide_console,
    ) -> None:
        result, _ = _invoke(runner, [_row("blank-note", number=4, notes="   \n ")])
        assert _lines(result.output) == ["4 blank-note"]

    def test_a_task_with_no_number_still_lists(
        self, runner: CliRunner, tmp_config, wide_console,
    ) -> None:
        """A task saved before numbers existed is still something you finished."""
        result, _ = _invoke(runner, [_row("ancient", number=None, notes="old")])
        assert _lines(result.output) == ["ancient (Notes: old)"]

    def test_a_multiline_note_is_flattened_onto_the_line(
        self, runner: CliRunner, tmp_config, wide_console,
    ) -> None:
        """A newline in a note would split the row; whitespace is collapsed."""
        result, _ = _invoke(
            runner, [_row("wrapped", number=7, notes="first line\n\nsecond   line")],
        )
        assert _lines(result.output) == [
            "7 wrapped (Notes: first line second line)",
        ]

    def test_a_pinned_task_keeps_the_listings_marker(
        self, runner: CliRunner, tmp_config, wide_console,
    ) -> None:
        result, _ = _invoke(runner, [_row("kept-up-top", number=8, pinned=True)])
        assert _lines(result.output) == [f"{PIN_MARKER}8 kept-up-top"]


class TestLatestWidth:
    """One line per task holds however long the note is."""

    _LONG_NOTE = "word " * 60

    def _narrow(self, monkeypatch: pytest.MonkeyPatch, *, terminal: bool) -> None:
        monkeypatch.setattr(
            cli_mod, "console", Console(width=60, force_terminal=terminal),
        )

    def test_a_long_note_is_cut_to_the_window(
        self, runner: CliRunner, tmp_config, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        self._narrow(monkeypatch, terminal=True)
        result, _ = _invoke(runner, [_row("long", number=3, notes=self._LONG_NOTE)])
        lines = _lines(result.output)
        assert len(lines) == 1
        assert len(lines[0]) <= 60
        # The note is what gets cut, so the bracket it opened still closes.
        assert lines[0].startswith("3 long (Notes: word")
        assert lines[0].endswith(f"…{LATEST_NOTES_CLOSE}")

    def test_a_short_note_is_left_alone(
        self, runner: CliRunner, tmp_config, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        self._narrow(monkeypatch, terminal=True)
        result, _ = _invoke(runner, [_row("short", number=3, notes="fits")])
        assert _lines(result.output) == ["3 short (Notes: fits)"]

    def test_nothing_is_cut_when_there_is_no_window(
        self, runner: CliRunner, tmp_config, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Piped to a file or `grep`, the note must survive whole."""
        self._narrow(monkeypatch, terminal=False)
        result, _ = _invoke(runner, [_row("long", number=3, notes=self._LONG_NOTE)])
        assert self._LONG_NOTE.strip() in " ".join(_lines(result.output))
        assert "…" not in result.output

    def test_no_room_for_the_note_drops_the_parenthetical(
        self, runner: CliRunner, tmp_config, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An empty `(Notes: )` would be chrome reporting nothing."""
        monkeypatch.setattr(
            cli_mod, "console", Console(width=20, force_terminal=True),
        )
        result, _ = _invoke(
            runner, [_row("a-rather-long-name", number=3, notes="never fits")],
        )
        lines = _lines(result.output)
        assert len(lines) == 1
        assert len(lines[0]) <= 20
        assert LATEST_NOTES_OPEN.strip() not in lines[0]

    def test_the_builder_leaves_the_line_whole_without_a_width(self) -> None:
        line = _build_latest_line(_row("long", number=3, notes=self._LONG_NOTE))
        assert line.plain.endswith(f"word{LATEST_NOTES_CLOSE}")
        assert "…" not in line.plain
        assert len(line.plain) > 60


class TestLatestStyles:
    """The line carries the listings' own styles rather than private copies.

    Read off the built ``Text`` rather than off rendered escape codes: which
    codes a colour comes out as depends on the terminal the suite happens to
    run under, while the style a span is given does not.
    """

    def _style_of(self, line: Text, fragment: str) -> str:
        start = line.plain.index(fragment)
        for span in line.spans:
            if (span.start, span.end) == (start, start + len(fragment)):
                return str(span.style)
        raise AssertionError(f"no span covers {fragment!r} in {line.plain!r}")

    @pytest.mark.parametrize("engine", [ENGINE_CLAUDE, ENGINE_CODEX])
    def test_the_name_keeps_its_engine_colour_and_gist_link(
        self, engine: str,
    ) -> None:
        row = _row("finished", engine=engine)
        row["gist_url"] = "https://gist.github.com/shi/abc123"
        style = self._style_of(_build_latest_line(row), "finished")
        assert ENGINE_NAME_STYLE[engine] in style
        assert "link https://gist.github.com/shi/abc123" in style

    def test_the_note_is_drawn_in_the_listings_note_style(self) -> None:
        line = _build_latest_line(_row("finished", notes="do not forget"))
        assert self._style_of(line, "do not forget") == NOTES_STYLE

    def test_the_number_is_drawn_in_the_listings_number_style(self) -> None:
        line = _build_latest_line(_row("finished", number=12))
        assert self._style_of(line, "12 ") == NUMBER_STYLE

    def test_the_parenthetical_is_chrome_not_note_text(self) -> None:
        """The label reads as chrome, so only the note carries the note style."""
        line = _build_latest_line(_row("finished", notes="do not forget"))
        assert self._style_of(line, LATEST_NOTES_OPEN) == LATEST_NOTES_LABEL_STYLE
        assert self._style_of(line, LATEST_NOTES_CLOSE) == LATEST_NOTES_LABEL_STYLE

    def test_the_listing_is_styled(
        self, runner: CliRunner, tmp_config, wide_console,
    ) -> None:
        result, _ = _invoke(runner, [_row("finished", notes="a note")])
        assert "\x1b[" in result.output


class TestLatestCount:
    def _ten_plus_two(self) -> list[dict]:
        return [
            _row(f"task-{index:02d}", hour=index, number=index)
            for index in range(1, LATEST_COUNT_DEFAULT + 3)
        ]

    def test_defaults_to_ten(
        self, runner: CliRunner, tmp_config, wide_console,
    ) -> None:
        result, _ = _invoke(runner, self._ten_plus_two())
        assert LATEST_COUNT_DEFAULT == 10
        assert len(_lines(result.output)) == LATEST_COUNT_DEFAULT

    def test_n_limits_the_listing(
        self, runner: CliRunner, tmp_config, wide_console,
    ) -> None:
        result, _ = _invoke(runner, self._ten_plus_two(), "-n", "3")
        assert _names_in_order(result.output) == ["task-12", "task-11", "task-10"]

    def test_long_flag_works_too(
        self, runner: CliRunner, tmp_config, wide_console,
    ) -> None:
        result, _ = _invoke(runner, self._ten_plus_two(), "--num", "2")
        assert _names_in_order(result.output) == ["task-12", "task-11"]

    def test_n_larger_than_the_listing_shows_everything(
        self, runner: CliRunner, tmp_config, wide_console,
    ) -> None:
        result, _ = _invoke(runner, [_row("only-one")], "-n", "50")
        assert _names_in_order(result.output) == ["only-one"]

    @pytest.mark.parametrize("value", ["0", "-1"])
    def test_a_count_below_one_is_refused(
        self, runner: CliRunner, tmp_config, value: str,
    ) -> None:
        """Showing nothing is never what asking for a count meant."""
        result, _ = _invoke(runner, [_row("finished")], "-n", value)
        assert result.exit_code != 0

    def test_a_non_numeric_count_is_refused(
        self, runner: CliRunner, tmp_config,
    ) -> None:
        result, _ = _invoke(runner, [_row("finished")], "-n", "lots")
        assert result.exit_code != 0


class TestLatestEmpty:
    def test_no_done_tasks_says_so(
        self, runner: CliRunner, tmp_config, wide_console,
    ) -> None:
        result, _ = _invoke(runner, [_row("still-going", status="WORKING")])
        assert result.exit_code == 0
        assert _strip_ansi(result.output) == "No tasks marked done yet.\n"

    def test_no_tasks_at_all_says_the_same(
        self, runner: CliRunner, tmp_config, wide_console,
    ) -> None:
        result, _ = _invoke(runner, [])
        assert result.exit_code == 0
        assert _strip_ansi(result.output) == "No tasks marked done yet.\n"


class TestLatestRowSelection:
    """``_latest_done_rows`` on its own, away from the rendering."""

    def test_returns_at_most_num_rows(self) -> None:
        rows = [_row(f"t{index}", hour=index) for index in range(5)]
        assert len(_latest_done_rows(rows, 2)) == 2

    def test_keeps_the_newest_rows_not_the_first_seen(self) -> None:
        rows = [_row("old", hour=1), _row("new", hour=9), _row("mid", hour=5)]
        assert [row["name"] for row in _latest_done_rows(rows, 2)] == ["new", "mid"]

    def test_an_empty_listing_gives_no_rows(self) -> None:
        assert _latest_done_rows([], 10) == []
