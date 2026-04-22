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
    UNCLAIMED = "UNCLAIMED"
    WORKING = "WORKING"
    NEEDS_ATTENTION = "NEEDS_ATTENTION"
    AGENT_FINISHED = "AGENT_FINISHED"
    DONE = "DONE"
    DISCARDED = "DISCARDED"
    ERROR = "ERROR"

    @property
    def is_terminal(self) -> bool:
        return self in (TaskStatus.DONE, TaskStatus.DISCARDED)

    @property
    def is_claimable(self) -> bool:
        return self == TaskStatus.UNCLAIMED


STYLE_FOR_STATUS: dict[TaskStatus, str] = {
    TaskStatus.UNCLAIMED: "yellow",
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
    status: TaskStatus = TaskStatus.UNCLAIMED
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

    def set_status(self, status: TaskStatus) -> None:
        """Set status and update the ``status_changed_at`` timestamp.

        When the task leaves the sleep-visible states (``UNCLAIMED`` and
        ``WORKING``), ``sleep_seconds`` is dropped so stale metadata
        doesn't leak into a future non-sleep reply cycle.
        """
        self.status = status
        self.status_changed_at = datetime.now(timezone.utc).isoformat()
        if status not in (TaskStatus.UNCLAIMED, TaskStatus.WORKING):
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
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Task:
        return cls(
            name=d["name"],
            prompt=d["prompt"],
            status=TaskStatus(d["status"]),
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
        )


@dataclass
class LogEntry:
    role: str
    content: str
    timestamp: str

    def to_dict(self) -> dict[str, str]:
        return {"role": self.role, "content": self.content, "timestamp": self.timestamp}

    @classmethod
    def from_dict(cls, d: dict[str, str]) -> LogEntry:
        return cls(role=d["role"], content=d["content"], timestamp=d.get("timestamp", ""))

    @classmethod
    def now(cls, role: str, content: str) -> LogEntry:
        return cls(role=role, content=content, timestamp=datetime.now(timezone.utc).isoformat())
