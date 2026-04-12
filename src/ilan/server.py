"""Background HTTP server that drives the agent scheduler loop.

Started automatically on the first ``ilan`` command and stopped via
``ilan server stop``.  Binds to an ephemeral port on 127.0.0.1 and writes
the PID + port to ``<workdir>/server.pid``.
"""

from __future__ import annotations

import json
import os
import re
import signal
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from ilan import __version__, config as cfg, get_git_commit
from ilan.models import Task, TaskStatus, validate_task_name
from ilan.runner import Runner
from ilan.store import Store

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
        os.kill(info["pid"], 0)
        return info
    except (json.JSONDecodeError, ProcessLookupError, KeyError, PermissionError):
        pf.unlink(missing_ok=True)
        return None


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
    ("POST",   r"^/tasks/([^/]+)/reply$",      "handle_task_reply"),
    ("POST",   r"^/tasks/([^/]+)/kill$",       "handle_task_kill"),
    ("POST",   r"^/tasks/([^/]+)/rename$",     "handle_task_rename"),
    ("GET",    r"^/tasks/([^/]+)/logs$",       "handle_task_logs"),
    ("GET",    r"^/tasks/([^/]+)/log-path$",   "handle_task_log_path"),
    ("GET",    r"^/tasks/([^/]+)/tail$",       "handle_task_tail"),
    ("GET",    r"^/tasks/([^/]+)/path$",       "handle_task_path"),
    ("POST",   r"^/clear-everything$",         "handle_clear_everything"),
    ("POST",   r"^/stop$",                     "handle_stop"),
]


# ── HTTP server subclass ─────────────────────────────────────────────

class _HTTPServer(ThreadingHTTPServer):
    daemon_threads = True

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
        self._nudge_event = threading.Event()
        self._httpd: _HTTPServer | None = None

    # ── lifecycle ────────────────────────────────────────────────

    def run(self, host: str = "0.0.0.0", port: int = DEFAULT_PORT) -> None:
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

        sched = threading.Thread(target=self._scheduler_loop, daemon=True)
        sched.start()

        try:
            self._httpd.serve_forever(poll_interval=0.5)
        finally:
            pf.unlink(missing_ok=True)

    def shutdown(self) -> None:
        self._stop_event.set()
        self._nudge_event.set()
        if self._httpd:
            threading.Thread(target=self._httpd.shutdown, daemon=True).start()

    def nudge(self) -> None:
        """Wake the scheduler so it runs immediately."""
        self._nudge_event.set()

    # ── scheduler ────────────────────────────────────────────────

    def _scheduler_loop(self) -> None:
        while not self._stop_event.is_set():
            with self.lock:
                self.runner.schedule()
            self._nudge_event.wait(POLL_INTERVAL)
            self._nudge_event.clear()


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
                self._json({"error": f"Task {name} not found"}, 404)
            return task

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
            conf = cfg.load()
            conf[key] = int(value) if key in cfg.INT_KEYS else value
            cfg.save(conf)
            self._json({"ok": True, "key": key, "value": conf[key]})

        # ── tasks ────────────────────────────────────────────────

        def handle_list_tasks(self):
            qs = self.path.split("?", 1)[1] if "?" in self.path else ""
            show_all = "all=true" in qs
            with self._ilan.lock:
                tasks = self._ilan.store.load_tasks()
            rows = []
            for t in sorted(tasks.values(), key=lambda t: t.status_changed_at or t.created_at, reverse=True):
                if not show_all and t.status.is_terminal:
                    continue
                rows.append({
                    "name": t.name,
                    "status": t.status.value,
                    "created_at": t.created_at,
                    "status_changed_at": t.status_changed_at,
                    "alias": t.alias,
                    "needs_review": t.needs_review,
                    "cost_usd": t.cost_usd,
                })
            self._json({"tasks": rows})

        def handle_add_task(self):
            body = self._body()
            name, prompt = body["name"], body["prompt"]
            err = validate_task_name(name)
            if err:
                self._json({"error": err}, 400)
                return
            with self._ilan.lock:
                if self._ilan.store.get_task(name) is not None:
                    existing = self._ilan.store.get_task(name)
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
                )
                self._ilan.store.put_task(task)
            self._ilan.nudge()
            self._json({"ok": True})

        def handle_get_task(self, name: str):
            with self._ilan.lock:
                task = self._get_task_or_404(name)
            if task:
                self._json({"task": task.to_dict()})

        def handle_delete_task(self, name: str):
            with self._ilan.lock:
                task = self._get_task_or_404(name)
                if task is None:
                    return
                if task.status == TaskStatus.WORKING:
                    self._ilan.runner.kill(task)
                self._ilan.store.delete_task(task.name)
            self._json({"ok": True, "name": task.name})

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
                self._ilan.store.put_task(task)
            self._json({"ok": True, "name": task.name})

        def handle_task_discard(self, name: str):
            with self._ilan.lock:
                task = self._get_task_or_404(name)
                if task is None:
                    return
                if task.status == TaskStatus.WORKING:
                    self._ilan.runner.kill(task)
                task.set_status(TaskStatus.DISCARDED)
                task.alias = None
                task.needs_review = False
                self._ilan.store.put_task(task)
            self._json({"ok": True, "name": task.name})

        def handle_task_undone(self, name: str):
            with self._ilan.lock:
                task = self._get_task_or_404(name)
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
                task = self._get_task_or_404(name)
                if task is None:
                    return
                if task.status != TaskStatus.DISCARDED:
                    self._json({"error": f"Task is {task.status.value}, not DISCARDED"}, 409)
                    return
                task.set_status(TaskStatus.NEEDS_ATTENTION)
                task.alias = self._ilan.store.next_available_alias()
                self._ilan.store.put_task(task)
            self._json({"ok": True, "name": task.name})

        def handle_task_reply(self, name: str):
            body = self._body()
            message = body["message"]
            with self._ilan.lock:
                task = self._get_task_or_404(name)
                if task is None:
                    return
                if task.status.is_terminal:
                    self._json({"error": f"Task is {task.status.value}. Cannot reply."}, 409)
                    return

                store = self._ilan.store
                runner = self._ilan.runner

                if task.status == TaskStatus.UNCLAIMED:
                    task.cached_replies.append(message)
                    store.append_log(task.name, "user", message)
                    store.put_task(task)
                    self._json({"ok": True, "warning": "Task is UNCLAIMED. Reply cached."})
                    return

                if task.status == TaskStatus.WORKING:
                    runner.reply_to_working(task, message)
                    self._json({"ok": True, "message": "Interrupted agent and resumed with reply."})
                    return

                # NEEDS_ATTENTION / AGENT_FINISHED / ERROR
                task.cached_replies.append(message)
                task.needs_review = False
                task.set_status(TaskStatus.UNCLAIMED)
                store.append_log(task.name, "user", message)
                store.put_task(task)
            self._ilan.nudge()
            self._json({"ok": True, "message": "Reply cached. Task set to UNCLAIMED."})

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
                self._ilan.store.put_task(task)
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
            self._json({"ok": True, "old_name": old_task_name, "new_name": task.name})

        def handle_task_logs(self, name: str):
            with self._ilan.lock:
                task = self._get_task_or_404(name)
                if task is None:
                    return
                if task.needs_review:
                    task.needs_review = False
                    self._ilan.store.put_task(task)
                entries = self._ilan.store.read_logs(task.name)
            self._json({"logs": [e.to_dict() for e in entries]})

        def handle_task_log_path(self, name: str):
            with self._ilan.lock:
                task = self._get_task_or_404(name)
                if task is None:
                    return
                path = self._ilan.store.log_path(task.name)
            self._json({"path": str(path)})

        def handle_task_tail(self, name: str):
            with self._ilan.lock:
                task = self._get_task_or_404(name)
                if task is None:
                    return
                if task.needs_review:
                    task.needs_review = False
                    self._ilan.store.put_task(task)
                entries = self._ilan.store.read_logs(task.name)

            if not entries:
                self._json({"entries": [], "warning": "No logs yet."})
                return

            last_asst = None
            for i in range(len(entries) - 1, -1, -1):
                if entries[i].role == "assistant":
                    last_asst = i
                    break
            if last_asst is None:
                self._json({"entries": [], "warning": "No assistant messages yet."})
                return

            self._json({"entries": [e.to_dict() for e in entries[last_asst:]]})

        def handle_task_path(self, name: str):
            with self._ilan.lock:
                task = self._get_task_or_404(name)
            if task is None:
                return
            if not task.session_log_path and task.session_id:
                log_path = Runner._find_session_log(task.session_id)
                if log_path:
                    task.session_log_path = str(log_path)
                    with self._ilan.lock:
                        self._ilan.store.put_task(task)
            if not task.session_log_path:
                self._json({"error": f"No session log path for task {task.name}"}, 404)
                return
            self._json({"path": task.session_log_path})

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
    server = IlanServer()
    server.run()


if __name__ == "__main__":
    main()
