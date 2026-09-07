"""Tests for ``ilan task notes`` — model, server endpoint, CLI, and columns."""

from __future__ import annotations

import json
import re
import signal
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

import pytest
from click.testing import CliRunner
from rich.console import Console

import ilan.cli as cli_mod
from ilan.cli import (
    NOTES_COLUMN_WIDTH,
    NOTES_STYLE,
    TIMESTAMP_COLUMN_WIDTH,
    _build_dashboard_table,
    _build_notes_cell,
    main,
)
from ilan.models import Task, TaskStatus
from ilan.server import IlanServer
from ilan.store import Store

from tests.helpers import SERVE_POLL_INTERVAL, wait_until_serving


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m|\x1b\]8;[^\x1b]*\x1b\\")


def _strip_ansi(s: str) -> str:
    return _ANSI_RE.sub("", s)


_TZ = ZoneInfo("US/Pacific")


# ── model / store ───────────────────────────────────────────────────────


class TestNotesField:
    def test_defaults_to_none_and_round_trips(self, tmp_workdir: Path) -> None:
        store = Store(tmp_workdir)
        store.put_task(Task(name="plain", prompt="p"))
        store.put_task(Task(name="annotated", prompt="p", notes="why this exists"))

        tasks = store.load_tasks()
        assert tasks["plain"].notes is None
        assert tasks["annotated"].notes == "why this exists"

    def test_from_dict_without_notes_defaults_none(self) -> None:
        # Tasks saved before this field existed must still load.
        t = Task.from_dict({"name": "old", "prompt": "p", "status": "WORKING"})
        assert t.notes is None

    def test_to_dict_carries_notes(self) -> None:
        assert Task(name="t", prompt="p", notes="n").to_dict()["notes"] == "n"

    def test_branched_child_does_not_inherit_the_note(self, tmp_workdir: Path) -> None:
        """The child diverges immediately, so the parent's note would mislead.

        Same treatment as ``summary_one_liner``: what the parent is about is
        not what the child was branched off to do.
        """
        store = Store(tmp_workdir)
        parent = Task(name="parent", prompt="p", notes="parent's reminder")
        store.put_task(parent)
        child = store.branch_task(
            parent, "child",
            alias="cc", task_hash="1111aaaa", now="2026-01-01T00:00:00+00:00",
        )
        assert child.notes is None
        assert store.get_task("parent").notes == "parent's reminder"


# ── server endpoint ─────────────────────────────────────────────────────


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


def _post(server: IlanServer, path: str, body: dict) -> tuple[int, dict]:
    req = Request(
        f"{server._test_url}{path}",  # type: ignore[attr-defined]
        data=json.dumps(body).encode(),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read())
    except HTTPError as exc:
        return exc.code, json.loads(exc.read())


def _list(server: IlanServer, *, show_all: bool = False) -> list[dict]:
    url = f"{server._test_url}/tasks" + ("?all=true" if show_all else "")  # type: ignore[attr-defined]
    with urlopen(url, timeout=5) as resp:
        return json.loads(resp.read())["tasks"]


def _seed(
    server: IlanServer,
    name: str,
    *,
    alias: str | None = None,
    notes: str | None = None,
    status: TaskStatus = TaskStatus.WORKING,
) -> None:
    ts = "2026-07-29T00:00:00+00:00"
    server.store.put_task(Task(
        name=name, prompt="p", status=status, created_at=ts, status_changed_at=ts,
        alias=alias, notes=notes,
    ))


class TestNotesEndpoint:
    def test_sets_the_note(self, ilan_server: IlanServer) -> None:
        _seed(ilan_server, "alpha")
        code, body = _post(ilan_server, "/tasks/alpha/notes", {"notes": "ship by Friday"})
        assert code == 200
        assert body == {"ok": True, "name": "alpha", "notes": "ship by Friday"}
        assert ilan_server.store.get_task("alpha").notes == "ship by Friday"

    def test_replaces_rather_than_appends(self, ilan_server: IlanServer) -> None:
        """Rewriting a note is how you correct one, so the old text must go."""
        _seed(ilan_server, "alpha", notes="first")
        _post(ilan_server, "/tasks/alpha/notes", {"notes": "second"})
        assert ilan_server.store.get_task("alpha").notes == "second"

    def test_empty_note_clears_it(self, ilan_server: IlanServer) -> None:
        _seed(ilan_server, "alpha", notes="stale")
        code, body = _post(ilan_server, "/tasks/alpha/notes", {"notes": ""})
        assert code == 200
        assert body["notes"] is None
        assert ilan_server.store.get_task("alpha").notes is None

    def test_whitespace_only_note_clears_it(self, ilan_server: IlanServer) -> None:
        """`ilan notes t "   "` reads as "remove it", not as a blank note."""
        _seed(ilan_server, "alpha", notes="stale")
        code, body = _post(ilan_server, "/tasks/alpha/notes", {"notes": "   \n "})
        assert code == 200
        assert body["notes"] is None

    def test_surrounding_whitespace_is_stripped(self, ilan_server: IlanServer) -> None:
        _seed(ilan_server, "alpha")
        _, body = _post(ilan_server, "/tasks/alpha/notes", {"notes": "  padded  "})
        assert body["notes"] == "padded"

    def test_multiline_note_is_kept_verbatim(self, ilan_server: IlanServer) -> None:
        _seed(ilan_server, "alpha")
        _, body = _post(ilan_server, "/tasks/alpha/notes", {"notes": "one\ntwo"})
        assert body["notes"] == "one\ntwo"

    def test_accepts_an_alias(self, ilan_server: IlanServer) -> None:
        _seed(ilan_server, "alpha", alias="aa")
        code, body = _post(ilan_server, "/tasks/aa/notes", {"notes": "via alias"})
        assert code == 200
        assert body["name"] == "alpha"
        assert ilan_server.store.get_task("alpha").notes == "via alias"

    def test_unknown_task_is_404(self, ilan_server: IlanServer) -> None:
        code, body = _post(ilan_server, "/tasks/nope/notes", {"notes": "x"})
        assert code == 404
        assert "not found" in body["error"]

    def test_a_closed_task_can_still_be_annotated(self, ilan_server: IlanServer) -> None:
        """Unlike the alias, a note is not frozen when the task closes.

        Writing down what a finished task was about is exactly the case this
        command exists for.
        """
        _seed(ilan_server, "alpha", status=TaskStatus.DONE)
        code, body = _post(ilan_server, "/tasks/alpha/notes", {"notes": "shipped in #412"})
        assert code == 200
        assert ilan_server.store.get_task("alpha").notes == "shipped in #412"

    def test_missing_body_field_clears_rather_than_500s(
        self, ilan_server: IlanServer,
    ) -> None:
        _seed(ilan_server, "alpha", notes="stale")
        code, body = _post(ilan_server, "/tasks/alpha/notes", {})
        assert code == 200
        assert body["notes"] is None

    def test_list_payload_carries_notes(self, ilan_server: IlanServer) -> None:
        _seed(ilan_server, "alpha", notes="in the payload")
        _seed(ilan_server, "beta")
        rows = {r["name"]: r for r in _list(ilan_server)}
        assert rows["alpha"]["notes"] == "in the payload"
        assert rows["beta"]["notes"] is None


class TestNotesSurviveClosing:
    """A note outlives the task's working life.

    ``done`` drops the alias and ``discard`` drops the unread marker, so a
    task losing state on close is the norm here; the note must be the
    exception. It is the record of what the task was for, which is exactly
    what you want left over once the work itself is finished.
    """

    def _post_plain(self, server: IlanServer, path: str) -> int:
        req = Request(f"{server._test_url}{path}", method="POST")  # type: ignore[attr-defined]
        try:
            with urlopen(req, timeout=5) as resp:
                return resp.status
        except HTTPError as exc:
            return exc.code

    def test_done_keeps_the_note(self, ilan_server: IlanServer) -> None:
        _seed(ilan_server, "alpha", alias="aa", notes="what this was for")
        assert self._post_plain(ilan_server, "/tasks/alpha/done") == 200
        task = ilan_server.store.get_task("alpha")
        assert task.status is TaskStatus.DONE
        assert task.notes == "what this was for"
        # The alias *is* dropped on done, which is what makes the note's
        # survival a deliberate difference rather than an accident.
        assert task.alias is None

    def test_discard_keeps_the_note(self, ilan_server: IlanServer) -> None:
        _seed(ilan_server, "alpha", alias="aa", notes="why this was dropped")
        assert self._post_plain(ilan_server, "/tasks/alpha/discard") == 200
        task = ilan_server.store.get_task("alpha")
        assert task.status is TaskStatus.DISCARDED
        assert task.notes == "why this was dropped"

    def test_note_survives_a_done_undone_round_trip(
        self, ilan_server: IlanServer,
    ) -> None:
        _seed(ilan_server, "alpha", alias="aa", notes="still relevant")
        self._post_plain(ilan_server, "/tasks/alpha/done")
        assert self._post_plain(ilan_server, "/tasks/alpha/undone") == 200
        task = ilan_server.store.get_task("alpha")
        assert task.status is TaskStatus.NEEDS_ATTENTION
        assert task.notes == "still relevant"

    def test_note_survives_a_discard_undiscard_round_trip(
        self, ilan_server: IlanServer,
    ) -> None:
        _seed(ilan_server, "alpha", alias="aa", notes="still relevant")
        self._post_plain(ilan_server, "/tasks/alpha/discard")
        assert self._post_plain(ilan_server, "/tasks/alpha/undiscard") == 200
        assert ilan_server.store.get_task("alpha").notes == "still relevant"

    def test_closed_tasks_carry_their_notes_into_ls_dash_a(
        self, ilan_server: IlanServer,
    ) -> None:
        """``ilan ls -a`` is where a closed task's note is actually read."""
        _seed(ilan_server, "alpha", alias="aa", notes="shipped in #412")
        self._post_plain(ilan_server, "/tasks/alpha/done")
        assert _list(ilan_server) == []  # hidden without -a
        rows = {r["name"]: r for r in _list(ilan_server, show_all=True)}
        assert rows["alpha"]["status"] == "DONE"
        assert rows["alpha"]["notes"] == "shipped in #412"


# ── CLI ─────────────────────────────────────────────────────────────────


@pytest.fixture()
def runner() -> CliRunner:
    return CliRunner()


# All four spellings are the same command; none may quietly go missing.
_ALL_SPELLINGS = [
    ["notes", "aa", "n"],
    ["note", "aa", "n"],
    ["task", "notes", "aa", "n"],
    ["task", "note", "aa", "n"],
]


class TestNotesCommand:
    @pytest.mark.parametrize("args", _ALL_SPELLINGS)
    def test_every_spelling_hits_the_same_endpoint(
        self, runner: CliRunner, tmp_config, args: list[str],
    ) -> None:
        client = MagicMock()
        client.set_notes.return_value = {"ok": True, "name": "alpha", "notes": "n"}
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, args)
        assert result.exit_code == 0
        client.set_notes.assert_called_once_with("aa", "n")

    def test_reports_the_note_it_set(self, runner: CliRunner, tmp_config) -> None:
        client = MagicMock()
        client.set_notes.return_value = {
            "ok": True, "name": "alpha", "notes": "ship by Friday",
        }
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["notes", "alpha", "ship by Friday"])
        assert result.exit_code == 0
        out = _strip_ansi(result.output)
        assert "alpha" in out
        assert "ship by Friday" in out

    def test_reports_a_clear(self, runner: CliRunner, tmp_config) -> None:
        client = MagicMock()
        client.set_notes.return_value = {"ok": True, "name": "alpha", "notes": None}
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["notes", "alpha", ""])
        assert result.exit_code == 0
        assert "cleared" in _strip_ansi(result.output)
        client.set_notes.assert_called_once_with("alpha", "")

    def test_error_exits_nonzero(self, runner: CliRunner, tmp_config) -> None:
        client = MagicMock()
        client.set_notes.return_value = {"error": "Task nope not found"}
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["notes", "nope", "n"])
        assert result.exit_code == 1
        assert "not found" in _strip_ansi(result.output)

    def test_note_is_required(self, runner: CliRunner, tmp_config) -> None:
        """Without a note there is nothing to do, so this is a usage error."""
        client = MagicMock()
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["notes", "alpha"])
        assert result.exit_code != 0
        client.set_notes.assert_not_called()


class TestClientSetNotes:
    def test_posts_to_the_notes_endpoint(self) -> None:
        from ilan.client import Client

        client = Client()
        with patch.object(Client, "post", return_value={"ok": True}) as post:
            client.set_notes("alpha", "n")
        post.assert_called_once_with("/tasks/alpha/notes", {"notes": "n"})


# ── cell rendering ──────────────────────────────────────────────────────


def _row(name: str = "alpha", *, notes: str | None = None) -> dict:
    row = {
        "name": name,
        "alias": None,
        "status": "WORKING",
        "created_at": "2026-07-29T00:00:00+00:00",
        "status_changed_at": "2026-07-29T00:00:00+00:00",
        "needs_review": False,
        "pinned": False,
    }
    if notes is not None:
        row["notes"] = notes
    return row


class TestNotesCell:
    def test_note_is_light_red(self) -> None:
        cell = _build_notes_cell(_row(notes="remember this"))
        assert cell.plain == "remember this"
        assert len(cell.spans) == 1
        assert str(cell.spans[0].style) == NOTES_STYLE

    def test_light_red_is_a_real_rich_color(self) -> None:
        """``light_red`` is not a Rich color name; ``light_coral`` is the light red."""
        from rich.color import Color

        assert Color.parse(NOTES_STYLE).get_truecolor() == (255, 135, 135)

    def test_style_lives_on_a_span_not_the_base_text(self) -> None:
        """A base style is emitted across the cell's right padding too."""
        cell = _build_notes_cell(_row(notes="remember this"))
        assert str(cell.style) in ("", "none")

    def test_missing_note_renders_empty(self) -> None:
        assert _build_notes_cell(_row()).plain == ""

    def test_blank_note_renders_empty(self) -> None:
        assert _build_notes_cell(_row(notes="   ")).plain == ""

    def test_row_without_the_field_renders(self) -> None:
        # A newer client talking to an older server still renders.
        assert _build_notes_cell({"name": "alpha"}).plain == ""


# ── the Notes column ────────────────────────────────────────────────────


class TestDashboardNotesColumn:
    def test_column_is_present_even_when_nobody_has_a_note(self) -> None:
        """The column is unconditional, so the layout never shifts under you."""
        table = _build_dashboard_table([_row("a")], _TZ)
        assert [c.header for c in table.columns] == [
            "(Alias) Name", "Status", "Created", "Last Changed", "Notes",
        ]

    def test_column_present_with_a_note(self) -> None:
        table = _build_dashboard_table([_row("a", notes="x")], _TZ)
        assert [c.header for c in table.columns] == [
            "(Alias) Name", "Status", "Created", "Last Changed", "Notes",
        ]

    def test_notes_column_is_last(self) -> None:
        table = _build_dashboard_table([_row("a", notes="x")], _TZ)
        assert table.columns[-1].header == "Notes"

    def test_cell_holds_the_note(self) -> None:
        table = _build_dashboard_table([_row("a", notes="the reminder")], _TZ)
        assert table.columns[-1]._cells[0].plain == "the reminder"

    def test_annotated_and_bare_rows_share_the_column(self) -> None:
        rows = [_row("a", notes="only mine"), _row("b")]
        table = _build_dashboard_table(rows, _TZ)
        assert [c.plain for c in table.columns[-1]._cells] == ["only mine", ""]

    def test_narrow_drops_created_but_keeps_notes(self) -> None:
        """The note is the point of the column; ``Created`` is the spare one."""
        table = _build_dashboard_table([_row("a", notes="x")], _TZ, narrow=True)
        assert [c.header for c in table.columns] == [
            "(Alias) Name", "Status", "Last Changed", "Notes",
        ]

    def test_notes_width_is_fixed_not_a_ratio(self) -> None:
        """A ratio would grow the column with the terminal and move the rest."""
        table = _build_dashboard_table([_row("a", notes="x")], _TZ)
        notes = table.columns[-1]
        assert notes.width == NOTES_COLUMN_WIDTH
        assert notes.ratio is None

    def test_timestamp_columns_are_fixed_too(self) -> None:
        """Pinning these is what pays for Notes — see the module constants."""
        table = _build_dashboard_table([_row("a", notes="x")], _TZ)
        created, changed = table.columns[2], table.columns[3]
        assert (created.header, changed.header) == ("Created", "Last Changed")
        assert created.width == changed.width == TIMESTAMP_COLUMN_WIDTH
        assert created.ratio is changed.ratio is None

    def test_only_name_and_status_flex(self) -> None:
        table = _build_dashboard_table([_row("a", notes="x")], _TZ)
        assert [c.ratio for c in table.columns] == [10, 16, None, None, None]

    def test_empty_listing_keeps_the_placeholder_row_aligned(self) -> None:
        table = _build_dashboard_table([], _TZ)
        assert [c.header for c in table.columns][-1] == "Notes"
        assert all(len(c._cells) == 1 for c in table.columns)

    def test_timestamp_width_fits_the_common_stamp_on_one_line(self) -> None:
        """15 fits "Today 13:30 PDT" and "09-05 13:30 PDT" without folding."""
        assert len("Today 13:30 PDT") == TIMESTAMP_COLUMN_WIDTH
        assert len("09-05 13:30 PDT") == TIMESTAMP_COLUMN_WIDTH


def _invoke_ls(
    runner: CliRunner, rows: list[dict], monkeypatch: pytest.MonkeyPatch,
    width: int = 200,
):
    monkeypatch.setattr(cli_mod, "console", Console(width=width, force_terminal=False))
    client = MagicMock()
    client.ensure_server.return_value = {}
    client.version_mismatch = None
    client.is_remote = False
    client.get_config.return_value = {"config": {"api-key-codex": "sk-x"}}
    client.list_tasks.return_value = {"tasks": rows}
    with patch("ilan.cli._client", return_value=client):
        return runner.invoke(main, ["ls"])


class TestLsNotesColumn:
    def test_header_present_even_when_nobody_has_a_note(
        self, runner: CliRunner, tmp_config, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        result = _invoke_ls(runner, [_row("a"), _row("b")], monkeypatch)
        assert result.exit_code == 0
        assert "Notes" in _strip_ansi(result.output)

    def test_header_and_note_shown(
        self, runner: CliRunner, tmp_config, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        result = _invoke_ls(
            runner, [_row("a", notes="do not forget"), _row("b")], monkeypatch,
        )
        assert result.exit_code == 0
        out = _strip_ansi(result.output)
        assert "Notes" in out
        assert "do not forget" in out

    def test_column_keeps_its_width_whether_or_not_a_note_is_set(
        self, runner: CliRunner, tmp_config, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The point of the fixed width: the table does not move under you.

        Rendering the same tasks with and without notes must produce rules of
        exactly the same shape, so writing a note never shifts the columns to
        its left.
        """
        bare = _invoke_ls(runner, [_row("a"), _row("b")], monkeypatch)
        annotated = _invoke_ls(
            runner, [_row("a", notes="a note"), _row("b")], monkeypatch,
        )
        def rule(out: str) -> str:
            return next(l for l in _strip_ansi(out).splitlines() if l.startswith("┏"))
        assert rule(bare.output) == rule(annotated.output)

    def test_a_long_note_does_not_widen_the_column(
        self, runner: CliRunner, tmp_config, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        short = _invoke_ls(runner, [_row("a", notes="hi")], monkeypatch)
        long = _invoke_ls(
            runner, [_row("a", notes="hi " * 60)], monkeypatch,
        )
        def rule(out: str) -> str:
            return next(l for l in _strip_ansi(out).splitlines() if l.startswith("┏"))
        assert rule(short.output) == rule(long.output)

    def test_note_renders_in_light_red(
        self, runner: CliRunner, tmp_config, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            cli_mod, "console",
            Console(width=200, force_terminal=True, color_system="256", no_color=False),
        )
        client = MagicMock()
        client.ensure_server.return_value = {}
        client.version_mismatch = None
        client.is_remote = False
        client.get_config.return_value = {"config": {"api-key-codex": "sk-x"}}
        client.list_tasks.return_value = {"tasks": [_row("a", notes="in red")]}
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["ls"])
        assert result.exit_code == 0
        # light_coral is 256-colour 210, so the note opens an SGR 38;5;210 run.
        assert "\x1b[38;5;210min red\x1b[0m" in result.output

    def test_concise_view_has_no_notes(
        self, runner: CliRunner, tmp_config, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``-c`` is one greppable line per task; a note would break that shape."""
        monkeypatch.setattr(cli_mod, "console", Console(width=200, force_terminal=False))
        client = MagicMock()
        client.ensure_server.return_value = {}
        client.version_mismatch = None
        client.is_remote = False
        client.list_tasks.return_value = {"tasks": [_row("a", notes="hidden here")]}
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["ls", "-c"])
        assert result.exit_code == 0
        assert _strip_ansi(result.output) == "a WORKING\n"


class TestNotesWidthLeavesRoomForTheRest:
    """A fixed column never yields, so its width has to be chosen, not guessed.

    Widening ``NOTES_COLUMN_WIDTH`` takes the characters straight out of Name
    and Status, the two columns a listing is useless without. These pin the
    ceiling so a future bump has to be a deliberate decision.
    """

    _NARROW_WINDOW = 70

    def _render_ls_at(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, notes_width: int,
    ) -> str:
        monkeypatch.setattr(cli_mod, "NOTES_COLUMN_WIDTH", notes_width)
        row = _row("narrow-task")
        row["status_changed_at"] = "2026-04-13T01:00:00+00:00"
        result = _invoke_ls(runner, [row], monkeypatch, width=self._NARROW_WINDOW)
        assert result.exit_code == 0
        return _strip_ansi(result.output)

    def test_name_survives_beside_notes_on_a_narrow_window(
        self, runner: CliRunner, tmp_config, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        out = self._render_ls_at(runner, monkeypatch, NOTES_COLUMN_WIDTH)
        assert "narrow-task" in out  # not truncated to "narrow-t…"
        assert "Notes" in out

    def test_a_wider_notes_column_would_truncate_the_name(
        self, runner: CliRunner, tmp_config, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Proves the ceiling is real rather than a number nobody measured."""
        out = self._render_ls_at(runner, monkeypatch, NOTES_COLUMN_WIDTH + 2)
        assert "narrow-task" not in out
