"""Tests for burnable (``xxx-``) tasks — name generation, store, server, CLI.

A burnable task is one whose name starts with ``xxx-``. ``ilan add`` mints such
a name when no ``-n`` is given, and ``done`` / ``discard`` delete a burnable
task outright instead of closing it. Burnability is derived from the current
name alone, so renaming moves a task in and out of it.
"""

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

from ilan.cli import main
from ilan.models import (
    BURNABLE_NOUNS,
    BURNABLE_PREFIX,
    BURNABLE_VERBS,
    Task,
    TaskStatus,
    is_burnable_name,
    random_burnable_name,
    validate_task_name,
)
from ilan.runner import Runner
from ilan.server import IlanServer
from ilan.store import Store

from tests.helpers import wait_until_serving

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
_HASH_NAME_RE = re.compile(rf"^{re.escape(BURNABLE_PREFIX)}[0-9a-f]{{8}}$")


def _strip_ansi(s: str) -> str:
    return _ANSI_RE.sub("", s)


def _unwrap(s: str) -> str:
    """Collapse whitespace so asserts survive Rich wrapping the line."""
    return " ".join(s.split())


# ── name generation (models) ────────────────────────────────────────────


class TestIsBurnableName:
    @pytest.mark.parametrize("name", [
        "xxx-cat-likes-fin",
        "xxx-a",
        "xxx--",
        "xxx-",
    ])
    def test_burnable(self, name: str) -> None:
        assert is_burnable_name(name) is True

    @pytest.mark.parametrize("name", [
        "fix-bug",
        "xxx",            # the hyphen is part of the marker
        "xx-cat",
        "xxxcat",
        "my-xxx-task",    # the prefix has to lead
        "-xxx-cat",
        "XXX-cat",        # matched exactly, so upper case is a normal task
        "Xxx-cat",
        "",
    ])
    def test_not_burnable(self, name: str) -> None:
        assert is_burnable_name(name) is False


class TestRandomBurnableName:
    def test_prefix_and_shape(self) -> None:
        name = random_burnable_name()
        assert name.startswith(BURNABLE_PREFIX)
        subject, verb, obj = name[len(BURNABLE_PREFIX):].split("-")
        assert subject in BURNABLE_NOUNS
        assert verb in BURNABLE_VERBS
        assert obj in BURNABLE_NOUNS

    def test_the_two_nouns_always_differ(self) -> None:
        for _ in range(200):
            subject, _verb, obj = random_burnable_name()[len(BURNABLE_PREFIX):].split("-")
            assert subject != obj

    def test_generated_names_are_valid_task_names(self) -> None:
        """A minted name must survive the same validation a typed one does."""
        for _ in range(200):
            assert validate_task_name(random_burnable_name()) is None

    def test_generated_names_are_burnable(self) -> None:
        for _ in range(50):
            assert is_burnable_name(random_burnable_name()) is True

    def test_draws_vary(self) -> None:
        assert len({random_burnable_name() for _ in range(200)}) > 20

    @pytest.mark.parametrize("pool", [BURNABLE_NOUNS, BURNABLE_VERBS])
    def test_pool_words_are_hyphen_free_lower_case_letters(
        self, pool: tuple[str, ...]
    ) -> None:
        """Names are parsed and validated by splitting on ``-``, so a word
        carrying a hyphen (or anything a task name may not contain) would
        silently break both."""
        for word in pool:
            assert re.fullmatch(r"[a-z]+", word), word

    @pytest.mark.parametrize("pool", [BURNABLE_NOUNS, BURNABLE_VERBS])
    def test_pools_have_no_duplicates(self, pool: tuple[str, ...]) -> None:
        assert len(set(pool)) == len(pool)

    def test_the_pools_are_disjoint(self) -> None:
        """An overlap would make ``xxx-naps-naps-cat`` readable two ways."""
        assert not set(BURNABLE_NOUNS) & set(BURNABLE_VERBS)

    def test_nouns_pool_is_big_enough_to_sample_two(self) -> None:
        assert len(BURNABLE_NOUNS) >= 2


# ── unique names (store) ────────────────────────────────────────────────


class TestNextAvailableBurnableName:
    def test_returns_a_free_valid_burnable_name(self, tmp_workdir: Path) -> None:
        store = Store(tmp_workdir)
        name = store.next_available_burnable_name()
        assert is_burnable_name(name)
        assert validate_task_name(name) is None
        assert store.get_task(name) is None

    def test_skips_a_taken_name(
        self, tmp_workdir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store = Store(tmp_workdir)
        store.put_task(Task(name="xxx-taken-word", prompt="p"))
        draws = iter(["xxx-taken-word", "xxx-free-word"])
        monkeypatch.setattr("ilan.store.random_burnable_name", lambda: next(draws))
        assert store.next_available_burnable_name() == "xxx-free-word"

    def test_avoids_a_closed_task_too(
        self, tmp_workdir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A DONE task still owns its name (and its log file), so reusing it
        would collide."""
        store = Store(tmp_workdir)
        store.put_task(Task(name="xxx-old-word", prompt="p", status=TaskStatus.DONE))
        draws = iter(["xxx-old-word", "xxx-new-word"])
        monkeypatch.setattr("ilan.store.random_burnable_name", lambda: next(draws))
        assert store.next_available_burnable_name() == "xxx-new-word"

    def test_falls_back_to_a_hash_when_every_draw_collides(
        self, tmp_workdir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store = Store(tmp_workdir)
        store.put_task(Task(name="xxx-taken-word", prompt="p"))
        monkeypatch.setattr(
            "ilan.store.random_burnable_name", lambda: "xxx-taken-word"
        )
        name = store.next_available_burnable_name(attempts=3)
        assert _HASH_NAME_RE.match(name), name
        assert validate_task_name(name) is None
        assert store.get_task(name) is None

    def test_attempts_bounds_the_number_of_draws(
        self, tmp_workdir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store = Store(tmp_workdir)
        store.put_task(Task(name="xxx-taken-word", prompt="p"))
        draw = MagicMock(return_value="xxx-taken-word")
        monkeypatch.setattr("ilan.store.random_burnable_name", draw)
        store.next_available_burnable_name(attempts=2)
        assert draw.call_count == 2

    def test_hash_fallback_retries_on_collision(
        self, tmp_workdir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The fallback is checked against the store as well, so even a taken
        hash name is not handed out."""
        store = Store(tmp_workdir)
        store.put_task(Task(name=f"{BURNABLE_PREFIX}aaaaaaaa", prompt="p"))
        monkeypatch.setattr(
            "ilan.store.random_burnable_name", lambda: f"{BURNABLE_PREFIX}aaaaaaaa"
        )
        hashes = iter(["aaaaaaaa", "aaaaaaaa", "bbbbbbbb"])
        monkeypatch.setattr("ilan.store.generate_task_hash", lambda: next(hashes))
        assert store.next_available_burnable_name(attempts=1) == f"{BURNABLE_PREFIX}bbbbbbbb"


# ── server ──────────────────────────────────────────────────────────────


@pytest.fixture()
def ilan_server(tmp_workdir: Path, tmp_config: Path, env_with_mock_claude: None):
    """Start an IlanServer on an ephemeral port with a non-spawning runner."""
    import ilan.config as cfg_mod

    cfg_mod.save({**cfg_mod.DEFAULTS, "workdir": str(tmp_workdir)})

    server = IlanServer()
    server.runner.start = lambda task: True  # type: ignore[method-assign]
    server.runner.kill = MagicMock()  # type: ignore[method-assign]
    server.runner.reap_finished = lambda: None  # type: ignore[method-assign]

    with patch.object(signal, "signal"):
        t = threading.Thread(
            target=server.run,
            kwargs={"host": "127.0.0.1", "port": 0, "poll_interval": 0.01},
            daemon=True,
        )
        t.start()

        port = wait_until_serving(server)
        server._test_url = f"http://127.0.0.1:{port}"  # type: ignore[attr-defined]

        yield server

        server.shutdown()
        t.join(timeout=3)


def _post(server: IlanServer, path: str, body: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body else None
    req = Request(f"{server._test_url}{path}", data=data, method="POST")  # type: ignore[attr-defined]
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read())
    except HTTPError as exc:
        return exc.code, json.loads(exc.read())


def _get(server: IlanServer, path: str) -> tuple[int, dict]:
    req = Request(f"{server._test_url}{path}")  # type: ignore[attr-defined]
    try:
        with urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read())
    except HTTPError as exc:
        return exc.code, json.loads(exc.read())


def _seed(
    server: IlanServer,
    name: str,
    *,
    status: TaskStatus = TaskStatus.WORKING,
    task_hash: str = "1111aaaa",
    parent_name: str | None = None,
) -> Task:
    task = Task(
        name=name, prompt="p", status=status, created_at="2026-08-12T00:00:00+00:00",
        status_changed_at="2026-08-12T00:00:00+00:00", alias="aa",
        task_hash=task_hash, parent_name=parent_name,
    )
    server.store.put_task(task)
    return task


class TestAddWithoutAName:
    def test_server_mints_a_burnable_name(self, ilan_server: IlanServer) -> None:
        code, body = _post(ilan_server, "/tasks", {"prompt": "do a quick thing"})
        assert code == 200
        assert body["ok"] is True
        name = body["name"]
        assert is_burnable_name(name)
        assert ilan_server.store.get_task(name) is not None

    def test_the_minted_task_is_a_normal_task_otherwise(
        self, ilan_server: IlanServer
    ) -> None:
        _code, body = _post(ilan_server, "/tasks", {"prompt": "build X"})
        task = ilan_server.store.get_task(body["name"])
        assert task.prompt == "build X"
        assert task.alias is not None
        assert task.task_hash is not None
        assert task.engine == "claude"
        logs = _get(ilan_server, f"/tasks/{body['name']}/logs")[1]["logs"]
        assert [(e["role"], e["content"]) for e in logs] == [("user", "build X")]

    def test_two_unnamed_adds_get_different_names(
        self, ilan_server: IlanServer
    ) -> None:
        first = _post(ilan_server, "/tasks", {"prompt": "a"})[1]["name"]
        second = _post(ilan_server, "/tasks", {"prompt": "b"})[1]["name"]
        assert first != second
        assert len(ilan_server.store.load_tasks()) == 2

    def test_an_explicit_name_is_echoed_back(self, ilan_server: IlanServer) -> None:
        code, body = _post(ilan_server, "/tasks", {"name": "fix-bug", "prompt": "p"})
        assert code == 200
        assert body["name"] == "fix-bug"

    def test_an_empty_name_is_still_an_error(self, ilan_server: IlanServer) -> None:
        """``-n ""`` is a mistake, not a request for a generated name."""
        code, body = _post(ilan_server, "/tasks", {"name": "", "prompt": "p"})
        assert code == 400
        assert "3 characters" in body["error"]
        assert ilan_server.store.load_tasks() == {}

    def test_a_whitespace_only_name_is_an_error(self, ilan_server: IlanServer) -> None:
        code, _body = _post(ilan_server, "/tasks", {"name": "   ", "prompt": "p"})
        assert code == 400
        assert ilan_server.store.load_tasks() == {}

    def test_a_padded_name_is_stripped(self, ilan_server: IlanServer) -> None:
        code, body = _post(ilan_server, "/tasks", {"name": "  fix-bug  ", "prompt": "p"})
        assert code == 200
        assert body["name"] == "fix-bug"
        assert ilan_server.store.get_task("fix-bug") is not None

    def test_an_invalid_explicit_name_is_still_rejected(
        self, ilan_server: IlanServer
    ) -> None:
        code, _body = _post(ilan_server, "/tasks", {"name": "has space", "prompt": "p"})
        assert code == 400
        assert ilan_server.store.load_tasks() == {}

    def test_an_unnamed_max_task_still_lands_on_fable(
        self, ilan_server: IlanServer
    ) -> None:
        _code, body = _post(ilan_server, "/tasks", {"prompt": "p", "max": True})
        task = ilan_server.store.get_task(body["name"])
        assert is_burnable_name(task.name)
        assert task.model == "claude-fable-5"


class TestBurnOnDone:
    def test_a_burnable_task_is_deleted_instead_of_closed(
        self, ilan_server: IlanServer
    ) -> None:
        _seed(ilan_server, "xxx-cat-likes-fin")
        code, body = _post(ilan_server, "/tasks/xxx-cat-likes-fin/done")
        assert code == 200
        assert body == {"ok": True, "name": "xxx-cat-likes-fin", "removed": True}
        assert ilan_server.store.get_task("xxx-cat-likes-fin") is None

    def test_the_log_file_goes_too(self, ilan_server: IlanServer) -> None:
        _seed(ilan_server, "xxx-cat-likes-fin")
        ilan_server.store.append_log("xxx-cat-likes-fin", "user", "hello")
        assert ilan_server.store.log_path("xxx-cat-likes-fin").exists()
        _post(ilan_server, "/tasks/xxx-cat-likes-fin/done")
        assert not ilan_server.store.log_path("xxx-cat-likes-fin").exists()

    def test_a_burned_task_is_gone_from_the_closed_list(
        self, ilan_server: IlanServer
    ) -> None:
        _seed(ilan_server, "xxx-cat-likes-fin")
        _post(ilan_server, "/tasks/xxx-cat-likes-fin/done")
        names = [t["name"] for t in _get(ilan_server, "/tasks?all=true")[1]["tasks"]]
        assert names == []

    def test_no_task_number_is_minted(self, ilan_server: IlanServer) -> None:
        """Numbers exist to revive closed tasks; a burned one can't be revived,
        so it must not consume one."""
        _seed(ilan_server, "keeper")
        _seed(ilan_server, "xxx-cat-likes-fin")
        _post(ilan_server, "/tasks/xxx-cat-likes-fin/done")
        _post(ilan_server, "/tasks/keeper/done")
        assert ilan_server.store.get_task("keeper").number == 1

    def test_a_burned_task_cannot_be_revived(self, ilan_server: IlanServer) -> None:
        _seed(ilan_server, "xxx-cat-likes-fin")
        _post(ilan_server, "/tasks/xxx-cat-likes-fin/done")
        code, body = _post(ilan_server, "/tasks/xxx-cat-likes-fin/undone")
        assert code == 404
        assert "error" in body

    def test_a_working_agent_is_killed_first(self, ilan_server: IlanServer) -> None:
        task = _seed(ilan_server, "xxx-cat-likes-fin", status=TaskStatus.WORKING)
        _post(ilan_server, "/tasks/xxx-cat-likes-fin/done")
        ilan_server.runner.kill.assert_called_once()
        assert ilan_server.runner.kill.call_args[0][0].name == task.name

    def test_a_finished_agent_is_not_killed(self, ilan_server: IlanServer) -> None:
        _seed(ilan_server, "xxx-cat-likes-fin", status=TaskStatus.AGENT_FINISHED)
        _post(ilan_server, "/tasks/xxx-cat-likes-fin/done")
        ilan_server.runner.kill.assert_not_called()

    def test_the_tmux_sessions_are_killed(self, ilan_server: IlanServer) -> None:
        _seed(ilan_server, "xxx-cat-likes-fin", task_hash="beefcafe")
        with patch("ilan.server.kill_tmux_sessions_by_prefix") as kill_tmux:
            _post(ilan_server, "/tasks/xxx-cat-likes-fin/done")
        kill_tmux.assert_called_once_with("beefcafe")

    def test_children_survive_and_are_reparented(
        self, ilan_server: IlanServer
    ) -> None:
        """Burning is a delete, so it re-parents the branch tree the same way
        ``ilan rm`` does rather than orphaning children."""
        _seed(ilan_server, "grandparent")
        _seed(ilan_server, "xxx-cat-likes-fin", parent_name="grandparent")
        _seed(ilan_server, "child", parent_name="xxx-cat-likes-fin")
        _post(ilan_server, "/tasks/xxx-cat-likes-fin/done")
        child = ilan_server.store.get_task("child")
        assert child is not None
        assert child.parent_name == "grandparent"
        assert child.deleted_ancestors == ["xxx-cat-likes-fin"]

    def test_a_normal_task_still_closes(self, ilan_server: IlanServer) -> None:
        _seed(ilan_server, "fix-bug")
        code, body = _post(ilan_server, "/tasks/fix-bug/done")
        assert code == 200
        assert body == {"ok": True, "name": "fix-bug", "removed": False}
        task = ilan_server.store.get_task("fix-bug")
        assert task.status == TaskStatus.DONE
        assert task.alias is None
        assert task.number == 1

    def test_burning_by_alias_works(self, ilan_server: IlanServer) -> None:
        """The prefix is checked on the resolved task, not on what was typed."""
        _seed(ilan_server, "xxx-cat-likes-fin")
        code, body = _post(ilan_server, "/tasks/aa/done")
        assert code == 200
        assert body["removed"] is True
        assert ilan_server.store.get_task("xxx-cat-likes-fin") is None


class TestBurnOnDiscard:
    def test_a_burnable_task_is_deleted_instead_of_discarded(
        self, ilan_server: IlanServer
    ) -> None:
        _seed(ilan_server, "xxx-cat-likes-fin")
        code, body = _post(ilan_server, "/tasks/xxx-cat-likes-fin/discard")
        assert code == 200
        assert body == {"ok": True, "name": "xxx-cat-likes-fin", "removed": True}
        assert ilan_server.store.get_task("xxx-cat-likes-fin") is None

    def test_a_burned_task_cannot_be_undiscarded(
        self, ilan_server: IlanServer
    ) -> None:
        _seed(ilan_server, "xxx-cat-likes-fin")
        _post(ilan_server, "/tasks/xxx-cat-likes-fin/discard")
        code, _body = _post(ilan_server, "/tasks/xxx-cat-likes-fin/undiscard")
        assert code == 404

    def test_a_working_agent_is_killed_first(self, ilan_server: IlanServer) -> None:
        _seed(ilan_server, "xxx-cat-likes-fin", status=TaskStatus.WORKING)
        _post(ilan_server, "/tasks/xxx-cat-likes-fin/discard")
        ilan_server.runner.kill.assert_called_once()

    def test_a_normal_task_still_discards(self, ilan_server: IlanServer) -> None:
        _seed(ilan_server, "fix-bug")
        code, body = _post(ilan_server, "/tasks/fix-bug/discard")
        assert code == 200
        assert body == {"ok": True, "name": "fix-bug", "removed": False}
        task = ilan_server.store.get_task("fix-bug")
        assert task.status == TaskStatus.DISCARDED
        # A discarded task keeps its alias so ``undiscard`` can find it.
        assert task.alias == "aa"


class TestRenameMovesTasksInAndOutOfBurnable:
    def test_renaming_away_from_the_prefix_makes_a_task_keepable(
        self, ilan_server: IlanServer
    ) -> None:
        _seed(ilan_server, "xxx-cat-likes-fin")
        _post(ilan_server, "/tasks/xxx-cat-likes-fin/rename", {"new_name": "fix-bug"})
        code, body = _post(ilan_server, "/tasks/fix-bug/done")
        assert code == 200
        assert body["removed"] is False
        assert ilan_server.store.get_task("fix-bug").status == TaskStatus.DONE

    def test_renaming_into_the_prefix_makes_a_task_burnable(
        self, ilan_server: IlanServer
    ) -> None:
        _seed(ilan_server, "fix-bug")
        _post(ilan_server, "/tasks/fix-bug/rename", {"new_name": "xxx-fix-bug"})
        code, body = _post(ilan_server, "/tasks/xxx-fix-bug/discard")
        assert code == 200
        assert body["removed"] is True
        assert ilan_server.store.get_task("xxx-fix-bug") is None
        assert ilan_server.store.get_task("fix-bug") is None


def _seed_branchable_parent(server: IlanServer, name: str = "parent-task") -> Task:
    """Seed a parent with an established session, so it can be branched."""
    parent = Task(
        name=name, prompt="root prompt", created_at="2026-08-12T00:00:00+00:00",
        status_changed_at="2026-08-12T00:00:00+00:00", session_id="sid-1",
        session_log_path="/fake/sid-1.jsonl", alias="aa", task_hash="abcd1234",
    )
    server.store.put_task(parent)
    server.store.append_log(name, "user", "hello")
    server.store.append_log(name, "assistant", "hi")
    return parent


def _branch(
    server: IlanServer, parent: str, body: dict
) -> tuple[int, dict]:
    """POST a branch request with the parent's session log made to look present."""
    with patch.object(Runner, "find_session_log", return_value=Path("/fake/sid-1.jsonl")):
        return _post(server, f"/tasks/{parent}/branch", body)


class TestBranchWithoutAName:
    def test_server_mints_a_burnable_child_name(self, ilan_server: IlanServer) -> None:
        _seed_branchable_parent(ilan_server)
        code, body = _branch(ilan_server, "parent-task", {"message": "try plan B"})
        assert code == 200, body
        assert body["ok"] is True
        assert body["parent_name"] == "parent-task"
        child_name = body["name"]
        assert is_burnable_name(child_name)
        assert ilan_server.store.get_task(child_name) is not None

    def test_the_minted_child_is_a_normal_branch_otherwise(
        self, ilan_server: IlanServer
    ) -> None:
        """A generated name must not cost the child anything a named one gets:
        the inherited session, log, first assignment, and branch notice."""
        _seed_branchable_parent(ilan_server)
        _code, body = _branch(ilan_server, "parent-task", {"message": "try plan B"})
        child = ilan_server.store.get_task(body["name"])
        assert child.parent_name == "parent-task"
        assert child.session_id == "sid-1"
        assert child.cached_replies == ["try plan B"]
        assert child.awaiting_branch_notice is True
        assert child.alias is not None and child.alias != "aa"
        assert child.gist_branch_point == 2
        assert child.gist_branch_parent_name == "parent-task"
        assert [e.content for e in ilan_server.store.read_logs(child.name)] == [
            "hello", "hi", "try plan B",
        ]

    def test_two_unnamed_branches_get_different_names(
        self, ilan_server: IlanServer
    ) -> None:
        _seed_branchable_parent(ilan_server)
        first = _branch(ilan_server, "parent-task", {"message": "a"})[1]["name"]
        second = _branch(ilan_server, "parent-task", {"message": "b"})[1]["name"]
        assert first != second
        assert len(ilan_server.store.load_tasks()) == 3  # parent + two children

    def test_an_explicit_child_name_is_still_honoured(
        self, ilan_server: IlanServer
    ) -> None:
        _seed_branchable_parent(ilan_server)
        code, body = _branch(
            ilan_server, "parent-task", {"new_name": "child-task", "message": "m"}
        )
        assert code == 200
        assert body["name"] == "child-task"
        assert ilan_server.store.get_task("child-task") is not None

    def test_an_empty_child_name_is_still_an_error(
        self, ilan_server: IlanServer
    ) -> None:
        _seed_branchable_parent(ilan_server)
        code, body = _branch(
            ilan_server, "parent-task", {"new_name": "", "message": "m"}
        )
        assert code == 400
        assert "3 characters" in body["error"]
        assert list(ilan_server.store.load_tasks()) == ["parent-task"]

    def test_a_padded_child_name_is_stripped(self, ilan_server: IlanServer) -> None:
        _seed_branchable_parent(ilan_server)
        code, body = _branch(
            ilan_server, "parent-task", {"new_name": "  child-task  ", "message": "m"}
        )
        assert code == 200
        assert body["name"] == "child-task"

    def test_a_refused_branch_mints_nothing(self, ilan_server: IlanServer) -> None:
        """The name is drawn only once the branch is known to be viable, so a
        rejected request leaves no half-created task behind."""
        _seed_branchable_parent(ilan_server)
        code, _body = _branch(ilan_server, "parent-task", {})  # no message
        assert code == 400
        assert list(ilan_server.store.load_tasks()) == ["parent-task"]

    def test_a_branch_off_a_normal_parent_is_still_burnable(
        self, ilan_server: IlanServer
    ) -> None:
        """Burnability follows the child's own name, not its parent's."""
        _seed_branchable_parent(ilan_server, "keeper-parent")
        _code, body = _branch(ilan_server, "keeper-parent", {"message": "m"})
        assert is_burnable_name(body["name"])

    def test_a_named_child_of_a_burnable_parent_is_not_burnable(
        self, ilan_server: IlanServer
    ) -> None:
        """The other direction: a burnable parent does not infect its child."""
        _seed_branchable_parent(ilan_server, "xxx-cat-likes-fin")
        _code, body = _branch(
            ilan_server, "xxx-cat-likes-fin", {"new_name": "keeper", "message": "m"}
        )
        assert body["name"] == "keeper"
        code, done_body = _post(ilan_server, "/tasks/keeper/done")
        assert code == 200
        assert done_body["removed"] is False
        assert ilan_server.store.get_task("keeper").status == TaskStatus.DONE

    def test_a_minted_child_burns_on_done_and_leaves_the_parent(
        self, ilan_server: IlanServer
    ) -> None:
        _seed_branchable_parent(ilan_server)
        child_name = _branch(ilan_server, "parent-task", {"message": "m"})[1]["name"]
        code, body = _post(ilan_server, f"/tasks/{child_name}/done")
        assert code == 200
        assert body["removed"] is True
        assert ilan_server.store.get_task(child_name) is None
        assert ilan_server.store.get_task("parent-task") is not None


# ── CLI ─────────────────────────────────────────────────────────────────


@pytest.fixture()
def runner() -> CliRunner:
    return CliRunner()


def _make_client(**overrides) -> MagicMock:
    client = MagicMock()
    client.ensure_server.return_value = {}
    client.version_mismatch = None
    client.is_remote = False
    for k, v in overrides.items():
        setattr(client, k, v)
    return client


class TestAddCli:
    @pytest.mark.parametrize("argv", [["add"], ["task", "add"]])
    def test_name_is_optional_and_the_server_name_is_reported(
        self, runner: CliRunner, tmp_config, argv: list[str]
    ) -> None:
        client = _make_client()
        client.add_task.return_value = {"ok": True, "name": "xxx-cat-likes-fin"}
        with patch("ilan.cli._client", return_value=client), \
             patch("ilan.cli.shutil.which", return_value="/usr/bin/tmux"):
            result = runner.invoke(main, [*argv, "-d", "do a quick thing"])
        assert result.exit_code == 0
        client.add_task.assert_called_once_with(
            None, "do a quick thing", None, max_model=False
        )
        out = _unwrap(_strip_ansi(result.output))
        assert "Task xxx-cat-likes-fin added." in out

    def test_a_generated_name_comes_with_a_burn_warning(
        self, runner: CliRunner, tmp_config
    ) -> None:
        client = _make_client()
        client.add_task.return_value = {"ok": True, "name": "xxx-cat-likes-fin"}
        with patch("ilan.cli._client", return_value=client), \
             patch("ilan.cli.shutil.which", return_value="/usr/bin/tmux"):
            result = runner.invoke(main, ["add", "-d", "p"])
        out = _unwrap(_strip_ansi(result.output))
        assert "Burnable" in out
        assert "will delete it" in out
        assert "Rename it to keep it" in out

    def test_an_explicitly_burnable_name_is_warned_about_too(
        self, runner: CliRunner, tmp_config
    ) -> None:
        client = _make_client()
        client.add_task.return_value = {"ok": True, "name": "xxx-my-scratch"}
        with patch("ilan.cli._client", return_value=client), \
             patch("ilan.cli.shutil.which", return_value="/usr/bin/tmux"):
            result = runner.invoke(main, ["add", "-n", "xxx-my-scratch", "-d", "p"])
        assert result.exit_code == 0
        client.add_task.assert_called_once_with(
            "xxx-my-scratch", "p", None, max_model=False
        )
        assert "Burnable" in _strip_ansi(result.output)

    def test_a_normal_name_gets_no_warning(
        self, runner: CliRunner, tmp_config
    ) -> None:
        client = _make_client()
        client.add_task.return_value = {"ok": True, "name": "fix-bug"}
        with patch("ilan.cli._client", return_value=client), \
             patch("ilan.cli.shutil.which", return_value="/usr/bin/tmux"):
            result = runner.invoke(main, ["add", "-n", "fix-bug", "-d", "p"])
        out = _strip_ansi(result.output)
        assert "Task fix-bug added." in _unwrap(out)
        assert "Burnable" not in out

    def test_an_unnamed_max_task_reports_the_generated_name(
        self, runner: CliRunner, tmp_config
    ) -> None:
        client = _make_client()
        client.add_task.return_value = {"ok": True, "name": "xxx-owl-naps-dune"}
        with patch("ilan.cli._client", return_value=client), \
             patch("ilan.cli.shutil.which", return_value="/usr/bin/tmux"):
            result = runner.invoke(main, ["add", "-d", "p", "--max"])
        assert result.exit_code == 0
        client.add_task.assert_called_once_with(None, "p", "claude", max_model=True)
        out = _unwrap(_strip_ansi(result.output))
        assert "Task xxx-owl-naps-dune added on FABLE" in out
        assert "Burnable" in out

    def test_a_prompt_is_still_required(self, runner: CliRunner, tmp_config) -> None:
        """Dropping ``-n`` must not make ``-d``/``-f`` optional as well."""
        client = _make_client()
        with patch("ilan.cli._client", return_value=client), \
             patch("ilan.cli.shutil.which", return_value="/usr/bin/tmux"):
            result = runner.invoke(main, ["add"])
        assert result.exit_code == 1
        client.add_task.assert_not_called()


class TestBranchCli:
    @pytest.mark.parametrize("argv", [["branch"], ["task", "branch"]])
    def test_name_is_optional_and_the_server_name_is_reported(
        self, runner: CliRunner, tmp_config, argv: list[str]
    ) -> None:
        client = _make_client()
        client.branch_task.return_value = {
            "ok": True, "name": "xxx-cat-likes-fin", "parent_name": "fix-bug",
        }
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, [*argv, "fix-bug", "-d", "try plan B"])
        assert result.exit_code == 0, result.output
        client.branch_task.assert_called_once_with("fix-bug", None, "try plan B")
        out = _unwrap(_strip_ansi(result.output))
        assert "Branched xxx-cat-likes-fin from fix-bug." in out

    def test_a_generated_child_name_comes_with_a_burn_warning(
        self, runner: CliRunner, tmp_config
    ) -> None:
        client = _make_client()
        client.branch_task.return_value = {
            "ok": True, "name": "xxx-cat-likes-fin", "parent_name": "fix-bug",
        }
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["branch", "fix-bug", "-d", "m"])
        out = _unwrap(_strip_ansi(result.output))
        assert "Burnable" in out
        assert "will delete it" in out
        assert "Rename it to keep it" in out

    def test_a_normal_child_name_gets_no_warning(
        self, runner: CliRunner, tmp_config
    ) -> None:
        client = _make_client()
        client.branch_task.return_value = {
            "ok": True, "name": "child", "parent_name": "fix-bug",
        }
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["branch", "fix-bug", "-n", "child", "-d", "m"])
        out = _strip_ansi(result.output)
        assert "Branched child from fix-bug." in _unwrap(out)
        assert "Burnable" not in out

    def test_an_explicitly_burnable_child_name_is_warned_about_too(
        self, runner: CliRunner, tmp_config
    ) -> None:
        client = _make_client()
        client.branch_task.return_value = {
            "ok": True, "name": "xxx-my-spike", "parent_name": "fix-bug",
        }
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(
                main, ["branch", "fix-bug", "-n", "xxx-my-spike", "-d", "m"]
            )
        assert result.exit_code == 0
        client.branch_task.assert_called_once_with("fix-bug", "xxx-my-spike", "m")
        assert "Burnable" in _strip_ansi(result.output)

    def test_an_assignment_is_still_required(
        self, runner: CliRunner, tmp_config
    ) -> None:
        """Dropping ``-n`` must not make ``-d``/``-f`` optional as well: a child
        with no assignment of its own is a reply to the parent, not a branch."""
        client = _make_client()
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["branch", "fix-bug"])
        assert result.exit_code == 1
        assert "Exactly one of --file / --description" in _strip_ansi(result.output)
        client.branch_task.assert_not_called()


class TestDoneDiscardCli:
    def test_done_reports_a_burned_task_as_removed(
        self, runner: CliRunner, tmp_config
    ) -> None:
        client = _make_client()
        client.mark_done.return_value = {
            "ok": True, "name": "xxx-cat-likes-fin", "removed": True,
        }
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["done", "xxx-cat-likes-fin"])
        assert result.exit_code == 0
        out = _unwrap(_strip_ansi(result.output))
        assert "Burnable task xxx-cat-likes-fin removed" in out
        assert "DONE" not in out

    def test_discard_reports_a_burned_task_as_removed(
        self, runner: CliRunner, tmp_config
    ) -> None:
        client = _make_client()
        client.mark_discard.return_value = {
            "ok": True, "name": "xxx-cat-likes-fin", "removed": True,
        }
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["discard", "xxx-cat-likes-fin"])
        assert result.exit_code == 0
        out = _unwrap(_strip_ansi(result.output))
        assert "Burnable task xxx-cat-likes-fin removed" in out
        assert "discarded" not in out

    def test_a_normal_done_message_is_unchanged(
        self, runner: CliRunner, tmp_config
    ) -> None:
        client = _make_client()
        client.mark_done.return_value = {
            "ok": True, "name": "fix-bug", "removed": False,
        }
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["done", "fix-bug"])
        assert "Task fix-bug marked DONE." in _unwrap(_strip_ansi(result.output))

    def test_a_normal_discard_message_is_unchanged(
        self, runner: CliRunner, tmp_config
    ) -> None:
        client = _make_client()
        client.mark_discard.return_value = {
            "ok": True, "name": "fix-bug", "removed": False,
        }
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["discard", "fix-bug"])
        assert "Task fix-bug discarded." in _unwrap(_strip_ansi(result.output))

    def test_an_old_server_response_reads_as_a_normal_close(
        self, runner: CliRunner, tmp_config
    ) -> None:
        """A response without ``removed`` (a server that predates burning)
        must not be reported as a removal."""
        client = _make_client()
        client.mark_done.return_value = {"ok": True, "name": "fix-bug"}
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["done", "fix-bug"])
        assert "marked DONE" in _strip_ansi(result.output)

    def test_a_mixed_batch_reports_each_task_its_own_way(
        self, runner: CliRunner, tmp_config
    ) -> None:
        client = _make_client()
        client.mark_done.side_effect = [
            {"ok": True, "name": "xxx-cat-likes-fin", "removed": True},
            {"ok": True, "name": "fix-bug", "removed": False},
        ]
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["done", "xxx-cat-likes-fin", "fix-bug"])
        assert result.exit_code == 0
        out = _unwrap(_strip_ansi(result.output))
        assert "Burnable task xxx-cat-likes-fin removed" in out
        assert "Task fix-bug marked DONE." in out

    def test_a_failure_mid_batch_still_processes_the_rest(
        self, runner: CliRunner, tmp_config
    ) -> None:
        client = _make_client()
        client.mark_done.side_effect = [
            {"error": "Task nope not found"},
            {"ok": True, "name": "xxx-cat-likes-fin", "removed": True},
        ]
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["done", "nope", "xxx-cat-likes-fin"])
        assert result.exit_code == 1
        out = _unwrap(_strip_ansi(result.output))
        assert "not found" in out
        assert "Burnable task xxx-cat-likes-fin removed" in out
