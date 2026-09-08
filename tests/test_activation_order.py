"""Tests for activation ordering — ``activated_at`` and the listing it drives.

A task activates when it is created and again every time ``undone`` /
``undiscard`` brings it back, and ``/tasks`` orders on that rather than on
creation. The property these tests are here to hold onto is that reviving a
task puts it at the *bottom* of the listing, beside the work the user is
actually holding.
"""

from __future__ import annotations

import json
import signal
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from ilan.models import Task, TaskStatus
from ilan.server import IlanServer
from ilan.store import Store

from tests.helpers import SERVE_POLL_INTERVAL, wait_until_serving


# ── store: minting the next activation stamp ────────────────────────────


class TestNextActivationTs:
    def test_empty_store_returns_now(self, tmp_workdir: Path) -> None:
        before = datetime.now(timezone.utc)
        ts = datetime.fromisoformat(Store(tmp_workdir).next_activation_ts())
        assert before <= ts <= datetime.now(timezone.utc)

    def test_past_activations_do_not_hold_it_back(self, tmp_workdir: Path) -> None:
        store = Store(tmp_workdir)
        store.put_task(Task(
            name="old", prompt="p", created_at="2020-01-01T00:00:00+00:00",
        ))
        before = datetime.now(timezone.utc)
        assert datetime.fromisoformat(store.next_activation_ts()) >= before

    def test_steps_past_a_future_activation(self, tmp_workdir: Path) -> None:
        """A clock that ran ahead must not leave a revived task mid-list."""
        store = Store(tmp_workdir)
        ahead = datetime.now(timezone.utc) + timedelta(days=30)
        store.put_task(Task(name="ahead", prompt="p", activated_at=ahead.isoformat()))

        ts = store.next_activation_ts()

        assert datetime.fromisoformat(ts) == ahead + timedelta(microseconds=1)
        # Ordering is done on the strings, so that is what has to compare right.
        assert ts > ahead.isoformat()

    def test_steps_past_the_latest_of_several(self, tmp_workdir: Path) -> None:
        store = Store(tmp_workdir)
        now = datetime.now(timezone.utc)
        for days, name in ((10, "near"), (30, "far"), (20, "middle")):
            store.put_task(Task(
                name=name, prompt="p",
                activated_at=(now + timedelta(days=days)).isoformat(),
            ))

        ts = datetime.fromisoformat(store.next_activation_ts())

        assert ts == now + timedelta(days=30) + timedelta(microseconds=1)

    def test_unreadable_stamps_are_ignored(self, tmp_workdir: Path) -> None:
        """A junk timestamp must not veto the clamp, or block minting at all."""
        store = Store(tmp_workdir)
        store.put_task(Task(name="junk", prompt="p", activated_at="not-a-date"))
        before = datetime.now(timezone.utc)
        assert datetime.fromisoformat(store.next_activation_ts()) >= before

    def test_a_missing_stamp_falls_back_to_created_at(self, tmp_workdir: Path) -> None:
        """A pre-existing task's creation time still counts as its activation."""
        store = Store(tmp_workdir)
        ahead = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
        store.put_task(Task(name="legacy", prompt="p", created_at=ahead))
        assert datetime.fromisoformat(store.next_activation_ts()) > \
            datetime.fromisoformat(ahead)


class TestActivatedAtField:
    def test_defaults_to_created_at_on_construction(self) -> None:
        """Before any store round-trip: the invariant holds in memory too."""
        ts = "2026-03-03T03:03:03+00:00"
        assert Task(name="t", prompt="p", created_at=ts).activated_at == ts

    def test_an_explicit_value_is_left_alone(self) -> None:
        revived = "2026-04-04T04:04:04+00:00"
        t = Task(
            name="t", prompt="p",
            created_at="2026-03-03T03:03:03+00:00", activated_at=revived,
        )
        assert t.activated_at == revived

    def test_round_trips_through_the_store(self, tmp_workdir: Path) -> None:
        store = Store(tmp_workdir)
        ts = "2026-05-05T05:05:05+00:00"
        store.put_task(Task(name="t", prompt="p", activated_at=ts))
        assert store.load_tasks()["t"].activated_at == ts

    def test_falls_back_to_created_at_when_absent(self) -> None:
        """A store written before activation ordering existed must still load."""
        t = Task.from_dict({
            "name": "old", "prompt": "p", "status": "WORKING",
            "created_at": "2025-06-01T00:00:00+00:00",
        })
        assert t.activated_at == "2025-06-01T00:00:00+00:00"

    def test_a_branched_child_activates_at_its_creation(
        self, tmp_workdir: Path,
    ) -> None:
        store = Store(tmp_workdir)
        parent = Task(
            name="parent", prompt="p", created_at="2026-01-01T00:00:00+00:00",
        )
        store.put_task(parent)
        child = store.branch_task(
            parent, "child",
            alias="cc", task_hash="1111aaaa", now="2026-02-02T00:00:00+00:00",
        )
        assert child.activated_at == "2026-02-02T00:00:00+00:00"


# ── server: the listing ─────────────────────────────────────────────────


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


def _post(server: IlanServer, path: str, body: dict | None = None) -> dict:
    url = f"{server._test_url}{path}"  # type: ignore[attr-defined]
    data = json.dumps(body).encode() if body else None
    req = Request(url, data=data, method="POST")
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urlopen(req, timeout=5) as resp:
            return json.loads(resp.read())
    except HTTPError as exc:
        return json.loads(exc.read())


def _names(server: IlanServer, *, show_all: bool = True) -> list[str]:
    url = f"{server._test_url}/tasks" + ("?all=true" if show_all else "")  # type: ignore[attr-defined]
    with urlopen(url, timeout=5) as resp:
        return [r["name"] for r in json.loads(resp.read())["tasks"]]


def _seed(
    server: IlanServer,
    name: str,
    *,
    hour: int,
    pinned: bool = False,
    status: TaskStatus = TaskStatus.WORKING,
) -> None:
    """Put a task in the store activated at *hour* on a fixed day."""
    ts = f"2026-07-29T{hour:02d}:00:00+00:00"
    server.store.put_task(Task(
        name=name, prompt="p", status=status, created_at=ts,
        status_changed_at=ts, activated_at=ts, pinned=pinned,
    ))


class TestNewTasksActivateAtCreation:
    def test_created_and_activated_stamps_match(self, ilan_server: IlanServer) -> None:
        _post(ilan_server, "/tasks", {"name": "fresh", "prompt": "P"})
        task = ilan_server.store.get_task("fresh")
        assert task is not None
        assert task.activated_at == task.created_at != ""

    def test_the_stamp_is_persisted_and_not_just_derived_on_read(
        self, ilan_server: IlanServer,
    ) -> None:
        """``tasks.json`` carries the field, so nothing has to re-derive it."""
        _post(ilan_server, "/tasks", {"name": "fresh", "prompt": "P"})
        raw = json.loads((ilan_server.store.workdir / "tasks.json").read_text())
        assert raw["fresh"]["activated_at"] == raw["fresh"]["created_at"] != ""

    def test_creation_order_is_the_listing_order(self, ilan_server: IlanServer) -> None:
        for name in ("one", "two", "three"):
            _post(ilan_server, "/tasks", {"name": name, "prompt": "P"})
        assert _names(ilan_server) == ["one", "two", "three"]


class TestReviveMovesToTheBottom:
    def test_undone_moves_the_task_to_the_bottom(self, ilan_server: IlanServer) -> None:
        _seed(ilan_server, "first", hour=0)
        _seed(ilan_server, "second", hour=1)
        _seed(ilan_server, "third", hour=2)

        _post(ilan_server, "/tasks/first/done")
        _post(ilan_server, "/tasks/first/undone")

        assert _names(ilan_server) == ["second", "third", "first"]

    def test_undiscard_moves_the_task_to_the_bottom(
        self, ilan_server: IlanServer,
    ) -> None:
        _seed(ilan_server, "first", hour=0)
        _seed(ilan_server, "second", hour=1)
        _seed(ilan_server, "third", hour=2)

        _post(ilan_server, "/tasks/first/discard")
        _post(ilan_server, "/tasks/first/undiscard")

        assert _names(ilan_server) == ["second", "third", "first"]

    def test_reviving_the_newest_task_leaves_it_last(
        self, ilan_server: IlanServer,
    ) -> None:
        _seed(ilan_server, "first", hour=0)
        _seed(ilan_server, "last", hour=1)

        _post(ilan_server, "/tasks/last/done")
        _post(ilan_server, "/tasks/last/undone")

        assert _names(ilan_server) == ["first", "last"]

    def test_revives_stack_in_the_order_they_happened(
        self, ilan_server: IlanServer,
    ) -> None:
        for hour, name in enumerate(("a", "b", "c")):
            _seed(ilan_server, name, hour=hour)

        for name in ("b", "a"):
            _post(ilan_server, f"/tasks/{name}/done")
            _post(ilan_server, f"/tasks/{name}/undone")

        assert _names(ilan_server) == ["c", "b", "a"]

    def test_a_second_revive_moves_it_to_the_bottom_again(
        self, ilan_server: IlanServer,
    ) -> None:
        _seed(ilan_server, "revived", hour=0)
        _seed(ilan_server, "other", hour=1)

        _post(ilan_server, "/tasks/revived/done")
        _post(ilan_server, "/tasks/revived/undone")
        # `other` is newly activated, so it now sits below `revived`…
        _post(ilan_server, "/tasks/other/done")
        _post(ilan_server, "/tasks/other/undone")
        assert _names(ilan_server) == ["revived", "other"]
        # …and reviving `revived` again puts it back underneath.
        _post(ilan_server, "/tasks/revived/done")
        _post(ilan_server, "/tasks/revived/undone")
        assert _names(ilan_server) == ["other", "revived"]

    def test_bottom_even_when_a_stored_activation_is_in_the_future(
        self, ilan_server: IlanServer,
    ) -> None:
        """The guarantee does not rest on the machine's clock being sane."""
        ahead = (datetime.now(timezone.utc) + timedelta(days=365)).isoformat()
        ilan_server.store.put_task(Task(
            name="from-the-future", prompt="p",
            created_at=ahead, status_changed_at=ahead, activated_at=ahead,
        ))
        _seed(ilan_server, "revived", hour=0)

        _post(ilan_server, "/tasks/revived/done")
        _post(ilan_server, "/tasks/revived/undone")

        assert _names(ilan_server) == ["from-the-future", "revived"]

    def test_a_revived_task_shows_without_all(self, ilan_server: IlanServer) -> None:
        """Reviving makes the task live again, so it is not filtered out."""
        _seed(ilan_server, "first", hour=0)
        _seed(ilan_server, "revived", hour=1)

        _post(ilan_server, "/tasks/revived/done")
        assert _names(ilan_server, show_all=False) == ["first"]

        _post(ilan_server, "/tasks/revived/undone")
        assert _names(ilan_server, show_all=False) == ["first", "revived"]


class TestClosingDoesNotReorder:
    @pytest.mark.parametrize("action", ["done", "discard"])
    def test_closing_leaves_the_task_where_it_was(
        self, ilan_server: IlanServer, action: str,
    ) -> None:
        _seed(ilan_server, "first", hour=0)
        _seed(ilan_server, "closed", hour=1)
        _seed(ilan_server, "third", hour=2)

        _post(ilan_server, f"/tasks/closed/{action}")

        assert _names(ilan_server) == ["first", "closed", "third"]

    def test_a_rejected_revive_does_not_reorder(self, ilan_server: IlanServer) -> None:
        """A 409 must not spend an activation on a task it refused to revive."""
        _seed(ilan_server, "working", hour=0)
        _seed(ilan_server, "other", hour=1)

        body = _post(ilan_server, "/tasks/working/undone")

        assert "not DONE" in body["error"]
        assert _names(ilan_server) == ["working", "other"]
        assert ilan_server.store.get_task("working").activated_at == \
            "2026-07-29T00:00:00+00:00"  # type: ignore[union-attr]


class TestPinsOutrankActivation:
    def test_reviving_a_pinned_task_keeps_it_in_the_pinned_block(
        self, ilan_server: IlanServer,
    ) -> None:
        _seed(ilan_server, "pin-old", hour=0, pinned=True)
        _seed(ilan_server, "pin-new", hour=1, pinned=True)
        _seed(ilan_server, "plain", hour=2)

        _post(ilan_server, "/tasks/pin-old/done")
        _post(ilan_server, "/tasks/pin-old/undone")

        # Bottom of the pinned block, not the bottom of the table.
        assert _names(ilan_server) == ["pin-new", "pin-old", "plain"]

    def test_unpinning_drops_it_into_activation_order(
        self, ilan_server: IlanServer,
    ) -> None:
        _seed(ilan_server, "early", hour=0)
        _seed(ilan_server, "revived", hour=1, pinned=True)
        _seed(ilan_server, "late", hour=2)

        _post(ilan_server, "/tasks/revived/done")
        _post(ilan_server, "/tasks/revived/undone")
        assert _names(ilan_server) == ["revived", "early", "late"]

        _post(ilan_server, "/tasks/revived/unpin")
        assert _names(ilan_server) == ["early", "late", "revived"]


class TestLegacyStores:
    def test_tasks_without_activated_at_keep_creation_order(
        self, ilan_server: IlanServer,
    ) -> None:
        """Nothing moves when a store predating activation ordering is loaded."""
        for hour, name in enumerate(("oldest", "middle", "newest")):
            ts = f"2026-07-29T{hour:02d}:00:00+00:00"
            ilan_server.store.put_task(Task(
                name=name, prompt="p", created_at=ts, status_changed_at=ts,
            ))

        assert _names(ilan_server) == ["oldest", "middle", "newest"]

    def test_reviving_a_legacy_task_still_sends_it_to_the_bottom(
        self, ilan_server: IlanServer,
    ) -> None:
        for hour, name in enumerate(("revived", "other")):
            ts = f"2026-07-29T{hour:02d}:00:00+00:00"
            ilan_server.store.put_task(Task(
                name=name, prompt="p", status=TaskStatus.DONE,
                created_at=ts, status_changed_at=ts,
            ))

        _post(ilan_server, "/tasks/revived/undone")

        assert _names(ilan_server) == ["other", "revived"]
