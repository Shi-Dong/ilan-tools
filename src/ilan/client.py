"""Thin HTTP client that talks to the ilan background server.

Auto-starts the server on the first call if it isn't already running.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from ilan import config as cfg
from ilan import get_git_commit
from ilan.server import pid_file_path, read_server_info

SERVER_URL_ENV = "ILAN_SERVER_URL"


class Client:
    """JSON-over-HTTP client for the ilan server.

    Resolution order for the server address:

    1. Explicit *base_url* or *port* passed to ``__init__``.
    2. The ``ILAN_SERVER_URL`` environment variable.
    3. Auto-discover (and auto-start) a local server via the PID file.
    """

    version_mismatch: str | None = None
    """Set after :meth:`ensure_server` if local/remote commits differ."""

    def __init__(self, *, port: int | None = None, base_url: str | None = None) -> None:
        if base_url:
            self._base_url: str | None = base_url.rstrip("/")
            self._remote = True
        elif port:
            self._base_url = f"http://127.0.0.1:{port}"
            self._remote = False
        else:
            env_url = os.environ.get(SERVER_URL_ENV)
            if env_url:
                self._base_url = env_url.rstrip("/")
                self._remote = True
            else:
                self._base_url = None
                self._remote = False

    @property
    def is_remote(self) -> bool:
        return self._remote

    # ── server lifecycle ─────────────────────────────────────────

    def ensure_server(self) -> dict:
        """Return server info, starting a local server if necessary.

        For remote servers (``ILAN_SERVER_URL``), just verifies reachability.
        """
        if self._remote:
            try:
                self.health()
            except Exception as exc:
                raise RuntimeError(
                    f"Cannot reach remote ilan server at {self._base_url}: {exc}"
                ) from exc
            self._check_remote_version()
            return {"url": self._base_url}

        info = self._probe()
        if info:
            return info

        self._start_server()
        return self._wait_for_server()

    def _check_remote_version(self) -> None:
        """Warn if the local ilan commit differs from the remote server's."""
        local_commit = get_git_commit()
        if local_commit is None:
            return
        try:
            resp = self.version()
            server_commit = resp.get("commit")
        except Exception:
            return
        if server_commit is None:
            return
        if local_commit != server_commit:
            self.version_mismatch = (
                f"local={local_commit}, server={server_commit}"
            )

    def _probe(self) -> dict | None:
        info = read_server_info()
        if info is None:
            return None
        try:
            req = Request(f"http://127.0.0.1:{info['port']}/health")
            urlopen(req, timeout=2)
            return info
        except (URLError, OSError):
            return None

    def _start_server(self) -> None:
        workdir = cfg.get_workdir()
        workdir.mkdir(parents=True, exist_ok=True)
        log_path = workdir / "server.log"
        with open(log_path, "a") as log_f:
            subprocess.Popen(
                [sys.executable, "-m", "ilan.server"],
                stdin=subprocess.DEVNULL,
                stdout=log_f,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )

    def _wait_for_server(self, timeout: float = 10) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            info = self._probe()
            if info:
                return info
            time.sleep(0.15)
        raise RuntimeError("Timed out waiting for ilan server to start")

    # ── HTTP helpers ─────────────────────────────────────────────

    def _url(self, path: str) -> str:
        if self._base_url is None:
            info = self.ensure_server()
            self._base_url = f"http://127.0.0.1:{info['port']}"
        return f"{self._base_url}{path}"

    def _request(self, method: str, path: str, body: dict | None = None, *, timeout: float = 120) -> dict:
        url = self._url(path)
        data = json.dumps(body).encode() if body else None
        req = Request(url, data=data, method=method)
        if data:
            req.add_header("Content-Type", "application/json")
        try:
            with urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read())
        except HTTPError as exc:
            return json.loads(exc.read())
        except URLError as exc:
            raise ConnectionError(f"Cannot reach ilan server: {exc}") from exc

    def get(self, path: str, *, timeout: float = 120) -> dict:
        return self._request("GET", path, timeout=timeout)

    def post(self, path: str, body: dict | None = None, *, timeout: float = 120) -> dict:
        return self._request("POST", path, body, timeout=timeout)

    def delete(self, path: str) -> dict:
        return self._request("DELETE", path)

    # ── high-level API ───────────────────────────────────────────

    def health(self) -> dict:                    return self.get("/health")
    def version(self) -> dict:                   return self.get("/version")
    def get_config(self) -> dict:                return self.get("/config")
    def set_config(self, key: str, value) -> dict:
        return self.post("/config/set", {"key": key, "value": value})

    def list_tasks(self, show_all: bool = False) -> dict:
        path = "/tasks?all=true" if show_all else "/tasks"
        return self.get(path)

    def add_task(self, name: str, prompt: str) -> dict:
        return self.post("/tasks", {"name": name, "prompt": prompt})

    def get_task(self, name: str) -> dict:       return self.get(f"/tasks/{name}")
    def delete_task(self, name: str) -> dict:    return self.delete(f"/tasks/{name}")
    def mark_done(self, name: str) -> dict:      return self.post(f"/tasks/{name}/done")
    def mark_discard(self, name: str) -> dict:   return self.post(f"/tasks/{name}/discard")
    def undone(self, name: str) -> dict:         return self.post(f"/tasks/{name}/undone")
    def undiscard(self, name: str) -> dict:      return self.post(f"/tasks/{name}/undiscard")
    def mark_unread(self, name: str) -> dict:    return self.post(f"/tasks/{name}/unread")
    def kill_task(self, name: str) -> dict:      return self.post(f"/tasks/{name}/kill")
    def get_logs(self, name: str) -> dict:       return self.get(f"/tasks/{name}/logs")
    def get_log_path(self, name: str) -> dict:   return self.get(f"/tasks/{name}/log-path")
    def get_tail(self, name: str) -> dict:       return self.get(f"/tasks/{name}/tail")
    def get_path(self, name: str) -> dict:       return self.get(f"/tasks/{name}/path")

    def rename_task(self, old_name: str, new_name: str) -> dict:
        return self.post(f"/tasks/{old_name}/rename", {"new_name": new_name})

    def branch_task(self, old_name: str, new_name: str, message: str | None = None) -> dict:
        body: dict = {"new_name": new_name}
        if message is not None:
            body["message"] = message
        return self.post(f"/tasks/{old_name}/branch", body)

    def reply(self, name: str, message: str) -> dict:
        return self.post(f"/tasks/{name}/reply", {"message": message})

    def sleep_task(self, name: str, seconds: int) -> dict:
        return self.post(f"/tasks/{name}/sleep", {"seconds": seconds})

    def summarize_task(self, name: str) -> dict:
        # Summarization runs claude -p on the server, which can take
        # well over a minute on long logs. Give it a generous ceiling.
        return self.post(f"/tasks/{name}/summarize", timeout=1200)

    def clear_everything(self) -> dict:          return self.post("/clear-everything")
    def stop_server(self) -> dict:               return self.post("/stop")
