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
    main,
    _build_latest_table,
    _latest_done_rows,
)
from ilan.models import ENGINE_CLAUDE, ENGINE_CODEX, ENGINE_NAME_STYLE
from ilan.task_display import NOTES_STYLE, NUMBER_STYLE


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _strip_ansi(s: str) -> str:
    return _ANSI_RE.sub("", s)


@pytest.fixture()
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture()
def wide_console(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force a console wide enough that no cell folds onto a second line."""
    monkeypatch.setattr(cli_mod, "console", Console(width=200, force_terminal=True))


def _row(
    name: str,
    *,
    status: str = "DONE",
    hour: int = 0,
    number: int | None = 1,
    notes: str | None = None,
    engine: str = ENGINE_CLAUDE,
) -> dict:
    return {
        "name": name,
        "alias": None,
        "status": status,
        "status_changed_at": f"2026-09-15T{hour:02d}:00:00+00:00",
        "number": number,
        "notes": notes,
        "engine": engine,
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


def _body_lines(output: str) -> list[str]:
    """The table's content lines, without its header and border rules."""
    return [
        line for line in _strip_ansi(output).splitlines() if line.startswith("│")
    ]


def _cells(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip("│").split("│")]


def _headers(output: str) -> list[str]:
    header = next(
        line for line in _strip_ansi(output).splitlines() if "Notes" in line
    )
    return [cell.strip() for cell in header.strip("┃").split("┃")]


def _names_in_order(output: str) -> list[str]:
    return [cells[1] for line in _body_lines(output) if (cells := _cells(line))[1]]


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


class TestLatestColumns:
    def test_shows_number_name_and_note(
        self, runner: CliRunner, tmp_config, wide_console,
    ) -> None:
        result, _ = _invoke(
            runner, [_row("ship-the-fix", number=12, notes="follow up on the flake")],
        )
        assert _cells(_body_lines(result.output)[0]) == [
            "12", "ship-the-fix", "follow up on the flake",
        ]

    def test_headers(self, runner: CliRunner, tmp_config, wide_console) -> None:
        result, _ = _invoke(runner, [_row("finished")])
        assert _headers(result.output) == ["#", "Name", "Notes"]

    def test_a_task_with_no_note_leaves_the_cell_empty(
        self, runner: CliRunner, tmp_config, wide_console,
    ) -> None:
        result, _ = _invoke(runner, [_row("no-note", number=4)])
        assert _cells(_body_lines(result.output)[0]) == ["4", "no-note", ""]

    def test_a_task_with_no_number_still_lists(
        self, runner: CliRunner, tmp_config, wide_console,
    ) -> None:
        """A task saved before numbers existed is still something you finished."""
        result, _ = _invoke(runner, [_row("ancient", number=None)])
        assert _cells(_body_lines(result.output)[0]) == ["", "ancient", ""]

    def test_the_listing_is_styled(
        self, runner: CliRunner, tmp_config, wide_console,
    ) -> None:
        result, _ = _invoke(runner, [_row("finished", notes="a note")])
        assert "\x1b[" in result.output


class TestLatestStyles:
    """The cells carry the listings' own styles rather than private copies.

    Read off the built table rather than off rendered escape codes: which
    codes a colour comes out as depends on the terminal the suite happens to
    run under, while the style a cell is given does not.
    """

    def _cell(self, row: dict, column: int) -> Text:
        return list(_build_latest_table([row]).columns[column].cells)[0]

    @pytest.mark.parametrize("engine", [ENGINE_CLAUDE, ENGINE_CODEX])
    def test_the_name_keeps_its_engine_colour_and_gist_link(
        self, engine: str,
    ) -> None:
        row = _row("finished", engine=engine)
        row["gist_url"] = "https://gist.github.com/shi/abc123"
        cell = self._cell(row, 1)
        assert cell.plain == "finished"
        assert ENGINE_NAME_STYLE[engine] in str(cell.style)
        assert "link https://gist.github.com/shi/abc123" in str(cell.style)

    def test_the_note_is_drawn_in_the_listings_note_style(self) -> None:
        cell = self._cell(_row("finished", notes="do not forget"), 2)
        assert cell.plain == "do not forget"
        assert cell.style == NOTES_STYLE

    def test_the_number_column_is_drawn_in_the_number_style(self) -> None:
        assert _build_latest_table([_row("finished")]).columns[0].style == NUMBER_STYLE


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
        assert len(_names_in_order(result.output)) == LATEST_COUNT_DEFAULT

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
