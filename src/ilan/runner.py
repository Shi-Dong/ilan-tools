from __future__ import annotations

import contextlib
import json
import os
import re
import signal
import subprocess
import time
from pathlib import Path

from ilan import budget
from ilan import config as cfg
from ilan.backends import Backend, ClaudeBackend, CodexBackend
from ilan.models import DEFAULT_ENGINE, ENGINE_CLAUDE, ENGINE_CODEX, Task, TaskStatus
from ilan.oneliner import generate_one_liner
from ilan.store import Store

__all__ = ["Runner", "STATUS_SUFFIX"]

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


def _branch_notice(parent_name: str | None, *, assignment_below: bool) -> str:
    """Build the preamble that separates a branched task from its parent.

    A branched task inherits the parent's conversation verbatim — a forked
    session log for Claude, a rendered transcript for Codex — so without this
    it cannot tell background context from its own assignment and resumes
    whatever the parent had in flight. The identifier paragraph matters just as
    much: every inherited turn ends in the parent's ``TMUX SESSION
    REQUIREMENT`` block, so the child reads the parent's hash many times over
    and its own exactly once, at the end of the current prompt.

    ``assignment_below`` says where the real instruction sits: appended right
    after this notice (Claude, which resumes natively and is sent only the new
    turn) or as the last entry of the rendered history (Codex, whose catch-up
    prompt carries the whole log).
    """
    parent = f"`{parent_name}`" if parent_name else "another task"
    where = (
        "the instruction that follows this notice"
        if assignment_below
        else "the final user message in the history above"
    )
    return (
        "---\n"
        f"NEW SEPARATE TASK — BRANCHED FROM {parent}\n\n"
        f"You have inherited the conversation of {parent}. That conversation is "
        "REFERENCE CONTEXT ONLY: read it to understand what was learned and "
        "decided, and reuse anything from it that helps. It is not your "
        "assignment.\n\n"
        "You are a different task now. Do not resume, continue, or finish "
        f"{parent}'s work. Its open steps, pending TODOs, agreed plans, and "
        "unanswered questions stay with it, and any long-running process or "
        "monitoring loop it started remains its responsibility — do not adopt, "
        "babysit, or tear those down unless you are explicitly asked to.\n\n"
        f"Discard {parent}'s identifiers too. Any task hash or tmux session "
        f"prefix appearing in the inherited conversation belongs to {parent}, "
        "not to you. Your own hash is the one in the TMUX SESSION REQUIREMENT "
        "at the end of this prompt; use only that one, and never send commands "
        "to a tmux session named with the old prefix.\n\n"
        f"Your assignment is {where}, and nothing else.\n"
        "---\n"
    )


def _branch_divider(parent_name: str | None) -> str:
    """Mark where a branched task's inherited history ends in a rendered
    transcript.

    The one-shot branch notice covers only the child's first prompt; every
    later replay of the unified log (a backend switch, a lost-session reseed)
    would otherwise present the parent's turns as this task's own history,
    with nothing marking the boundary.

    The wording is deliberately neutral — "the turns below define this task's
    work" — rather than repeating the notice's imperatives: a branched task's
    own turns start with its branch assignment, so pointing below the line is
    enough, and the full "do not resume the parent's work" instruction already
    ran in the notice on the first prompt.
    """
    parent = f"`{parent_name}`" if parent_name else "the parent task"
    return (
        "\n--- BRANCH POINT: inherited history ends here ---\n"
        f"Every turn above this line was inherited from {parent} when this "
        f"task was branched off it; treat it as reference context from "
        f"{parent}. Every turn below this line is this task's own "
        "conversation, and it is the turns below that define this task's "
        "work."
    )


# Budget for the rendered catch-up history. Codex rejects any input over
# 1,048,576 characters (`input_too_large`), and a months-long unified log can
# blow well past that; keep the newest turns and drop the oldest, staying far
# enough under the limit to leave room for the agent's own context.
_CATCHUP_MAX_CHARS = 500_000


def _render_catchup(
    entries: list,
    *,
    fresh: bool,
    branch_notice: str | None = None,
    inherited_count: int = 0,
    parent_name: str | None = None,
) -> str:
    """Render unified-log entries as a catch-up preamble for a switched engine.

    ``fresh`` distinguishes seeding a brand-new session with the whole
    transcript from catching a resumed session up on the turns it missed.
    When the history exceeds ``_CATCHUP_MAX_CHARS`` the oldest turns are
    dropped (the newest ones matter most for continuing the work); the full
    history remains available in the task's unified log and Gist mirror.

    ``branch_notice`` marks the transcript as a *branched* task's inherited
    context: the framing flips from "continue this work" to "this is
    background", and the notice replaces the trailing continue instruction so
    the separation is the last thing read before the assignment.

    ``inherited_count`` says how many of *entries* were inherited from the
    parent at the branch point; when the rendered window contains turns on
    both sides of that point a divider from :func:`_branch_divider` is placed
    between them, so a replay keeps the two boundaries distinct: the
    header/footer carry the *switch* semantics (continue this task) while the
    divider carries the *branch* semantics (the prefix above it belongs to
    ``parent_name``).
    """
    if branch_notice:
        header = (
            "You are a new task branched off another agent's session. Below is "
            "that agent's conversation, inherited as background context."
        )
    elif fresh:
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
    segments = []
    for entry in entries:
        speaker = "User" if entry.role == "user" else "Assistant"
        segments.append(f"\n[{speaker}]\n{entry.content}")

    kept: list[str] = []
    total = 0
    for segment in reversed(segments):
        if kept and total + len(segment) > _CATCHUP_MAX_CHARS:
            break
        kept.append(segment)
        total += len(segment)
    kept.reverse()
    omitted = len(segments) - len(kept)

    # Truncation drops oldest-first, so the divider position simply shifts
    # left with the drop count; at zero or below the whole inherited prefix
    # is gone and there is nothing left to separate. The upper bound is
    # defensive — every branch logs its assignment at branch time, so no
    # rendered slice should be purely inherited — but a divider claiming
    # "the turns below define the work" must never sit above zero turns.
    boundary = inherited_count - omitted
    if 0 < boundary < len(kept):
        kept.insert(boundary, _branch_divider(parent_name))

    parts = [header, "", "--- BEGIN CONVERSATION HISTORY ---"]
    if omitted:
        parts.append(
            f"\n[... {omitted} earlier turn(s) omitted to fit the prompt size "
            f"limit ...]"
        )
    parts.extend(kept)
    parts.append("\n--- END CONVERSATION HISTORY ---")
    parts.append("\n" + (branch_notice or "Please continue working on this task."))
    return "\n".join(parts)


class Runner:
    """Spawns / kills / reaps agent processes.

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
            if (
                task.pid is not None
                and self._pid_alive(task.pid)
                and not self._output_complete(task.name)
            ):
                continue  # genuinely still running
            self._try_reap(task)
            recovered.append(task.name)
        return recovered

    def start(self, task: Task) -> bool:
        """Build the task's next prompt and spawn its agent immediately.

        Returns True on success. The caller must persist any state the
        prompt depends on (e.g. ``cached_replies``) before calling, so a
        failed spawn can retry from the stored copy without losing it.
        """
        prompt, resume = self._build_prompt(task)
        return self._spawn(task, prompt, resume=resume)

    def reap_finished(self) -> None:
        """Reap agents whose process has exited. Called by the poll loop."""
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
            self.store.put_task(task)
            self.start(task)

    def switch_engine(self, task: Task, target_engine: str) -> None:
        """Lazily switch a task's backend to *target_engine*.

        This does not restart the agent — it only rewires which backend the
        task will use on its next spawn. The outgoing engine's active
        session is preserved in the per-engine map, and the incoming engine's
        own native session (if any) is made active so switching back and forth
        never discards a backend's conversation. When the incoming engine is
        behind the unified log, ``awaiting_catchup`` is set so the next spawn
        injects the turns it missed (Option A: native resume + catch-up, or a
        fresh session seeded with the transcript when it has never run).

        The caller is responsible for ensuring the task is not mid-flight:
        the server rejects switching a WORKING task, so its in-flight output
        is always parsed by the engine that produced it.
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
            # EPERM: the stored pid now belongs to another user's
            # process — e.g. it was spawned by a server that ran under
            # a different account, or the OS recycled the pid after the
            # agent died. It isn't ours to signal; forget it instead of
            # crashing the request that triggered the kill.
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(task.pid, signal.SIGTERM)
        proc = self._procs.pop(task.name, None)
        if proc is not None:
            proc.wait(timeout=5)
        task.pid = None

    # ── internals ────────────────────────────────────────────────────

    def _spawn(self, task: Task, prompt: str, *, resume: bool) -> bool:
        """Spawn an agent process. Returns True on success.

        The prompt is written to a file and fed to the agent over stdin, not
        argv: a long catch-up transcript can exceed the OS ARG_MAX (E2BIG at
        exec would kill the calling thread) and ~1 MB argv values crash
        codex-cli outright with SIGSEGV before it writes any output. stderr
        is captured to a per-task file so CLI startup failures — which leave
        an empty stdout and would otherwise vanish — stay diagnosable.
        """
        tmux_instr = _tmux_instruction(task.task_hash, task.name) if task.task_hash else ""
        full_prompt = prompt + tmux_instr + STATUS_SUFFIX
        cmd, env = self._backend_for(task.engine).build_command(
            task.model, resume=resume, session_id=task.session_id
        )

        out_path = self.store.output_path(task.name)
        prompt_path = self.store.prompt_path(task.name)
        err_path = self.store.stderr_path(task.name)
        workdir = cfg.get_workdir()
        workdir.mkdir(parents=True, exist_ok=True)
        try:
            prompt_path.write_text(full_prompt)
            with (
                open(prompt_path) as in_f,
                open(out_path, "w") as out_f,
                open(err_path, "w") as err_f,
            ):
                proc = subprocess.Popen(
                    cmd,
                    cwd=workdir,
                    env=env,
                    stdin=in_f,
                    stdout=out_f,
                    stderr=err_f,
                    start_new_session=True,
                )
        except OSError:
            # Persist ERROR onto a pristine copy of the task: the in-memory
            # object may hold consumed prompt state (cleared cached_replies,
            # dropped session_id) that must not be saved for a spawn that
            # never happened, or a later retry would lose those messages.
            stored = self.store.get_task(task.name) or task
            stored.set_status(TaskStatus.ERROR)
            self.store.put_task(stored)
            return False

        self._procs[task.name] = proc
        task.pid = proc.pid
        # Neither backend's session log records the reasoning effort, so
        # capture what this spawn was given (the ``effort`` config the
        # backend read when building the command) for attribution at reap.
        task.spawn_effort = str(cfg.load().get("effort", "xhigh"))
        # Same for the paying account: resolve it from the credentials this
        # spawn just authenticated with, since a later credential config change
        # would affect the *next* spawn only.
        task.spawn_budget = budget.detect(task.engine, env)
        task.set_status(TaskStatus.WORKING)
        self.store.put_task(task)
        return True

    def _build_prompt(self, task: Task) -> tuple[str, bool]:
        """Return (prompt_text, is_resume) for a task about to be scheduled."""
        if task.session_id and not self.find_session_log(task.session_id, task.engine):
            # The session log is gone — deleted, or created under another
            # account's home (both backends store session logs per-user).
            # The next spawn must start a fresh session; seed it with the
            # full unified-log transcript via the catch-up path, otherwise
            # the new agent would receive only the original prompt and
            # every intermediate turn would silently vanish.
            task.sessions.pop(task.engine, None)
            task.session_id = None
            task.session_log_path = None
            task.awaiting_catchup = True
            task.log_cursors[task.engine] = 0

        if task.awaiting_catchup:
            return self._build_catchup_prompt(task)

        notice = (
            _branch_notice(task.parent_name, assignment_below=True) + "\n"
            if task.awaiting_branch_notice
            else ""
        )

        if task.cached_replies:
            replies = "\n\n".join(task.cached_replies)
            task.cached_replies = []
            if task.session_id:
                return notice + replies, True
            return notice + task.prompt + "\n\n" + replies, False

        if task.session_id:
            return notice + "Please continue working on this task.", True
        return notice + task.prompt, False

    def _build_catchup_prompt(self, task: Task) -> tuple[str, bool]:
        """Build the post-switch prompt that catches the active engine up.

        The unified log already holds every turn, including any brand-new
        reply that triggered this schedule, so the interim slice fully conveys
        what the engine missed; pending ``cached_replies`` are cleared because
        they are part of that slice. Resumes the engine's native session when
        it has one, otherwise starts a fresh session seeded with the
        transcript.

        ``awaiting_catchup`` and the engine's log cursor are deliberately NOT
        consumed here: they only advance in ``_try_reap`` once the turn has
        verifiably completed. If the spawn or the agent dies before producing
        output, the pending catch-up survives and the retry rebuilds the same
        slice instead of silently resuming from a cursor the engine never saw.
        """
        entries = self.store.read_logs(task.name)
        seen = task.log_cursors.get(task.engine, 0)
        interim = entries[seen:]
        has_session = bool(task.session_id)
        task.cached_replies = []

        if not interim:
            if has_session:
                return "Please continue working on this task.", True
            return task.prompt, False

        # A branch always logs its assignment before the first spawn, so a
        # pending notice implies that assignment is the last interim entry.
        notice = (
            _branch_notice(task.parent_name, assignment_below=False)
            if task.awaiting_branch_notice
            else None
        )
        return (
            _render_catchup(
                interim,
                fresh=not has_session,
                branch_notice=notice,
                # Cursors only ever rest at 0 or past the branch point (reap
                # advances them to the full log length, which the inherited
                # prefix never exceeds), so this is the prefix length when the
                # slice spans the branch point and <= 0 otherwise.
                inherited_count=task.gist_branch_point - seen,
                parent_name=task.parent_name,
            ),
            has_session,
        )

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

        # The model that produced *this* turn's reply, if we can detect it.
        # Kept turn-local (not read back from ``task.last_assistant_model``) so
        # a detection miss tags nothing rather than inheriting the prior turn's
        # model — which, after a backend switch, could be the other engine's.
        turn_model: str | None = None
        message_usage = None
        sid = result.session_id
        if sid:
            log_path = self.find_session_log(sid, task.engine)
            if log_path:
                task.session_id = sid
                task.session_log_path = str(log_path)
                # Keep the per-engine session map current so a later backend
                # switch can resume this engine's native session.
                task.set_session_for(task.engine, sid)
                # Cache the model that produced this turn's assistant message
                # so ``ilan tail`` can show it without rescanning the session
                # log on every request.
                turn_model = backend.last_assistant_model(log_path)
                if turn_model:
                    task.last_assistant_model = turn_model
                # Codex's process output carries cumulative thread counters.
                # Its rollout transcript exposes task boundaries, allowing the
                # backend to replace them with this invocation's delta. Other
                # backends return None and keep their native per-turn usage.
                turn_usage = backend.last_turn_token_usage(log_path)
                if turn_usage is not None:
                    result.input_tokens = turn_usage.input_tokens
                    result.output_tokens = turn_usage.output_tokens
                    result.cache_read_input_tokens = (
                        turn_usage.cache_read_input_tokens
                    )
                # The result counters cover the complete backend invocation for
                # task-level totals. The log entry instead records only the
                # final native assistant message so ``ilan tail`` describes the
                # message it displays.
                message_usage = backend.last_assistant_token_usage(log_path)

        # The effort and paying account behind this turn come from the
        # spawn-time captures, not the session log (neither backend records
        # them there).
        turn_effort = task.spawn_effort
        if turn_effort:
            task.last_assistant_effort = turn_effort
        turn_budget = task.spawn_budget
        if turn_budget:
            task.last_assistant_budget = turn_budget
        # The backend prices each invocation separately, so this is the cost of
        # this turn alone. Backends that report no cost leave it ``None`` so
        # nothing is attributed rather than a misleading $0.00.
        turn_cost = result.cost_usd or None
        if turn_cost:
            task.last_assistant_cost_usd = turn_cost

        task.input_tokens += result.input_tokens
        task.output_tokens += result.output_tokens
        task.cache_read_input_tokens += result.cache_read_input_tokens
        task.cost_usd += result.cost_usd

        response = result.result_text
        if response:
            self.store.append_log(
                task.name,
                "assistant",
                response,
                model=turn_model,
                effort=turn_effort,
                budget=turn_budget,
                cost_usd=turn_cost,
                input_tokens=(
                    message_usage.input_tokens if message_usage else None
                ),
                output_tokens=(
                    message_usage.output_tokens if message_usage else None
                ),
                cache_read_input_tokens=(
                    message_usage.cache_read_input_tokens
                    if message_usage else None
                ),
                task_alias=task.alias,
            )

        # This engine's native session now reflects every unified-log entry
        # through its own just-appended turn, so advance its cursor. A future
        # switch to another engine compares against this to know what to
        # catch that engine up on. Any pending catch-up was part of the prompt
        # this turn consumed, so it is only marked done here — after the turn
        # verifiably completed — never at prompt-build time.
        task.log_cursors[task.engine] = len(self.store.read_logs(task.name))
        task.awaiting_catchup = False
        # Same rule for the branch notice: only spent once a turn has actually
        # read it, so a spawn that dies first still delivers it on the retry.
        task.awaiting_branch_notice = False

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
        the assistant's new reply to Luna. Returns ``None`` if there is
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
        if re.search(r"\[STATUS:\s*NEEDS_ATTENTION\]", response):
            return TaskStatus.NEEDS_ATTENTION
        return TaskStatus.AGENT_FINISHED

    def find_session_log(self, session_id: str, engine: str = DEFAULT_ENGINE) -> Path | None:
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
