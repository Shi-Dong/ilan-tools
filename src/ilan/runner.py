from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import time
from pathlib import Path

from . import config as cfg
from .models import Task, TaskStatus
from .store import Store

CLAUDE_BASE_FLAGS = [
    "--dangerously-skip-permissions",
    "--model", "opus",
    "--effort", "high",
    "--output-format", "json",
]

STATUS_SUFFIX = """

---
IMPORTANT — when you finish responding, you MUST output exactly one of these
markers on the very last line of your response (no extra text after it):

[STATUS: DONE] — you believe the task is complete.
[STATUS: NEEDS_ATTENTION] — you are blocked and need the user's input to proceed.
"""


class Runner:
    """Spawns / kills / reaps ``claude -p`` processes and schedules work."""

    def __init__(self, store: Store) -> None:
        self.store = store

    # ── public API ───────────────────────────────────────────────────

    def recover(self) -> list[str]:
        """Reconcile WORKING tasks against actual process state.

        Called once at server startup.  For every task marked WORKING whose
        agent process is no longer alive, read the output file the agent
        wrote to the workdir and determine the real status.  Returns the
        names of recovered tasks.
        """
        recovered: list[str] = []
        for task in self.store.load_tasks().values():
            if task.status != TaskStatus.WORKING:
                continue
            if task.pid is not None and self._pid_alive(task.pid):
                continue
            # Agent is gone — check the output file it left behind
            self._try_reap(task)
            recovered.append(task.name)
        return recovered

    def schedule(self) -> None:
        """Reap finished agents, then fill empty slots with unclaimed tasks."""
        self._reap_all()

        max_agents = int(cfg.load().get("num-agents", 5))
        tasks = self.store.load_tasks()
        running = sum(1 for t in tasks.values() if t.status == TaskStatus.WORKING)

        for task in sorted(tasks.values(), key=lambda t: t.created_at):
            if running >= max_agents:
                break
            if task.status != TaskStatus.UNCLAIMED:
                continue
            prompt, resume = self._build_prompt(task)
            self._spawn(task, prompt, resume=resume)
            running += 1

    def reply_to_working(self, task: Task, message: str) -> None:
        """Kill the running agent and immediately resume the session."""
        self.kill(task)
        time.sleep(0.5)
        self._try_reap(task)

        self.store.append_log(task.name, "user", message)

        if task.session_id:
            self._spawn(task, message, resume=True)
        else:
            task.cached_replies.append(message)
            task.status = TaskStatus.UNCLAIMED
            self.store.put_task(task)

    def kill(self, task: Task) -> None:
        if task.pid and self._pid_alive(task.pid):
            try:
                os.kill(task.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        task.pid = None

    # ── internals ────────────────────────────────────────────────────

    def _spawn(self, task: Task, prompt: str, *, resume: bool) -> bool:
        """Spawn a claude process. Returns True on success."""
        cmd = ["claude", "-p", prompt + STATUS_SUFFIX, *CLAUDE_BASE_FLAGS]
        if resume and task.session_id:
            cmd.extend(["--resume", task.session_id])

        out_path = self.store.output_path(task.name)
        try:
            with open(out_path, "w") as out_f:
                proc = subprocess.Popen(
                    cmd,
                    stdout=out_f,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
        except FileNotFoundError:
            task.status = TaskStatus.ERROR
            self.store.put_task(task)
            return False

        task.pid = proc.pid
        task.status = TaskStatus.WORKING
        self.store.put_task(task)

        if not resume:
            self.store.append_log(task.name, "user", task.prompt)
        return True

    def _build_prompt(self, task: Task) -> tuple[str, bool]:
        """Return (prompt_text, is_resume) for a task about to be scheduled."""
        if task.cached_replies:
            replies = "\n\n".join(task.cached_replies)
            task.cached_replies = []
            if task.session_id:
                return replies, True
            return task.prompt + "\n\n" + replies, False

        if task.session_id:
            return "Please continue working on this task.", True
        return task.prompt, False

    def _reap_all(self) -> None:
        for task in self.store.load_tasks().values():
            if task.status == TaskStatus.WORKING and task.pid is not None:
                if not self._pid_alive(task.pid):
                    self._try_reap(task)

    def _try_reap(self, task: Task) -> None:
        """Parse claude output and update task status after process exits."""
        task.pid = None
        out_path = self.store.output_path(task.name)

        try:
            with open(out_path) as f:
                result = json.load(f)
        except (json.JSONDecodeError, FileNotFoundError):
            task.status = TaskStatus.ERROR
            self.store.put_task(task)
            return

        sid = result.get("session_id")
        if sid:
            task.session_id = sid

        response = result.get("result", "")
        if response:
            self.store.append_log(task.name, "assistant", response)

        if result.get("is_error"):
            task.status = TaskStatus.ERROR
        else:
            task.status = self._parse_status_marker(response)
        self.store.put_task(task)

    @staticmethod
    def _parse_status_marker(response: str) -> TaskStatus:
        """Extract ``[STATUS: …]`` from the last lines of the response."""
        if not response:
            return TaskStatus.AGENT_FINISHED
        match = re.search(r"\[STATUS:\s*NEEDS_ATTENTION\]", response)
        if match:
            return TaskStatus.NEEDS_ATTENTION
        return TaskStatus.AGENT_FINISHED

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
