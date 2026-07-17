from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import time
from pathlib import Path

from ilan import config as cfg
from ilan.backends import Backend, ClaudeBackend, CodexBackend
from ilan.backends.claude import last_assistant_model
from ilan.models import DEFAULT_ENGINE, ENGINE_CLAUDE, ENGINE_CODEX, Task, TaskStatus
from ilan.oneliner import generate_one_liner
from ilan.store import Store

# Re-exported for backwards compatibility: server.py imports it from here.
__all__ = ["Runner", "last_assistant_model", "STATUS_SUFFIX"]

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


def _render_catchup(entries: list, *, fresh: bool) -> str:
    """Render unified-log entries as a catch-up preamble for a switched engine.

    ``fresh`` distinguishes seeding a brand-new session with the whole
    transcript from catching a resumed session up on the turns it missed.
    """
    if fresh:
        header = (
            "You are taking over this task from another agent. Below is the "
            "full conversation so far. Read it carefully, then continue the work."
        )
    else:
        header = (
            "While you were away, another agent advanced this task. Below are "
            "the conversation turns you missed. Catch up on them, then continue "
            "the work."
        )
    parts = [header, "", "--- BEGIN CONVERSATION HISTORY ---"]
    for entry in entries:
        speaker = "User" if entry.role == "user" else "Assistant"
        parts.append(f"\n[{speaker}]\n{entry.content}")
    parts.append("\n--- END CONVERSATION HISTORY ---")
    parts.append("\nPlease continue working on this task.")
    return "\n".join(parts)


class Runner:
    """Spawns / kills / reaps agent processes and schedules work.

    The backend (Claude Code, Codex, …) owns every CLI-specific detail; this
    class stays agnostic and drives it through the ``Backend`` interface.
    Each task names its backend via ``task.engine``; ``_backend_for`` maps that
    to the concrete adapter so a single Runner can drive both engines at once.
    """

    def __init__(self, store: Store, backends: dict[str, Backend] | None = None) -> None:
        self.store = store
        self._backends: dict[str, Backend] = backends or {
            ENGINE_CLAUDE: ClaudeBackend(),
            ENGINE_CODEX: CodexBackend(),
        }
        self._procs: dict[str, subprocess.Popen] = {}

    def _backend_for(self, engine: str) -> Backend:
        return self._backends.get(engine, self._backends[DEFAULT_ENGINE])

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

    def switch_engine(self, task: Task, target_engine: str) -> None:
        """Lazily switch a task's backend to *target_engine*.

        This does not restart the agent — it only rewires which backend the
        task will use on its next schedule. The outgoing engine's active
        session is preserved in the per-engine map, and the incoming engine's
        own native session (if any) is made active so switching back and forth
        never discards a backend's conversation. When the incoming engine is
        behind the unified log, ``awaiting_catchup`` is set so the next spawn
        injects the turns it missed (Option A: native resume + catch-up, or a
        fresh session seeded with the transcript when it has never run).

        The caller is responsible for ensuring the task is not mid-flight; a
        WORKING task should be reaped or killed before switching so its output
        is parsed by the engine that produced it.
        """
        if target_engine == task.engine:
            return
        if task.session_id:
            task.set_session_for(task.engine, task.session_id)
        task.engine = target_engine
        task.session_id = task.sessions.get(target_engine)
        task.session_log_path = None
        seen = task.log_cursors.get(target_engine, 0)
        task.awaiting_catchup = len(self.store.read_logs(task.name)) > seen
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
        """Spawn an agent process. Returns True on success."""
        tmux_instr = _tmux_instruction(task.task_hash, task.name) if task.task_hash else ""
        full_prompt = prompt + tmux_instr + STATUS_SUFFIX
        cmd, env = self._backend_for(task.engine).build_command(
            full_prompt, task.model, resume=resume, session_id=task.session_id
        )

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

        # Log the opening prompt only when the conversation is brand new. A
        # fresh-session backend switch also spawns with resume=False but its
        # history is already in the log, so re-appending would duplicate it.
        if not resume and not self.store.read_logs(task.name):
            self.store.append_log(task.name, "user", task.prompt)
        return True

    def _build_prompt(self, task: Task) -> tuple[str, bool]:
        """Return (prompt_text, is_resume) for a task about to be scheduled."""
        if task.session_id and not self._find_session_log(task.session_id, task.engine):
            task.session_id = None
            task.session_log_path = None

        if task.awaiting_catchup:
            return self._build_catchup_prompt(task)

        if task.cached_replies:
            replies = "\n\n".join(task.cached_replies)
            task.cached_replies = []
            if task.session_id:
                return replies, True
            return task.prompt + "\n\n" + replies, False

        if task.session_id:
            return "Please continue working on this task.", True
        return task.prompt, False

    def _build_catchup_prompt(self, task: Task) -> tuple[str, bool]:
        """Build the post-switch prompt that catches the active engine up.

        The unified log already holds every turn, including any brand-new
        reply that triggered this schedule, so the interim slice fully conveys
        what the engine missed; pending ``cached_replies`` are cleared because
        they are part of that slice. Resumes the engine's native session when
        it has one, otherwise starts a fresh session seeded with the
        transcript. Marks the engine caught up so the branch fires only once.
        """
        task.awaiting_catchup = False
        entries = self.store.read_logs(task.name)
        seen = task.log_cursors.get(task.engine, 0)
        interim = entries[seen:]
        has_session = bool(task.session_id)
        task.cached_replies = []
        task.log_cursors[task.engine] = len(entries)

        if not interim:
            if has_session:
                return "Please continue working on this task.", True
            return task.prompt, False

        return _render_catchup(interim, fresh=not has_session), has_session

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
        """Parse agent output and update task status after process exits."""
        task.pid = None
        out_path = self.store.output_path(task.name)

        backend = self._backend_for(task.engine)
        result = backend.parse_output(out_path)
        if result is None:
            task.set_status(TaskStatus.ERROR)
            self.store.put_task(task)
            return

        sid = result.session_id
        if sid:
            log_path = self._find_session_log(sid, task.engine)
            if log_path:
                task.session_id = sid
                task.session_log_path = str(log_path)
                # Keep the per-engine session map current so a later backend
                # switch can resume this engine's native session.
                task.set_session_for(task.engine, sid)
                # Cache the model that produced this turn's assistant message
                # so ``ilan tail`` can show it without rescanning the session
                # log on every request.
                model = backend.last_assistant_model(log_path)
                if model:
                    task.last_assistant_model = model

        task.input_tokens += result.input_tokens
        task.output_tokens += result.output_tokens
        task.cache_read_input_tokens += result.cache_read_input_tokens
        task.cost_usd += result.cost_usd

        response = result.result_text
        if response:
            self.store.append_log(task.name, "assistant", response)

        # This engine's native session now reflects every unified-log entry
        # through its own just-appended turn, so advance its cursor. A future
        # switch to another engine compares against this to know what to
        # catch that engine up on.
        task.log_cursors[task.engine] = len(self.store.read_logs(task.name))

        if result.is_error:
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

    def _find_session_log(self, session_id: str, engine: str = DEFAULT_ENGINE) -> Path | None:
        """Locate the session log for the given session ID under *engine*'s backend."""
        return self._backend_for(engine).find_session_log(session_id)

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
