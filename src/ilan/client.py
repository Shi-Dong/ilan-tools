"""Thin HTTP client that talks to the ilan background server.

Auto-starts the server on the first call if it isn't already running.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from . import config as cfg
from .server import pid_file_path, read_server_info


class Client:
    """JSON-over-HTTP client for the ilan server."""

    def __init__(self, port: int | None = None) -> None:
        self._base_url: str | None = f"http://127.0.0.1:{port}" if port else None

    # ── server lifecycle ─────────────────────────────────────────

    def ensure_server(self) -> dict:
        """Return server info, starting the server if necessary."""
        info = self._probe()
        if info:
            return info

        self._start_server()
        return self._wait_for_server()

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

    def _request(self, method: str, path: str, body: dict | None = None) -> dict:
        url = self._url(path)
        data = json.dumps(body).encode() if body else None
        req = Request(url, data=data, method=method)
        if data:
            req.add_header("Content-Type", "application/json")
        try:
            with urlopen(req, timeout=120) as resp:
                return json.loads(resp.read())
        except HTTPError as exc:
            return json.loads(exc.read())
        except URLError as exc:
            raise ConnectionError(f"Cannot reach ilan server: {exc}") from exc

    def get(self, path: str) -> dict:
        return self._request("GET", path)

    def post(self, path: str, body: dict | None = None) -> dict:
        return self._request("POST", path, body)

    def delete(self, path: str) -> dict:
        return self._request("DELETE", path)

    # ── high-level API ───────────────────────────────────────────

    def health(self) -> dict:                    return self.get("/health")
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
    def kill_task(self, name: str) -> dict:      return self.post(f"/tasks/{name}/kill")
    def get_logs(self, name: str) -> dict:       return self.get(f"/tasks/{name}/logs")
    def get_tail(self, name: str) -> dict:       return self.get(f"/tasks/{name}/tail")

    def reply(self, name: str, message: str) -> dict:
        return self.post(f"/tasks/{name}/reply", {"message": message})

    def clear_everything(self) -> dict:          return self.post("/clear-everything")
    def stop_server(self) -> dict:               return self.post("/stop")
