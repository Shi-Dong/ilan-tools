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

_CLAUDE_STATIC_FLAGS = [
    "--dangerously-skip-permissions",
    "--output-format", "json",
]


def _claude_flags() -> list[str]:
    """Build claude flags, reading model/effort from config at call time."""
    conf = cfg.load()
    return [
        *_CLAUDE_STATIC_FLAGS,
        "--model", str(conf.get("model", "opus")),
        "--effort", str(conf.get("effort", "high")),
    ]

STATUS_SUFFIX = """

---
IMPORTANT — before ending your response you MUST:

1. Provide a clear answer to the user's question or a summary of what you did.
2. On the very last line, output exactly one of these markers (no extra text after it):

[STATUS: DONE] — you believe the task is complete.
[STATUS: NEEDS_ATTENTION] — you are blocked and need the user's input to proceed.

Never emit a status marker without first giving a substantive response.
"""


class Runner:
    """Spawns / kills / reaps ``claude -p`` processes and schedules work."""

    def __init__(self, store: Store) -> None:
        self.store = store
        self._procs: dict[str, subprocess.Popen] = {}

    # ── public API ───────────────────────────────────────────────────

    def recover(self) -> list[str]:
        """Reconcile WORKING tasks against actual process state.

        Called once at server startup.  We have no Popen objects from the
        previous server, so we rely on two signals:

        1. ``_pid_alive`` — is the PID still a running process?
        2. ``_output_complete`` — did the agent write a full JSON result to
           its output file?  This catches zombies whose PID entry lingers
           after a server restart.
        """
        recovered: list[str] = []
        for task in self.store.load_tasks().values():
            if task.status != TaskStatus.WORKING:
                continue
            if task.pid is not None and self._pid_alive(task.pid):
                if not self._output_complete(task.name):
                    continue  # genuinely still running
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
            task.set_status(TaskStatus.UNCLAIMED)
            self.store.put_task(task)

    def kill(self, task: Task) -> None:
        if task.pid and self._pid_alive(task.pid):
            try:
                os.kill(task.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        proc = self._procs.pop(task.name, None)
        if proc is not None:
            proc.wait(timeout=5)
        task.pid = None

    # ── internals ────────────────────────────────────────────────────

    def _spawn(self, task: Task, prompt: str, *, resume: bool) -> bool:
        """Spawn a claude process. Returns True on success."""
        cmd = ["claude", "-p", prompt + STATUS_SUFFIX, *_claude_flags()]
        if resume and task.session_id:
            cmd.extend(["--resume", task.session_id])

        out_path = self.store.output_path(task.name)
        workdir = cfg.get_workdir()
        workdir.mkdir(parents=True, exist_ok=True)
        try:
            with open(out_path, "w") as out_f:
                proc = subprocess.Popen(
                    cmd,
                    cwd=workdir,
                    stdout=out_f,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
        except FileNotFoundError:
            task.set_status(TaskStatus.ERROR)
            self.store.put_task(task)
            return False

        self._procs[task.name] = proc
        task.pid = proc.pid
        task.set_status(TaskStatus.WORKING)
        self.store.put_task(task)

        if not resume:
            self.store.append_log(task.name, "user", task.prompt)
        return True

    def _build_prompt(self, task: Task) -> tuple[str, bool]:
        """Return (prompt_text, is_resume) for a task about to be scheduled."""
        if task.session_id and not self._find_session_log(task.session_id):
            task.session_id = None
            task.session_log_path = None

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
            if task.status != TaskStatus.WORKING or task.pid is None:
                continue
            proc = self._procs.get(task.name)
            if proc is not None:
                if proc.poll() is not None:
                    self._procs.pop(task.name, None)
                    self._try_reap(task)
            elif not self._pid_alive(task.pid) or self._output_complete(task.name):
                self._try_reap(task)

    def _try_reap(self, task: Task) -> None:
        """Parse claude output and update task status after process exits."""
        task.pid = None
        out_path = self.store.output_path(task.name)

        try:
            with open(out_path) as f:
                result = json.load(f)
        except (json.JSONDecodeError, FileNotFoundError):
            task.set_status(TaskStatus.ERROR)
            self.store.put_task(task)
            return

        sid = result.get("session_id")
        if sid:
            log_path = self._find_session_log(sid)
            if log_path:
                task.session_id = sid
                task.session_log_path = str(log_path)

        response = result.get("result", "")
        if response:
            self.store.append_log(task.name, "assistant", response)

        if result.get("is_error"):
            task.set_status(TaskStatus.ERROR)
        else:
            new_status = self._parse_status_marker(response)
            task.set_status(new_status)
            if new_status in (TaskStatus.NEEDS_ATTENTION, TaskStatus.AGENT_FINISHED):
                task.needs_review = True
        self.store.put_task(task)

    def _output_complete(self, task_name: str) -> bool:
        """Return True if the output file contains a valid JSON result."""
        out_path = self.store.output_path(task_name)
        if not out_path.exists() or out_path.stat().st_size == 0:
            return False
        try:
            with open(out_path) as f:
                json.load(f)
            return True
        except (json.JSONDecodeError, OSError):
            return False

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
    def _find_session_log(session_id: str) -> Path | None:
        """Locate the Claude Code session log for the given session ID."""
        claude_dir = Path.home() / ".claude" / "projects"
        if not claude_dir.is_dir():
            return None
        matches = list(claude_dir.glob(f"*/{session_id}.jsonl"))
        return matches[0] if matches else None

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        # Try to reap a zombie first.  waitpid with WNOHANG returns (pid, status)
        # if the child has exited (clearing the zombie), or (0, 0) if still running.
        # It raises ChildProcessError if pid is not our child.
        try:
            wpid, _ = os.waitpid(pid, os.WNOHANG)
            if wpid != 0:
                return False  # was a zombie, now reaped
        except ChildProcessError:
            pass  # not our child — fall through to kill-based check

        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
