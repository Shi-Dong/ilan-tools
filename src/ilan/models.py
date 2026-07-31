from __future__ import annotations

import itertools
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


def generate_task_hash() -> str:
    """Generate an 8-character hex hash for a task."""
    return os.urandom(4).hex()

_TASK_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def validate_task_name(name: str) -> str | None:
    """Return an error message if *name* is not a valid task name, else ``None``."""
    if len(name) < 3:
        return "Task name must be at least 3 characters"
    if not _TASK_NAME_RE.match(name):
        return "Task name may only contain letters, digits, hyphens, and underscores"
    return None


ALIAS_CHARS = "asdfghjkl"
_BANNED_ALIASES: set[str] = {"ls"}
ALIAS_POOL: list[str] = [
    "".join(p) for p in itertools.product(ALIAS_CHARS, repeat=2)
    if "".join(p) not in _BANNED_ALIASES
]


class TaskStatus(str, Enum):
    WORKING = "WORKING"
    NEEDS_ATTENTION = "NEEDS_ATTENTION"
    AGENT_FINISHED = "AGENT_FINISHED"
    DONE = "DONE"
    DISCARDED = "DISCARDED"
    ERROR = "ERROR"

    @property
    def is_terminal(self) -> bool:
        return self in (TaskStatus.DONE, TaskStatus.DISCARDED)


# Anthropic's Mythos-class "Fable" model. Tasks "maxed" via ``ilan max`` run
# on this model instead of the configured default; ``ilan unmax`` clears it.
FABLE_MODEL = "claude-fable-5"

# Shortest allowed ``reply -t`` interval (CLI and server both enforce it):
# more frequent re-sends would interrupt the agent faster than it can make
# meaningful progress between messages.
REPLY_EVERY_MIN_SECONDS = 1200


def is_fable_model(model: str | None) -> bool:
    return model == FABLE_MODEL


# ── Agent backends (engines) ─────────────────────────────────────────────
# A task's ``engine`` names which agent CLI drives it. It defaults to Claude
# Code for backward compatibility; ``ilan switch-backend`` toggles it. Each
# engine keeps its *own* native session id in ``Task.sessions`` so a task can
# be switched away from a backend and back with no loss of that backend's
# conversation — switching back resumes the native session.
ENGINE_CLAUDE = "claude"
ENGINE_CODEX = "codex"
VALID_ENGINES: tuple[str, ...] = (ENGINE_CLAUDE, ENGINE_CODEX)
DEFAULT_ENGINE = ENGINE_CLAUDE


def other_engine(engine: str) -> str:
    """Return the engine to toggle to (the chain has exactly two backends)."""
    return ENGINE_CODEX if engine == ENGINE_CLAUDE else ENGINE_CLAUDE


# Colour of a task's name in ls/dashboard, keyed by engine, so the running
# backend is legible at a glance: light orange for Claude, light blue for Codex.
ENGINE_NAME_STYLE: dict[str, str] = {
    ENGINE_CLAUDE: "orange1",
    ENGINE_CODEX: "light_sky_blue1",
}


STYLE_FOR_STATUS: dict[TaskStatus, str] = {
    TaskStatus.WORKING: "bold cyan",
    TaskStatus.NEEDS_ATTENTION: "bold red",
    TaskStatus.AGENT_FINISHED: "green",
    TaskStatus.DONE: "dim green",
    TaskStatus.DISCARDED: "dim",
    TaskStatus.ERROR: "bold red",
}

# Label shown instead of AGENT_FINISHED / NEEDS_ATTENTION while a ``reply -t``
# cycle is running. Those two statuses normally mean "a human has to answer",
# which is exactly what a cycling task does not need: its timer re-prompts the
# agent on its own. The stored status is untouched — this is display only.
AGENT_IN_LOOP_LABEL = "AGENT_IN_LOOP"
# A hue no other status uses, because every green shade is already spoken for by
# AGENT_FINISHED and DONE — a cycling task has to be tellable apart from a finished
# one at a glance. Light, since in `ls -c` this always lands on the reply-every grey
# background.
AGENT_IN_LOOP_STYLE = "medium_purple1"
IN_LOOP_STATUSES = frozenset(
    {TaskStatus.AGENT_FINISHED, TaskStatus.NEEDS_ATTENTION}
)


def display_status(
    status: TaskStatus, reply_every_seconds: int | None
) -> tuple[str, str]:
    """Return the (label, style) to render for a task's status."""
    if reply_every_seconds and status in IN_LOOP_STATUSES:
        return AGENT_IN_LOOP_LABEL, AGENT_IN_LOOP_STYLE
    return status.value, STYLE_FOR_STATUS.get(status, "")


@dataclass
class Task:
    name: str
    prompt: str
    status: TaskStatus = TaskStatus.WORKING
    created_at: str = ""
    status_changed_at: str = ""
    session_id: str | None = None
    session_log_path: str | None = None
    pid: int | None = None
    cached_replies: list[str] = field(default_factory=list)
    alias: str | None = None
    task_hash: str | None = None
    needs_review: bool = False
    pinned: bool = False
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cost_usd: float = 0.0
    sleep_seconds: int | None = None
    # Active ``reply -t`` cycle: while ``reply_every_seconds`` is set, the
    # server re-sends ``reply_every_message`` to the task whenever the wall
    # clock passes ``reply_every_next_at`` (an ISO timestamp). Any *human*
    # reply (reply/tap/cancel/sleep) ends the cycle; the automatic re-sends
    # themselves do not.
    reply_every_seconds: int | None = None
    reply_every_message: str | None = None
    reply_every_next_at: str | None = None
    parent_name: str | None = None
    # Names of already-deleted tasks that used to sit between this task and its
    # current ``parent_name``, nearest ancestor first. Deleting a task re-parents
    # its children onto their grandparent, which would silently collapse the
    # branch topology; recording the removed link lets ``ilan task tree`` draw a
    # tombstone where the task used to be instead of pretending the child was
    # branched off the grandparent directly.
    deleted_ancestors: list[str] = field(default_factory=list)
    summary_one_liner: str | None = None
    model: str | None = None
    # The model that generated the most recent assistant message, cached at
    # reap time so ``ilan tail`` need not rescan the Claude session log. This
    # is the *observed* model, distinct from ``model`` above (the *configured*
    # model used by ``ilan max`` / ``unmax``).
    last_assistant_model: str | None = None
    # Reasoning-effort level passed to the most recent agent spawn. Neither
    # backend's session log records the effort, so it is captured here at
    # spawn time (from the ``effort`` config) and copied to
    # ``last_assistant_effort`` when the turn is reaped.
    spawn_effort: str | None = None
    # The effort behind the most recent assistant message. Kept separate from
    # ``spawn_effort`` so that, while a new turn is in flight, the cached
    # model/effort pair still describes the *previous* (visible) message.
    last_assistant_effort: str | None = None
    # Which account paid for the most recent spawn ("Team", "API", …). Also
    # absent from both session logs, so it is resolved from the local
    # credentials at spawn time and, like the effort, copied to
    # ``last_assistant_budget`` at reap.
    spawn_budget: str | None = None
    last_assistant_budget: str | None = None
    # What the most recent assistant message cost, in USD. Unlike the effort
    # and the paying account this is reported by the backend itself, so it is
    # captured at reap rather than at spawn.
    last_assistant_cost_usd: float | None = None
    # GitHub Gist mirror of the conversation. ``gist_id`` / ``gist_url`` are
    # set the first time the async syncer creates the task's secret Gist.
    # ``gist_synced_count`` is an absolute cursor into the unified log so the
    # syncer only posts new messages. For a branched task it starts at
    # ``gist_branch_point``, intentionally skipping the inherited log prefix.
    gist_id: str | None = None
    gist_url: str | None = None
    gist_synced_count: int = 0
    # Number of inherited unified-log entries present when this task was
    # branched. Those entries stay in the local log for agent context but are
    # represented in the child Gist by a link to the parent's final pre-branch
    # comment instead of being posted again.
    gist_branch_point: int = 0
    gist_branch_parent_name: str | None = None
    gist_parent_comment_url: str | None = None
    # The task name currently written into the Gist's Markdown title line. When
    # a task is renamed this diverges from ``name``, which tells the syncer to
    # rewrite the title so it tracks the new name.
    gist_title_name: str | None = None
    # The exact Gist description, which GitHub uses as the browser-tab title.
    # Tracking the rendered value makes punctuation changes detectable even
    # when the task name itself has not changed.
    gist_description: str | None = None
    # Which agent CLI drives this task. Toggled by ``ilan switch-backend``.
    engine: str = DEFAULT_ENGINE
    # Per-engine native session ids ({"claude": <uuid>, "codex": <uuid>}). Each
    # backend resumes its own session, so switching engines never discards the
    # other backend's conversation. ``session_id``/``session_log_path`` above
    # remain the *active* engine's session, kept in sync with this map.
    sessions: dict[str, str] = field(default_factory=dict)
    # Per-engine cursor into the unified log: how many ``logs/<task>.jsonl``
    # entries each engine's native session has already absorbed. Advanced at
    # reap time. When a backend switch leaves the newly-active engine behind
    # this count, the gap is the set of turns it must be caught up on.
    log_cursors: dict[str, int] = field(default_factory=dict)
    # Set by a lazy backend switch when the newly-active engine is behind the
    # unified log; consumed at the next spawn to inject a catch-up preamble
    # (resume) or seed a fresh session with the transcript. Reset once spent.
    awaiting_catchup: bool = False

    def set_session_for(self, engine: str, session_id: str) -> None:
        """Record the native session id for *engine*."""
        self.sessions[engine] = session_id

    def clear_reply_every(self) -> None:
        """Drop the active ``reply -t`` cycle, if any."""
        self.reply_every_seconds = None
        self.reply_every_message = None
        self.reply_every_next_at = None

    def set_status(self, status: TaskStatus) -> None:
        """Set status and update the ``status_changed_at`` timestamp.

        When the task leaves the sleep-visible ``WORKING`` state,
        ``sleep_seconds`` is dropped so stale metadata doesn't leak into a
        future non-sleep reply cycle. A terminal status additionally ends any
        ``reply -t`` cycle: a closed task must not be revived by a timer.
        """
        self.status = status
        self.status_changed_at = datetime.now(timezone.utc).isoformat()
        if status is not TaskStatus.WORKING:
            self.sleep_seconds = None
        if status.is_terminal:
            self.clear_reply_every()

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "prompt": self.prompt,
            "status": self.status.value,
            "created_at": self.created_at,
            "status_changed_at": self.status_changed_at,
            "session_id": self.session_id,
            "session_log_path": self.session_log_path,
            "pid": self.pid,
            "cached_replies": self.cached_replies,
            "alias": self.alias,
            "task_hash": self.task_hash,
            "needs_review": self.needs_review,
            "pinned": self.pinned,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_input_tokens": self.cache_read_input_tokens,
            "cost_usd": self.cost_usd,
            "sleep_seconds": self.sleep_seconds,
            "reply_every_seconds": self.reply_every_seconds,
            "reply_every_message": self.reply_every_message,
            "reply_every_next_at": self.reply_every_next_at,
            "parent_name": self.parent_name,
            "deleted_ancestors": self.deleted_ancestors,
            "summary_one_liner": self.summary_one_liner,
            "model": self.model,
            "last_assistant_model": self.last_assistant_model,
            "spawn_effort": self.spawn_effort,
            "last_assistant_effort": self.last_assistant_effort,
            "spawn_budget": self.spawn_budget,
            "last_assistant_budget": self.last_assistant_budget,
            "last_assistant_cost_usd": self.last_assistant_cost_usd,
            "gist_id": self.gist_id,
            "gist_url": self.gist_url,
            "gist_synced_count": self.gist_synced_count,
            "gist_branch_point": self.gist_branch_point,
            "gist_branch_parent_name": self.gist_branch_parent_name,
            "gist_parent_comment_url": self.gist_parent_comment_url,
            "gist_title_name": self.gist_title_name,
            "gist_description": self.gist_description,
            "engine": self.engine,
            "sessions": self.sessions,
            "log_cursors": self.log_cursors,
            "awaiting_catchup": self.awaiting_catchup,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Task:
        return cls(
            name=d["name"],
            prompt=d["prompt"],
            status=cls._migrate_status(d["status"]),
            created_at=d.get("created_at", ""),
            status_changed_at=d.get("status_changed_at", d.get("created_at", "")),
            session_id=d.get("session_id"),
            session_log_path=d.get("session_log_path"),
            pid=d.get("pid"),
            cached_replies=d.get("cached_replies", []),
            alias=d.get("alias"),
            task_hash=d.get("task_hash"),
            needs_review=d.get("needs_review", False),
            pinned=d.get("pinned", False),
            input_tokens=d.get("input_tokens", 0),
            output_tokens=d.get("output_tokens", 0),
            cache_read_input_tokens=d.get("cache_read_input_tokens", 0),
            cost_usd=d.get("cost_usd", 0.0),
            sleep_seconds=d.get("sleep_seconds"),
            reply_every_seconds=d.get("reply_every_seconds"),
            reply_every_message=d.get("reply_every_message"),
            reply_every_next_at=d.get("reply_every_next_at"),
            parent_name=d.get("parent_name"),
            deleted_ancestors=list(d.get("deleted_ancestors") or []),
            summary_one_liner=d.get("summary_one_liner"),
            model=d.get("model"),
            last_assistant_model=d.get("last_assistant_model"),
            spawn_effort=d.get("spawn_effort"),
            last_assistant_effort=d.get("last_assistant_effort"),
            spawn_budget=d.get("spawn_budget"),
            last_assistant_budget=d.get("last_assistant_budget"),
            last_assistant_cost_usd=d.get("last_assistant_cost_usd"),
            gist_id=d.get("gist_id"),
            gist_url=d.get("gist_url"),
            gist_synced_count=d.get("gist_synced_count", 0),
            gist_branch_point=d.get("gist_branch_point", 0),
            gist_branch_parent_name=d.get("gist_branch_parent_name"),
            gist_parent_comment_url=d.get("gist_parent_comment_url"),
            gist_title_name=d.get("gist_title_name"),
            gist_description=d.get("gist_description"),
            engine=d.get("engine", DEFAULT_ENGINE),
            sessions=cls._migrate_sessions(d),
            log_cursors=dict(d.get("log_cursors") or {}),
            awaiting_catchup=d.get("awaiting_catchup", False),
        )

    @staticmethod
    def _migrate_status(value: str) -> TaskStatus:
        """Map the retired ``UNCLAIMED`` status of persisted legacy tasks to
        ``NEEDS_ATTENTION``: they were waiting to be scheduled, and now that
        agents spawn immediately the user's next reply is what starts them.
        """
        if value == "UNCLAIMED":
            return TaskStatus.NEEDS_ATTENTION
        return TaskStatus(value)

    @staticmethod
    def _migrate_sessions(d: dict[str, Any]) -> dict[str, str]:
        """Build the per-engine session map, seeding it from the legacy single
        ``session_id`` for tasks persisted before the map existed.

        Legacy tasks predate the second backend, so their session belongs to
        whichever engine the task carries (Claude by default).
        """
        sessions = dict(d.get("sessions") or {})
        legacy_sid = d.get("session_id")
        if legacy_sid and not sessions:
            sessions[d.get("engine", DEFAULT_ENGINE)] = legacy_sid
        return sessions


# Budget label for a spawn billed to an API key rather than a subscription.
# Lives here rather than in :mod:`ilan.budget` (which resolves it) because the
# cost formatter below keys off it, and ``budget`` already imports this module.
API = "API"


def format_cost_usd(cost: float | None, budget: str | None) -> str | None:
    """Render a message cost as ``$0.12``, or ``None`` when it shouldn't be shown.

    Shared by the ``ilan task tail`` hint and the Gist attribution so the two
    always agree. Only an API-key spend is real money: on a subscription the
    backend still reports a price, but it is what the tokens *would* have cost
    on the API, not an amount charged, so showing it next to ``budget: Team``
    would read as a bill that nobody pays. A zero cost likewise means the
    backend did not price the turn, not that the turn was free.
    """
    if not cost or budget != API:
        return None
    return f"${cost:.2f}"


@dataclass
class LogEntry:
    role: str
    content: str
    timestamp: str
    # Model that produced this message (assistant replies only). Older entries
    # predate this field and stay ``None`` so they render unchanged.
    model: str | None = None
    # Reasoning-effort level the agent was spawned with (assistant replies
    # only). Older entries predate this field and stay ``None``.
    effort: str | None = None
    # Account that paid for the message ("Team", "API", …; assistant replies
    # only). Older entries predate this field and stay ``None``.
    budget: str | None = None
    # What this message cost, in USD (assistant replies only). Older entries,
    # and backends that don't price a turn, stay ``None``.
    cost_usd: float | None = None
    # Token usage for the backend invocation that produced this assistant
    # reply. Older entries predate these fields and stay ``None`` so ``ilan
    # tail`` can distinguish unknown usage from a real zero-token category.
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_input_tokens: int | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "role": self.role, "content": self.content, "timestamp": self.timestamp,
        }
        if self.model:
            d["model"] = self.model
        if self.effort:
            d["effort"] = self.effort
        if self.budget:
            d["budget"] = self.budget
        if self.cost_usd:
            d["cost_usd"] = self.cost_usd
        for key, value in (
            ("input_tokens", self.input_tokens),
            ("output_tokens", self.output_tokens),
            ("cache_read_input_tokens", self.cache_read_input_tokens),
        ):
            if value is not None:
                d[key] = value
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> LogEntry:
        return cls(
            role=d["role"],
            content=d["content"],
            timestamp=d.get("timestamp", ""),
            model=d.get("model") or None,
            effort=d.get("effort") or None,
            budget=d.get("budget") or None,
            cost_usd=d.get("cost_usd") or None,
            input_tokens=d.get("input_tokens"),
            output_tokens=d.get("output_tokens"),
            cache_read_input_tokens=d.get("cache_read_input_tokens"),
        )

    @classmethod
    def now(
        cls, role: str, content: str, model: str | None = None,
        effort: str | None = None, budget: str | None = None,
        cost_usd: float | None = None,
        input_tokens: int | None = None, output_tokens: int | None = None,
        cache_read_input_tokens: int | None = None,
    ) -> LogEntry:
        return cls(
            role=role,
            content=content,
            timestamp=datetime.now(timezone.utc).isoformat(),
            model=model,
            effort=effort,
            budget=budget,
            cost_usd=cost_usd,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_input_tokens=cache_read_input_tokens,
        )
