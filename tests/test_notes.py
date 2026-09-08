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
    NAME_TO_PAIR,
    NAME_TO_PAIR_PLAIN,
    STATUS_MAX_WIDTH,
    STATUS_TO_NOTES,
    TIMESTAMP_COLUMN_WIDTH,
    _build_dashboard_table,
    _build_notes_cell,
    _build_status_cell,
    main,
)
from ilan.models import (
    MAX_NOTES_LENGTH,
    Task,
    TaskStatus,
    join_notes,
    truncate_notes,
)
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


class TestTruncateNotes:
    """The length rule, as a plain function.

    A note longer than the limit is cut to fit rather than refused: a
    reminder's first sentence is worth more than an error telling the user to
    count characters. Kept out of the dataclass on purpose — like ``name``, a
    note is normalised where user input arrives (the route), not on every
    ``Task`` a test or a migration happens to construct.
    """

    def test_the_limit_is_256(self) -> None:
        assert MAX_NOTES_LENGTH == 256

    def test_a_short_note_is_returned_unchanged(self) -> None:
        assert truncate_notes("a reminder") == "a reminder"

    def test_an_empty_note_stays_empty(self) -> None:
        """Clearing goes through the same path, so it must survive it."""
        assert truncate_notes("") == ""

    def test_exactly_the_limit_is_kept_whole(self) -> None:
        note = "x" * MAX_NOTES_LENGTH
        assert truncate_notes(note) == note

    def test_one_over_the_limit_loses_its_last_character(self) -> None:
        assert truncate_notes("x" * (MAX_NOTES_LENGTH + 1)) == "x" * MAX_NOTES_LENGTH

    def test_the_front_is_what_survives(self) -> None:
        """A reminder says what it is about at the start, not the end."""
        note = "the important part " + "z" * MAX_NOTES_LENGTH
        assert truncate_notes(note).startswith("the important part")

    def test_a_much_longer_note_is_cut_to_the_limit(self) -> None:
        assert len(truncate_notes("x" * 5000)) == MAX_NOTES_LENGTH

    def test_a_cut_landing_on_a_space_leaves_no_trailing_whitespace(self) -> None:
        """Slicing mid-space would otherwise store a note ending in a space."""
        note = "a" * (MAX_NOTES_LENGTH - 1) + " tail"
        cut = truncate_notes(note)
        assert cut == "a" * (MAX_NOTES_LENGTH - 1)
        assert cut == cut.strip()

    def test_it_counts_characters_not_bytes(self) -> None:
        note = "é" * (MAX_NOTES_LENGTH + 10)  # 2 bytes each in UTF-8
        assert len(truncate_notes(note)) == MAX_NOTES_LENGTH


class TestJoinNotes:
    """The separator rule for ``-a``."""

    def test_joins_with_exactly_one_space(self) -> None:
        assert join_notes("first", "second") == "first second"

    def test_appending_to_no_note_just_sets_it(self) -> None:
        """No existing note means no separator, not a leading space."""
        assert join_notes(None, "second") == "second"
        assert join_notes("", "second") == "second"

    def test_both_sides_are_stripped(self) -> None:
        assert join_notes("  first  ", "  second  ") == "first second"

    def test_padding_never_becomes_extra_separators(self) -> None:
        """However the user padded the argument, the join is one space."""
        assert join_notes("first   ", "\n\n  second") == "first second"

    def test_an_empty_addition_leaves_the_note_alone(self) -> None:
        assert join_notes("first", "") == "first"
        assert join_notes("first", "   \n ") == "first"

    def test_both_empty_gives_empty(self) -> None:
        assert join_notes(None, "") == ""

    def test_inner_whitespace_is_untouched(self) -> None:
        """Only the ends are stripped; the note's own shape is the user's."""
        assert join_notes("one\ntwo", "three  four") == "one\ntwo three  four"


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
        assert body == {
            "ok": True, "name": "alpha", "notes": "ship by Friday",
            "truncated": False,
        }
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

    def test_a_note_at_the_limit_is_accepted(self, ilan_server: IlanServer) -> None:
        _seed(ilan_server, "alpha")
        note = "x" * MAX_NOTES_LENGTH
        code, body = _post(ilan_server, "/tasks/alpha/notes", {"notes": note})
        assert code == 200
        assert body["notes"] == note

    def test_a_note_over_the_limit_is_trimmed_not_refused(
        self, ilan_server: IlanServer,
    ) -> None:
        """The note is saved, cut to the limit, and the cut is reported."""
        _seed(ilan_server, "alpha", notes="the old note")
        code, body = _post(
            ilan_server, "/tasks/alpha/notes", {"notes": "x" * (MAX_NOTES_LENGTH + 1)},
        )
        assert code == 200
        assert body["truncated"] is True
        assert body["notes"] == "x" * MAX_NOTES_LENGTH
        assert ilan_server.store.get_task("alpha").notes == "x" * MAX_NOTES_LENGTH

    def test_a_note_at_the_limit_is_not_reported_as_trimmed(
        self, ilan_server: IlanServer,
    ) -> None:
        """``truncated`` must mean something was actually dropped."""
        _seed(ilan_server, "alpha")
        _, body = _post(
            ilan_server, "/tasks/alpha/notes", {"notes": "x" * MAX_NOTES_LENGTH},
        )
        assert body["truncated"] is False

    def test_the_front_of_an_over_long_note_is_what_is_kept(
        self, ilan_server: IlanServer,
    ) -> None:
        _seed(ilan_server, "alpha")
        _, body = _post(
            ilan_server, "/tasks/alpha/notes",
            {"notes": "the important part " + "z" * MAX_NOTES_LENGTH},
        )
        assert body["notes"].startswith("the important part")

    def test_the_limit_is_measured_after_stripping(
        self, ilan_server: IlanServer,
    ) -> None:
        """Padding the user did not intend must not cost part of the budget."""
        _seed(ilan_server, "alpha")
        note = "x" * MAX_NOTES_LENGTH
        code, body = _post(
            ilan_server, "/tasks/alpha/notes", {"notes": f"   {note}   "},
        )
        assert code == 200
        assert body["notes"] == note

    def test_the_limit_counts_characters_not_bytes(
        self, ilan_server: IlanServer,
    ) -> None:
        """A multi-byte character is one character, not two or three."""
        _seed(ilan_server, "alpha")
        note = "é" * MAX_NOTES_LENGTH  # 2 bytes each in UTF-8
        code, body = _post(ilan_server, "/tasks/alpha/notes", {"notes": note})
        assert code == 200
        assert body["notes"] == note


class TestNotesAppendEndpoint:
    """``append`` mode on the route, which is what ``-a`` drives.

    Appending is done server-side rather than read-modify-write in the client
    so it is atomic under the store lock: two appends both land instead of the
    second overwriting the first from a stale read.
    """

    def test_append_joins_with_one_space(self, ilan_server: IlanServer) -> None:
        _seed(ilan_server, "alpha", notes="first")
        code, body = _post(
            ilan_server, "/tasks/alpha/notes", {"notes": "second", "append": True},
        )
        assert code == 200
        assert body["notes"] == "first second"
        assert ilan_server.store.get_task("alpha").notes == "first second"

    def test_append_to_a_task_with_no_note_just_sets_it(
        self, ilan_server: IlanServer,
    ) -> None:
        _seed(ilan_server, "alpha")
        _, body = _post(
            ilan_server, "/tasks/alpha/notes", {"notes": "first", "append": True},
        )
        assert body["notes"] == "first"  # no leading space

    def test_append_strips_the_fragment(self, ilan_server: IlanServer) -> None:
        _seed(ilan_server, "alpha", notes="first")
        _, body = _post(
            ilan_server, "/tasks/alpha/notes",
            {"notes": "  \n second \n ", "append": True},
        )
        assert body["notes"] == "first second"

    def test_append_normalises_a_padded_existing_note(
        self, ilan_server: IlanServer,
    ) -> None:
        """A note stored padded by an older write is stripped on the way out."""
        _seed(ilan_server, "alpha", notes="  first  ")
        _, body = _post(
            ilan_server, "/tasks/alpha/notes", {"notes": "second", "append": True},
        )
        assert body["notes"] == "first second"

    def test_repeated_appends_accumulate(self, ilan_server: IlanServer) -> None:
        _seed(ilan_server, "alpha")
        for word in ("one", "two", "three"):
            _post(ilan_server, "/tasks/alpha/notes", {"notes": word, "append": True})
        assert ilan_server.store.get_task("alpha").notes == "one two three"

    def test_an_empty_append_leaves_the_note_alone(
        self, ilan_server: IlanServer,
    ) -> None:
        """Unlike a bare write, an empty append is a no-op, not a clear."""
        _seed(ilan_server, "alpha", notes="keep me")
        code, body = _post(
            ilan_server, "/tasks/alpha/notes", {"notes": "   ", "append": True},
        )
        assert code == 200
        assert body["notes"] == "keep me"

    def test_append_is_trimmed_on_the_combined_length(
        self, ilan_server: IlanServer,
    ) -> None:
        """The limit applies to the joined note, and the tail of it is cut."""
        existing = "x" * (MAX_NOTES_LENGTH - 2)
        _seed(ilan_server, "alpha", notes=existing)
        # existing + " " + "yy" is one character over, so the last y goes.
        code, body = _post(
            ilan_server, "/tasks/alpha/notes", {"notes": "yy", "append": True},
        )
        assert code == 200
        assert body["truncated"] is True
        assert body["notes"] == f"{existing} y"
        assert len(body["notes"]) == MAX_NOTES_LENGTH

    def test_appending_to_a_full_note_keeps_the_note_it_had(
        self, ilan_server: IlanServer,
    ) -> None:
        """Nothing fits, so the note is unchanged rather than mangled."""
        existing = "x" * MAX_NOTES_LENGTH
        _seed(ilan_server, "alpha", notes=existing)
        _, body = _post(
            ilan_server, "/tasks/alpha/notes", {"notes": "more", "append": True},
        )
        assert body["truncated"] is True
        assert body["notes"] == existing

    def test_an_append_that_exactly_fills_the_limit_is_accepted(
        self, ilan_server: IlanServer,
    ) -> None:
        existing = "x" * (MAX_NOTES_LENGTH - 2)
        _seed(ilan_server, "alpha", notes=existing)
        code, body = _post(
            ilan_server, "/tasks/alpha/notes", {"notes": "y", "append": True},
        )
        assert code == 200
        assert body["notes"] == f"{existing} y"
        assert len(body["notes"]) == MAX_NOTES_LENGTH

    def test_append_accepts_an_alias(self, ilan_server: IlanServer) -> None:
        _seed(ilan_server, "alpha", alias="aa", notes="first")
        code, body = _post(
            ilan_server, "/tasks/aa/notes", {"notes": "second", "append": True},
        )
        assert code == 200
        assert body["name"] == "alpha"

    def test_append_on_an_unknown_task_is_404(self, ilan_server: IlanServer) -> None:
        code, _ = _post(
            ilan_server, "/tasks/nope/notes", {"notes": "x", "append": True},
        )
        assert code == 404

    def test_a_falsey_append_flag_still_replaces(
        self, ilan_server: IlanServer,
    ) -> None:
        """An older client omits the key entirely; that must mean replace."""
        _seed(ilan_server, "alpha", notes="first")
        _, body = _post(
            ilan_server, "/tasks/alpha/notes", {"notes": "second", "append": False},
        )
        assert body["notes"] == "second"

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
        client.set_notes.assert_called_once_with("aa", "n", append=False)

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
        client.set_notes.assert_called_once_with("alpha", "", append=False)

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

    def test_an_over_long_note_surfaces_the_server_error(
        self, runner: CliRunner, tmp_config,
    ) -> None:
        """The server owns the rule; the CLI just reports what it says.

        No client-side length check, matching ``alias``: one authority means
        the web app and any future client get the same answer.
        """
        client = MagicMock()
        client.set_notes.return_value = {
            "error": f"Note is 200 characters; the limit is {MAX_NOTES_LENGTH}."
        }
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["notes", "alpha", "x" * 200])
        assert result.exit_code == 1
        out = _strip_ansi(result.output)
        assert str(MAX_NOTES_LENGTH) in out
        # It is still sent: the client does not second-guess the limit.
        client.set_notes.assert_called_once_with("alpha", "x" * 200, append=False)

    @pytest.mark.parametrize("args", [["notes", "--help"], ["task", "notes", "--help"]])
    def test_help_quotes_the_real_limit(
        self, runner: CliRunner, tmp_config, args: list[str],
    ) -> None:
        result = runner.invoke(main, args)
        assert result.exit_code == 0
        assert str(MAX_NOTES_LENGTH) in _strip_ansi(result.output)

    @pytest.mark.parametrize("args", [["notes", "--help"], ["task", "notes", "--help"]])
    def test_help_mentions_both_flags(
        self, runner: CliRunner, tmp_config, args: list[str],
    ) -> None:
        out = _strip_ansi(runner.invoke(main, args).output)
        assert "-a" in out
        assert "-c" in out


def _invoke_notes(runner: CliRunner, argv: list[str], notes: str | None = "RESULT"):
    """Run a notes command against a mock client; return (result, mock)."""
    client = MagicMock()
    client.set_notes.return_value = {"ok": True, "name": "alpha", "notes": notes}
    with patch("ilan.cli._client", return_value=client):
        result = runner.invoke(main, argv)
    return result, client


# Every spelling has to grow the flags, not just the canonical one.
_NOTES_PREFIXES = [["notes"], ["note"], ["task", "notes"], ["task", "note"]]


class TestNotesAppendFlag:
    @pytest.mark.parametrize("prefix", _NOTES_PREFIXES)
    def test_append_sends_the_text_with_the_append_flag(
        self, runner: CliRunner, tmp_config, prefix: list[str],
    ) -> None:
        result, client = _invoke_notes(runner, [*prefix, "alpha", "-a", "more"])
        assert result.exit_code == 0
        client.set_notes.assert_called_once_with("alpha", "more", append=True)

    def test_long_form_works_too(self, runner: CliRunner, tmp_config) -> None:
        _, client = _invoke_notes(runner, ["notes", "alpha", "--append", "more"])
        client.set_notes.assert_called_once_with("alpha", "more", append=True)

    def test_the_appended_text_is_stripped_before_it_is_sent(
        self, runner: CliRunner, tmp_config,
    ) -> None:
        _, client = _invoke_notes(runner, ["notes", "alpha", "-a", "  \n more \n "])
        client.set_notes.assert_called_once_with("alpha", "more", append=True)

    def test_it_reports_the_whole_note_not_the_fragment(
        self, runner: CliRunner, tmp_config,
    ) -> None:
        """After an append what matters is what the note now says."""
        result, _ = _invoke_notes(
            runner, ["notes", "alpha", "-a", "more"], notes="first more",
        )
        out = _strip_ansi(result.output)
        assert "now reads" in out
        assert "first more" in out

    def test_a_server_rejection_exits_nonzero(
        self, runner: CliRunner, tmp_config,
    ) -> None:
        client = MagicMock()
        client.set_notes.return_value = {
            "error": f"Appending would make the note 200 characters; "
                     f"the limit is {MAX_NOTES_LENGTH}."
        }
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["notes", "alpha", "-a", "more"])
        assert result.exit_code == 1
        assert "Appending would make" in _strip_ansi(result.output)


class TestNotesClearFlag:
    @pytest.mark.parametrize("prefix", _NOTES_PREFIXES)
    def test_clear_sends_an_empty_note(
        self, runner: CliRunner, tmp_config, prefix: list[str],
    ) -> None:
        """An empty note already means "remove it", so -c needs no new route."""
        result, client = _invoke_notes(runner, [*prefix, "alpha", "-c"], notes=None)
        assert result.exit_code == 0
        client.set_notes.assert_called_once_with("alpha", "", append=False)

    def test_long_form_works_too(self, runner: CliRunner, tmp_config) -> None:
        _, client = _invoke_notes(runner, ["notes", "alpha", "--clear"], notes=None)
        client.set_notes.assert_called_once_with("alpha", "", append=False)

    def test_it_reports_a_clear(self, runner: CliRunner, tmp_config) -> None:
        result, _ = _invoke_notes(runner, ["notes", "alpha", "-c"], notes=None)
        assert "cleared" in _strip_ansi(result.output)

    @pytest.mark.parametrize("prefix", _NOTES_PREFIXES)
    def test_clear_with_text_is_rejected(
        self, runner: CliRunner, tmp_config, prefix: list[str],
    ) -> None:
        """-c takes nothing of its own, so text alongside it is a mistake."""
        result, client = _invoke_notes(runner, [*prefix, "alpha", "-c", "oops"])
        assert result.exit_code == 1
        assert "-c takes no note" in _strip_ansi(result.output)
        client.set_notes.assert_not_called()


def _fake_editor(new_text: str | None, returncode: int = 0):
    """Stand in for ``subprocess.run``: rewrite the temp file, then exit.

    ``new_text=None`` leaves the file exactly as ilan wrote it, which is what
    an editor opened and closed without a change looks like.
    """
    def run(cmd, *args, **kwargs):
        if new_text is not None:
            Path(cmd[-1]).write_text(new_text)
        return MagicMock(returncode=returncode)

    return run


def _invoke_editor(
    runner: CliRunner,
    argv: list[str],
    *,
    current: str | None = "the old note",
    new_text: str | None = None,
    returncode: int = 0,
    editor: str = "vim",
):
    """Run a ``-e`` notes command against a mock client and a fake editor."""
    client = MagicMock()
    client.get_task.return_value = {"task": {"name": "alpha", "notes": current}}
    client.set_notes.return_value = {
        "ok": True, "name": "alpha", "notes": (new_text or "").strip() or None,
    }
    seen: dict = {}

    def run(cmd, *args, **kwargs):
        seen["cmd"] = list(cmd)
        seen["prefill"] = Path(cmd[-1]).read_text()
        return _fake_editor(new_text, returncode)(cmd, *args, **kwargs)

    # `shutil.which` is patched, not left to the host: CI runners do not all
    # ship vim, and these tests are about ilan's behaviour, not the image's.
    with patch("ilan.cli._client", return_value=client), \
            patch("ilan.cli.subprocess.run", side_effect=run), \
            patch("ilan.cli.shutil.which", return_value=f"/usr/bin/{editor}"), \
            patch("ilan.cli.cfg.load", return_value={"editor": editor}):
        result = runner.invoke(main, argv)
    return result, client, seen


class TestNotesEditorFlag:
    """``-e`` opens the task's note in the configured editor."""

    @pytest.mark.parametrize("prefix", _NOTES_PREFIXES)
    def test_every_spelling_takes_the_flag(
        self, runner: CliRunner, tmp_config, prefix: list[str],
    ) -> None:
        result, client, _ = _invoke_editor(
            runner, [*prefix, "alpha", "-e"], new_text="a brand new note",
        )
        assert result.exit_code == 0
        client.set_notes.assert_called_once_with(
            "alpha", "a brand new note", append=False,
        )

    def test_long_form_works_too(self, runner: CliRunner, tmp_config) -> None:
        _, client, _ = _invoke_editor(
            runner, ["notes", "alpha", "--editor"], new_text="edited",
        )
        client.set_notes.assert_called_once_with("alpha", "edited", append=False)

    def test_the_editor_is_prefilled_with_the_current_note(
        self, runner: CliRunner, tmp_config,
    ) -> None:
        """Editing a note means starting from it, not from a blank buffer."""
        _, _, seen = _invoke_editor(
            runner, ["notes", "alpha", "-e"], current="the old note",
        )
        assert seen["prefill"] == "the old note"

    def test_a_task_with_no_note_opens_empty(
        self, runner: CliRunner, tmp_config,
    ) -> None:
        _, _, seen = _invoke_editor(runner, ["notes", "alpha", "-e"], current=None)
        assert seen["prefill"] == ""

    def test_it_runs_the_configured_editor(
        self, runner: CliRunner, tmp_config,
    ) -> None:
        _, _, seen = _invoke_editor(
            runner, ["notes", "alpha", "-e"], new_text="x", editor="nano",
        )
        assert seen["cmd"][0] == "nano"

    def test_no_read_only_flag_is_passed(
        self, runner: CliRunner, tmp_config,
    ) -> None:
        """``ilan log`` opens read-only; a note has to be writable."""
        _, _, seen = _invoke_editor(
            runner, ["notes", "alpha", "-e"], new_text="x", editor="vim",
        )
        assert seen["cmd"] == ["vim", seen["cmd"][-1]]
        assert "-R" not in seen["cmd"]

    def test_the_edited_text_is_stripped(
        self, runner: CliRunner, tmp_config,
    ) -> None:
        """Editors add a trailing newline; that must not reach the note."""
        _, client, _ = _invoke_editor(
            runner, ["notes", "alpha", "-e"], new_text="  edited  \n",
        )
        client.set_notes.assert_called_once_with("alpha", "edited", append=False)

    def test_emptying_the_buffer_clears_the_note(
        self, runner: CliRunner, tmp_config,
    ) -> None:
        result, client, _ = _invoke_editor(
            runner, ["notes", "alpha", "-e"], new_text="",
        )
        assert result.exit_code == 0
        client.set_notes.assert_called_once_with("alpha", "", append=False)
        assert "cleared" in _strip_ansi(result.output)

    def test_an_unchanged_buffer_sends_nothing(
        self, runner: CliRunner, tmp_config,
    ) -> None:
        """Closing the editor without editing should not be a write."""
        result, client, _ = _invoke_editor(
            runner, ["notes", "alpha", "-e"], current="same", new_text="same",
        )
        assert result.exit_code == 0
        assert "unchanged" in _strip_ansi(result.output)
        client.set_notes.assert_not_called()

    def test_a_buffer_left_untouched_counts_as_unchanged(
        self, runner: CliRunner, tmp_config,
    ) -> None:
        """The realistic no-op: the editor never writes the file at all."""
        result, client, _ = _invoke_editor(
            runner, ["notes", "alpha", "-e"], current="same", new_text=None,
        )
        assert "unchanged" in _strip_ansi(result.output)
        client.set_notes.assert_not_called()

    def test_a_non_zero_editor_exit_abandons_the_edit(
        self, runner: CliRunner, tmp_config,
    ) -> None:
        """``:cq`` in vim means "throw this away", not "empty the note".

        Reading it as an empty buffer would clear the note on an editor crash.
        """
        result, client, _ = _invoke_editor(
            runner, ["notes", "alpha", "-e"], new_text="typed but abandoned",
            returncode=1,
        )
        assert result.exit_code == 1
        client.set_notes.assert_not_called()
        assert "unchanged" in _strip_ansi(result.output)

    def test_an_unknown_task_never_opens_an_editor(
        self, runner: CliRunner, tmp_config,
    ) -> None:
        """The 404 comes back from the prefill fetch, before any editor runs.

        ``cfg.load`` and ``shutil.which`` are patched so this asserts the 404
        path and not the editor pre-check, which now runs first and would
        otherwise fire on any host without the configured editor installed.
        """
        client = MagicMock()
        client.get_task.return_value = {"error": "Task nope not found"}
        with patch("ilan.cli._client", return_value=client), \
                patch("ilan.cli.cfg.load", return_value={"editor": "vim"}), \
                patch("ilan.cli.shutil.which", return_value="/usr/bin/vim"), \
                patch("ilan.cli.subprocess.run") as run:
            result = runner.invoke(main, ["notes", "nope", "-e"])
        assert result.exit_code == 1
        run.assert_not_called()
        assert "not found" in _strip_ansi(result.output)

    def test_over_long_text_is_sent_and_trimmed_by_the_server(
        self, runner: CliRunner, tmp_config,
    ) -> None:
        """No client-side length check: the server owns the limit and trims."""
        typed = "x" * (MAX_NOTES_LENGTH + 1)
        result, client, _ = _invoke_editor(
            runner, ["notes", "alpha", "-e"], new_text=typed,
        )
        assert result.exit_code == 0
        client.set_notes.assert_called_once_with("alpha", typed, append=False)

    def test_text_at_the_limit_is_accepted(
        self, runner: CliRunner, tmp_config,
    ) -> None:
        typed = "x" * MAX_NOTES_LENGTH
        result, client, _ = _invoke_editor(
            runner, ["notes", "alpha", "-e"], new_text=typed,
        )
        assert result.exit_code == 0
        client.set_notes.assert_called_once_with("alpha", typed, append=False)

    def test_the_temp_file_is_cleaned_up(
        self, runner: CliRunner, tmp_config,
    ) -> None:
        _, _, seen = _invoke_editor(
            runner, ["notes", "alpha", "-e"], new_text="edited",
        )
        assert not Path(seen["cmd"][-1]).exists()

    def test_the_temp_file_is_named_after_the_task(
        self, runner: CliRunner, tmp_config,
    ) -> None:
        """So a stray buffer in a split window still says what it belongs to."""
        _, _, seen = _invoke_editor(
            runner, ["notes", "alpha", "-e"], new_text="edited",
        )
        assert "alpha" in Path(seen["cmd"][-1]).name

    def test_the_buffer_holds_only_the_note(
        self, runner: CliRunner, tmp_config,
    ) -> None:
        """No instruction header: nothing to strip, and a note may start with #."""
        _, _, seen = _invoke_editor(
            runner, ["notes", "alpha", "-e"], current="# a heading-ish note",
        )
        assert seen["prefill"] == "# a heading-ish note"

    @pytest.mark.parametrize(
        "extra, expected",
        [
            (["-c"], "use one or the other"),
            (["-a", "x"], "use one or the other"),
            (["some text"], "-e takes no note"),
        ],
    )
    def test_it_is_exclusive_with_the_other_ways_to_write_a_note(
        self, runner: CliRunner, tmp_config, extra: list[str], expected: str,
    ) -> None:
        client = MagicMock()
        with patch("ilan.cli._client", return_value=client), \
                patch("ilan.cli.subprocess.run") as run:
            result = runner.invoke(main, ["notes", "alpha", "-e", *extra])
        assert result.exit_code == 1
        assert expected in _strip_ansi(result.output)
        run.assert_not_called()
        client.set_notes.assert_not_called()


class TestEditorMustBeUsable:
    """``-e`` refuses up front when there is no editor it could open.

    Checked before the task is even fetched, so the user gets one clear
    message instead of a temp file and whatever their shell makes of an
    unknown command.
    """

    def _invoke(self, runner: CliRunner, *, configured: str, on_path: str | None):
        client = MagicMock()
        client.get_task.return_value = {"task": {"name": "alpha", "notes": "n"}}
        with patch("ilan.cli._client", return_value=client), \
                patch("ilan.cli.cfg.load", return_value={"editor": configured}), \
                patch("ilan.cli.shutil.which", return_value=on_path), \
                patch("ilan.cli.subprocess.run") as run:
            result = runner.invoke(main, ["notes", "alpha", "-e"])
        return result, client, run

    def test_a_blank_editor_setting_is_an_error(
        self, runner: CliRunner, tmp_config,
    ) -> None:
        result, client, run = self._invoke(runner, configured="", on_path=None)
        assert result.exit_code == 1
        out = " ".join(_strip_ansi(result.output).split())
        assert "No editor configured" in out
        assert "ilan config set editor" in out  # says how to fix it
        run.assert_not_called()
        client.set_notes.assert_not_called()

    def test_an_editor_that_is_not_installed_is_an_error(
        self, runner: CliRunner, tmp_config,
    ) -> None:
        """``editor`` defaults to emacs, which many machines do not have."""
        result, client, run = self._invoke(runner, configured="emacs", on_path=None)
        assert result.exit_code == 1
        out = " ".join(_strip_ansi(result.output).split())
        assert "emacs" in out
        assert "PATH" in out
        assert "ilan config set editor" in out
        run.assert_not_called()
        client.set_notes.assert_not_called()

    def test_it_refuses_before_touching_the_task(
        self, runner: CliRunner, tmp_config,
    ) -> None:
        """No point fetching a note we have no way to show the user."""
        _, client, _ = self._invoke(runner, configured="", on_path=None)
        client.get_task.assert_not_called()

    def test_a_whitespace_only_setting_counts_as_unset(
        self, runner: CliRunner, tmp_config,
    ) -> None:
        result, _, run = self._invoke(runner, configured="   ", on_path=None)
        assert result.exit_code == 1
        assert "No editor configured" in " ".join(_strip_ansi(result.output).split())
        run.assert_not_called()

    def test_an_installed_editor_proceeds(
        self, runner: CliRunner, tmp_config,
    ) -> None:
        _, _, run = self._invoke(runner, configured="vim", on_path="/usr/bin/vim")
        assert run.called

    def test_an_exec_failure_after_the_check_is_still_reported(
        self, runner: CliRunner, tmp_config,
    ) -> None:
        """`which` can pass and exec still fail — a race, or bad permissions."""
        client = MagicMock()
        client.get_task.return_value = {"task": {"name": "alpha", "notes": "n"}}
        with patch("ilan.cli._client", return_value=client), \
                patch("ilan.cli.cfg.load", return_value={"editor": "vim"}), \
                patch("ilan.cli.shutil.which", return_value="/usr/bin/vim"), \
                patch("ilan.cli.subprocess.run",
                      side_effect=PermissionError("denied")):
            result = runner.invoke(main, ["notes", "alpha", "-e"])
        assert result.exit_code == 1
        assert "Cannot run" in " ".join(_strip_ansi(result.output).split())
        client.set_notes.assert_not_called()


class TestTruncationIsReported:
    """Over-long notes are trimmed, not refused — but never silently."""

    def _invoke(self, runner: CliRunner, argv: list[str], *, truncated: bool):
        client = MagicMock()
        client.set_notes.return_value = {
            "ok": True, "name": "alpha", "notes": "x" * MAX_NOTES_LENGTH,
            "truncated": truncated,
        }
        with patch("ilan.cli._client", return_value=client):
            return runner.invoke(main, argv), client

    def test_an_over_long_note_succeeds(
        self, runner: CliRunner, tmp_config,
    ) -> None:
        """The whole point of the change: this is no longer an error."""
        result, client = self._invoke(
            runner, ["notes", "alpha", "y" * 200], truncated=True,
        )
        assert result.exit_code == 0
        # Sent whole; the server is what shortens it.
        client.set_notes.assert_called_once_with("alpha", "y" * 200, append=False)

    def test_the_cut_is_reported(self, runner: CliRunner, tmp_config) -> None:
        result, _ = self._invoke(
            runner, ["notes", "alpha", "y" * 200], truncated=True,
        )
        out = " ".join(_strip_ansi(result.output).split())
        assert str(MAX_NOTES_LENGTH) in out
        assert "dropped the rest" in out

    def test_the_saved_note_is_still_shown(
        self, runner: CliRunner, tmp_config,
    ) -> None:
        """The warning must not replace the confirmation of what was saved."""
        result, _ = self._invoke(
            runner, ["notes", "alpha", "y" * 200], truncated=True,
        )
        out = " ".join(_strip_ansi(result.output).split())
        assert "Note for alpha set to" in out

    def test_nothing_is_said_when_nothing_was_cut(
        self, runner: CliRunner, tmp_config,
    ) -> None:
        result, _ = self._invoke(runner, ["notes", "alpha", "short"], truncated=False)
        assert "dropped" not in _strip_ansi(result.output)

    def test_an_older_server_without_the_flag_says_nothing(
        self, runner: CliRunner, tmp_config,
    ) -> None:
        """A response predating `truncated` must not read as a truncation."""
        client = MagicMock()
        client.set_notes.return_value = {"ok": True, "name": "alpha", "notes": "n"}
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["notes", "alpha", "n"])
        assert result.exit_code == 0
        assert "dropped" not in _strip_ansi(result.output)

    def test_an_over_long_append_is_reported_too(
        self, runner: CliRunner, tmp_config,
    ) -> None:
        result, _ = self._invoke(
            runner, ["notes", "alpha", "-a", "y" * 200], truncated=True,
        )
        assert result.exit_code == 0
        out = " ".join(_strip_ansi(result.output).split())
        assert "dropped the rest" in out
        assert "now reads" in out


class TestNotesFlagCombinations:
    """Each of note / -a / -c / -e says what the note should become.

    Any two of them are a contradiction, so they are rejected before the
    request goes out rather than silently letting one win.
    """

    def test_append_and_clear_together_is_rejected(
        self, runner: CliRunner, tmp_config,
    ) -> None:
        result, client = _invoke_notes(runner, ["notes", "alpha", "-a", "x", "-c"])
        assert result.exit_code == 1
        assert "use one or the other" in _strip_ansi(result.output)
        client.set_notes.assert_not_called()

    def test_text_and_append_together_is_rejected(
        self, runner: CliRunner, tmp_config,
    ) -> None:
        result, client = _invoke_notes(runner, ["notes", "alpha", "text", "-a", "x"])
        assert result.exit_code == 1
        assert "not as a second argument" in _strip_ansi(result.output)
        client.set_notes.assert_not_called()

    def test_neither_text_nor_a_flag_is_rejected(
        self, runner: CliRunner, tmp_config,
    ) -> None:
        """The note argument is optional now, so "nothing to do" needs saying."""
        result, client = _invoke_notes(runner, ["notes", "alpha"])
        assert result.exit_code == 1
        out = _strip_ansi(result.output)
        # The message names every way out, so it is never a dead end.
        assert "-a" in out and "-c" in out and "-e" in out
        client.set_notes.assert_not_called()

    def test_a_plain_note_still_replaces(self, runner: CliRunner, tmp_config) -> None:
        """The original two-argument form must keep working unchanged."""
        _, client = _invoke_notes(runner, ["notes", "alpha", "the note"])
        client.set_notes.assert_called_once_with("alpha", "the note", append=False)

    def test_a_plain_note_is_stripped_before_it_is_sent(
        self, runner: CliRunner, tmp_config,
    ) -> None:
        _, client = _invoke_notes(runner, ["notes", "alpha", "  \n the note \n "])
        client.set_notes.assert_called_once_with("alpha", "the note", append=False)


class TestClientAppendArgument:
    def test_append_true_sets_the_body_flag(self) -> None:
        from ilan.client import Client

        with patch.object(Client, "post", return_value={"ok": True}) as post:
            Client().set_notes("alpha", "n", append=True)
        post.assert_called_once_with(
            "/tasks/alpha/notes", {"notes": "n", "append": True},
        )

    def test_append_false_omits_the_key(self) -> None:
        """An older server has never seen the key; do not send a falsey one."""
        from ilan.client import Client

        with patch.object(Client, "post", return_value={"ok": True}) as post:
            Client().set_notes("alpha", "n")
        post.assert_called_once_with("/tasks/alpha/notes", {"notes": "n"})


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


def _pair_total(table) -> int:
    """Total width of the Status and Notes columns of a wide dashboard."""
    return sum(c.width for c in table.columns[1:3])


def _name_width(table, terminal_width: int) -> int:
    """What Rich actually gives Name, the only ratio column."""
    fixed = sum(c.width for c in table.columns[1:])
    chrome = 3 * len(table.columns) + 1
    return terminal_width - chrome - fixed


class TestWideningNotesCostsOnlyStatus:
    """The point of sizing the pair together: nothing else moves.

    These numbers are what the dashboard rendered before Status and Notes
    were sized as a pair. Name and the two timestamp columns must come out
    identical, Status must be no wider, and Notes no narrower — otherwise the
    trade has come out of the wrong column.
    """

    # width: (Name, Status, Notes, Created, Last Changed) as it was before.
    _BEFORE = {
        140: (30, 38, 26, 15, 15),
        150: (34, 44, 26, 15, 15),
        180: (47, 61, 26, 15, 15),
        200: (56, 72, 26, 15, 15),
        240: (73, 95, 26, 15, 15),
    }

    def _now(self, width: int) -> tuple[int, ...]:
        table = _build_dashboard_table([], _TZ, width=width)
        name = _name_width(table, width)
        return (name, *(c.width for c in table.columns[1:]))

    @pytest.mark.parametrize("width", sorted(_BEFORE))
    def test_name_is_untouched(self, width: int) -> None:
        assert self._now(width)[0] == self._BEFORE[width][0]

    @pytest.mark.parametrize("width", sorted(_BEFORE))
    def test_the_timestamp_columns_are_untouched(self, width: int) -> None:
        assert self._now(width)[3:] == self._BEFORE[width][3:]

    @pytest.mark.parametrize("width", sorted(_BEFORE))
    def test_status_never_grows(self, width: int) -> None:
        assert self._now(width)[1] <= self._BEFORE[width][1]

    @pytest.mark.parametrize("width", sorted(_BEFORE))
    def test_notes_never_shrinks(self, width: int) -> None:
        assert self._now(width)[2] >= self._BEFORE[width][2]

    @pytest.mark.parametrize("width", [150, 180, 200, 240])
    def test_the_trade_is_real_above_the_narrowest_window(
        self, width: int,
    ) -> None:
        """At 140 the pair was already 3:2, so only wider windows move."""
        now, before = self._now(width), self._BEFORE[width]
        assert now[1] < before[1]  # Status narrower
        assert now[2] > before[2]  # Notes wider

    @pytest.mark.parametrize("width", sorted(_BEFORE))
    def test_the_pair_total_is_conserved(self, width: int) -> None:
        """Which is *why* nothing else moves."""
        now, before = self._now(width), self._BEFORE[width]
        assert now[1] + now[2] == before[1] + before[2]


class TestDashboardNotesColumn:
    _WIDE = ["(Alias) Name", "Status", "Notes", "Created", "Last Changed"]
    _NARROW = ["(Alias) Name", "Status", "Last Changed"]

    def test_column_is_present_even_when_nobody_has_a_note(self) -> None:
        """The column is unconditional, so the layout never shifts under you."""
        table = _build_dashboard_table([_row("a")], _TZ)
        assert [c.header for c in table.columns] == self._WIDE

    def test_column_present_with_a_note(self) -> None:
        table = _build_dashboard_table([_row("a", notes="x")], _TZ)
        assert [c.header for c in table.columns] == self._WIDE

    def test_notes_sits_beside_status(self) -> None:
        """The agent's summary and the user's note answer the same question.

        Reading one after the other should not mean crossing two timestamp
        columns to get there.
        """
        table = _build_dashboard_table([_row("a", notes="x")], _TZ)
        headers = [c.header for c in table.columns]
        assert headers[headers.index("Status") + 1] == "Notes"

    def test_cell_holds_the_note(self) -> None:
        table = _build_dashboard_table([_row("a", notes="the reminder")], _TZ)
        assert table.columns[2]._cells[0].plain == "the reminder"

    def test_annotated_and_bare_rows_share_the_column(self) -> None:
        rows = [_row("a", notes="only mine"), _row("b")]
        table = _build_dashboard_table(rows, _TZ)
        assert [c.plain for c in table.columns[2]._cells] == ["only mine", ""]

    def test_narrow_drops_the_notes_column_entirely(self) -> None:
        """Too narrow for both, so the note moves into the Status cell."""
        table = _build_dashboard_table([_row("a", notes="x")], _TZ, narrow=True)
        assert [c.header for c in table.columns] == self._NARROW
        assert "Notes" not in self._NARROW

    @pytest.mark.parametrize("width", [140, 150, 180, 200, 240])
    @pytest.mark.parametrize("one_liner", [True, False])
    def test_status_and_notes_are_held_near_three_to_two(
        self, width: int, one_liner: bool,
    ) -> None:
        """They carry the two answers to "what is this task about".

        "Roughly": the pair's total is whatever room the two of them already
        had, and an integer total rarely splits 3:2 exactly, so the realised
        quotient is checked against a tenth either side of 1.5.
        """
        status, notes = _build_dashboard_table(
            [_row("a", notes="x")], _TZ, show_one_liner=one_liner, width=width,
        ).columns[1:3]
        assert (status.header, notes.header) == ("Status", "Notes")
        wanted, per = STATUS_TO_NOTES
        assert abs(status.width / notes.width - wanted / per) < 0.1

    def test_the_pair_gets_explicit_widths_not_ratios(self) -> None:
        """A ratio would put Notes in competition with Name for space."""
        status, notes = _build_dashboard_table(
            [_row("a", notes="x")], _TZ, width=200,
        ).columns[1:3]
        assert status.ratio is notes.ratio is None
        assert status.width and notes.width

    def test_status_is_a_plain_ratio_when_narrow(self) -> None:
        """With no Notes column there is no pair to split."""
        columns = _build_dashboard_table(
            [_row("a")], _TZ, narrow=True, width=100,
        ).columns
        assert columns[1].header == "Status"
        assert columns[1].ratio == NAME_TO_PAIR[1]
        assert columns[0].ratio == NAME_TO_PAIR[0]  # Name is untouched

    def test_timestamp_columns_are_fixed_too(self) -> None:
        """Pinning these is what pays for Notes — see the module constants."""
        table = _build_dashboard_table([_row("a", notes="x")], _TZ)
        created, changed = table.columns[3], table.columns[4]
        assert (created.header, changed.header) == ("Created", "Last Changed")
        assert created.width == changed.width == TIMESTAMP_COLUMN_WIDTH
        assert created.ratio is changed.ratio is None

    def test_only_name_flexes(self) -> None:
        table = _build_dashboard_table([_row("a", notes="x")], _TZ, width=200)
        assert [c.ratio for c in table.columns] == [10, None, None, None, None]

    def test_the_pair_leads_only_while_it_holds_a_summary(self) -> None:
        """With the one-liner off, Status holds a label and a duration, so
        Name takes the bulk instead.

        Asserted on the shares rather than the realised widths: the pair also
        absorbs the room Notes was pinned at, so at some terminal widths the
        two come out level even though the shares differ.
        """
        assert NAME_TO_PAIR[0] < NAME_TO_PAIR[1]
        assert NAME_TO_PAIR_PLAIN[0] > NAME_TO_PAIR_PLAIN[1]

    @pytest.mark.parametrize("width", [150, 180, 200, 240])
    def test_name_is_never_squeezed_out_by_the_pair(self, width: int) -> None:
        """Whatever the pair takes, Name keeps a workable slice of the table."""
        for one_liner in (True, False):
            table = _build_dashboard_table(
                [], _TZ, show_one_liner=one_liner, width=width,
            )
            assert _name_width(table, width) >= 20

    def test_empty_listing_keeps_the_placeholder_row_aligned(self) -> None:
        table = _build_dashboard_table([], _TZ)
        assert [c.header for c in table.columns] == self._WIDE
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


def _ls_headers(out: str) -> list[str]:
    """The header row of a rendered ``ilan ls`` table, cell by cell."""
    header = next(l for l in _strip_ansi(out).splitlines() if "(Alias) Name" in l)
    return [cell.strip() for cell in header.strip("┃").split("┃")]


def _ls_column_widths(out: str) -> list[int]:
    """Content width of each column, read off the top border rule."""
    rule = next(l for l in _strip_ansi(out).splitlines() if l.startswith("┏"))
    # Each segment is the column plus its one-space padding on either side.
    return [len(seg) - 2 for seg in rule.strip("┏┓").split("┳")]


class TestLsColumnLayout:
    """Column order and widths in ``ilan ls``."""

    def test_notes_sits_beside_status(
        self, runner: CliRunner, tmp_config, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        out = _invoke_ls(runner, [_row("a", notes="x")], monkeypatch).output
        assert _ls_headers(out) == [
            "(Alias) Name", "Status", "Notes", "Created", "Last Changed",
        ]

    def test_narrow_drops_the_notes_column(
        self, runner: CliRunner, tmp_config, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        out = _invoke_ls(
            runner, [_row("a", notes="x")], monkeypatch, width=100,
        ).output
        assert _ls_headers(out) == ["(Alias) Name", "Status", "Last Changed"]

    def test_status_is_capped(
        self, runner: CliRunner, tmp_config, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A one-line summary used to grow Status past every other column."""
        row = _row("a", notes="x")
        row["summary_one_liner"] = (
            "Rewrote the tokenizer's lookahead, split the parser, and moved "
            "the whole grammar over to the new dispatch table."
        )
        out = _invoke_ls(runner, [row], monkeypatch).output
        headers = _ls_headers(out)
        widths = _ls_column_widths(out)
        assert widths[headers.index("Status")] == STATUS_MAX_WIDTH

    def test_a_short_status_does_not_reserve_the_cap(
        self, runner: CliRunner, tmp_config, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Capped, not pinned: a listing of short statuses stays compact.

        This is the one column that is still content-sized, because `-a`
        suppresses the summary and reserving room for one would waste it.
        """
        out = _invoke_ls(runner, [_row("a", notes="x")], monkeypatch).output
        headers = _ls_headers(out)
        widths = _ls_column_widths(out)
        assert widths[headers.index("Status")] < STATUS_MAX_WIDTH

    def test_notes_uses_the_wide_tier_on_a_wide_window(
        self, runner: CliRunner, tmp_config, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        out = _invoke_ls(runner, [_row("a", notes="x")], monkeypatch).output
        headers = _ls_headers(out)
        widths = _ls_column_widths(out)
        assert widths[headers.index("Notes")] == NOTES_COLUMN_WIDTH

    def test_status_and_notes_are_already_near_three_to_two(self) -> None:
        """``ls`` needed no trade: 38:26 is 1.46, within a character of 3:2.

        It is the dashboard that ran away from the ratio, because there
        Status grew with the terminal while Notes stayed pinned.
        """
        wanted, per = STATUS_TO_NOTES
        assert abs(STATUS_MAX_WIDTH * per - NOTES_COLUMN_WIDTH * wanted) <= per

    def test_the_note_column_still_does_not_move_with_its_contents(
        self, runner: CliRunner, tmp_config, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Widening the column must not have cost it its fixed width."""
        short = _invoke_ls(runner, [_row("a", notes="hi")], monkeypatch).output
        long = _invoke_ls(runner, [_row("a", notes="hi " * 40)], monkeypatch).output
        assert _ls_column_widths(short) == _ls_column_widths(long)


class TestNarrowWindowInlinesTheNote:
    """Below the threshold there is no Notes column; the note moves inside
    the Status cell, beneath the one-line summary.

    That removes the old constraint entirely: the Notes column used to have
    to be narrow enough to sit beside a task name on a 70-column window, and
    now it simply is not there.
    """

    _NARROW_WINDOW = 70

    def _render(self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> str:
        row = _row("narrow-task", notes="the reminder")
        row["status_changed_at"] = "2026-04-13T01:00:00+00:00"
        result = _invoke_ls(runner, [row], monkeypatch, width=self._NARROW_WINDOW)
        assert result.exit_code == 0
        return _strip_ansi(result.output)

    def test_the_note_is_shown_without_a_column(
        self, runner: CliRunner, tmp_config, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        out = self._render(runner, monkeypatch)
        assert "Notes" not in _ls_headers(out)
        assert "the reminder" in out

    def test_the_task_name_survives(
        self, runner: CliRunner, tmp_config, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The point of dropping the column: the name is readable again."""
        assert "narrow-task" in self._render(runner, monkeypatch)

    def test_the_note_sits_under_the_summary_in_the_status_cell(self) -> None:
        row = _row("a", notes="THENOTE")
        row["summary_one_liner"] = "THESUMMARY"
        cell = _build_status_cell(row, show_one_liner=True, inline_note=True)
        lines = cell.plain.splitlines()
        assert lines[0].startswith("WORKING")
        assert lines[1] == "THESUMMARY"
        assert lines[2] == "THENOTE"

    def test_the_note_keeps_its_own_colour_inline(self) -> None:
        """So it reads as a note rather than more of the summary."""
        row = _row("a", notes="THENOTE")
        row["summary_one_liner"] = "THESUMMARY"
        cell = _build_status_cell(row, show_one_liner=True, inline_note=True)
        note_span = next(
            sp for sp in cell.spans if cell.plain[sp.start:sp.end] == "THENOTE"
        )
        assert str(note_span.style) == NOTES_STYLE

    def test_no_summary_puts_the_note_straight_under_the_status(self) -> None:
        cell = _build_status_cell(
            _row("a", notes="THENOTE"), show_one_liner=True, inline_note=True,
        )
        assert cell.plain.splitlines()[1] == "THENOTE"

    def test_it_still_shows_with_the_one_liner_off(self) -> None:
        """`-a` suppresses the summary, but the note is the user's own text."""
        row = _row("a", notes="THENOTE")
        row["summary_one_liner"] = "THESUMMARY"
        cell = _build_status_cell(row, show_one_liner=False, inline_note=True)
        assert "THESUMMARY" not in cell.plain
        assert cell.plain.splitlines()[1] == "THENOTE"

    def test_a_task_with_no_note_gains_no_line(self) -> None:
        cell = _build_status_cell(_row("a"), show_one_liner=True, inline_note=True)
        assert "\n" not in cell.plain

    def test_a_blank_note_gains_no_line(self) -> None:
        cell = _build_status_cell(
            _row("a", notes="   "), show_one_liner=True, inline_note=True,
        )
        assert "\n" not in cell.plain

    def test_the_wide_layout_leaves_the_status_cell_alone(self) -> None:
        """With a Notes column present the note must not appear twice."""
        row = _row("a", notes="THENOTE")
        cell = _build_status_cell(row, show_one_liner=True, inline_note=False)
        assert "THENOTE" not in cell.plain
