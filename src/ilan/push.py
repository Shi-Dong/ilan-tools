"""Web Push to phones that have installed the web app.

A phone that has added the app to its Home Screen can hand the server a private
"mailbox" at its platform's push service — an endpoint URL plus two keys. When
a task finishes, the server encrypts a small note to those keys and posts it to
the endpoint, and the platform wakes the phone to show it. Nothing here needs
the phone to be reachable, on the tailnet, or awake.

Three things live in ``<workdir>/push/``, none of them in the repository:

- ``vapid.pem`` — the server's signing key, generated once. Its public half is
  what the app hands to the browser when subscribing, and it is how the push
  service knows later pushes come from the same server. Mode 0600.
- ``subscriptions.json`` — one entry per device, keyed by endpoint.

Sending runs on its own daemon thread, fed by a queue, exactly as the Gist
syncer does: the reaper only enqueues, so a slow or unreachable push service
can never hold the server lock. A device whose endpoint has expired (the push
service answers 404 or 410) is forgotten on the spot.
"""

from __future__ import annotations

import contextlib
import json
import os
import queue
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from cryptography.hazmat.primitives import serialization
from py_vapid import Vapid, b64urlencode
from pywebpush import WebPushException, webpush

from ilan import config as cfg
from ilan.models import Task, TaskStatus

# The VAPID ``sub`` claim: who a push service may contact about this server's
# traffic. The spec allows a URL, but Apple's service rejects the token outright
# for a URL and for any host without a dot (``mailto:x@localhost`` gets a 403
# BadJwtToken; the unit tests, which stub the sender, could never have shown
# that — the smoke test against the real service did). So it is a ``mailto:``
# with a dotted host, and a setting rather than a constant, because a real
# address is what the claim is for: set ``push-contact`` to yours.
DEFAULT_PUSH_CONTACT = "mailto:ilan@example.com"


def push_contact() -> str:
    """The configured contact, or the default if it would not be accepted.

    A wrong value here does not fail loudly — every push would come back 403
    and no phone would ever hear — so anything that is not a ``mailto:`` falls
    back rather than being sent.
    """
    value = str(cfg.load().get("push-contact", "")).strip()
    return value if value.startswith("mailto:") and "@" in value else DEFAULT_PUSH_CONTACT

# How long a push service should hold a note for a phone that is offline.
# Longer than a night's sleep; a finish is stale news after that anyway.
PUSH_TTL_SECONDS = 24 * 3600

# The statuses a reaped task can land in, and the words a notification uses
# for each. Words, not the enum value: this is read on a lock screen.
FINISH_WORDS: dict[TaskStatus, str] = {
    TaskStatus.AGENT_FINISHED: "Agent finished",
    TaskStatus.NEEDS_ATTENTION: "Needs attention",
    TaskStatus.ERROR: "Error",
}

# Lock-screen notifications are cut off well before the payload limit would
# bite; this keeps a runaway summary from filling the screen.
BODY_LIMIT = 240


def should_notify(task: Task) -> bool:
    """Whether a task that has just been reaped is worth a notification.

    A reap that lands in one of the finished statuses is — unless the task is
    on a ``reply -t`` cycle, in which case nothing is, errors included. The
    cycle re-prompts the agent on its own, so no person is being waited on
    (the list shows such a task as AGENT IN LOOP for the same reason), and a
    looping task that kept failing would otherwise ring the phone every cycle,
    as often as every twenty minutes. Someone who wants to hear about a loop
    is looking at the list; the phone is for the finishes that need a person.
    """
    if task.reply_every_seconds:
        return False
    return task.status in FINISH_WORDS


def build_payload(task: Task) -> dict[str, str]:
    """The note a phone shows for *task*: its name, how it finished, and the
    one-line summary when there is one.

    Deliberately no alias — it is a two-letter shorthand for typing at a
    terminal and means nothing on a lock screen. ``tag`` lets a second finish
    of the same task replace the first notification rather than stack under
    it, and ``url`` is where a tap should land.
    """
    words = FINISH_WORDS.get(task.status, task.status.value)
    summary = (task.summary_one_liner or "").strip()
    body = f"{words} — {summary}" if summary else words
    if len(body) > BODY_LIMIT:
        body = body[: BODY_LIMIT - 1].rstrip() + "…"
    return {
        "title": task.name,
        "body": body,
        "tag": f"task:{task.name}",
        "url": f"#/t/{task.name}",
        "status": task.status.value,
    }


def validate_subscription(sub: Any) -> dict[str, Any] | None:
    """The subscription a browser produced, reduced to what sending needs, or
    ``None`` if it is not one. Nothing else the browser sent is kept."""
    if not isinstance(sub, dict):
        return None
    endpoint = sub.get("endpoint")
    keys = sub.get("keys")
    if not isinstance(endpoint, str) or not endpoint.startswith("https://"):
        return None
    if not isinstance(keys, dict):
        return None
    p256dh, auth = keys.get("p256dh"), keys.get("auth")
    if not (isinstance(p256dh, str) and p256dh and isinstance(auth, str) and auth):
        return None
    return {"endpoint": endpoint, "keys": {"p256dh": p256dh, "auth": auth}}


class PushNotifier:
    """Owns the signing key, the device list, and the sending thread.

    *sender* is ``pywebpush.webpush`` in production and a recorder in tests.
    """

    def __init__(
        self,
        workdir: Callable[[], Path] = cfg.get_workdir,
        sender: Callable[..., Any] = webpush,
    ) -> None:
        self._workdir = workdir
        self._sender = sender
        self._vapid: Vapid | None = None
        self._subs_lock = threading.Lock()
        self._queue: queue.Queue[dict[str, str]] = queue.Queue()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ── files ────────────────────────────────────────────────────

    def _dir(self) -> Path:
        d = self._workdir() / "push"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _vapid_path(self) -> Path:
        return self._dir() / "vapid.pem"

    def _subs_path(self) -> Path:
        return self._dir() / "subscriptions.json"

    def _keys(self) -> Vapid:
        """The server's signing key, created on first use and kept forever.

        Kept because the phones remember the public half: a new key would
        silently orphan every existing subscription.
        """
        if self._vapid is not None:
            return self._vapid
        path = self._vapid_path()
        if path.exists():
            self._vapid = Vapid.from_pem(path.read_bytes())
        else:
            vapid = Vapid()
            vapid.generate_keys()
            path.write_bytes(vapid.private_pem())
            os.chmod(path, 0o600)
            self._vapid = vapid
        return self._vapid

    def public_key(self) -> str:
        """The application server key a browser subscribes with: the public
        half as a base64url-encoded uncompressed P-256 point."""
        raw = self._keys().public_key.public_bytes(
            serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint,
        )
        return b64urlencode(raw)

    # ── subscriptions ────────────────────────────────────────────

    def _load(self) -> dict[str, dict[str, Any]]:
        path = self._subs_path()
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def _save(self, subs: dict[str, dict[str, Any]]) -> None:
        tmp = self._subs_path().with_suffix(".json.tmp")
        tmp.write_text(json.dumps(subs, indent=2, sort_keys=True))
        os.replace(tmp, self._subs_path())

    def subscribe(self, sub: Any) -> int | None:
        """Remember a device. Returns how many are known, or ``None`` if *sub*
        is not a subscription. Re-subscribing an endpoint is a no-op."""
        clean = validate_subscription(sub)
        if clean is None:
            return None
        with self._subs_lock:
            subs = self._load()
            if clean["endpoint"] not in subs:
                subs[clean["endpoint"]] = {
                    **clean, "added_at": datetime.now(timezone.utc).isoformat(),
                }
                self._save(subs)
            return len(subs)

    def unsubscribe(self, endpoint: Any) -> bool:
        """Forget a device. True if it was known."""
        if not isinstance(endpoint, str):
            return False
        with self._subs_lock:
            subs = self._load()
            if endpoint not in subs:
                return False
            del subs[endpoint]
            self._save(subs)
            return True

    def subscription_count(self) -> int:
        with self._subs_lock:
            return len(self._load())

    # ── sending ──────────────────────────────────────────────────

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="push-sender", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def notify_finished(self, task: Task) -> bool:
        """Queue a notification for a just-reaped task, if it merits one and
        anyone is listening. Never blocks and never raises; returns whether a
        note was queued."""
        if not should_notify(task) or self.subscription_count() == 0:
            return False
        self._queue.put(build_payload(task))
        return True

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                payload = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            with contextlib.suppress(Exception):
                self.send(payload)

    def send(self, payload: dict[str, str]) -> int:
        """Post *payload* to every device. Returns how many were reached.

        A device whose push service says 404 or 410 has gone — the app was
        removed or the subscription expired — and is dropped so it is not
        retried forever. Any other failure is reported and the device kept.
        """
        with self._subs_lock:
            subs = dict(self._load())
        data = json.dumps(payload)
        reached = 0
        gone: list[str] = []
        for endpoint, sub in subs.items():
            try:
                self._sender(
                    subscription_info={"endpoint": sub["endpoint"], "keys": sub["keys"]},
                    data=data,
                    vapid_private_key=self._keys(),
                    vapid_claims={"sub": push_contact()},
                    ttl=PUSH_TTL_SECONDS,
                    timeout=10,
                )
                reached += 1
            except WebPushException as exc:
                if exc.status_code in (404, 410):
                    gone.append(endpoint)
                else:
                    print(f"push: {endpoint[:40]}… failed: {exc}", file=sys.stderr)
            except Exception as exc:  # noqa: BLE001 — one bad device must not stop the rest
                print(f"push: {endpoint[:40]}… failed: {exc}", file=sys.stderr)
        for endpoint in gone:
            self.unsubscribe(endpoint)
        return reached
