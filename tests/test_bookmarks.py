"""Tests for ``ilan bookmark`` / ``ilan bookmarks``: model, server and CLI."""

from __future__ import annotations

import io
import json
import random
import re
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest
from click.testing import CliRunner
from rich.console import Console
from rich.text import Text

import ilan.server as srv_mod
from ilan import config as cfg
from ilan import gist as gist_mod
from ilan.cli import (
    BOOKMARK_HASH_PREFIX_STYLE,
    BOOKMARK_HASH_REST_STYLE,
    BOOKMARK_LINK_STYLE,
    BOOKMARK_LINK_TEXT,
    BOOKMARK_TIME_STYLE,
    BOOKMARKS_COUNT_DEFAULT,
    _bookmark_lines,
    _print_bookmarks,
    main,
)
from ilan import models as models_mod
from ilan.models import (
    MAX_BOOKMARKS,
    Bookmark,
    LogEntry,
    Task,
    TaskStatus,
    bookmarks_matching,
    new_bookmark_hash,
    unique_prefixes,
)
from ilan.server import IlanServer
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
        task.add_bookmark(Bookmark(_stamp(0), "u0", "summary", hash="0123abcd"))
        d = task.to_dict()
        assert d["bookmarks"] == [
            {"hash": "0123abcd", "created_at": _stamp(0), "url": "u0", "summary": "summary"},
        ]
        assert Task.from_dict(d).bookmarks == task.bookmarks

    def test_a_bookmark_without_a_hash_gets_one_from_its_url(self) -> None:
        first = Bookmark.from_dict({"created_at": _stamp(0), "url": "https://gist/1"})
        again = Bookmark(_stamp(9), "https://gist/1")
        other = Bookmark(_stamp(0), "https://gist/2")
        assert re.fullmatch(r"[0-9a-f]{8}", first.hash)
        # The same on every load, and different for a different reply.
        assert first.hash == again.hash != other.hash


class TestBookmarkHashes:
    def test_a_new_hash_is_eight_hex_digits(self) -> None:
        assert re.fullmatch(r"[0-9a-f]{8}", new_bookmark_hash(set()))

    def test_a_new_hash_avoids_every_taken_one(self, monkeypatch: pytest.MonkeyPatch) -> None:
        draws = iter([bytes.fromhex("aaaa1111"), bytes.fromhex("bbbb2222")])
        monkeypatch.setattr(models_mod.os, "urandom", lambda n: next(draws))
        assert new_bookmark_hash({"aaaa1111"}) == "bbbb2222"

    @pytest.mark.parametrize("hashes,expected", [
        ([], {}),
        # Never shorter than two characters, even alone.
        (["4f3a9c2e"], {"4f3a9c2e": "4f"}),
        (["12345678", "9abcdef0"], {"12345678": "12", "9abcdef0": "9a"}),
        # One more character than the longest start shared with another hash.
        (["abcd1234", "abef5678"], {"abcd1234": "abc", "abef5678": "abe"}),
        (["aa110000", "aa120000", "ab000000"],
         {"aa110000": "aa11", "aa120000": "aa12", "ab000000": "ab"}),
        # Hashes that differ only at the end need all of it.
        (["ab000000", "ab000001"], {"ab000000": "ab000000", "ab000001": "ab000001"}),
    ])
    def test_unique_prefixes(self, hashes: list[str], expected: dict[str, str]) -> None:
        assert unique_prefixes(hashes) == expected

    def test_every_prefix_names_exactly_one_hash(self) -> None:
        rng = random.Random(0)
        hashes = [f"{rng.getrandbits(32):08x}" for _ in range(300)]
        prefixes = unique_prefixes(hashes)
        assert len(prefixes) == 300
        for full, prefix in prefixes.items():
            assert len(prefix) >= 2
            assert [h for h in prefixes if h.startswith(prefix)] == [full]

    def test_matching_takes_any_case(self) -> None:
        one = Task(name="t-one", prompt="p", bookmarks=[Bookmark("t", "u1", hash="abcd1234")])
        two = Task(name="t-two", prompt="p", bookmarks=[Bookmark("t", "u2", hash="abef5678")])
        assert [b.hash for _, b in bookmarks_matching([one, two], "ABC")] == ["abcd1234"]
        assert len(bookmarks_matching([one, two], "ab")) == 2
        assert bookmarks_matching([one, two], "ff") == []

    def test_a_task_saved_before_bookmarks_loads_with_none(self) -> None:
        assert Task.from_dict({"name": "old", "prompt": "p", "status": "WORKING"}).bookmarks == []

    def test_a_missing_summary_loads_as_empty(self) -> None:
        assert Bookmark.from_dict({"created_at": "x", "url": "u", "summary": None}).summary == ""


# ── finding a reply's comment on the Gist ───────────────────────────────


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

    @pytest.mark.parametrize("child_replies", [(), ("user",)])
    def test_a_branch_that_has_not_replied_is_refused(
        self, ilan_server: IlanServer, github, child_replies: tuple[str, ...],
    ) -> None:
        # The child's log holds the parent's reply, inherited at branching,
        # and only the user's messages since.
        self._branched(ilan_server, child_replies=child_replies)
        resp = _post(ilan_server, "/tasks/child-task/bookmarks")
        assert resp["error"] == (
            "Task child-task has no reply of its own yet: it has not answered "
            "since it was branched."
        )
        github.assert_not_called()
        assert _bookmarks(ilan_server, "child-task") == []
        assert _bookmarks(ilan_server, "parent-task") == []

    def test_the_parent_can_still_bookmark_the_reply(self, ilan_server: IlanServer, github) -> None:
        self._branched(ilan_server, child_replies=())
        assert _post(ilan_server, "/tasks/parent-task/bookmarks")["ok"] is True
        assert _looked_up(github) == ("gid0", PARENT_GIST_URL, "assistant text", 0)


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
        row = rows[1]
        assert row == {
            "task": "task-b", "engine": "codex", "created_at": _stamp(3),
            "url": "https://task-b/3", "summary": "task-b 3",
            "hash": Bookmark(_stamp(3), "https://task-b/3").hash, "prefix": row["prefix"],
        }
        assert row["hash"].startswith(row["prefix"]) and len(row["prefix"]) >= 2

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


def _delete(server: IlanServer, path: str) -> dict:
    url = f"{server._test_url}{path}"  # type: ignore[attr-defined]
    try:
        with urlopen(Request(url, method="DELETE"), timeout=5) as resp:
            return json.loads(resp.read())
    except HTTPError as exc:
        return json.loads(exc.read())


def _seed_hashed(server: IlanServer, name: str, hashes: list[str]) -> None:
    """A task holding one bookmark per hash in *hashes*, oldest first."""
    _add_task(server, name)
    with server.lock:
        task = server.store.get_task(name)
        for i, bookmark_hash in enumerate(hashes):
            task.add_bookmark(Bookmark(_stamp(i), f"https://{name}/{i}", f"{name} {i}", bookmark_hash))
        server.store.put_task(task)


@pytest.fixture()
def hashed(ilan_server: IlanServer) -> IlanServer:
    """Hashes ``abc11111`` on task-a, ``abd22222`` and ``f0000000`` on task-b."""
    _seed_hashed(ilan_server, "task-a", ["abc11111"])
    _seed_hashed(ilan_server, "task-b", ["abd22222", "f0000000"])
    return ilan_server


class TestBookmarkHashesOnTheServer:
    def test_a_new_bookmark_gets_a_hash_no_task_has(
        self, ilan_server: IlanServer, github, monkeypatch,
    ) -> None:
        _seed_hashed(ilan_server, "other-task", ["aaaa1111"])
        _add_task(ilan_server, "bm-task")
        taken: list[list[str]] = []

        def fake_new(hashes: list[str]) -> str:
            taken.append(sorted(hashes))
            return "feedc0de"

        monkeypatch.setattr(srv_mod, "new_bookmark_hash", fake_new)
        resp = _post(ilan_server, "/tasks/bm-task/bookmarks")
        assert (resp["bookmark"]["hash"], resp["bookmark"]["prefix"]) == ("feedc0de", "fe")
        # Unique among the bookmarks of every task, not just this one.
        assert taken == [["aaaa1111"]]
        assert _bookmarks(ilan_server, "bm-task")[0].hash == "feedc0de"

    def test_the_hash_is_eight_hex_digits(self, ilan_server: IlanServer, github) -> None:
        _add_task(ilan_server, "bm-task")
        saved = _post(ilan_server, "/tasks/bm-task/bookmarks")["bookmark"]
        assert re.fullmatch(r"[0-9a-f]{8}", saved["hash"])
        assert saved["hash"].startswith(saved["prefix"])

    def test_bookmarking_the_same_reply_keeps_its_hash(self, ilan_server: IlanServer, github) -> None:
        _add_task(ilan_server, "bm-task")
        first = _post(ilan_server, "/tasks/bm-task/bookmarks")["bookmark"]["hash"]
        again = _post(ilan_server, "/tasks/bm-task/bookmarks")
        assert again["replaced"] is True
        assert again["bookmark"]["hash"] == first

    def test_prefixes_are_unique_across_all_tasks(self, hashed: IlanServer) -> None:
        # task-a's only bookmark still needs three characters, for task-b's.
        rows = _get(hashed, "/tasks/task-a/bookmarks")["bookmarks"]
        assert [(r["hash"], r["prefix"]) for r in rows] == [("abc11111", "abc")]
        rows = _get(hashed, "/bookmarks")["bookmarks"]
        assert {r["hash"]: r["prefix"] for r in rows} == {
            "abc11111": "abc", "abd22222": "abd", "f0000000": "f0",
        }

    @pytest.mark.parametrize("ref", ["abc", "ABC", "abc1", "abc11111"])
    def test_a_unique_prefix_or_the_whole_hash_finds_it(self, hashed: IlanServer, ref: str) -> None:
        found = _get(hashed, f"/bookmarks/{ref}")["bookmark"]
        assert (found["task"], found["hash"], found["prefix"]) == ("task-a", "abc11111", "abc")
        assert (found["url"], found["summary"]) == ("https://task-a/0", "task-a 0")

    def test_a_shared_prefix_is_refused_naming_every_match(self, hashed: IlanServer) -> None:
        assert _get(hashed, "/bookmarks/ab")["error"] == (
            "ab starts 2 bookmark hashes: abc11111 (task-a), abd22222 (task-b). "
            "Give more of the hash."
        )

    @pytest.mark.parametrize("ref", ["a", "zz", "ab-c"])
    def test_a_malformed_hash_is_refused(self, hashed: IlanServer, ref: str) -> None:
        assert "is not a bookmark hash" in _get(hashed, f"/bookmarks/{ref}")["error"]

    def test_no_match(self, hashed: IlanServer) -> None:
        assert _get(hashed, "/bookmarks/99")["error"] == "No bookmark has a hash starting with 99."

    def test_removing_deletes_that_bookmark_only(self, hashed: IlanServer) -> None:
        resp = _delete(hashed, "/bookmarks/abd")
        assert resp["ok"] is True
        removed = resp["bookmark"]
        assert (removed["task"], removed["hash"], removed["prefix"]) == ("task-b", "abd22222", "abd")
        # Its task keeps its other bookmark, and the other task keeps its own.
        assert [b.hash for b in _bookmarks(hashed, "task-b")] == ["f0000000"]
        assert [b.hash for b in _bookmarks(hashed, "task-a")] == ["abc11111"]
        assert "No bookmark" in _get(hashed, "/bookmarks/abd22222")["error"]
        # With its neighbour gone, the other hash's prefix shrinks back to two.
        assert _get(hashed, "/bookmarks/ab")["bookmark"]["prefix"] == "ab"

    def test_removing_by_a_shared_prefix_removes_nothing(self, hashed: IlanServer) -> None:
        assert "Give more of the hash" in _delete(hashed, "/bookmarks/ab")["error"]
        assert len(_bookmarks(hashed, "task-a")) == 1
        assert len(_bookmarks(hashed, "task-b")) == 2

    def test_a_closed_task_s_bookmark_can_be_removed(self, hashed: IlanServer) -> None:
        _post(hashed, "/tasks/task-a/done")
        assert _delete(hashed, "/bookmarks/abc")["ok"] is True
        assert _bookmarks(hashed, "task-a") == []


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
    """A bookmark as the server returns it; its hash is ``<minute>c0ffee`` in hex."""
    row = {
        "created_at": _stamp(minute), "url": f"https://gist/{minute}", "summary": summary,
        "hash": f"{minute:02x}c0ffee", "prefix": f"{minute:02x}c",
    }
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
        assert result.output.splitlines() == [
            f"Bookmarked the latest reply of my-task (3 of {MAX_BOOKMARKS}).",
            "2026-10-27 20:05  [Link](https://gist/5)  05c0ffee",
            "Did the thing.",
        ]

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
            f"Removed the oldest bookmark, 00c0ffee from 2026-10-27 20:00, to keep {MAX_BOOKMARKS}."
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
        # Newest first, as the server sends them: the time and link, the
        # summary under them, and a blank line before the next bookmark. A
        # bookmark with no summary is its first line alone.
        assert result.output.splitlines() == [
            "2026-10-27 20:09  [Link](https://gist/9)  09c0ffee",
            "Did the thing.",
            "",
            "2026-10-27 20:02  [Link](https://gist/2)  02c0ffee",
        ]

    def test_piped_a_long_summary_stays_on_one_line(self, runner: CliRunner, utc) -> None:
        summary = " ".join(["word"] * 40)  # 199 characters, far past 80 columns
        client = _client(get_bookmarks={"name": "my-task", "bookmarks": [_row(9, summary)]})
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["bookmarks", "aa"])
        assert result.output.splitlines() == ["2026-10-27 20:09  [Link](https://gist/9)  09c0ffee", summary]

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
            "2026-10-27 20:07  task-b  [Link](https://gist/7)  07c0ffee",
            "Did the thing.",
            "",
            "2026-10-27 20:01  task-a  [Link](https://gist/1)  01c0ffee",
            "Did the thing.",
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


def _found(**overrides) -> dict:
    """What the server answers when a hash names one bookmark."""
    return {"bookmark": {**_row(5, task="my-task"), **overrides}}


class TestBookmarkOpen:
    def test_opens_the_link_in_the_browser(self, runner: CliRunner, utc) -> None:
        client = _client(get_bookmark=_found())
        with patch("ilan.cli._client", return_value=client), \
                patch("ilan.cli.webbrowser.open", return_value=True) as browser:
            result = runner.invoke(main, ["bookmark-open", "05C"])
        assert result.exit_code == 0, result.output
        # Any case is accepted, and asked about in lower case.
        client.get_bookmark.assert_called_once_with("05c")
        browser.assert_called_once_with("https://gist/5")
        assert result.output == "Opening bookmark 05c0ffee of my-task.\n"

    @pytest.mark.parametrize("ref", ["0", "zz", "05-c"])
    def test_a_malformed_hash_never_reaches_the_server(self, runner: CliRunner, ref: str) -> None:
        with patch("ilan.cli._client") as client, patch("ilan.cli.webbrowser.open") as browser:
            result = runner.invoke(main, ["bookmark-open", ref])
        assert result.exit_code == 2
        assert "at least 2 characters" in result.output
        client.assert_not_called()
        browser.assert_not_called()

    def test_an_unknown_or_shared_prefix_exits_1(self, runner: CliRunner, utc) -> None:
        error = "ab starts 2 bookmark hashes: abc11111 (a), abd22222 (b). Give more of the hash."
        client = _client(get_bookmark={"error": error})
        with patch("ilan.cli._client", return_value=client), \
                patch("ilan.cli.webbrowser.open") as browser:
            result = runner.invoke(main, ["bookmark-open", "ab"])
        assert result.exit_code == 1
        assert "Give more of the hash." in result.output
        browser.assert_not_called()

    def test_without_a_browser_it_prints_the_link(self, runner: CliRunner, utc) -> None:
        client = _client(get_bookmark=_found())
        with patch("ilan.cli._client", return_value=client), \
                patch("ilan.cli.webbrowser.open", return_value=False):
            result = runner.invoke(main, ["bookmark-open", "05c"])
        assert result.exit_code == 1
        assert "The link is https://gist/5" in result.output


class TestBookmarkRm:
    REMOVED = {"ok": True, "bookmark": _row(5, task="my-task")}

    def test_shows_it_asks_and_removes_it_by_its_whole_hash(self, runner: CliRunner, utc) -> None:
        client = _client(get_bookmark=_found(), remove_bookmark=self.REMOVED)
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["bookmark-rm", "05c"], input="y\n")
        assert result.exit_code == 0, result.output
        client.get_bookmark.assert_called_once_with("05c")
        client.remove_bookmark.assert_called_once_with("05c0ffee")
        lines = result.output.splitlines()
        assert lines[:2] == [
            "2026-10-27 20:05  my-task  [Link](https://gist/5)  05c0ffee",
            "Did the thing.",
        ]
        assert lines[2].startswith("Remove this bookmark permanently? [y/N]:")
        assert lines[-1] == "Removed bookmark 05c0ffee of my-task."

    @pytest.mark.parametrize("answer", ["n\n", "\n"])
    def test_anything_but_yes_keeps_it(self, runner: CliRunner, utc, answer: str) -> None:
        client = _client(get_bookmark=_found(), remove_bookmark=self.REMOVED)
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["bookmark-rm", "05c"], input=answer)
        assert result.exit_code == 0, result.output
        client.remove_bookmark.assert_not_called()
        assert "Removed" not in result.output

    def test_yes_skips_the_question(self, runner: CliRunner, utc) -> None:
        client = _client(get_bookmark=_found(), remove_bookmark=self.REMOVED)
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["bookmark-rm", "-y", "05c"])
        assert result.exit_code == 0, result.output
        assert "Remove this bookmark permanently?" not in result.output
        client.remove_bookmark.assert_called_once_with("05c0ffee")

    def test_an_unknown_hash_asks_nothing(self, runner: CliRunner, utc) -> None:
        client = _client(get_bookmark={"error": "No bookmark has a hash starting with 99."})
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["bookmark-rm", "99"], input="y\n")
        assert result.exit_code == 1
        assert "No bookmark has a hash starting with 99." in result.output
        assert "Remove this bookmark" not in result.output
        client.remove_bookmark.assert_not_called()

    def test_a_failed_removal_exits_1(self, runner: CliRunner, utc) -> None:
        client = _client(get_bookmark=_found(), remove_bookmark={"error": "No bookmark has a hash starting with 05c0ffee."})
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["bookmark-rm", "-y", "05c"])
        assert result.exit_code == 1
        assert "Removed" not in result.output

    def test_a_malformed_hash_never_reaches_the_server(self, runner: CliRunner) -> None:
        with patch("ilan.cli._client") as client:
            result = runner.invoke(main, ["bookmark-rm", "x"])
        assert result.exit_code == 2
        client.assert_not_called()


@pytest.mark.parametrize("argv", [["bookmark-open", "ab"], ["bookmark-rm", "-y", "ab"]])
def test_an_old_server_asks_for_a_restart_on_the_new_commands(runner: CliRunner, utc, argv) -> None:
    client = _client(get_bookmark=OLD_SERVER, remove_bookmark=OLD_SERVER)
    with patch("ilan.cli._client", return_value=client), patch("ilan.cli.webbrowser.open") as browser:
        result = runner.invoke(main, argv)
    assert result.exit_code == 1
    assert "predates bookmarks" in result.output
    browser.assert_not_called()


def test_main_help_lists_the_new_commands_whole(runner: CliRunner) -> None:
    result = runner.invoke(main, ["-h"], terminal_width=120)
    rows = {
        line.split(None, 1)[0]: line.split(None, 1)[1]
        for line in result.output.splitlines()
        if line.startswith("  bookmark")
    }
    assert rows["bookmark-open"] == "Open a bookmark in your browser."
    assert rows["bookmark-rm"] == "Remove a bookmark permanently."


def _span_styles(line: Text) -> dict[str, str]:
    """Each styled stretch of *line*, by its text."""
    return {line.plain[s.start:s.end]: str(s.style) for s in line.spans}


class TestBookmarkRendering:
    def test_three_parts_in_three_colours(self, utc) -> None:
        head, summary = _bookmark_lines(_row(5), terminal=True, with_task=False)
        assert head.plain == f"2026-10-27 20:05  {BOOKMARK_LINK_TEXT}  05c0ffee"
        # The link shows its text and carries the URL as a hyperlink. The
        # separator is unstyled, so the underline and the link stop at the word.
        assert _span_styles(head) == {
            "2026-10-27 20:05": BOOKMARK_TIME_STYLE,
            BOOKMARK_LINK_TEXT: f"{BOOKMARK_LINK_STYLE} link https://gist/5",
            # The hash: its unique prefix set apart from the rest.
            "05c": BOOKMARK_HASH_PREFIX_STYLE,
            "0ffee": BOOKMARK_HASH_REST_STYLE,
        }
        assert (summary.plain, str(summary.style)) == ("Did the thing.", ONE_LINER_STYLE)
        styles = (
            BOOKMARK_TIME_STYLE, BOOKMARK_LINK_STYLE, ONE_LINER_STYLE,
            BOOKMARK_HASH_PREFIX_STYLE, BOOKMARK_HASH_REST_STYLE,
        )
        colours = [
            {word for word in style.split() if word not in {"underline", "italic", "bold"}}
            for style in styles
        ]
        # One colour each, and no two parts sharing one: in particular the
        # hash's prefix stands out from the rest of the hash.
        assert all(len(c) == 1 for c in colours)
        assert len(set().union(*colours)) == len(styles)
        assert "underline" in BOOKMARK_HASH_PREFIX_STYLE.split()
        assert "underline" not in BOOKMARK_HASH_REST_STYLE.split()

    def test_all_tasks_put_the_name_between_time_and_link(self, utc) -> None:
        head, _ = _bookmark_lines(_row(5, task="task-b"), terminal=True, with_task=True)
        assert head.plain == f"2026-10-27 20:05  task-b  {BOOKMARK_LINK_TEXT}  05c0ffee"
        assert _span_styles(head)["task-b"] == "bold orange1"

    def test_no_summary_is_one_line(self, utc) -> None:
        assert len(_bookmark_lines(_row(5, " "), terminal=True, with_task=False)) == 1

    def test_a_long_summary_wraps_on_lines_of_its_own(self, utc) -> None:
        summary = "A summary long enough that it has to wrap in a forty-column window."
        out = io.StringIO()
        narrow = Console(file=out, force_terminal=True, color_system=None, width=40)
        with patch("ilan.cli.console", narrow):
            _print_bookmarks([_row(5, summary), _row(1)], with_task=False)
        lines = out.getvalue().splitlines()
        blank = lines.index("")
        first, wrapped, second = lines[0], lines[1:blank], lines[blank + 1:]
        assert first == f"2026-10-27 20:05  {BOOKMARK_LINK_TEXT}  05c0ffee"
        # The summary starts at the left edge and wraps there, word by word.
        assert len(wrapped) > 1
        assert all(len(line) <= 40 for line in wrapped)
        assert " ".join(" ".join(wrapped).split()) == summary
        assert second == [f"2026-10-27 20:01  {BOOKMARK_LINK_TEXT}  01c0ffee", "Did the thing."]

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
        assert visible.splitlines() == [f"2026-10-27 20:05  {BOOKMARK_LINK_TEXT}  05c0ffee", "Did the thing."]
