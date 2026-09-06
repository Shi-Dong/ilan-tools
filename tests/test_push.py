"""Web Push: what is sent, to whom, and when — without ever reaching a push
service.

The sender is replaced by a recorder throughout, so what is asserted is the
contract: the payload's contents, which reaps qualify, how devices are kept and
dropped, and that a finish reaped by the live reaper reaches the sender exactly
once, with the summary the reaper produced.
"""

from __future__ import annotations

import json
import os
import stat
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from pywebpush import WebPushException

import ilan.config as cfg_mod
from ilan.models import Task, TaskStatus
from ilan.push import (
    FINISH_WORDS,
    PUSH_TTL_SECONDS,
    DEFAULT_PUSH_CONTACT,
    push_contact,
    PushNotifier,
    build_payload,
    should_notify,
    validate_subscription,
)
from ilan.server import IlanServer
# The server fixture and its request helpers live with the server tests; pytest
# picks the fixture up from this module's namespace once it is imported here.
from tests.test_server import _get, _post, ilan_server  # noqa: F401

SUB = {
    "endpoint": "https://web.push.apple.com/QAbc123",
    "expirationTime": None,
    "keys": {"p256dh": "BPubKeyFake", "auth": "AuthFake"},
}
SUB2 = {"endpoint": "https://web.push.apple.com/QDef456", "keys": {"p256dh": "BOther", "auth": "A2"}}


def _task(**over) -> Task:
    base = dict(name="train-on-chess", prompt="P", status=TaskStatus.AGENT_FINISHED)
    base.update(over)
    return Task(**base)


class Recorder:
    """Stands in for pywebpush.webpush, remembering every call and failing
    on demand per endpoint."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.fail: dict[str, int | Exception] = {}

    def __call__(self, **kwargs) -> str:
        self.calls.append(kwargs)
        endpoint = kwargs["subscription_info"]["endpoint"]
        failure = self.fail.get(endpoint)
        if isinstance(failure, Exception):
            raise failure
        if failure is not None:
            # As pywebpush raises it: the status lives on the response object.
            raise WebPushException(f"status {failure}", response=SimpleNamespace(status_code=failure))
        return "ok"


@pytest.fixture()
def notifier(tmp_path: Path) -> tuple[PushNotifier, Recorder]:
    rec = Recorder()
    return PushNotifier(workdir=lambda: tmp_path, sender=rec), rec


# ── the note itself ───────────────────────────────────────────────────────

class TestPayload:
    def test_title_is_the_task_name_and_nothing_else(self) -> None:
        assert build_payload(_task(alias="aa"))["title"] == "train-on-chess"

    @pytest.mark.parametrize(("status", "words"), [
        (TaskStatus.AGENT_FINISHED, "Agent finished"),
        (TaskStatus.NEEDS_ATTENTION, "Needs attention"),
        (TaskStatus.ERROR, "Error"),
    ])
    def test_body_says_how_it_finished_in_words(self, status: TaskStatus, words: str) -> None:
        payload = build_payload(_task(status=status, summary_one_liner=None))
        assert payload["body"] == words
        assert payload["status"] == status.value

    def test_body_carries_the_one_line_summary(self) -> None:
        payload = build_payload(_task(summary_one_liner="Ran the suite; two flakes"))
        assert payload["body"] == "Agent finished — Ran the suite; two flakes"

    def test_a_blank_summary_is_not_appended(self) -> None:
        assert build_payload(_task(summary_one_liner="   "))["body"] == "Agent finished"

    def test_the_alias_appears_nowhere(self) -> None:
        payload = build_payload(_task(alias="zq", summary_one_liner="done"))
        assert "zq" not in json.dumps(payload), "the alias means nothing on a lock screen"

    def test_a_tap_lands_on_the_task_and_repeats_replace(self) -> None:
        payload = build_payload(_task())
        assert payload["url"] == "#/t/train-on-chess"
        assert payload["tag"] == "task:train-on-chess"

    def test_a_runaway_summary_is_clipped(self) -> None:
        body = build_payload(_task(summary_one_liner="x" * 1000))["body"]
        assert len(body) <= 240 and body.endswith("…")

    def test_every_finished_status_has_words(self) -> None:
        assert set(FINISH_WORDS) == {
            TaskStatus.AGENT_FINISHED, TaskStatus.NEEDS_ATTENTION, TaskStatus.ERROR,
        }


# ── which reaps qualify ───────────────────────────────────────────────────

class TestShouldNotify:
    @pytest.mark.parametrize("status", [TaskStatus.AGENT_FINISHED, TaskStatus.NEEDS_ATTENTION, TaskStatus.ERROR])
    def test_a_finish_notifies(self, status: TaskStatus) -> None:
        assert should_notify(_task(status=status))

    @pytest.mark.parametrize("status", [TaskStatus.WORKING, TaskStatus.DONE, TaskStatus.DISCARDED])
    def test_other_statuses_do_not(self, status: TaskStatus) -> None:
        assert not should_notify(_task(status=status))

    @pytest.mark.parametrize("status", list(TaskStatus))
    def test_nothing_on_a_reply_every_cycle_does(self, status: TaskStatus) -> None:
        """The cycle re-prompts the agent, so nobody is being waited on — and a
        looping task that kept erroring would otherwise ring the phone every
        cycle, as often as every twenty minutes. Errors included, therefore."""
        assert not should_notify(_task(status=status, reply_every_seconds=1200))

    def test_the_cycle_rule_is_about_the_cycle_not_the_status_words(self) -> None:
        """A task with no cycle keeps notifying on an error; only the loop
        silences it."""
        assert should_notify(_task(status=TaskStatus.ERROR))
        assert not should_notify(_task(status=TaskStatus.ERROR, reply_every_seconds=1200))


# ── devices ───────────────────────────────────────────────────────────────

class TestSubscriptions:
    def test_a_browser_subscription_is_reduced_to_what_sending_needs(self) -> None:
        assert validate_subscription(SUB) == {
            "endpoint": SUB["endpoint"], "keys": {"p256dh": "BPubKeyFake", "auth": "AuthFake"},
        }

    @pytest.mark.parametrize("bad", [
        None, "text", {}, {"endpoint": "http://insecure", "keys": SUB["keys"]},
        {"endpoint": SUB["endpoint"]}, {"endpoint": SUB["endpoint"], "keys": {"p256dh": "x"}},
        {"endpoint": SUB["endpoint"], "keys": {"p256dh": "", "auth": "a"}},
    ])
    def test_anything_else_is_refused(self, bad) -> None:
        assert validate_subscription(bad) is None

    def test_subscribe_persists_and_is_idempotent(self, notifier, tmp_path: Path) -> None:
        n, _ = notifier
        assert n.subscribe(SUB) == 1
        assert n.subscribe(SUB) == 1, "re-subscribing the same device must not duplicate it"
        assert n.subscribe(SUB2) == 2
        stored = json.loads((tmp_path / "push" / "subscriptions.json").read_text())
        assert set(stored) == {SUB["endpoint"], SUB2["endpoint"]}
        assert "expirationTime" not in stored[SUB["endpoint"]], "only what sending needs is kept"

    def test_subscribe_refuses_junk(self, notifier) -> None:
        n, _ = notifier
        assert n.subscribe({"endpoint": "nope"}) is None
        assert n.subscription_count() == 0

    def test_unsubscribe(self, notifier) -> None:
        n, _ = notifier
        n.subscribe(SUB)
        assert n.unsubscribe(SUB["endpoint"]) is True
        assert n.unsubscribe(SUB["endpoint"]) is False
        assert n.unsubscribe(None) is False
        assert n.subscription_count() == 0

    def test_a_fresh_notifier_reads_what_an_earlier_one_saved(self, tmp_path: Path) -> None:
        PushNotifier(workdir=lambda: tmp_path, sender=Recorder()).subscribe(SUB)
        assert PushNotifier(workdir=lambda: tmp_path, sender=Recorder()).subscription_count() == 1


# ── the signing key ───────────────────────────────────────────────────────

class TestKeys:
    def test_the_public_key_is_an_application_server_key(self, notifier) -> None:
        n, _ = notifier
        key = n.public_key()
        # An uncompressed P-256 point is 65 bytes; base64url without padding
        # makes that 87 characters starting with the 0x04 marker's 'B'.
        assert len(key) == 87 and key.startswith("B")
        assert "=" not in key and "+" not in key and "/" not in key

    def test_the_key_is_created_once_and_kept(self, tmp_path: Path) -> None:
        """A new key would orphan every phone that subscribed with the old one."""
        first = PushNotifier(workdir=lambda: tmp_path, sender=Recorder()).public_key()
        second = PushNotifier(workdir=lambda: tmp_path, sender=Recorder()).public_key()
        assert first == second

    def test_the_private_key_is_readable_by_its_owner_only(self, notifier, tmp_path: Path) -> None:
        n, _ = notifier
        n.public_key()
        mode = stat.S_IMODE(os.stat(tmp_path / "push" / "vapid.pem").st_mode)
        assert mode == 0o600, oct(mode)

    def test_no_key_file_exists_until_someone_asks(self, notifier, tmp_path: Path) -> None:
        assert not (tmp_path / "push" / "vapid.pem").exists()


# ── sending ───────────────────────────────────────────────────────────────

class TestSending:
    def test_every_device_gets_the_note_signed_by_this_server(self, notifier) -> None:
        n, rec = notifier
        n.subscribe(SUB); n.subscribe(SUB2)
        payload = build_payload(_task(summary_one_liner="Ran the suite"))
        assert n.send(payload) == 2
        assert {c["subscription_info"]["endpoint"] for c in rec.calls} == {SUB["endpoint"], SUB2["endpoint"]}
        call = rec.calls[0]
        assert json.loads(call["data"]) == payload
        assert call["vapid_claims"] == {"sub": DEFAULT_PUSH_CONTACT}
        assert call["ttl"] == PUSH_TTL_SECONDS
        assert call["subscription_info"]["keys"] == {"p256dh": "BPubKeyFake", "auth": "AuthFake"}
        assert call["vapid_private_key"] is n._keys()

    @pytest.mark.parametrize("status", [404, 410])
    def test_a_gone_device_is_forgotten(self, notifier, status: int) -> None:
        n, rec = notifier
        n.subscribe(SUB); n.subscribe(SUB2)
        rec.fail[SUB["endpoint"]] = status
        assert n.send(build_payload(_task())) == 1
        assert n.subscription_count() == 1
        assert n.unsubscribe(SUB["endpoint"]) is False, "it was already dropped"

    def test_any_other_failure_keeps_the_device_and_the_rest_are_still_sent(
        self, notifier, capsys,
    ) -> None:
        n, rec = notifier
        n.subscribe(SUB); n.subscribe(SUB2)
        rec.fail[SUB["endpoint"]] = 500
        assert n.send(build_payload(_task())) == 1
        assert n.subscription_count() == 2
        assert "failed" in capsys.readouterr().err

    def test_a_crash_in_the_sender_never_escapes(self, notifier) -> None:
        n, rec = notifier
        n.subscribe(SUB)
        rec.fail[SUB["endpoint"]] = RuntimeError("boom")
        assert n.send(build_payload(_task())) == 0
        assert n.subscription_count() == 1

    def test_notify_queues_only_when_it_should_and_someone_listens(self, notifier) -> None:
        n, _ = notifier
        assert n.notify_finished(_task()) is False, "no devices, nothing to queue"
        n.subscribe(SUB)
        assert n.notify_finished(_task(status=TaskStatus.WORKING)) is False
        assert n.notify_finished(_task()) is True
        assert n._queue.qsize() == 1

    def test_the_thread_delivers_what_was_queued(self, notifier) -> None:
        n, rec = notifier
        n.subscribe(SUB)
        n.start()
        try:
            assert n.notify_finished(_task(summary_one_liner="Ran the suite"))
            deadline = time.monotonic() + 5
            while not rec.calls and time.monotonic() < deadline:
                time.sleep(0.02)
        finally:
            n.stop()
        assert len(rec.calls) == 1
        assert json.loads(rec.calls[0]["data"])["body"] == "Agent finished — Ran the suite"


# ── the routes ────────────────────────────────────────────────────────────

class TestRoutes:
    def test_status_hands_out_the_key_and_the_count(self, ilan_server: IlanServer) -> None:
        status = _get(ilan_server, "/push")
        assert len(status["public_key"]) == 87
        assert status["subscriptions"] == 0

    def test_subscribe_and_unsubscribe(self, ilan_server: IlanServer) -> None:
        assert _post(ilan_server, "/push/subscribe", SUB) == {"ok": True, "subscriptions": 1}
        assert _post(ilan_server, "/push/subscribe", SUB) == {"ok": True, "subscriptions": 1}
        assert _get(ilan_server, "/push")["subscriptions"] == 1
        assert _post(ilan_server, "/push/unsubscribe", {"endpoint": SUB["endpoint"]}) == {
            "ok": True, "removed": True, "subscriptions": 0,
        }

    def test_junk_is_refused_with_a_reason(self, ilan_server: IlanServer) -> None:
        resp = _post(ilan_server, "/push/subscribe", {"endpoint": "http://insecure"})
        assert "error" in resp and "https" in resp["error"]


# ── the reaper is the source ──────────────────────────────────────────────

class TestReaperNotifies:
    """A live finish, reaped by the real reaper with the mock agent, reaches
    the sender exactly once — with the words for its status and the summary
    the reaper produced. Recorded, not sent: the sender is swapped for a
    recorder before the server's reaper thread runs."""

    def _server(self, tmp_workdir: Path, monkeypatch: pytest.MonkeyPatch, status: str) -> tuple[IlanServer, Recorder]:
        cfg_mod.save({**cfg_mod.DEFAULTS, "workdir": str(tmp_workdir)})
        monkeypatch.setenv("MOCK_CLAUDE_STATUS", status)
        # The one-liner would otherwise shell out to a real `codex` if one is
        # on PATH; the reaper's own summary is what should reach the phone.
        monkeypatch.setattr("ilan.runner.generate_one_liner", lambda user, assistant: "Ran the suite")
        server = IlanServer()
        rec = Recorder()
        server.push = PushNotifier(workdir=lambda: tmp_workdir, sender=rec)
        server.push.subscribe(SUB)
        return server, rec

    def _reap_once(self, server: IlanServer, name: str) -> list[Task]:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            with server.lock:
                proc = server.runner._procs.get(name)
                if proc is None or proc.poll() is not None:
                    reaped = server.runner.reap_finished()
                    if reaped:
                        return reaped
            time.sleep(0.05)
        raise AssertionError("the mock agent never finished")

    @pytest.mark.parametrize(("mock_status", "words"), [
        ("DONE", "Agent finished"), ("NEEDS_ATTENTION", "Needs attention"), ("ERROR", "Error"),
    ])
    def test_a_live_finish_is_announced_once(
        self, tmp_workdir: Path, tmp_config: Path, env_with_mock_claude: None,
        monkeypatch: pytest.MonkeyPatch, mock_status: str, words: str,
    ) -> None:
        server, rec = self._server(tmp_workdir, monkeypatch, mock_status)
        with server.lock:
            task = Task(name="live-task", prompt="P", alias="zq")
            server.store.put_task(task)
            assert server.runner.start(task)
        reaped = self._reap_once(server, "live-task")
        assert [t.name for t in reaped] == ["live-task"]

        for t in reaped:
            server.push.notify_finished(t)
        assert server.push.send(server.push._queue.get_nowait()) == 1
        assert len(rec.calls) == 1
        note = json.loads(rec.calls[0]["data"])
        assert note["title"] == "live-task"
        expected = words if mock_status == "ERROR" else f"{words} — Ran the suite"
        assert note["body"] == expected
        assert "zq" not in json.dumps(note)

    def test_the_reaper_loop_hands_reaped_tasks_to_the_notifier(
        self, tmp_workdir: Path, tmp_config: Path, env_with_mock_claude: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Through the server's own loop this time, not by calling the reaper:
        one tick, one note, and the loop must not hold the lock while
        handing it over."""
        server, rec = self._server(tmp_workdir, monkeypatch, "DONE")
        with server.lock:
            task = Task(name="loop-task", prompt="P")
            server.store.put_task(task)
            assert server.runner.start(task)
        proc = server.runner._procs["loop-task"]
        proc.wait(timeout=10)

        handed: list[Task] = []
        lock_held_during_handoff: list[bool] = []

        def spy(task: Task) -> bool:
            handed.append(task)
            lock_held_during_handoff.append(server.lock.locked())
            return True

        server.push.notify_finished = spy  # type: ignore[method-assign]
        server._stop_event.set()  # one pass through the loop body, then out
        server._reaper_loop.__wrapped__ if hasattr(server._reaper_loop, "__wrapped__") else None
        # Run exactly one iteration by clearing the stop flag, starting the loop
        # in a thread, and stopping it after the first tick.
        server._stop_event.clear()
        t = threading.Thread(target=server._reaper_loop, daemon=True)
        t.start()
        deadline = time.monotonic() + 5
        while not handed and time.monotonic() < deadline:
            time.sleep(0.02)
        server._stop_event.set()
        t.join(timeout=3)
        assert [x.name for x in handed] == ["loop-task"]
        assert lock_held_during_handoff == [False], "the hand-off ran under the server lock"

    def test_a_finish_inside_a_cycle_is_not_announced(
        self, tmp_workdir: Path, tmp_config: Path, env_with_mock_claude: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        server, rec = self._server(tmp_workdir, monkeypatch, "DONE")
        with server.lock:
            task = Task(name="cycling-task", prompt="P", reply_every_seconds=1200)
            server.store.put_task(task)
            assert server.runner.start(task)
        reaped = self._reap_once(server, "cycling-task")
        assert [t.name for t in reaped] == ["cycling-task"]
        assert all(server.push.notify_finished(t) is False for t in reaped)
        assert server.push._queue.empty()


# ── the contact ───────────────────────────────────────────────────────────

class TestContact:
    """Apple rejects the whole token for a contact it dislikes, and nothing in
    a stubbed test would show it. What can be pinned is the shape Apple
    accepted in the smoke test, and that a bad setting cannot reach a push."""

    def test_the_default_is_a_mailto_with_a_dotted_host(self) -> None:
        assert DEFAULT_PUSH_CONTACT.startswith("mailto:")
        host = DEFAULT_PUSH_CONTACT.split("@", 1)[1]
        assert "." in host, "Apple answers 403 BadJwtToken for a host without a dot"

    def test_a_configured_contact_is_what_is_signed(self, tmp_config: Path, notifier) -> None:
        cfg_mod.save({**cfg_mod.DEFAULTS, "push-contact": "mailto:me@example.org"})
        assert push_contact() == "mailto:me@example.org"
        n, rec = notifier
        n.subscribe(SUB)
        n.send(build_payload(_task()))
        assert rec.calls[0]["vapid_claims"] == {"sub": "mailto:me@example.org"}

    @pytest.mark.parametrize("bad", ["", "me@example.org", "https://example.org", "mailto:", "   "])
    def test_anything_but_a_mailto_falls_back(self, tmp_config: Path, bad: str) -> None:
        cfg_mod.save({**cfg_mod.DEFAULTS, "push-contact": bad})
        assert push_contact() == DEFAULT_PUSH_CONTACT


# ── the dependency ────────────────────────────────────────────────────────

def test_the_push_library_is_a_declared_dependency() -> None:
    """The venv having it is not enough: a fresh install has to get it too."""
    pyproject = (Path(__file__).resolve().parent.parent / "pyproject.toml").read_text()
    deps = pyproject.split("dependencies = [", 1)[1].split("]", 1)[0]
    assert "pywebpush" in deps
