from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import time
from pathlib import Path

from ilan import config as cfg
from ilan.models import GLM_BASE_URL, Task, TaskStatus, is_glm_model, resolve_model
from ilan.oneliner import generate_one_liner
from ilan.store import Store

_CLAUDE_STATIC_FLAGS = [
    "--dangerously-skip-permissions",
    "--output-format", "json",
]


def _effective_model(model_override: str | None = None) -> str:
    """Resolve the model string passed to ``claude --model`` for a spawn.

    *model_override* (a task's ``model``, set via ``ilan max``) takes
    precedence over the configured default; ``None`` falls back to config.
    Friendly aliases (e.g. ``glm``) are resolved to their real model id.
    """
    conf = cfg.load()
    return resolve_model(model_override or str(conf.get("model", "opus")))


def _claude_flags(model_override: str | None = None) -> list[str]:
    """Build claude flags, reading model/effort from config at call time."""
    conf = cfg.load()
    return [
        *_CLAUDE_STATIC_FLAGS,
        "--model", _effective_model(model_override),
        "--effort", str(conf.get("effort", "high")),
    ]


def _model_env(model_override: str | None = None) -> dict[str, str]:
    """Env overrides for the spawned agent based on the effective model.

    GLM models route through Z.ai's Anthropic-compatible endpoint, which
    needs ``ANTHROPIC_BASE_URL`` plus a bearer token in ``ANTHROPIC_AUTH_TOKEN``
    (taken from the ``glm-api-key`` config). Non-GLM models get nothing here.
    """
    if not is_glm_model(_effective_model(model_override)):
        return {}
    env = {"ANTHROPIC_BASE_URL": GLM_BASE_URL}
    glm_key = str(cfg.load().get("glm-api-key", "")).strip()
    if glm_key:
        env["ANTHROPIC_AUTH_TOKEN"] = glm_key
    return env

STATUS_SUFFIX = """

---
IMPORTANT — before ending your response you MUST:

1. Provide a clear answer to the user's question or a summary of what you did.
2. On the very last line, output exactly one of these markers (no extra text after it):

[STATUS: DONE] — you believe the task is complete.
[STATUS: NEEDS_ATTENTION] — you are blocked and need the user's input to proceed.

Never emit a status marker without first giving a substantive response.
"""


def _tmux_instruction(task_hash: str, task_name: str) -> str:
    """Build the tmux session instruction injected into agent prompts."""
    session_prefix = task_hash
    default_session = f"{task_hash}-claude-{task_name}"
    return (
        f"\n\n---\n"
        f"TMUX SESSION REQUIREMENT: You MUST do all your terminal work inside tmux "
        f"sessions whose names start with `{session_prefix}`. Your default session "
        f"should be `{default_session}` — create it if it does not already exist "
        f"(`tmux new-session -d -s {default_session}` then send commands to it). "
        f"You may create additional tmux sessions for this task (e.g. for parallel "
        f"work), but every session name MUST be prefixed with `{session_prefix}`.\n"
    )


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
        # The reap above parses the interrupted turn as if the agent had
        # voluntarily finished, which flips ``needs_review`` to True. But the
        # user just typed a reply — they don't need to be re-notified about
        # output they're already looking at — so clear the flag here.
        task.needs_review = False
        self.store.put_task(task)

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
        tmux_instr = _tmux_instruction(task.task_hash, task.name) if task.task_hash else ""
        cmd = ["claude", "-p", prompt + tmux_instr + STATUS_SUFFIX, *_claude_flags(task.model)]
        if resume and task.session_id:
            cmd.extend(["--resume", task.session_id])

        env = os.environ.copy()
        glm_env = _model_env(task.model)
        if glm_env:
            # GLM auth is a bearer token, not an x-api-key; drop any inherited
            # Anthropic key so it can't shadow the Z.ai credentials.
            env.pop("ANTHROPIC_API_KEY", None)
            env.update(glm_env)
        else:
            api_key = str(cfg.load().get("api-key", "")).strip()
            if api_key:
                env["ANTHROPIC_API_KEY"] = api_key

        out_path = self.store.output_path(task.name)
        workdir = cfg.get_workdir()
        workdir.mkdir(parents=True, exist_ok=True)
        try:
            with open(out_path, "w") as out_f:
                proc = subprocess.Popen(
                    cmd,
                    cwd=workdir,
                    env=env,
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

        usage = result.get("usage") or {}
        task.input_tokens += usage.get("input_tokens", 0)
        task.output_tokens += usage.get("output_tokens", 0)
        task.cache_read_input_tokens += usage.get("cache_read_input_tokens", 0)
        task.cost_usd += result.get("total_cost_usd", 0.0)

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
                task.summary_one_liner = self._generate_one_liner(task, response)
        self.store.put_task(task)

    def _generate_one_liner(self, task: Task, assistant_response: str) -> str | None:
        """Best-effort one-line summary of the WORKING→finished transition.

        Reads the last user message from the task's log and feeds it +
        the assistant's new reply to Haiku. Returns ``None`` if there is
        no API key or the request fails — the field stays unset and the
        display falls back to status-only.
        """
        last_user = ""
        for entry in reversed(self.store.read_logs(task.name)):
            if entry.role == "user":
                last_user = entry.content
                break
        return generate_one_liner(last_user, assistant_response)

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
