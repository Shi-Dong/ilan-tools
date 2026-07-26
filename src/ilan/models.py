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
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cost_usd: float = 0.0
    sleep_seconds: int | None = None
    parent_name: str | None = None
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
    # GitHub Gist mirror of the conversation. ``gist_id`` / ``gist_url`` are
    # set the first time the async syncer creates the task's secret Gist;
    # ``gist_synced_count`` records how many log entries have already been
    # posted as Gist comments so the syncer only posts new messages.
    gist_id: str | None = None
    gist_url: str | None = None
    gist_synced_count: int = 0
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

    def session_for(self, engine: str | None = None) -> str | None:
        """Return the native session id for *engine* (defaults to active)."""
        return self.sessions.get(engine or self.engine)

    def set_session_for(self, engine: str, session_id: str) -> None:
        """Record the native session id for *engine*."""
        self.sessions[engine] = session_id

    def set_status(self, status: TaskStatus) -> None:
        """Set status and update the ``status_changed_at`` timestamp.

        When the task leaves the sleep-visible ``WORKING`` state,
        ``sleep_seconds`` is dropped so stale metadata doesn't leak into a
        future non-sleep reply cycle.
        """
        self.status = status
        self.status_changed_at = datetime.now(timezone.utc).isoformat()
        if status is not TaskStatus.WORKING:
            self.sleep_seconds = None

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
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_input_tokens": self.cache_read_input_tokens,
            "cost_usd": self.cost_usd,
            "sleep_seconds": self.sleep_seconds,
            "parent_name": self.parent_name,
            "summary_one_liner": self.summary_one_liner,
            "model": self.model,
            "last_assistant_model": self.last_assistant_model,
            "spawn_effort": self.spawn_effort,
            "last_assistant_effort": self.last_assistant_effort,
            "gist_id": self.gist_id,
            "gist_url": self.gist_url,
            "gist_synced_count": self.gist_synced_count,
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
            input_tokens=d.get("input_tokens", 0),
            output_tokens=d.get("output_tokens", 0),
            cache_read_input_tokens=d.get("cache_read_input_tokens", 0),
            cost_usd=d.get("cost_usd", 0.0),
            sleep_seconds=d.get("sleep_seconds"),
            parent_name=d.get("parent_name"),
            summary_one_liner=d.get("summary_one_liner"),
            model=d.get("model"),
            last_assistant_model=d.get("last_assistant_model"),
            spawn_effort=d.get("spawn_effort"),
            last_assistant_effort=d.get("last_assistant_effort"),
            gist_id=d.get("gist_id"),
            gist_url=d.get("gist_url"),
            gist_synced_count=d.get("gist_synced_count", 0),
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

    def to_dict(self) -> dict[str, str]:
        d = {"role": self.role, "content": self.content, "timestamp": self.timestamp}
        if self.model:
            d["model"] = self.model
        if self.effort:
            d["effort"] = self.effort
        return d

    @classmethod
    def from_dict(cls, d: dict[str, str]) -> LogEntry:
        return cls(
            role=d["role"],
            content=d["content"],
            timestamp=d.get("timestamp", ""),
            model=d.get("model") or None,
            effort=d.get("effort") or None,
        )

    @classmethod
    def now(
        cls, role: str, content: str, model: str | None = None,
        effort: str | None = None,
    ) -> LogEntry:
        return cls(
            role=role,
            content=content,
            timestamp=datetime.now(timezone.utc).isoformat(),
            model=model,
            effort=effort,
        )
