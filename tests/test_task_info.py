"""Tests for ``ilan task info`` — rename history, fields, and the rendering."""

from __future__ import annotations

import json
import re
import signal
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest
from click.testing import CliRunner
from rich.console import Console
from rich.text import Text

import ilan.cli as cli_mod
from ilan.cli import (
    INFO_EMPTY,
    NOTES_STYLE,
    ONE_LINER_STYLE,
    RENAME_ARROW,
    _build_info_grid,
    _build_name_cell,
    _build_name_history,
    _build_name_label,
    _info_timestamp,
    _info_value,
    main,
)
from ilan.models import Task
from ilan.server import IlanServer
from ilan.store import Store

from tests.helpers import SERVE_POLL_INTERVAL, wait_until_serving


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _strip_ansi(s: str) -> str:
    return _ANSI_RE.sub("", s)


def _row(
    name: str,
    parent: str | None = None,
    *,
    hour: int = 0,
    status: str = "WORKING",
    alias: str | None = None,
    former_names: list[str] | None = None,
    notes: str | None = None,
    summary: str | None = None,
    deleted_ancestors: list[str] | None = None,
) -> dict:
    """Build one ``/tasks`` row; *hour* drives the creation order."""
    ts = f"2026-09-13T{hour:02d}:00:00+00:00"
    return {
        "name": name,
        "status": status,
        "created_at": ts,
        "status_changed_at": ts,
        "alias": alias,
        "former_names": former_names or [],
        "needs_review": False,
        "parent_name": parent,
        "deleted_ancestors": deleted_ancestors or [],
        "notes": notes,
        "summary_one_liner": summary,
    }


# ── Task.former_names ───────────────────────────────────────────────────


class TestFormerNamesField:
    def test_defaults_to_empty_and_round_trips(self, tmp_workdir: Path) -> None:
        store = Store(tmp_workdir)
        t = Task(name="alpha", prompt="p")
        assert t.former_names == []
        t.former_names = ["was-alpha"]
        store.put_task(t)
        assert store.get_task("alpha").former_names == ["was-alpha"]

    def test_from_dict_without_the_key_loads_empty(self) -> None:
        """A store written before the field existed still loads."""
        t = Task.from_dict({"name": "old", "prompt": "p", "status": "WORKING"})
        assert t.former_names == []

    def test_each_task_gets_its_own_list(self) -> None:
        """The default must not be shared between tasks."""
        a, b = Task(name="a", prompt="p"), Task(name="b", prompt="p")
        a.former_names.append("was-a")
        assert b.former_names == []


class TestRenameRecordsTheOldName:
    @pytest.fixture()
    def store(self, tmp_workdir: Path) -> Store:
        return Store(tmp_workdir)

    def _seed(self, store: Store, name: str) -> None:
        store.put_task(Task(name=name, prompt="p"))

    def test_rename_appends_the_outgoing_name(self, store: Store) -> None:
        self._seed(store, "alpha")
        store.rename_task("alpha", "beta")
        assert store.get_task("beta").former_names == ["alpha"]

    def test_repeated_renames_chain_oldest_first(self, store: Store) -> None:
        self._seed(store, "alpha")
        store.rename_task("alpha", "beta")
        store.rename_task("beta", "gamma")
        assert store.get_task("gamma").former_names == ["alpha", "beta"]

    def test_renaming_back_records_the_round_trip(self, store: Store) -> None:
        """The history is appended to, never rewritten, so a return shows."""
        self._seed(store, "alpha")
        store.rename_task("alpha", "beta")
        store.rename_task("beta", "alpha")
        assert store.get_task("alpha").former_names == ["alpha", "beta"]

    def test_an_unrenamed_task_has_no_history(self, store: Store) -> None:
        self._seed(store, "alpha")
        assert store.get_task("alpha").former_names == []

    def test_a_branched_child_starts_its_own_history(self, store: Store) -> None:
        """A child is a new task under a new name, not a rename of its parent."""
        self._seed(store, "alpha")
        store.rename_task("alpha", "beta")
        child = store.branch_task(
            store.get_task("beta"), "kid",
            alias="ss", task_hash="abcd1234", now="2026-09-13T00:00:00+00:00",
        )
        assert child.former_names == []
        assert store.get_task("beta").former_names == ["alpha"]

    def test_the_history_survives_a_reload(self, store: Store, tmp_workdir: Path) -> None:
        self._seed(store, "alpha")
        store.rename_task("alpha", "beta")
        assert Store(tmp_workdir).get_task("beta").former_names == ["alpha"]


# ── the listing rows carry it ───────────────────────────────────────────


@pytest.fixture()
def ilan_server(tmp_workdir: Path, tmp_config: Path, env_with_mock_claude: None):
    import ilan.config as cfg_mod

    cfg_mod.save({**cfg_mod.DEFAULTS, "workdir": str(tmp_workdir)})

    server = IlanServer()
    server.runner.start = lambda task: True  # type: ignore[method-assign]
    server.runner.reap_finished = lambda: None  # type: ignore[method-assign]

    with patch.object(signal, "signal"):
        t = threading.Thread(
            target=server.run,
            kwargs={"host": "127.0.0.1", "port": 0, "poll_interval": SERVE_POLL_INTERVAL},
            daemon=True,
        )
        t.start()

        port = wait_until_serving(server)
        server._test_url = f"http://127.0.0.1:{port}"  # type: ignore[attr-defined]

        yield server

        server.shutdown()
        t.join(timeout=3)


def _list(server: IlanServer) -> list[dict]:
    url = f"{server._test_url}/tasks"  # type: ignore[attr-defined]
    with urlopen(url, timeout=5) as resp:
        return json.loads(resp.read())["tasks"]


def _rename(server: IlanServer, name: str, new_name: str) -> tuple[int, dict]:
    url = f"{server._test_url}/tasks/{name}/rename"  # type: ignore[attr-defined]
    req = Request(url, data=json.dumps({"new_name": new_name}).encode(), method="POST")
    req.add_header("Content-Type", "application/json")
    try:
        with urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read())
    except HTTPError as exc:
        return exc.code, json.loads(exc.read())


class TestListingRowsCarryTheHistory:
    def test_row_has_the_field_even_when_empty(self, ilan_server: IlanServer) -> None:
        ilan_server.store.put_task(Task(name="alpha", prompt="p"))
        assert _list(ilan_server)[0]["former_names"] == []

    def test_row_reports_the_chain_after_a_rename(self, ilan_server: IlanServer) -> None:
        ilan_server.store.put_task(Task(name="alpha", prompt="p"))
        assert _rename(ilan_server, "alpha", "beta")[0] == 200
        assert _rename(ilan_server, "beta", "gamma")[0] == 200
        row = _list(ilan_server)[0]
        assert row["name"] == "gamma"
        assert row["former_names"] == ["alpha", "beta"]


# ── field builders ──────────────────────────────────────────────────────


class TestNameHistory:
    def test_none_when_never_renamed(self) -> None:
        assert _build_name_history(_row("alpha")) is None

    def test_none_when_the_key_is_absent(self) -> None:
        row = _row("alpha")
        del row["former_names"]
        assert _build_name_history(row) is None

    def test_chain_ends_with_the_current_name(self) -> None:
        history = _build_name_history(_row("gamma", former_names=["alpha", "beta"]))
        assert history is not None
        assert history.plain == f"alpha{RENAME_ARROW}beta{RENAME_ARROW}gamma"

    def test_blank_entries_are_dropped(self) -> None:
        history = _build_name_history(_row("beta", former_names=["", "alpha"]))
        assert history is not None
        assert history.plain == f"alpha{RENAME_ARROW}beta"


class TestInfoValues:
    def test_text_is_stripped(self) -> None:
        assert _info_value("  hello  ").plain == "hello"

    @pytest.mark.parametrize("empty", [None, "", "   "])
    def test_missing_values_render_a_dash(self, empty: str | None) -> None:
        assert _info_value(empty).plain == INFO_EMPTY

    def test_timestamp_is_formatted_in_the_configured_zone(self, tmp_config) -> None:
        assert _info_timestamp("2026-09-13T18:04:05+00:00").plain.endswith(":04:05 PDT")

    def test_missing_timestamp_renders_a_dash(self, tmp_config) -> None:
        assert _info_timestamp(None).plain == INFO_EMPTY
        assert _info_timestamp("").plain == INFO_EMPTY


def _grid_rows(row: dict) -> list[tuple[str, str]]:
    """Build the info grid and return its ``(label, value)`` pairs.

    Labels are plain strings and values are ``Text``, so both are read
    through ``str`` rather than assuming either shape.
    """
    labels, values = _build_info_grid(row).columns
    return [
        (str(label).strip(), (value.plain if isinstance(value, Text) else str(value)).strip())
        for label, value in zip(labels._cells, values._cells)
    ]


def _value_cell(row: dict, label: str) -> Text:
    """The ``Text`` the info grid holds in the value column of *label*'s row."""
    labels, values = _build_info_grid(row).columns
    index = [str(cell).strip() for cell in labels._cells].index(label)
    cell = values._cells[index]
    assert isinstance(cell, Text)
    return cell


class TestInfoGrid:
    def test_labels_and_their_order(self, tmp_config) -> None:
        row = _row("alpha", hour=1, notes="a note", summary="a summary")
        assert [label for label, _ in _grid_rows(row)] == [
            "Status", "Summary", "Notes", "Created", "Last Changed",
        ]

    def test_name_history_leads_when_the_task_was_renamed(self, tmp_config) -> None:
        row = _row("beta", hour=1, former_names=["alpha"])
        assert [label for label, _ in _grid_rows(row)][0] == "Name history"

    def test_every_requested_fact_is_shown(self, tmp_config) -> None:
        row = _row(
            "beta", hour=1, status="NEEDS_ATTENTION", former_names=["alpha"],
            notes="check the CI matrix", summary="narrowed it to the cache",
        )
        fields = dict(_grid_rows(row))
        assert fields["Name history"] == f"alpha{RENAME_ARROW}beta"
        assert fields["Status"] == "NEEDS_ATTENTION"
        assert fields["Notes"] == "check the CI matrix"
        assert fields["Summary"] == "narrowed it to the cache"
        assert fields["Created"].endswith("PDT")
        assert fields["Last Changed"].endswith("PDT")

    def test_absent_note_and_summary_render_a_dash(self, tmp_config) -> None:
        fields = dict(_grid_rows(_row("alpha", hour=1)))
        assert fields["Notes"] == fields["Summary"] == INFO_EMPTY

    def test_status_carries_its_elapsed_hint_but_not_the_summary(
        self, tmp_config,
    ) -> None:
        """The summary has a field of its own, so it must not be doubled up."""
        row = _row("alpha", hour=1, summary="a summary")
        status = dict(_grid_rows(row))["Status"]
        assert status.startswith("WORKING (for ")
        assert "a summary" not in status

    def test_the_note_keeps_its_listing_style(self, tmp_config) -> None:
        row = _row("alpha", hour=1, notes="a note")
        assert _value_cell(row, "Notes").style == NOTES_STYLE

    def test_the_summary_keeps_its_listing_style(self, tmp_config) -> None:
        """The same style the Status cell gives it, so the two cannot drift."""
        row = _row("alpha", hour=1, summary="a summary")
        assert _value_cell(row, "Summary").style == ONE_LINER_STYLE


class TestNameLabelSplit:
    """``_build_name_cell`` is the label plus the note; ``info`` uses the label."""

    def test_label_omits_the_note(self) -> None:
        row = _row("alpha", alias="aa", notes="a note")
        assert "a note" not in _build_name_label(row).plain

    def test_cell_still_carries_the_note_beneath_the_name(self) -> None:
        row = _row("alpha", alias="aa", notes="a note")
        assert _build_name_cell(row).plain == "(aa) alpha\na note"

    def test_cell_is_the_label_when_there_is_no_note(self) -> None:
        row = _row("alpha", alias="aa")
        assert _build_name_cell(row).plain == _build_name_label(row).plain


# ── ilan task info ──────────────────────────────────────────────────────


@pytest.fixture()
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture()
def wide_console(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep Rich from wrapping the grid or the tree mid-assert."""
    monkeypatch.setattr(cli_mod, "console", Console(width=200, force_terminal=False))


def _invoke(runner: CliRunner, rows: list[dict], *args: str):
    client = MagicMock()
    client.list_tasks.return_value = {"tasks": rows}
    with patch("ilan.cli._client", return_value=client):
        result = runner.invoke(main, list(args))
    return result, client


class TestInfoCommand:
    def _family(self) -> list[dict]:
        return [
            _row("investigate-oom", hour=1, alias="aa", status="NEEDS_ATTENTION"),
            _row("oom-retry-a", "investigate-oom", hour=2, alias="ss", status="DONE"),
            _row(
                "oom-retry-a-fix", "oom-retry-a", hour=3, alias="ff",
                former_names=["oom-fix"], notes="check the CI matrix",
                summary="narrowed it to the tokenizer cache",
            ),
        ]

    def test_shows_every_requested_fact(
        self, runner: CliRunner, tmp_config, wide_console,
    ) -> None:
        result, client = _invoke(runner, self._family(), "task", "info", "oom-retry-a-fix")
        assert result.exit_code == 0
        out = _strip_ansi(result.output)
        # Terminal tasks belong in the tree even though `ls` hides them.
        client.list_tasks.assert_called_once_with(show_all=True)
        assert out.splitlines()[0].strip() == "(ff) oom-retry-a-fix"
        assert f"oom-fix{RENAME_ARROW}oom-retry-a-fix" in out
        assert "WORKING" in out
        assert "check the CI matrix" in out
        assert "narrowed it to the tokenizer cache" in out
        assert out.count("Created") == 1
        assert out.count("Last Changed") == 1

    def test_draws_the_branch_tree_beneath_the_fields(
        self, runner: CliRunner, tmp_config, wide_console,
    ) -> None:
        result, _ = _invoke(runner, self._family(), "info", "oom-retry-a-fix")
        assert result.exit_code == 0
        out = _strip_ansi(result.output)
        assert out.index("Last Changed") < out.index("Branch tree")
        # The whole family is drawn, from the outermost ancestor down.
        assert "(aa) investigate-oom" in out
        assert "(ss) oom-retry-a" in out
        assert out.count("← this task") == 1
        assert "oom-retry-a-fix" in out.split("← this task")[0].splitlines()[-1]

    def test_indents_children_under_their_parent(
        self, runner: CliRunner, tmp_config, wide_console,
    ) -> None:
        result, _ = _invoke(runner, self._family(), "info", "investigate-oom")
        out = _strip_ansi(result.output)
        tree = out.split("Branch tree")[1]
        offsets = {
            name: next(line.index(name) for line in tree.splitlines() if name in line)
            for name in ("investigate-oom", "oom-retry-a", "oom-retry-a-fix")
        }
        assert (
            offsets["investigate-oom"] < offsets["oom-retry-a"] < offsets["oom-retry-a-fix"]
        )

    def test_a_task_with_no_relatives_still_gets_a_tree(
        self, runner: CliRunner, tmp_config, wide_console,
    ) -> None:
        result, _ = _invoke(runner, [_row("lonely", hour=1)], "info", "lonely")
        assert result.exit_code == 0
        tree = _strip_ansi(result.output).split("Branch tree")[1]
        assert "lonely" in tree
        assert "← this task" in tree

    def test_a_deleted_ancestor_is_a_tombstone(
        self, runner: CliRunner, tmp_config, wide_console,
    ) -> None:
        rows = [
            _row("root", hour=1),
            _row("orphan", "root", hour=2, deleted_ancestors=["gone-kid"]),
        ]
        result, _ = _invoke(runner, rows, "info", "orphan")
        assert result.exit_code == 0
        assert "gone-kid (deleted)" in _strip_ansi(result.output)

    def test_no_name_history_row_when_never_renamed(
        self, runner: CliRunner, tmp_config, wide_console,
    ) -> None:
        result, _ = _invoke(runner, [_row("alpha", hour=1)], "info", "alpha")
        assert "Name history" not in _strip_ansi(result.output)

    def test_note_is_not_repeated_under_the_name(
        self, runner: CliRunner, tmp_config, wide_console,
    ) -> None:
        """The note has a field; the heading must not carry a copy of it."""
        rows = [_row("alpha", hour=1, notes="check the CI matrix")]
        result, _ = _invoke(runner, rows, "info", "alpha")
        assert _strip_ansi(result.output).count("check the CI matrix") == 1

    def test_accepts_an_alias(
        self, runner: CliRunner, tmp_config, wide_console,
    ) -> None:
        result, _ = _invoke(runner, self._family(), "info", "ff")
        assert result.exit_code == 0
        assert _strip_ansi(result.output).splitlines()[0].strip() == "(ff) oom-retry-a-fix"

    def test_name_wins_over_a_colliding_alias(
        self, runner: CliRunner, tmp_config, wide_console,
    ) -> None:
        rows = [_row("aa", hour=1), _row("other", hour=2, alias="aa")]
        result, _ = _invoke(runner, rows, "info", "aa")
        assert result.exit_code == 0
        assert _strip_ansi(result.output).splitlines()[0].strip() == "aa"

    def test_unknown_task_exits_nonzero(
        self, runner: CliRunner, tmp_config, wide_console,
    ) -> None:
        result, _ = _invoke(runner, [_row("alpha", hour=1)], "info", "nope")
        assert result.exit_code == 1
        assert "not found" in _strip_ansi(result.output)

    def test_works_on_a_closed_task(
        self, runner: CliRunner, tmp_config, wide_console,
    ) -> None:
        rows = [_row("alpha", hour=1, status="DISCARDED")]
        result, _ = _invoke(runner, rows, "info", "alpha")
        assert result.exit_code == 0
        assert "DISCARDED" in _strip_ansi(result.output)

    def test_shorthand_matches_the_task_subcommand(
        self, runner: CliRunner, tmp_config, wide_console,
    ) -> None:
        rows = self._family()
        long_form, _ = _invoke(runner, rows, "task", "info", "oom-retry-a-fix")
        short_form, _ = _invoke(runner, rows, "info", "oom-retry-a-fix")
        assert short_form.exit_code == long_form.exit_code == 0
        assert short_form.output == long_form.output


# ── the retired tree command ────────────────────────────────────────────


class TestTreeCommandIsRetired:
    """``ilan tree`` was folded into ``ilan info``; neither spelling remains."""

    @pytest.mark.parametrize("argv", [["tree", "alpha"], ["task", "tree", "alpha"]])
    def test_the_command_is_gone(
        self, runner: CliRunner, tmp_config, argv: list[str],
    ) -> None:
        result, client = _invoke(runner, [_row("alpha", hour=1)], *argv)
        assert result.exit_code == 2
        assert "No such command" in result.output
        # Click rejects it before any request is made.
        client.list_tasks.assert_not_called()

    @pytest.mark.parametrize("group", [["--help"], ["task", "--help"]])
    def test_the_help_no_longer_lists_it(
        self, runner: CliRunner, tmp_config, group: list[str],
    ) -> None:
        result, _ = _invoke(runner, [], *group)
        assert result.exit_code == 0
        commands = _strip_ansi(result.output).split("Commands:")[1]
        assert "\n  tree" not in commands
        assert "\n  info" in commands
