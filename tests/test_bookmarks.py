"""Tests for ``ilan bookmark`` / ``ilan bookmarks``: model, server and CLI."""

from __future__ import annotations

import io
import json
import re
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.request import Request, urlopen

import pytest
from click.testing import CliRunner
from rich.console import Console
from rich.text import Text

import ilan.server as srv_mod
from ilan import config as cfg
from ilan import gist as gist_mod
from ilan.cli import (
    BOOKMARK_LINK_STYLE,
    BOOKMARK_LINK_TEXT,
    BOOKMARK_TIME_STYLE,
    BOOKMARKS_COUNT_DEFAULT,
    _build_bookmarks_grid,
    _print_bookmarks,
    main,
)
from ilan.models import MAX_BOOKMARKS, Bookmark, LogEntry, Task, TaskStatus
from ilan.server import IlanServer
from ilan.store import Store
from ilan.task_display import ONE_LINER_STYLE

from tests.helpers import get_json as _get, post_json as _post

GIST_URL = "https://gist.github.com/u/gid1"
PARENT_GIST_URL = "https://gist.github.com/u/gid0"


def _permalink(gist_url: str, comment_id: int) -> str:
    return f"{gist_url}?permalink_comment_id={comment_id}#gistcomment-{comment_id}"


def _stamp(minute: int) -> str:
    """A UTC ISO timestamp ``minute`` minutes past 2026-10-27 20:00 UTC."""
    return f"2026-10-27T{20 + minute // 60:02d}:{minute % 60:02d}:00+00:00"


# ── model ───────────────────────────────────────────────────────────────


class TestBookmarkModel:
    def test_add_keeps_time_order(self) -> None:
        task = Task(name="t-one", prompt="p")
        task.add_bookmark(Bookmark(_stamp(2), "u2"))
        task.add_bookmark(Bookmark(_stamp(1), "u1"))
        assert [b.url for b in task.bookmarks] == ["u1", "u2"]

    def test_a_new_bookmark_past_the_limit_evicts_the_oldest(self) -> None:
        task = Task(name="t-one", prompt="p")
        for minute in range(MAX_BOOKMARKS):
            assert task.add_bookmark(Bookmark(_stamp(minute), f"u{minute}")) == (False, [])
        replaced, evicted = task.add_bookmark(Bookmark(_stamp(MAX_BOOKMARKS), "new"))
        assert replaced is False
        assert evicted == [Bookmark(_stamp(0), "u0")]
        assert len(task.bookmarks) == MAX_BOOKMARKS
        assert task.bookmarks[0].url == "u1"
        assert task.bookmarks[-1].url == "new"

    def test_the_same_reply_again_replaces_its_bookmark(self) -> None:
        task = Task(name="t-one", prompt="p")
        task.add_bookmark(Bookmark(_stamp(0), "u0", ""))
        task.add_bookmark(Bookmark(_stamp(1), "u1", "other"))
        replaced, evicted = task.add_bookmark(Bookmark(_stamp(2), "u0", "now summarised"))
        assert (replaced, evicted) == (True, [])
        assert task.bookmarks == [
            Bookmark(_stamp(1), "u1", "other"),
            Bookmark(_stamp(2), "u0", "now summarised"),
        ]

    def test_replacing_at_the_limit_evicts_nothing(self) -> None:
        task = Task(name="t-one", prompt="p")
        for minute in range(MAX_BOOKMARKS):
            task.add_bookmark(Bookmark(_stamp(minute), f"u{minute}"))
        replaced, evicted = task.add_bookmark(Bookmark(_stamp(99), "u0"))
        assert (replaced, evicted) == (True, [])
        assert len(task.bookmarks) == MAX_BOOKMARKS
        assert task.bookmarks[-1] == Bookmark(_stamp(99), "u0")

    def test_round_trip_through_the_store_format(self) -> None:
        task = Task(name="t-one", prompt="p")
        task.add_bookmark(Bookmark(_stamp(0), "u0", "summary"))
        d = task.to_dict()
        assert d["bookmarks"] == [{"created_at": _stamp(0), "url": "u0", "summary": "summary"}]
        assert Task.from_dict(d).bookmarks == task.bookmarks

    def test_a_task_saved_before_bookmarks_loads_with_none(self) -> None:
        assert Task.from_dict({"name": "old", "prompt": "p", "status": "WORKING"}).bookmarks == []

    def test_a_missing_summary_loads_as_empty(self) -> None:
        assert Bookmark.from_dict({"created_at": "x", "url": "u", "summary": None}).summary == ""


# ── which Gist holds a reply ────────────────────────────────────────────


@pytest.fixture()
def store(tmp_workdir: Path) -> Store:
    return Store(tmp_workdir)


def _branch(store: Store, parent: Task, name: str) -> Task:
    return store.branch_task(parent, name, alias="zz", task_hash="h", now=_stamp(0))


class TestEntryGistOwner:
    def test_a_root_task_holds_every_entry(self, store: Store) -> None:
        task = Task(name="root-task", prompt="p")
        store.put_task(task)
        assert gist_mod.entry_gist_owner(store, task, 4).name == "root-task"

    def test_a_child_holds_what_came_after_the_branch(self, store: Store) -> None:
        parent = Task(name="parent-task", prompt="p")
        store.put_task(parent)
        for role in ("user", "assistant", "user"):
            store.append_log(parent.name, role, "x")
        child = _branch(store, parent, "child-task")
        assert child.gist_branch_point == 3
        assert gist_mod.entry_gist_owner(store, child, 3).name == "child-task"
        assert gist_mod.entry_gist_owner(store, child, 2).name == "parent-task"

    def test_an_inherited_entry_is_on_the_ancestor_that_wrote_it(self, store: Store) -> None:
        root = Task(name="root-task", prompt="p")
        store.put_task(root)
        for role in ("user", "assistant"):
            store.append_log(root.name, role, "x")
        child = _branch(store, root, "child-task")
        store.append_log(child.name, "user", "child's own")
        grandchild = _branch(store, child, "grandchild")
        # Entry 2 is the child's own, entry 1 the root's.
        assert gist_mod.entry_gist_owner(store, grandchild, 2).name == "child-task"
        assert gist_mod.entry_gist_owner(store, grandchild, 1).name == "root-task"

    def test_a_renamed_parent_is_still_found(self, store: Store) -> None:
        parent = Task(name="parent-task", prompt="p")
        store.put_task(parent)
        store.append_log(parent.name, "assistant", "x")
        _branch(store, parent, "child-task")
        store.rename_task("parent-task", "renamed-parent")
        child = store.get_task("child-task")
        assert gist_mod.entry_gist_owner(store, child, 0).name == "renamed-parent"

    def test_a_deleted_parent_is_not_replaced_by_a_namesake(self, store: Store) -> None:
        parent = Task(name="parent-task", prompt="p")
        store.put_task(parent)
        store.append_log(parent.name, "assistant", "x")
        _branch(store, parent, "child-task")
        store.delete_task("parent-task")
        child = store.get_task("child-task")
        assert gist_mod.entry_gist_owner(store, child, 0) is None
        # A new, unrelated task under the old name must not be mistaken for it.
        store.put_task(Task(name="parent-task", prompt="unrelated"))
        assert gist_mod.entry_gist_owner(store, child, 0) is None


def _entry(role: str, content: str, second: int) -> LogEntry:
    return LogEntry(role, content, f"2026-10-27T20:00:{second:02d}+00:00", model="m")


class FakeGist:
    """A Gist's comments as the API serves them, oldest first, with ids 1, 2, …"""

    def __init__(self, bodies: list[str]) -> None:
        self.bodies = bodies
        self.calls: list[str] = []

    def api(self, method: str, path: str, token: str, payload: dict | None = None):
        self.calls.append(path)
        if path == "/gists/gid1":
            return {"comments": len(self.bodies)}
        position = int(path.rsplit("page=", 1)[1])
        if not 1 <= position <= len(self.bodies):
            return []
        return [{"id": position, "body": self.bodies[position - 1]}]


class TestFindEntryCommentUrl:
    REPLY = _entry("assistant", "The answer.", 30)

    def _conversation(self) -> list[LogEntry]:
        return [
            _entry("user", "Question one?", 10), _entry("assistant", "Answer one.", 20),
            _entry("user", "Question two?", 25), self.REPLY, _entry("user", "Thanks.", 40),
        ]

    def _find(self, monkeypatch, bodies: list[str], later: int) -> tuple[str | None, FakeGist]:
        gist = FakeGist(bodies)
        monkeypatch.setattr(gist_mod, "_api_request", gist.api)
        return gist_mod.find_entry_comment_url("tok", "gid1", GIST_URL, self.REPLY, later), gist

    def test_counts_back_from_the_last_comment(self, monkeypatch, utc) -> None:
        bodies = [gist_mod.format_comment(e) for e in self._conversation()]
        url, gist = self._find(monkeypatch, bodies, later=1)
        assert url == _permalink(GIST_URL, 4)
        # The Gist's size, then straight to the comment: no search needed.
        assert gist.calls == ["/gists/gid1", "/gists/gid1/comments?per_page=1&page=4"]

    def test_duplicates_before_the_reply_do_not_matter(self, monkeypatch, utc) -> None:
        log = self._conversation()
        bodies = [gist_mod.format_comment(e) for e in log[:2]] * 3  # posted three times
        bodies += [gist_mod.format_comment(e) for e in log[2:]]
        url, gist = self._find(monkeypatch, bodies, later=1)
        assert url == _permalink(GIST_URL, 8)
        assert len(gist.calls) == 2

    def test_a_duplicate_after_the_reply_is_searched_past(self, monkeypatch, utc) -> None:
        bodies = [gist_mod.format_comment(e) for e in self._conversation()]
        bodies.append(bodies[-1])  # "Thanks." landed twice
        url, _ = self._find(monkeypatch, bodies, later=1)
        assert url == _permalink(GIST_URL, 4)

    def test_a_changed_time_zone_still_matches_on_the_text(self, monkeypatch, utc) -> None:
        bodies = [gist_mod.format_comment(e) for e in self._conversation()]
        cfg.save({**cfg.DEFAULTS, "time-zone": "Asia/Tokyo"})
        assert gist_mod.format_comment(self.REPLY) != bodies[3]
        url, _ = self._find(monkeypatch, bodies, later=1)
        assert url == _permalink(GIST_URL, 4)

    def test_no_comment_that_mirrors_the_reply_is_none(self, monkeypatch, utc) -> None:
        bodies = [gist_mod.format_comment(e) for e in self._conversation()]
        bodies[3] = "**Assistant (somewhen)**\n\nSomething else entirely."
        url, gist = self._find(monkeypatch, bodies, later=1)
        assert url is None
        # It looked at every comment in reach before giving up.
        assert len(gist.calls) == 1 + len(bodies)

    def test_an_empty_gist_is_none(self, monkeypatch, utc) -> None:
        url, gist = self._find(monkeypatch, [], later=0)
        assert url is None
        assert gist.calls == ["/gists/gid1"]

    def test_comment_url_still_falls_back_to_the_gist_page(self, monkeypatch) -> None:
        monkeypatch.setattr(gist_mod, "_api_request", lambda *a, **k: [])
        assert gist_mod.comment_url("tok", "gid1", GIST_URL, 3) == GIST_URL


# ── server ──────────────────────────────────────────────────────────────


def _add_task(
    server: IlanServer,
    name: str,
    *,
    replies: tuple[str, ...] = ("assistant",),
    gist: bool = True,
    summary: str | None = "Fixed the flaky test.",
) -> None:
    """A task whose log is its prompt followed by *replies*, mirrored in full."""
    assert "ok" in _post(server, "/tasks", {"name": name, "prompt": "P"})
    for role in replies:
        server.store.append_log(name, role, f"{role} text")
    with server.lock:
        task = server.store.get_task(name)
        task.set_status(TaskStatus.AGENT_FINISHED)
        task.summary_one_liner = summary
        if gist:
            task.gist_id = "gid1"
            task.gist_url = GIST_URL
            task.gist_synced_count = 1 + len(replies)
        server.store.put_task(task)


@pytest.fixture()
def github(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Gist mirroring on, with GitHub's comment lookup faked.

    Each reply gets its own comment id, the same one every time it is looked
    up. Only the server's view of the token is patched: the syncer still sees
    no token, so nothing in these tests reaches GitHub.
    """
    monkeypatch.setattr(srv_mod, "github_token", lambda: "tok")
    ids: dict[tuple[str, str], int] = {}

    def lookup(token: str, gist_id: str, gist_url: str, entry: LogEntry, later: int) -> str:
        return _permalink(gist_url, ids.setdefault((gist_id, entry.timestamp), len(ids) + 1))

    fake = MagicMock(side_effect=lookup)
    monkeypatch.setattr(srv_mod, "find_entry_comment_url", fake)
    return fake


def _looked_up(github: MagicMock) -> tuple[str, str, str, int]:
    """What the one lookup was asked for: Gist id and URL, reply text, later entries."""
    github.assert_called_once()
    token, gist_id, gist_url, entry, later = github.call_args.args
    assert (token, entry.role) == ("tok", "assistant")
    return gist_id, gist_url, entry.content, later


def _bookmarks(server: IlanServer, name: str) -> list[Bookmark]:
    return server.store.get_task(name).bookmarks


class TestAddBookmark:
    def test_refused_without_gist_mirroring(self, ilan_server: IlanServer, monkeypatch) -> None:
        _add_task(ilan_server, "bm-task")
        monkeypatch.setattr(srv_mod, "github_token", lambda: "")
        lookup = MagicMock()
        monkeypatch.setattr(srv_mod, "find_entry_comment_url", lookup)
        resp = _post(ilan_server, "/tasks/bm-task/bookmarks")
        assert "github-token" in resp["error"]
        lookup.assert_not_called()
        assert _bookmarks(ilan_server, "bm-task") == []

    def test_saves_the_link_and_the_summary(self, ilan_server: IlanServer, github) -> None:
        _add_task(ilan_server, "bm-task")
        resp = _post(ilan_server, "/tasks/bm-task/bookmarks")
        # The reply is the last entry, so nothing mirrored comes after it.
        assert _looked_up(github) == ("gid1", GIST_URL, "assistant text", 0)
        assert resp["ok"] is True
        assert resp["name"] == "bm-task"
        assert resp["bookmark"]["url"] == _permalink(GIST_URL, 1)
        assert resp["bookmark"]["summary"] == "Fixed the flaky test."
        assert (resp["replaced"], resp["evicted"]) == (False, [])
        assert (resp["count"], resp["limit"]) == (1, MAX_BOOKMARKS)
        assert _bookmarks(ilan_server, "bm-task") == [Bookmark.from_dict(resp["bookmark"])]

    def test_links_the_latest_reply_not_a_later_message(self, ilan_server: IlanServer, github) -> None:
        _add_task(ilan_server, "bm-task", replies=("assistant", "user", "assistant", "user"))
        with ilan_server.lock:
            reply = ilan_server.store.read_logs("bm-task")[3]
        _post(ilan_server, "/tasks/bm-task/bookmarks")
        # One user message follows the reply on the Gist.
        assert github.call_args.args[3] == reply
        assert _looked_up(github)[3] == 1

    def test_no_summary_saves_an_empty_one(self, ilan_server: IlanServer, github) -> None:
        _add_task(ilan_server, "bm-task", summary=None)
        resp = _post(ilan_server, "/tasks/bm-task/bookmarks")
        assert resp["bookmark"]["summary"] == ""
        assert _bookmarks(ilan_server, "bm-task")[0].summary == ""

    def test_by_alias(self, ilan_server: IlanServer, github) -> None:
        _add_task(ilan_server, "bm-task")
        alias = ilan_server.store.get_task("bm-task").alias
        assert _post(ilan_server, f"/tasks/{alias}/bookmarks")["name"] == "bm-task"

    def test_a_done_task_can_be_bookmarked(self, ilan_server: IlanServer, github) -> None:
        _add_task(ilan_server, "bm-task")
        _post(ilan_server, "/tasks/bm-task/done")
        assert _post(ilan_server, "/tasks/bm-task/bookmarks")["ok"] is True

    def test_unknown_task(self, ilan_server: IlanServer, github) -> None:
        assert "not found" in _post(ilan_server, "/tasks/no-such/bookmarks")["error"]

    def test_no_reply_yet(self, ilan_server: IlanServer, github) -> None:
        _add_task(ilan_server, "bm-task", replies=())
        assert "no reply" in _post(ilan_server, "/tasks/bm-task/bookmarks")["error"]
        github.assert_not_called()

    @pytest.mark.parametrize("gist,synced", [(False, 0), (True, 1)])
    def test_a_reply_not_mirrored_yet_is_refused_and_nudged(
        self, ilan_server: IlanServer, github, gist: bool, synced: int,
    ) -> None:
        _add_task(ilan_server, "bm-task", gist=gist)
        with ilan_server.lock:
            task = ilan_server.store.get_task("bm-task")
            task.gist_synced_count = synced
            ilan_server.store.put_task(task)
        with patch.object(ilan_server.gist, "enqueue") as enqueue:
            resp = _post(ilan_server, "/tasks/bm-task/bookmarks")
        assert "not on its Gist yet" in resp["error"]
        enqueue.assert_called_once_with("bm-task")
        github.assert_not_called()
        assert _bookmarks(ilan_server, "bm-task") == []

    def test_a_github_failure_saves_nothing(self, ilan_server: IlanServer, github) -> None:
        _add_task(ilan_server, "bm-task")
        github.side_effect = RuntimeError("GitHub down")
        resp = _post(ilan_server, "/tasks/bm-task/bookmarks")
        assert "GitHub down" in resp["error"]
        assert _bookmarks(ilan_server, "bm-task") == []

    def test_a_missing_comment_saves_nothing(self, ilan_server: IlanServer, github) -> None:
        _add_task(ilan_server, "bm-task")
        github.side_effect = lambda *args: None
        assert "error" in _post(ilan_server, "/tasks/bm-task/bookmarks")
        assert _bookmarks(ilan_server, "bm-task") == []

    def test_the_seventeenth_evicts_the_oldest(self, ilan_server: IlanServer, github) -> None:
        _add_task(ilan_server, "bm-task")
        with ilan_server.lock:
            task = ilan_server.store.get_task("bm-task")
            for minute in range(MAX_BOOKMARKS):
                task.add_bookmark(Bookmark(_stamp(minute), f"https://old/{minute}"))
            ilan_server.store.put_task(task)
        resp = _post(ilan_server, "/tasks/bm-task/bookmarks")
        assert resp["evicted"] == [Bookmark(_stamp(0), "https://old/0").to_dict()]
        assert resp["count"] == MAX_BOOKMARKS
        urls = [b.url for b in _bookmarks(ilan_server, "bm-task")]
        assert "https://old/0" not in urls
        assert urls[-1] == resp["bookmark"]["url"]

    def test_the_same_reply_twice_keeps_one_bookmark(self, ilan_server: IlanServer, github) -> None:
        _add_task(ilan_server, "bm-task", summary=None)
        _post(ilan_server, "/tasks/bm-task/bookmarks")
        with ilan_server.lock:
            task = ilan_server.store.get_task("bm-task")
            task.summary_one_liner = "Arrived later."
            ilan_server.store.put_task(task)
        resp = _post(ilan_server, "/tasks/bm-task/bookmarks")
        assert (resp["replaced"], resp["count"]) == (True, 1)
        assert [b.summary for b in _bookmarks(ilan_server, "bm-task")] == ["Arrived later."]

    def _branched(self, server: IlanServer, *, child_replies: tuple[str, ...]) -> None:
        """``parent-task`` (prompt, reply) branched into ``child-task``."""
        _add_task(server, "parent-task")
        with server.lock:
            parent = server.store.get_task("parent-task")
            parent.gist_id, parent.gist_url = "gid0", PARENT_GIST_URL
            server.store.put_task(parent)
            child = server.store.branch_task(
                parent, "child-task", alias="zz", task_hash="h", now=_stamp(0),
            )
            for role in ("user",) + child_replies:
                server.store.append_log("child-task", role, f"{role} text")
            child = server.store.get_task("child-task")
            child.gist_id, child.gist_url = "gid1", GIST_URL
            child.gist_synced_count = 2 + 1 + len(child_replies)
            server.store.put_task(child)

    def test_a_branch_links_its_own_reply_on_its_own_gist(self, ilan_server: IlanServer, github) -> None:
        self._branched(ilan_server, child_replies=("assistant",))
        resp = _post(ilan_server, "/tasks/child-task/bookmarks")
        assert _looked_up(github) == ("gid1", GIST_URL, "assistant text", 0)
        assert resp["bookmark"]["url"].startswith(GIST_URL)

    def test_a_reply_inherited_at_branching_links_the_parent_gist(
        self, ilan_server: IlanServer, github,
    ) -> None:
        self._branched(ilan_server, child_replies=())
        with ilan_server.lock:
            parent = ilan_server.store.get_task("parent-task")
            ilan_server.store.append_log("parent-task", "user", "the parent moved on")
            parent.gist_synced_count += 1
            ilan_server.store.put_task(parent)
        resp = _post(ilan_server, "/tasks/child-task/bookmarks")
        # Counted back on the parent's Gist, past what the parent added since.
        assert _looked_up(github) == ("gid0", PARENT_GIST_URL, "assistant text", 1)
        assert _bookmarks(ilan_server, "child-task")[0].url == resp["bookmark"]["url"]
        # The bookmark is the child's; the parent gets none.
        assert _bookmarks(ilan_server, "parent-task") == []

    def test_a_reply_inherited_from_a_deleted_task_is_refused(
        self, ilan_server: IlanServer, github,
    ) -> None:
        self._branched(ilan_server, child_replies=())
        ilan_server.store.delete_task("parent-task")
        assert "deleted" in _post(ilan_server, "/tasks/child-task/bookmarks")["error"]
        github.assert_not_called()


def _seed_bookmarks(server: IlanServer, name: str, minutes: list[int], *, engine: str = "claude") -> None:
    _add_task(server, name)
    with server.lock:
        task = server.store.get_task(name)
        task.engine = engine
        for minute in minutes:
            task.add_bookmark(Bookmark(_stamp(minute), f"https://{name}/{minute}", f"{name} {minute}"))
        server.store.put_task(task)


class TestListBookmarks:
    def test_a_task_lists_newest_first(self, ilan_server: IlanServer) -> None:
        _seed_bookmarks(ilan_server, "bm-task", [1, 3, 2])
        resp = _get(ilan_server, "/tasks/bm-task/bookmarks")
        assert resp["name"] == "bm-task"
        assert [b["url"] for b in resp["bookmarks"]] == [
            "https://bm-task/3", "https://bm-task/2", "https://bm-task/1",
        ]

    def test_a_done_task_still_lists_its_bookmarks(self, ilan_server: IlanServer) -> None:
        _seed_bookmarks(ilan_server, "bm-task", [1])
        _post(ilan_server, "/tasks/bm-task/done")
        assert len(_get(ilan_server, "/tasks/bm-task/bookmarks")["bookmarks"]) == 1

    def test_a_discarded_task_lists_by_alias(self, ilan_server: IlanServer) -> None:
        _seed_bookmarks(ilan_server, "bm-task", [1])
        alias = ilan_server.store.get_task("bm-task").alias
        _post(ilan_server, "/tasks/bm-task/discard")
        assert _get(ilan_server, f"/tasks/{alias}/bookmarks")["name"] == "bm-task"

    def test_unknown_task(self, ilan_server: IlanServer) -> None:
        assert "not found" in _get(ilan_server, "/tasks/no-such/bookmarks")["error"]

    def test_all_tasks_newest_first_with_their_names(self, ilan_server: IlanServer) -> None:
        _seed_bookmarks(ilan_server, "task-a", [1, 4])
        _seed_bookmarks(ilan_server, "task-b", [2, 3], engine="codex")
        rows = _get(ilan_server, "/bookmarks")["bookmarks"]
        assert [(r["task"], r["created_at"]) for r in rows] == [
            ("task-a", _stamp(4)), ("task-b", _stamp(3)), ("task-b", _stamp(2)), ("task-a", _stamp(1)),
        ]
        assert rows[1] == {
            "task": "task-b", "engine": "codex", "created_at": _stamp(3),
            "url": "https://task-b/3", "summary": "task-b 3",
        }

    def test_n_keeps_the_most_recent(self, ilan_server: IlanServer) -> None:
        _seed_bookmarks(ilan_server, "task-a", [1, 4])
        _seed_bookmarks(ilan_server, "task-b", [2, 3])
        rows = _get(ilan_server, "/bookmarks?n=2")["bookmarks"]
        assert [r["created_at"] for r in rows] == [_stamp(4), _stamp(3)]

    @pytest.mark.parametrize("n", ["0", "-1", "two"])
    def test_a_bad_n_is_an_error(self, ilan_server: IlanServer, n: str) -> None:
        assert "error" in _get(ilan_server, f"/bookmarks?n={n}")

    def test_none_yet(self, ilan_server: IlanServer) -> None:
        assert _get(ilan_server, "/bookmarks") == {"bookmarks": []}


class TestBookmarksFollowTheTask:
    def test_a_branch_does_not_inherit_them(self, ilan_server: IlanServer) -> None:
        _seed_bookmarks(ilan_server, "parent-task", [1, 2])
        with ilan_server.lock:
            parent = ilan_server.store.get_task("parent-task")
            ilan_server.store.branch_task(parent, "child-task", alias="zz", task_hash="h", now=_stamp(5))
        assert _get(ilan_server, "/tasks/child-task/bookmarks")["bookmarks"] == []
        assert len(_get(ilan_server, "/tasks/parent-task/bookmarks")["bookmarks"]) == 2

    def test_deleting_the_task_deletes_them(self, ilan_server: IlanServer) -> None:
        _seed_bookmarks(ilan_server, "bm-task", [1, 2])
        _seed_bookmarks(ilan_server, "other-task", [3])
        url = f"{ilan_server._test_url}/tasks/bm-task"  # type: ignore[attr-defined]
        with (
            patch("ilan.server.kill_tmux_sessions_by_prefix"),
            urlopen(Request(url, method="DELETE"), timeout=5) as resp,
        ):
            assert json.loads(resp.read())["ok"] is True
        assert [r["task"] for r in _get(ilan_server, "/bookmarks")["bookmarks"]] == ["other-task"]
        # A new task under the same name starts with none.
        _add_task(ilan_server, "bm-task")
        assert _get(ilan_server, "/tasks/bm-task/bookmarks")["bookmarks"] == []

    def test_closing_a_burnable_task_deletes_them(self, ilan_server: IlanServer) -> None:
        _seed_bookmarks(ilan_server, "xxx-scratch", [1])
        with patch("ilan.server.kill_tmux_sessions_by_prefix"):
            assert _post(ilan_server, "/tasks/xxx-scratch/done")["removed"] is True
        assert _get(ilan_server, "/bookmarks")["bookmarks"] == []

    def test_a_rename_carries_them(self, ilan_server: IlanServer) -> None:
        _seed_bookmarks(ilan_server, "bm-task", [1])
        _post(ilan_server, "/tasks/bm-task/rename", {"new_name": "new-name"})
        assert len(_get(ilan_server, "/tasks/new-name/bookmarks")["bookmarks"]) == 1
        assert _get(ilan_server, "/bookmarks")["bookmarks"][0]["task"] == "new-name"


# ── CLI ─────────────────────────────────────────────────────────────────


@pytest.fixture()
def utc(tmp_config: Path) -> None:
    """Show times in UTC, so ``_stamp(m)`` reads ``2026-10-27 20:mm``."""
    cfg.save({**cfg.DEFAULTS, "time-zone": "UTC"})


@pytest.fixture()
def runner() -> CliRunner:
    return CliRunner()


def _client(**responses: dict) -> MagicMock:
    client = MagicMock()
    for method, resp in responses.items():
        getattr(client, method).return_value = resp
    return client


def _row(minute: int, summary: str = "Did the thing.", task: str | None = None) -> dict:
    row = {"created_at": _stamp(minute), "url": f"https://gist/{minute}", "summary": summary}
    if task is not None:
        row.update(task=task, engine="claude")
    return row


def _saved(**overrides) -> dict:
    return {
        "ok": True, "name": "my-task", "bookmark": _row(5), "replaced": False,
        "evicted": [], "count": 3, "limit": MAX_BOOKMARKS, **overrides,
    }


class TestBookmarkCommand:
    @pytest.mark.parametrize("argv", [["bookmark", "aa"], ["task", "bookmark", "aa"]])
    def test_confirms_and_shows_the_bookmark(self, runner: CliRunner, utc, argv) -> None:
        client = _client(add_bookmark=_saved())
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, argv)
        assert result.exit_code == 0, result.output
        client.add_bookmark.assert_called_once_with("aa")
        lines = result.output.splitlines()
        assert lines[0] == f"Bookmarked the latest reply of my-task (3 of {MAX_BOOKMARKS})."
        assert lines[1] == "2026-10-27 20:05  [Link](https://gist/5)  Did the thing."

    def test_says_when_it_updated_a_bookmark(self, runner: CliRunner, utc) -> None:
        client = _client(add_bookmark=_saved(replaced=True))
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["bookmark", "aa"])
        assert result.output.startswith("Updated the bookmark of the latest reply of my-task")

    def test_says_which_bookmark_it_evicted(self, runner: CliRunner, utc) -> None:
        client = _client(add_bookmark=_saved(evicted=[_row(0)], count=MAX_BOOKMARKS))
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["bookmark", "aa"])
        assert (
            f"Removed the oldest bookmark, from 2026-10-27 20:00, to keep {MAX_BOOKMARKS}."
            in result.output
        )

    def test_an_error_exits_1(self, runner: CliRunner, utc) -> None:
        client = _client(add_bookmark={"error": "Bookmarks need Gist mirroring, which is off."})
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["bookmark", "aa"])
        assert result.exit_code == 1
        assert "Gist mirroring" in result.output


# What a server from before bookmarks answers each of their routes with.
OLD_SERVER = {"error": "Not found"}


@pytest.mark.parametrize("argv", [["bookmark", "aa"], ["bookmarks", "aa"], ["bookmarks"]])
def test_an_old_server_asks_for_a_restart(runner: CliRunner, utc, argv) -> None:
    client = _client(add_bookmark=OLD_SERVER, get_bookmarks=OLD_SERVER, list_bookmarks=OLD_SERVER)
    with patch("ilan.cli._client", return_value=client):
        result = runner.invoke(main, argv)
    assert result.exit_code == 1
    assert "predates bookmarks" in result.output
    assert "ilan server restart" in result.output


def test_a_real_old_server_route_miss_is_what_the_cli_expects(ilan_server: IlanServer) -> None:
    """Pin the route-miss text the restart hint keys on."""
    assert _get(ilan_server, "/no-such-route") == OLD_SERVER


class TestBookmarksCommand:
    def test_a_task_s_bookmarks(self, runner: CliRunner, utc) -> None:
        client = _client(get_bookmarks={"name": "my-task", "bookmarks": [_row(9), _row(2, "")]})
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["bookmarks", "aa"])
        assert result.exit_code == 0, result.output
        client.get_bookmarks.assert_called_once_with("aa")
        client.list_bookmarks.assert_not_called()
        # Newest first, as the server sends them; an empty summary leaves
        # nothing behind the link.
        assert result.output.splitlines() == [
            "2026-10-27 20:09  [Link](https://gist/9)  Did the thing.",
            "2026-10-27 20:02  [Link](https://gist/2)",
        ]

    @pytest.mark.parametrize("argv,num", [
        (["bookmarks"], BOOKMARKS_COUNT_DEFAULT),
        (["bookmarks", "-n", "3"], 3),
        (["task", "bookmarks", "--num", "25"], 25),
    ])
    def test_all_tasks_put_the_name_after_the_time(self, runner: CliRunner, utc, argv, num) -> None:
        client = _client(list_bookmarks={"bookmarks": [_row(7, task="task-b"), _row(1, task="task-a")]})
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, argv)
        assert result.exit_code == 0, result.output
        client.list_bookmarks.assert_called_once_with(num)
        assert result.output.splitlines() == [
            "2026-10-27 20:07  task-b  [Link](https://gist/7)  Did the thing.",
            "2026-10-27 20:01  task-a  [Link](https://gist/1)  Did the thing.",
        ]

    @pytest.mark.parametrize("argv", [
        ["bookmarks", "aa", "-n", "3"],
        ["bookmarks", "-n", "3", "aa"],
        ["bookmarks", "-n", "0"],
    ])
    def test_usage_errors_never_reach_the_server(self, runner: CliRunner, argv) -> None:
        with patch("ilan.cli._client") as client:
            result = runner.invoke(main, argv)
        assert result.exit_code == 2
        client.assert_not_called()

    def test_n_with_a_name_says_why(self, runner: CliRunner) -> None:
        with patch("ilan.cli._client"):
            result = runner.invoke(main, ["bookmarks", "aa", "-n", "3"])
        assert "cannot be used with a task name" in result.output

    def test_none_yet(self, runner: CliRunner, utc) -> None:
        client = _client(
            get_bookmarks={"name": "my-task", "bookmarks": []},
            list_bookmarks={"bookmarks": []},
        )
        with patch("ilan.cli._client", return_value=client):
            one = runner.invoke(main, ["bookmarks", "aa"])
            every = runner.invoke(main, ["bookmarks"])
        assert "Task my-task has no bookmarks yet." in one.output
        assert "No bookmarks yet." in every.output

    def test_an_error_exits_1(self, runner: CliRunner, utc) -> None:
        client = _client(get_bookmarks={"error": "Task nope not found"})
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["bookmarks", "nope"])
        assert result.exit_code == 1
        assert "not found" in result.output


class TestBookmarkRendering:
    def test_three_parts_in_three_colours(self, utc) -> None:
        grid = _build_bookmarks_grid([_row(5)], with_task=False)
        time_cell, link_cell, summary_cell = (list(c.cells)[0] for c in grid.columns)
        assert time_cell.plain == "2026-10-27 20:05"
        assert str(time_cell.style) == BOOKMARK_TIME_STYLE
        # The link shows its text and carries the URL as a hyperlink.
        assert link_cell.plain == BOOKMARK_LINK_TEXT
        (span,) = link_cell.spans
        assert str(span.style) == f"{BOOKMARK_LINK_STYLE} link https://gist/5"
        assert (summary_cell.plain, str(summary_cell.style)) == ("Did the thing.", ONE_LINER_STYLE)
        colours = [
            {word for word in style.split() if word not in {"underline", "italic", "bold"}}
            for style in (BOOKMARK_TIME_STYLE, BOOKMARK_LINK_STYLE, ONE_LINER_STYLE)
        ]
        assert all(len(c) == 1 for c in colours)
        assert len(set().union(*colours)) == 3

    def test_all_tasks_add_the_name_column(self, utc) -> None:
        grid = _build_bookmarks_grid([_row(5, task="task-b")], with_task=True)
        name_cell = list(grid.columns[1].cells)[0]
        assert isinstance(name_cell, Text)
        assert name_cell.plain == "task-b"
        assert "orange1" in str(name_cell.style)

    def test_a_narrow_terminal_wraps_only_the_summary(self, utc) -> None:
        summary = "A summary long enough that it has to wrap at sixty columns."
        out = io.StringIO()
        narrow = Console(file=out, force_terminal=True, color_system=None, width=60)
        with patch("ilan.cli.console", narrow):
            _print_bookmarks([_row(5, summary, task="full-of-bookmarks")], with_task=True)
        first, *rest = out.getvalue().splitlines()
        assert first.startswith(f"2026-10-27 20:05  full-of-bookmarks  {BOOKMARK_LINK_TEXT}  A summary")
        assert rest, "the summary should have wrapped"
        # Every continuation line sits under the summary, nothing else.
        indent = first.index("A summary")
        assert all(not line[:indent].strip() for line in rest)
        assert " ".join(" ".join(line.split()) for line in [first[indent:], *rest]) == summary

    def test_a_terminal_shows_link_text_and_hides_the_url(self, utc) -> None:
        url = "https://gist.github.com/u/" + "f" * 32 + "?permalink_comment_id=1#gistcomment-1"
        out = io.StringIO()
        terminal = Console(file=out, force_terminal=True, color_system="256", no_color=False, width=60)
        with patch("ilan.cli.console", terminal):
            _print_bookmarks([{**_row(5), "url": url}], with_task=False)
        rendered = out.getvalue()
        assert f";{url}\x1b\\" in rendered  # the OSC 8 hyperlink target
        visible = re.sub(r"\x1b\]8;[^\x1b]*\x1b\\|\x1b\[[0-9;]*m", "", rendered)
        assert url not in visible
        assert visible.split() == ["2026-10-27", "20:05", BOOKMARK_LINK_TEXT, "Did", "the", "thing."]
