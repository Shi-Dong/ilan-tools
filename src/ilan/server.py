"""Background HTTP server that spawns agents and reaps them when they exit.

Started automatically on the first ``ilan`` command and stopped via
``ilan server stop``.  Binds to an ephemeral port on 127.0.0.1 and writes
the PID + port to ``<workdir>/server.pid``.
"""

from __future__ import annotations

import getpass
import json
import os
import re
import signal
import sys
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from ilan import __version__, config as cfg, get_git_commit
from ilan.backends.claude import last_assistant_model
from ilan.gist import GistSyncer, github_token, last_comment_url
from ilan.models import (
    ALIAS_POOL,
    DEFAULT_ENGINE,
    ENGINE_CLAUDE,
    FABLE_MODEL,
    REPLY_EVERY_MIN_SECONDS,
    VALID_ENGINES,
    Task,
    TaskStatus,
    generate_task_hash,
    other_engine,
    parse_task_number,
    validate_task_name,
)
from ilan.runner import Runner
from ilan.store import Store
from ilan.tmux import kill_tmux_sessions_by_prefix

POLL_INTERVAL = 3  # seconds
DEFAULT_PORT = 4526


# ── PID file helpers (shared with client.py) ─────────────────────────

def pid_file_path():
    return cfg.get_workdir() / "server.pid"


def read_server_info() -> dict | None:
    """Return ``{"pid": int, "port": int}`` if the server is alive, else *None*."""
    pf = pid_file_path()
    if not pf.exists():
        return None
    try:
        with open(pf) as f:
            info = json.load(f)
    except (json.JSONDecodeError, PermissionError):
        pf.unlink(missing_ok=True)
        return None
    try:
        os.kill(info["pid"], 0)
    except PermissionError:
        # EPERM means the pid exists but belongs to another user (e.g. a
        # client account probing a server started by a different account).
        # The server is alive — don't delete its pid file.
        pass
    except (ProcessLookupError, KeyError):
        pf.unlink(missing_ok=True)
        return None
    return info


# ── server owner pinning (shared with client.py) ─────────────────────

def server_owner_path() -> Path:
    return cfg.get_workdir() / "server.owner"


def read_server_owner() -> str | None:
    """Return the account server startup is pinned to, or *None*.

    A ``server.owner`` file in the workdir (containing a username) pins
    server startup to that account.  This matters when several accounts
    share one workdir (e.g. on a ``noowners`` volume): a server started
    under another account spawns agents the pinned owner cannot signal,
    so every kill/reply on those tasks fails with EPERM.
    """
    path = server_owner_path()
    if not path.exists():
        return None
    return path.read_text().strip() or None


# ── URL routing table ────────────────────────────────────────────────

ROUTES: list[tuple[str, str, str]] = [
    ("GET",    r"^/version$",                  "handle_version"),
    ("GET",    r"^/health$",                   "handle_health"),
    ("GET",    r"^/config$",                   "handle_get_config"),
    ("POST",   r"^/config/set$",               "handle_set_config"),
    ("GET",    r"^/tasks$",                    "handle_list_tasks"),
    ("POST",   r"^/tasks$",                    "handle_add_task"),
    ("GET",    r"^/tasks/([^/]+)$",            "handle_get_task"),
    ("DELETE", r"^/tasks/([^/]+)$",            "handle_delete_task"),
    ("POST",   r"^/tasks/([^/]+)/done$",       "handle_task_done"),
    ("POST",   r"^/tasks/([^/]+)/discard$",    "handle_task_discard"),
    ("POST",   r"^/tasks/([^/]+)/undone$",     "handle_task_undone"),
    ("POST",   r"^/tasks/([^/]+)/undiscard$",  "handle_task_undiscard"),
    ("POST",   r"^/tasks/([^/]+)/unread$",     "handle_task_unread"),
    ("POST",   r"^/tasks/([^/]+)/pin$",        "handle_task_pin"),
    ("POST",   r"^/tasks/([^/]+)/unpin$",      "handle_task_unpin"),
    ("POST",   r"^/tasks/([^/]+)/reply$",      "handle_task_reply"),
    ("POST",   r"^/tasks/([^/]+)/sleep$",      "handle_task_sleep"),
    ("POST",   r"^/tasks/([^/]+)/kill$",       "handle_task_kill"),
    ("POST",   r"^/tasks/([^/]+)/rename$",     "handle_task_rename"),
    ("POST",   r"^/tasks/([^/]+)/alias$",      "handle_task_set_alias"),
    ("POST",   r"^/tasks/([^/]+)/branch$",     "handle_task_branch"),
    ("POST",   r"^/tasks/([^/]+)/max$",        "handle_task_max"),
    ("POST",   r"^/tasks/([^/]+)/unmax$",      "handle_task_unmax"),
    ("POST",   r"^/tasks/([^/]+)/switch-backend$", "handle_task_switch_backend"),
    ("GET",    r"^/tasks/([^/]+)/logs$",       "handle_task_logs"),
    ("GET",    r"^/tasks/([^/]+)/log-path$",   "handle_task_log_path"),
    ("GET",    r"^/tasks/([^/]+)/tail$",       "handle_task_tail"),
    ("GET",    r"^/tasks/([^/]+)/path$",       "handle_task_path"),
    ("GET",    r"^/tasks/([^/]+)/last-model$", "handle_task_last_model"),
    ("GET",    r"^/tasks/([^/]+)/history-url$", "handle_task_history_url"),
    ("POST",   r"^/clear-everything$",         "handle_clear_everything"),
    ("POST",   r"^/stop$",                     "handle_stop"),
]


# ── HTTP server subclass ─────────────────────────────────────────────

class _HTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    # socketserver defaults this to 5, which is tiny: a burst of short-lived
    # client connections (e.g. a misbehaving poller) overflows the kernel
    # accept queue, after which new SYNs — including from localhost — are
    # dropped and time out, making a healthy server look completely down.
    request_queue_size = 128

    def __init__(self, addr, handler_cls, ilan: IlanServer):
        super().__init__(addr, handler_cls)
        self.ilan = ilan


# ── Core server ──────────────────────────────────────────────────────

class IlanServer:
    def __init__(self) -> None:
        self.store = Store(cfg.get_workdir())
        self.runner = Runner(self.store)
        self.lock = threading.Lock()
        self._stop_event = threading.Event()
        self._httpd: _HTTPServer | None = None
        # Mirror every conversation to a secret GitHub Gist off the hot path.
        # Wiring the syncer to ``store.on_append`` means any message appended
        # anywhere (server handlers or runner reap) is enqueued automatically.
        self.gist = GistSyncer(self.store, self.lock)
        self.store.on_append = self.gist.enqueue
        self._backfill_task_numbers()

    def _backfill_task_numbers(self) -> None:
        """Number tasks that were already closed before numbering existed.

        Oldest first, so an existing task list ends up with the numbers it
        would have had if they had been minted all along.
        """
        tasks = self.store.load_tasks()
        pending = sorted(
            (t for t in tasks.values() if t.status.is_terminal and t.number is None),
            key=lambda t: t.created_at,
        )
        if not pending:
            return
        start = self.store.next_task_number()
        for offset, task in enumerate(pending):
            task.number = start + offset
        self.store.save_tasks(tasks)

    # ── lifecycle ────────────────────────────────────────────────

    def run(
        self,
        host: str = "0.0.0.0",
        port: int = DEFAULT_PORT,
        poll_interval: float = 0.5,
    ) -> None:
        # ``shutdown()`` blocks until the serve loop's next ``select()`` tick,
        # so *poll_interval* bounds shutdown latency. Tests pass a small value
        # to keep per-test server teardown cheap.
        handler_cls = _make_handler()
        self._httpd = _HTTPServer((host, port), handler_cls, self)
        actual_port = self._httpd.server_address[1]

        pf = pid_file_path()
        pf.parent.mkdir(parents=True, exist_ok=True)
        with open(pf, "w") as f:
            json.dump({"pid": os.getpid(), "port": actual_port}, f)

        signal.signal(signal.SIGTERM, lambda *_: self.shutdown())
        signal.signal(signal.SIGINT, lambda *_: self.shutdown())

        recovered = self.runner.recover()
        if recovered:
            print(f"Recovered {len(recovered)} task(s): {', '.join(recovered)}")

        reaper = threading.Thread(target=self._reaper_loop, daemon=True)
        reaper.start()

        self.gist.start()

        try:
            self._httpd.serve_forever(poll_interval=poll_interval)
        finally:
            pf.unlink(missing_ok=True)

    def shutdown(self) -> None:
        self._stop_event.set()
        self.gist.stop()
        if self._httpd:
            threading.Thread(target=self._httpd.shutdown, daemon=True).start()

    # ── reaper ───────────────────────────────────────────────────

    def _reaper_loop(self) -> None:
        while not self._stop_event.is_set():
            with self.lock:
                self.runner.reap_finished()
                self._fire_due_replies()
            self._stop_event.wait(POLL_INTERVAL)

    def _fire_due_replies(self) -> None:
        """Deliver due ``reply -t`` messages. Caller must hold the lock.

        Each firing behaves like a human reply — a WORKING agent is
        interrupted and resumed with the message, any other live task is
        restarted with it — except that it does not end the cycle: the same
        message is rescheduled ``reply_every_seconds`` later.
        """
        now = datetime.now(timezone.utc)
        for task in self.store.load_tasks().values():
            if not task.reply_every_seconds or not task.reply_every_next_at:
                continue
            if task.status.is_terminal:
                # ``set_status`` already clears the cycle on done/discard;
                # this guards tasks persisted before that rule existed.
                task.clear_reply_every()
                self.store.put_task(task)
                continue
            try:
                next_at = datetime.fromisoformat(task.reply_every_next_at)
            except ValueError:
                task.clear_reply_every()
                self.store.put_task(task)
                continue
            if now < next_at:
                continue
            message = task.reply_every_message or ""
            # Reschedule before delivering so a crash mid-send cannot
            # re-fire the same tick on recovery.
            task.reply_every_next_at = (
                now + timedelta(seconds=task.reply_every_seconds)
            ).isoformat()
            if task.status == TaskStatus.WORKING:
                self.runner.reply_to_working(task, message)
                continue
            # NEEDS_ATTENTION / AGENT_FINISHED / ERROR
            task.cached_replies.append(message)
            task.needs_review = False
            self.store.append_log(task.name, "user", message)
            self.store.put_task(task)
            self.runner.start(task)


# ── Request handler (built via closure to capture IlanServer) ────────

def _make_handler() -> type[BaseHTTPRequestHandler]:

    class Handler(BaseHTTPRequestHandler):
        server: _HTTPServer

        # ── plumbing ─────────────────────────────────────────────

        def _json(self, data: dict, status: int = 200) -> None:
            body = json.dumps(data).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _body(self) -> dict:
            length = int(self.headers.get("Content-Length", 0))
            return json.loads(self.rfile.read(length)) if length else {}

        def _dispatch(self, method: str) -> None:
            path = self.path.split("?")[0]
            for route_method, pattern, handler_name in ROUTES:
                if route_method != method:
                    continue
                m = re.match(pattern, path)
                if m:
                    try:
                        getattr(self, handler_name)(*m.groups())
                    except Exception as exc:
                        self._json({"error": str(exc)}, 500)
                    return
            self._json({"error": "Not found"}, 404)

        def do_GET(self):     self._dispatch("GET")
        def do_POST(self):    self._dispatch("POST")
        def do_DELETE(self):  self._dispatch("DELETE")
        def log_message(self, fmt, *args):  pass  # quiet

        # ── shortcuts ────────────────────────────────────────────

        @property
        def _ilan(self) -> IlanServer:
            return self.server.ilan

        def _get_task_or_404(self, name: str) -> Task | None:
            task = self._ilan.store.get_task_by_name_or_alias(name)
            if task is None:
                self._json({"error": self._not_found_error(name)}, 404)
            return task

        def _get_task_by_ref_or_404(self, ref: str) -> Task | None:
            """Like ``_get_task_or_404`` but also accepts a task number.

            Only ``undone`` / ``undiscard`` use this: the number is the handle
            for a closed task, and every other command keeps rejecting it so a
            number never stands in for a live task's name.
            """
            task = self._ilan.store.get_task_by_name_alias_or_number(ref)
            if task is None:
                self._json({"error": f"Task {ref} not found"}, 404)
            return task

        def _not_found_error(self, name: str) -> str:
            """404 message for *name*, calling out a number used out of place."""
            number = parse_task_number(name)
            if number is not None and (
                numbered := self._ilan.store.get_task_by_number(number)
            ) is not None:
                return (
                    f"Task number {number} is {numbered.name}; numbers only work "
                    f"with undone/undiscard. Use the task name."
                )
            return f"Task {name} not found"

        def _reply_every_confirmation(self, task: Task, body: dict) -> dict | None:
            """Challenge to return when a human message would end a ``reply -t``
            cycle the client hasn't confirmed ending (no ``override_reply_every``
            in *body*). ``None`` means proceed."""
            if not task.reply_every_seconds or body.get("override_reply_every"):
                return None
            return {
                "confirm_reply_every": True,
                "name": task.name,
                "reply_every_seconds": task.reply_every_seconds,
            }

        # ── route handlers ───────────────────────────────────────

        def handle_health(self):
            self._json({"status": "ok"})

        def handle_version(self):
            self._json({"version": __version__, "commit": get_git_commit()})

        def handle_get_config(self):
            self._json({"config": cfg.load()})

        def handle_set_config(self):
            body = self._body()
            key, value = body["key"], body["value"]
            if key not in cfg.VALID_KEYS:
                self._json({"error": f"Unknown config key: {key}"}, 400)
                return
            if key in cfg.CLIENT_SIDE_KEYS:
                self._json(
                    {"error": f"'{key}' is a client-side config; set it on the machine "
                              f"running the CLI, not the server."},
                    400,
                )
                return
            if key == "default-backend" and value not in VALID_ENGINES:
                self._json(
                    {"error": f"Invalid value {value!r} for default-backend. "
                              f"Choose from: {', '.join(VALID_ENGINES)}"},
                    400,
                )
                return
            if key == "effort" and value not in cfg.VALID_EFFORTS:
                self._json(
                    {"error": f"Invalid value {value!r} for effort. "
                              f"Choose from: {', '.join(cfg.VALID_EFFORTS)}"},
                    400,
                )
                return
            if key in cfg.MODEL_KEYS and not cfg.is_valid_model_id(key, str(value)):
                self._json(
                    {"error": f"Invalid model id {value!r} for {key}. Use the "
                              f"exact model id the backend CLI accepts (e.g. "
                              f"{cfg.MODEL_ID_EXAMPLES[key]}); aliases like "
                              f"'opus' are rejected."},
                    400,
                )
                return
            conf = cfg.load()
            if key in cfg.INT_KEYS:
                conf[key] = int(value)
            elif key in cfg.BOOL_KEYS:
                conf[key] = cfg.parse_bool(value)
            else:
                conf[key] = value
            cfg.save(conf)
            self._json({"ok": True, "key": key, "value": conf[key]})

        # ── tasks ────────────────────────────────────────────────

        def handle_list_tasks(self):
            qs = self.path.split("?", 1)[1] if "?" in self.path else ""
            show_all = "all=true" in qs
            with self._ilan.lock:
                tasks = self._ilan.store.load_tasks()

            rows = []
            # Pinned tasks float to the top; within each group, oldest first.
            for t in sorted(tasks.values(), key=lambda t: (not t.pinned, t.created_at)):
                # A pin overrides the default filter, so a pinned DONE /
                # DISCARDED task stays visible without `-a`; unpinning it is
                # what makes it drop out of the listing again.
                if not show_all and t.status.is_terminal and not t.pinned:
                    continue
                rows.append({
                    "name": t.name,
                    "status": t.status.value,
                    "created_at": t.created_at,
                    "status_changed_at": t.status_changed_at,
                    "alias": t.alias,
                    "number": t.number,
                    "needs_review": t.needs_review,
                    "pinned": t.pinned,
                    "cost_usd": t.cost_usd,
                    "sleep_seconds": t.sleep_seconds,
                    "reply_every_seconds": t.reply_every_seconds,
                    "parent_name": t.parent_name,
                    "deleted_ancestors": t.deleted_ancestors,
                    "summary_one_liner": t.summary_one_liner,
                    "model": t.model,
                    "gist_url": t.gist_url,
                    "engine": t.engine,
                })
            self._json({"tasks": rows})

        def handle_add_task(self):
            body = self._body()
            name, prompt = body["name"], body["prompt"]
            err = validate_task_name(name)
            if err:
                self._json({"error": err}, 400)
                return
            engine = body.get("agent") or cfg.load().get("default-backend", DEFAULT_ENGINE)
            if engine not in VALID_ENGINES:
                self._json(
                    {"error": f"Unknown agent {engine!r}. Choose from: {', '.join(VALID_ENGINES)}"},
                    400,
                )
                return
            # Fable is a Claude-only model, so a maxed task must run on the
            # Claude backend; reject the contradictory combination.
            want_max = bool(body.get("max"))
            if want_max and engine != ENGINE_CLAUDE:
                self._json(
                    {"error": (
                        f"Fable ({FABLE_MODEL}) is a Claude-only model; cannot "
                        f"create a {engine} task with max."
                    )},
                    400,
                )
                return
            with self._ilan.lock:
                if (existing := self._ilan.store.get_task(name)) is not None:
                    self._json(
                        {"error": f"Task {name} already exists (status: {existing.status.value})"},
                        409,
                    )
                    return
                now = datetime.now(timezone.utc).isoformat()
                alias = self._ilan.store.next_available_alias()
                task = Task(
                    name=name,
                    prompt=prompt,
                    created_at=now,
                    status_changed_at=now,
                    alias=alias,
                    task_hash=generate_task_hash(),
                    engine=engine,
                    model=FABLE_MODEL if want_max else None,
                )
                self._ilan.store.put_task(task)
                # Log the opening prompt before spawning so the unified log
                # always opens with the task statement.
                self._ilan.store.append_log(task.name, "user", prompt)
                self._ilan.runner.start(task)
            self._json({"ok": True})

        def handle_get_task(self, name: str):
            with self._ilan.lock:
                task = self._get_task_or_404(name)
            if task:
                self._json({"task": task.to_dict()})

        def handle_delete_task(self, name: str):
            qs = self.path.split("?", 1)[1] if "?" in self.path else ""
            force = "force=true" in qs
            with self._ilan.lock:
                task = self._get_task_or_404(name)
                if task is None:
                    return
                if not force:
                    tasks = self._ilan.store.load_tasks()
                    descendants = self._ilan.store.collect_descendants(task.name, tasks)
                    active = sorted(
                        d for d in descendants
                        if d in tasks and not tasks[d].status.is_terminal
                    )
                    if active:
                        self._json(
                            {"error": (
                                f"Task {task.name} has active descendant(s): "
                                f"{', '.join(active)}. Pass -f to force delete."
                            )},
                            409,
                        )
                        return
                task_hash = task.task_hash
                if task.status == TaskStatus.WORKING:
                    self._ilan.runner.kill(task)
                self._ilan.store.delete_task(task.name)
            if task_hash:
                kill_tmux_sessions_by_prefix(task_hash)
            self._json({"ok": True, "name": task.name})

        def _assign_number(self, task: Task) -> None:
            """Mint *task*'s number the first time it closes, then leave it be.

            Keeping the number across a revive is the point: ``undone 12``
            has to still mean the same task after it is closed a second time.
            """
            if task.number is None:
                task.number = self._ilan.store.next_task_number()

        def handle_task_done(self, name: str):
            with self._ilan.lock:
                task = self._get_task_or_404(name)
                if task is None:
                    return
                if task.status == TaskStatus.WORKING:
                    self._ilan.runner.kill(task)
                task.set_status(TaskStatus.DONE)
                task.alias = None
                task.needs_review = False
                self._assign_number(task)
                self._ilan.store.put_task(task)
            if task.task_hash:
                kill_tmux_sessions_by_prefix(task.task_hash)
            self._json({"ok": True, "name": task.name})

        def handle_task_discard(self, name: str):
            with self._ilan.lock:
                task = self._get_task_or_404(name)
                if task is None:
                    return
                if task.status == TaskStatus.WORKING:
                    self._ilan.runner.kill(task)
                task.set_status(TaskStatus.DISCARDED)
                # Keep the alias: a DISCARDED task is a recycle-bin entry that
                # ``undiscard`` can bring back, so it stays reachable by its
                # short alias (not just its full name).
                task.needs_review = False
                self._assign_number(task)
                self._ilan.store.put_task(task)
            if task.task_hash:
                kill_tmux_sessions_by_prefix(task.task_hash)
            self._json({"ok": True, "name": task.name})

        def handle_task_undone(self, name: str):
            with self._ilan.lock:
                task = self._get_task_by_ref_or_404(name)
                if task is None:
                    return
                if task.status != TaskStatus.DONE:
                    self._json({"error": f"Task is {task.status.value}, not DONE"}, 409)
                    return
                task.set_status(TaskStatus.NEEDS_ATTENTION)
                task.alias = self._ilan.store.next_available_alias()
                self._ilan.store.put_task(task)
            self._json({"ok": True, "name": task.name})

        def handle_task_undiscard(self, name: str):
            with self._ilan.lock:
                task = self._get_task_by_ref_or_404(name)
                if task is None:
                    return
                if task.status != TaskStatus.DISCARDED:
                    self._json({"error": f"Task is {task.status.value}, not DISCARDED"}, 409)
                    return
                task.set_status(TaskStatus.NEEDS_ATTENTION)
                # The task kept its alias through discard; only mint a new one
                # if it somehow has none (e.g. the pool was exhausted at add).
                if task.alias is None:
                    task.alias = self._ilan.store.next_available_alias()
                self._ilan.store.put_task(task)
            self._json({"ok": True, "name": task.name})

        def handle_task_unread(self, name: str):
            with self._ilan.lock:
                task = self._get_task_or_404(name)
                if task is None:
                    return
                if not task.needs_review:
                    task.needs_review = True
                    self._ilan.store.put_task(task)
            self._json({"ok": True, "name": task.name})

        def _set_pinned(self, name: str, pinned: bool):
            with self._ilan.lock:
                task = self._get_task_or_404(name)
                if task is None:
                    return
                if task.pinned != pinned:
                    task.pinned = pinned
                    self._ilan.store.put_task(task)
            self._json({"ok": True, "name": task.name, "pinned": pinned})

        def handle_task_pin(self, name: str):
            self._set_pinned(name, True)

        def handle_task_unpin(self, name: str):
            self._set_pinned(name, False)

        def handle_task_reply(self, name: str):
            body = self._body()
            message = body["message"]
            every_seconds = body.get("every_seconds")
            if every_seconds is not None and (
                not isinstance(every_seconds, int)
                or every_seconds < REPLY_EVERY_MIN_SECONDS
            ):
                self._json(
                    {
                        "error": "every_seconds must be an integer >= "
                        f"{REPLY_EVERY_MIN_SECONDS} (20 minutes)"
                    },
                    400,
                )
                return
            with self._ilan.lock:
                task = self._get_task_or_404(name)
                if task is None:
                    return
                if task.status.is_terminal:
                    self._json({"error": f"Task is {task.status.value}. Cannot reply."}, 409)
                    return
                if confirm := self._reply_every_confirmation(task, body):
                    self._json(confirm, 409)
                    return

                store = self._ilan.store
                runner = self._ilan.runner

                # A plain reply overrides any in-flight sleep: the agent is
                # no longer sleeping on behalf of an earlier ``ilan sleep``,
                # so drop ``sleep_seconds`` in every branch below to make
                # the ``(sleeping for Ns)`` suffix disappear.
                task.sleep_seconds = None
                # A human reply likewise ends the current ``reply -t`` cycle;
                # a reply carrying ``every_seconds`` starts a fresh one.
                task.clear_reply_every()
                if every_seconds:
                    task.reply_every_seconds = every_seconds
                    task.reply_every_message = message
                    task.reply_every_next_at = (
                        datetime.now(timezone.utc) + timedelta(seconds=every_seconds)
                    ).isoformat()

                if task.status == TaskStatus.WORKING:
                    runner.reply_to_working(task, message)
                    self._json({"ok": True, "message": "Interrupted agent and resumed with reply."})
                    return

                # NEEDS_ATTENTION / AGENT_FINISHED / ERROR
                task.cached_replies.append(message)
                task.needs_review = False
                store.append_log(task.name, "user", message)
                store.put_task(task)
                runner.start(task)
            self._json({
                "ok": True,
                "name": task.name,
                "message": f"Reply sent to {task.name}. Agent resumed.",
            })

        def handle_task_sleep(self, name: str):
            body = self._body()
            try:
                seconds = int(body["seconds"])
            except (KeyError, TypeError, ValueError):
                self._json({"error": "seconds (positive integer) is required"}, 400)
                return
            if seconds <= 0:
                self._json({"error": "seconds must be positive"}, 400)
                return
            allowed = (TaskStatus.NEEDS_ATTENTION, TaskStatus.AGENT_FINISHED)
            with self._ilan.lock:
                task = self._get_task_or_404(name)
                if task is None:
                    return
                if task.status not in allowed:
                    allowed_names = ", ".join(s.value for s in allowed)
                    self._json(
                        {"error": f"Task is {task.status.value}. Sleep only works on tasks in: {allowed_names}."},
                        409,
                    )
                    return
                if confirm := self._reply_every_confirmation(task, body):
                    self._json(confirm, 409)
                    return
                message = f"Sleep {seconds} seconds and give me a quick report after the sleep finishes."
                task.cached_replies.append(message)
                task.needs_review = False
                task.sleep_seconds = seconds
                # A sleep is a human reply too, so it ends any ``reply -t`` cycle.
                task.clear_reply_every()
                self._ilan.store.append_log(task.name, "user", message)
                self._ilan.store.put_task(task)
                self._ilan.runner.start(task)
            self._json({
                "ok": True,
                "name": task.name,
                "seconds": seconds,
                "message": f"Told {task.name} to sleep {seconds}s.",
            })

        def handle_task_kill(self, name: str):
            with self._ilan.lock:
                task = self._get_task_or_404(name)
                if task is None:
                    return
                if task.status != TaskStatus.WORKING:
                    self._json({"error": f"Task is {task.status.value}, not WORKING"}, 409)
                    return
                self._ilan.runner.kill(task)
                task.set_status(TaskStatus.ERROR)
                # ERROR is not terminal, so clear the ``reply -t`` cycle
                # explicitly: a kill must not be undone by the timer.
                task.clear_reply_every()
                self._ilan.store.put_task(task)
            if task.task_hash:
                kill_tmux_sessions_by_prefix(task.task_hash)
            self._json({"ok": True, "name": task.name})

        def handle_task_rename(self, name: str):
            body = self._body()
            new_name = body.get("new_name", "").strip()
            if not new_name:
                self._json({"error": "new_name is required"}, 400)
                return
            if len(new_name) < 3:
                self._json({"error": "Task name must be at least 3 characters"}, 400)
                return
            with self._ilan.lock:
                task = self._get_task_or_404(name)
                if task is None:
                    return
                if self._ilan.store.get_task(new_name) is not None:
                    self._json({"error": f"Task {new_name} already exists"}, 409)
                    return
                old_task_name = task.name
                task = self._ilan.store.rename_task(task.name, new_name)
            # Refresh the task's Gist title off the hot path so it tracks the
            # new name (no-op when mirroring is disabled or no Gist exists yet).
            self._ilan.gist.enqueue(task.name)
            self._json({"ok": True, "old_name": old_task_name, "new_name": task.name})

        def handle_task_set_alias(self, name: str):
            body = self._body()
            new_alias = (body.get("alias") or "").strip().lower()
            with self._ilan.lock:
                task = self._get_task_or_404(name)
                if task is None:
                    return
                # A terminal task's alias is frozen: DONE tasks drop theirs,
                # and a DISCARDED task keeps its alias so it can be undiscarded
                # by it. Either way, reassigning it here is not allowed —
                # restore the task first.
                if task.status.is_terminal:
                    self._json(
                        {"error": (
                            f"Task {task.name} is {task.status.value}; its alias "
                            "can't be changed. Restore it first."
                        )},
                        409,
                    )
                    return
                if new_alias and new_alias == task.alias:
                    self._json({"ok": True, "name": task.name, "alias": task.alias})
                    return
                if new_alias not in ALIAS_POOL:
                    self._json(
                        {"error": (
                            f"Invalid alias {new_alias!r}. An alias must be exactly "
                            "two letters drawn from 'asdfghjkl'."
                        )},
                        400,
                    )
                    return
                tasks = self._ilan.store.load_tasks()
                for other in tasks.values():
                    if other.name != task.name and other.alias == new_alias:
                        self._json(
                            {"error": (
                                f"Alias {new_alias!r} is already in use by task "
                                f"{other.name}."
                            )},
                            409,
                        )
                        return
                task.alias = new_alias
                self._ilan.store.put_task(task)
            self._json({"ok": True, "name": task.name, "alias": task.alias})

        def handle_task_branch(self, name: str):
            body = self._body()
            new_name = (body.get("new_name") or "").strip()
            err = validate_task_name(new_name)
            if err:
                self._json({"error": err}, 400)
                return
            message = body.get("message")
            if not message:
                self._json(
                    {"error": (
                        "Branching requires a first assignment for the child "
                        "task (-d/-f). To continue the parent's work in "
                        "place, reply to the parent instead."
                    )},
                    400,
                )
                return
            with self._ilan.lock:
                parent = self._get_task_or_404(name)
                if parent is None:
                    return
                if self._ilan.store.get_task(new_name) is not None:
                    self._json({"error": f"Task {new_name} already exists"}, 409)
                    return
                if not parent.session_id:
                    self._json(
                        {"error": (
                            f"Task {parent.name} has no Claude Code session yet. "
                            "Branching requires an established session to inherit."
                        )},
                        409,
                    )
                    return
                if self._ilan.runner.find_session_log(parent.session_id, parent.engine) is None:
                    self._json(
                        {"error": (
                            f"Session log for task {parent.name} not found on disk. "
                            "The session may have been lost; cannot branch."
                        )},
                        409,
                    )
                    return
                alias = self._ilan.store.next_available_alias()
                if alias is None:
                    self._json(
                        {"error": "Alias pool exhausted. Free up an alias before branching."},
                        409,
                    )
                    return
                now = datetime.now(timezone.utc).isoformat()
                child = self._ilan.store.branch_task(
                    parent,
                    new_name,
                    alias=alias,
                    task_hash=generate_task_hash(),
                    now=now,
                )
                child.cached_replies.append(message)
                # Every branch carries the child's first assignment, so every
                # child is told up front that the inherited conversation is
                # context to draw on, not work to finish.
                child.awaiting_branch_notice = True
                self._ilan.store.append_log(child.name, "user", message)
                self._ilan.store.put_task(child)
                self._ilan.runner.start(child)
            self._json({
                "ok": True,
                "name": child.name,
                "parent_name": parent.name,
            })

        def handle_task_max(self, name: str):
            with self._ilan.lock:
                task = self._get_task_or_404(name)
                if task is None:
                    return
                # Fable is an Anthropic model, so only the Claude backend can
                # run it. Maxing a task on any other backend would just feed
                # that backend a model it can't load and break its next spawn,
                # so it's a no-op here — flip the task to claude first.
                if task.engine != ENGINE_CLAUDE:
                    self._json({
                        "ok": True,
                        "name": task.name,
                        "model": task.model,
                        "warning": (
                            f"Task {task.name} runs on the {task.engine} backend; "
                            f"Fable ({FABLE_MODEL}) is a Claude-only model, so "
                            "max did nothing. Switch it to claude first "
                            f"(ilan task switch-backend {task.name})."
                        ),
                    })
                    return
                task.model = FABLE_MODEL
                self._ilan.store.put_task(task)
            self._json({"ok": True, "name": task.name, "model": task.model})

        def handle_task_unmax(self, name: str):
            with self._ilan.lock:
                task = self._get_task_or_404(name)
                if task is None:
                    return
                task.model = None
                self._ilan.store.put_task(task)
            self._json({"ok": True, "name": task.name, "model": task.model})

        def handle_task_switch_backend(self, name: str):
            with self._ilan.lock:
                task = self._get_task_or_404(name)
                if task is None:
                    return
                if task.status.is_terminal:
                    self._json(
                        {"error": (
                            f"Task {task.name} is {task.status.value}; "
                            "cannot switch its backend."
                        )},
                        409,
                    )
                    return
                # A WORKING task is mid-flight: killing it to flip the backend
                # would either discard the in-flight turn or misattribute its
                # output. Refuse and let the user wait or kill explicitly.
                if task.status == TaskStatus.WORKING:
                    self._json(
                        {"error": (
                            f"Task {task.name} is WORKING; cannot switch its "
                            "backend while the agent is running. Wait for it "
                            "to finish (or kill it) and try again."
                        )},
                        409,
                    )
                    return
                from_engine = task.engine
                target = other_engine(from_engine)
                self._ilan.runner.switch_engine(task, target)
            self._json({
                "ok": True,
                "name": task.name,
                "from_engine": from_engine,
                "engine": task.engine,
            })

        def handle_task_logs(self, name: str):
            with self._ilan.lock:
                task = self._get_task_or_404(name)
                if task is None:
                    return
                if task.needs_review:
                    task.needs_review = False
                    self._ilan.store.put_task(task)
                entries = self._ilan.store.read_logs(task.name)
            self._json({
                "name": task.name,
                "alias": task.alias,
                "last_assistant_model": task.last_assistant_model,
                "last_assistant_effort": task.last_assistant_effort,
                "last_assistant_budget": task.last_assistant_budget,
                "last_assistant_cost_usd": task.last_assistant_cost_usd,
                "logs": [e.to_dict() for e in entries],
            })

        def handle_task_log_path(self, name: str):
            with self._ilan.lock:
                task = self._get_task_or_404(name)
                if task is None:
                    return
                path = self._ilan.store.log_path(task.name)
            self._json({"path": str(path)})

        def handle_task_tail(self, name: str):
            qs = self.path.split("?", 1)[1] if "?" in self.path else ""
            n: int | None = None
            for part in qs.split("&"):
                if part.startswith("n="):
                    try:
                        n = int(part[2:])
                    except ValueError:
                        self._json({"error": "n must be an integer"}, 400)
                        return
                    if n <= 0:
                        self._json({"error": "n must be positive"}, 400)
                        return

            with self._ilan.lock:
                task = self._get_task_or_404(name)
                if task is None:
                    return
                if task.needs_review:
                    task.needs_review = False
                    self._ilan.store.put_task(task)
                entries = self._ilan.store.read_logs(task.name)

            meta = {
                "name": task.name,
                "alias": task.alias,
                "last_assistant_model": task.last_assistant_model,
                "last_assistant_effort": task.last_assistant_effort,
                "last_assistant_budget": task.last_assistant_budget,
                "last_assistant_cost_usd": task.last_assistant_cost_usd,
            }

            if not entries:
                self._json({**meta, "entries": [], "warning": "No logs yet."})
                return

            if n is not None:
                selected = entries[-n:]
                self._json({**meta, "entries": [e.to_dict() for e in selected]})
                return

            last_asst = None
            for i in range(len(entries) - 1, -1, -1):
                if entries[i].role == "assistant":
                    last_asst = i
                    break
            if last_asst is None:
                self._json({**meta, "entries": [], "warning": "No assistant messages yet."})
                return

            # Also include the most recent user message before the last
            # assistant, so the user has the prompt that elicited the reply
            # in view (and can tell the conversation apart at a glance).
            start = last_asst
            for j in range(last_asst - 1, -1, -1):
                if entries[j].role == "user":
                    start = j
                    break

            self._json({**meta, "entries": [e.to_dict() for e in entries[start:]]})

        def handle_task_path(self, name: str):
            with self._ilan.lock:
                task = self._get_task_or_404(name)
            if task is None:
                return
            if not task.session_log_path and task.session_id and (
                log_path := self._ilan.runner.find_session_log(task.session_id, task.engine)
            ):
                task.session_log_path = str(log_path)
                with self._ilan.lock:
                    self._ilan.store.put_task(task)
            if not task.session_log_path:
                self._json({"error": f"No session log path for task {task.name}"}, 404)
                return
            self._json({"path": task.session_log_path})

        def handle_task_last_model(self, name: str):
            with self._ilan.lock:
                task = self._get_task_or_404(name)
            if task is None:
                return
            # Fast path: return the model (and effort/budget) cached at reap time.
            if task.last_assistant_model:
                self._json({
                    "name": task.name,
                    "model": task.last_assistant_model,
                    "effort": task.last_assistant_effort,
                    "budget": task.last_assistant_budget,
                    "cost_usd": task.last_assistant_cost_usd,
                })
                return
            # Fallback for tasks last reaped before the cache existed: resolve
            # from the session log once, then backfill so future lookups are free.
            if not task.session_log_path and task.session_id and (
                log_path := self._ilan.runner.find_session_log(task.session_id, task.engine)
            ):
                task.session_log_path = str(log_path)
                with self._ilan.lock:
                    self._ilan.store.put_task(task)
            if not task.session_log_path:
                self._json({"error": f"No session log path for task {task.name}"}, 404)
                return
            log_file = Path(task.session_log_path)
            if not log_file.exists():
                self._json({"error": f"Session log file missing: {log_file}"}, 404)
                return
            model = last_assistant_model(log_file)
            if model is None:
                self._json(
                    {"error": f"No assistant message found in session log for task {task.name}"},
                    404,
                )
                return
            task.last_assistant_model = model
            with self._ilan.lock:
                self._ilan.store.put_task(task)
            # The session log carries none of the effort, the paying account or
            # the cost, so this fallback path resolves the model alone; the rest
            # stay whatever was cached.
            self._json({
                "name": task.name,
                "model": model,
                "effort": task.last_assistant_effort,
                "budget": task.last_assistant_budget,
                "cost_usd": task.last_assistant_cost_usd,
            })

        def handle_task_history_url(self, name: str):
            with self._ilan.lock:
                task = self._get_task_or_404(name)
                if task is None:
                    return
                gist_id, gist_url = task.gist_id, task.gist_url
                if gist_id and gist_url and task.needs_review:
                    task.needs_review = False
                    self._ilan.store.put_task(task)
            if not gist_id or not gist_url:
                self._json({"url": None})
                return
            if not (token := github_token()):
                self._json({"url": gist_url})
                return
            try:
                url = last_comment_url(token, gist_id, gist_url)
            except Exception:
                # Network/auth hiccups fall back to the plain Gist page.
                url = gist_url
            self._json({"url": url})

        def handle_clear_everything(self):
            with self._ilan.lock:
                for task in self._ilan.store.load_tasks().values():
                    if task.status == TaskStatus.WORKING:
                        self._ilan.runner.kill(task)
                self._ilan.store.delete_all()
            self._json({"ok": True})

        def handle_stop(self):
            self._json({"ok": True})
            self._ilan.shutdown()

    return Handler


def main() -> None:
    owner = read_server_owner()
    user = getpass.getuser()
    if owner is not None and user != owner:
        print(
            f"ilan workdir {cfg.get_workdir()} is pinned to user {owner!r} "
            f"(server.owner file); refusing to start a server as {user!r}.",
            file=sys.stderr,
        )
        sys.exit(1)
    server = IlanServer()
    server.run()


if __name__ == "__main__":
    main()
