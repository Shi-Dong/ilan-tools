from __future__ import annotations

import itertools
import os
import random
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


def generate_task_hash() -> str:
    """Generate an 8-character hex hash for a task."""
    return os.urandom(4).hex()

_TASK_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_TASK_NUMBER_RE = re.compile(r"^[0-9]+$")


def parse_task_number(value: str) -> int | None:
    """Return *value* read as a task number, or ``None`` if it is not one."""
    return int(value) if _TASK_NUMBER_RE.match(value) else None


def validate_task_name(name: str) -> str | None:
    """Return an error message if *name* is not a valid task name, else ``None``."""
    if len(name) < 3:
        return "Task name must be at least 3 characters"
    if not _TASK_NAME_RE.match(name):
        return "Task name may only contain letters, digits, hyphens, and underscores"
    # An all-digit name would shadow the task number that ``undone`` /
    # ``undiscard`` resolve, since both look the name up first.
    if parse_task_number(name) is not None:
        return "Task name may not be all digits: numbers refer to task numbers"
    return None


# A note has to stay glanceable inside a fixed-width column in ``ilan ls`` and
# ``ilan dashboard``, and every character past a line's worth pushes the rest
# of the listing further down the screen. Past this the text has stopped being
# a reminder and belongs in the conversation itself. Anything longer is cut to
# fit rather than refused — see :func:`truncate_notes`.
#
# This is a ceiling, not a target: a note that actually uses all of it fills
# roughly six lines of the Notes column on a wide window and ten on a narrow
# one, so a listing of maximal notes is a tall listing. The web app mirrors
# the number in ``app.js``, and a test pins the two together.
MAX_NOTES_LENGTH = 256


def truncate_notes(note: str) -> str:
    """Cut *note* down to ``MAX_NOTES_LENGTH``, keeping the front.

    Kept rather than refused: a note is a reminder, and the first sentence of
    one is worth more than an error telling the user to count characters. The
    front is what survives because that is where a reminder says what it is
    about. Callers report the cut so it is trimmed, not swallowed.

    Stripped again after the cut, since slicing can land on a space and leave
    a note ending in whitespace that was never part of the text.
    """
    return note[:MAX_NOTES_LENGTH].strip()


def join_notes(existing: str | None, addition: str) -> str:
    """Join a task's current note and an appended fragment with one space.

    Both sides are stripped first, so the result never carries leading or
    trailing whitespace and the separator is always exactly one space however
    the user padded their argument. Either side may be empty: appending to a
    task with no note just sets the note rather than leaving it indented by a
    stray separator.
    """
    parts = [part for part in ((existing or "").strip(), addition.strip()) if part]
    return " ".join(parts)


ALIAS_CHARS = "asdfghjkl"
_BANNED_ALIASES: set[str] = {"ls"}
ALIAS_POOL: list[str] = [
    "".join(p) for p in itertools.product(ALIAS_CHARS, repeat=2)
    if "".join(p) not in _BANNED_ALIASES
]


# ── Burnable tasks ───────────────────────────────────────────────────────
# A task whose name starts with this prefix is scratch work: ``done`` and
# ``discard`` delete it outright instead of parking it in the closed list,
# because a throwaway task is not worth a slot in the history. ``ilan add``
# mints such a name whenever no ``-n`` is given.
#
# Burnability is read off the *current* name and stored nowhere, so renaming is
# what moves a task in or out of it: rename ``xxx-cat-likes-fin`` to ``fix-auth``
# and it closes normally; rename ``fix-auth`` to ``xxx-fix-auth`` and it burns.
BURNABLE_PREFIX = "xxx-"


def is_burnable_name(name: str) -> bool:
    """Return whether *name* marks a task as burnable (deleted when closed)."""
    return name.startswith(BURNABLE_PREFIX)


# Word pools for generated burnable names. They read as a tiny sentence
# (``xxx-cat-likes-fin``) so an unnamed task is still pronounceable and
# memorable in ``ilan ls`` — a hash would be neither. Verbs are third-person
# singular so the middle word can never be mistaken for one of the nouns.
BURNABLE_NOUNS: tuple[str, ...] = (
    "ant", "bat", "bee", "cat", "cow", "crab", "dog", "elk", "fern", "fin",
    "fox", "frog", "goat", "hawk", "hen", "ibis", "koi", "lark", "mole",
    "moss", "moth", "newt", "owl", "pig", "pine", "ram", "reed", "seal",
    "swan", "toad", "wasp", "wolf", "yak", "brook", "cloud", "cove", "dune",
    "lake", "mesa", "peak", "ridge", "storm", "tide", "claw", "horn", "paw",
    "tail", "wing",
)
BURNABLE_VERBS: tuple[str, ...] = (
    "builds", "chases", "chews", "counts", "digs", "dodges", "drops", "finds",
    "follows", "guards", "hates", "hides", "jumps", "keeps", "likes", "meets",
    "naps", "paints", "sings", "sniffs", "spots", "steals", "wants", "watches",
)


def random_burnable_name() -> str:
    """Return a random ``xxx-noun-verb-noun`` name, e.g. ``xxx-cat-likes-fin``.

    The two nouns are drawn without replacement so a name never reads
    ``xxx-cat-likes-cat``. Draws are otherwise independent, so the name is not
    guaranteed to be free: callers that need a *unique* one go through
    :meth:`ilan.store.Store.next_available_burnable_name`.
    """
    subject, obj = random.sample(BURNABLE_NOUNS, 2)
    return f"{BURNABLE_PREFIX}{subject}-{random.choice(BURNABLE_VERBS)}-{obj}"


class TaskStatus(str, Enum):
    WORKING = "WORKING"
    SLEEPING = "SLEEPING"
    NEEDS_ATTENTION = "NEEDS_ATTENTION"
    AGENT_FINISHED = "AGENT_FINISHED"
    DONE = "DONE"
    DISCARDED = "DISCARDED"
    ERROR = "ERROR"

    @property
    def is_terminal(self) -> bool:
        return self in (TaskStatus.DONE, TaskStatus.DISCARDED)

    @property
    def is_running(self) -> bool:
        """Whether an agent process is currently running for this task."""
        return self in (TaskStatus.WORKING, TaskStatus.SLEEPING)


# Shortest allowed ``reply -t`` interval (CLI and server both enforce it):
# more frequent re-sends would interrupt the agent faster than it can make
# meaningful progress between messages.
REPLY_EVERY_MIN_SECONDS = 1200

# Canned replies behind ``ilan tap`` and ``ilan cancel``. They live here rather
# than in the CLI because the web app sends the same two messages through the
# same ``/tasks/<name>/reply`` route: a second copy in JavaScript would drift
# from this one the first time either wording is tuned.
TAP_MESSAGE = "How are things now? Pause what you are doing and give me a quick summary of the current situation."

CANCEL_MESSAGE = (
    "My previous message was sent by mistake. You can ignore that message and "
    "the instructions therein. If you are working on fulfilling a request "
    "specified in that message, you should stop immediately."
)


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


# ── Max models (``ilan max``) ────────────────────────────────────────────
# Every backend has one max model: the strongest thing that backend can run,
# worth its price on a hard turn. ``ilan max`` marks a task as maxed and
# ``ilan unmax`` clears the mark, putting the task back on the configured
# default. The mark never names a model: every spawn resolves it here, so
# bumping an id below moves every maxed task onto the new model at once.
FABLE_MODEL = "claude-fable-5-1"  # Anthropic's Fable, for the Claude backend
ASTRA_MODEL = "gpt-6-astra"  # OpenAI's GPT-6 Astra, for the Codex backend


@dataclass(frozen=True)
class MaxModel:
    """What ``ilan max`` means on one backend: which model, what to call it."""

    model: str
    # The tag shown beside a maxed task in ``ilan ls``, ``ilan dashboard`` and
    # the web app. It names the model rather than reading "MAX" so that a
    # glance at the list says *which* expensive model the task is burning.
    tag: str


MAX_MODELS: dict[str, MaxModel] = {
    ENGINE_CLAUDE: MaxModel(FABLE_MODEL, "FABLE"),
    ENGINE_CODEX: MaxModel(ASTRA_MODEL, "ASTRA"),
}

# Every id ``ilan max`` ever pinned, by the backend that ran it: what a task
# persisted before ``maxed`` existed can hold in ``model``. Only
# ``Task._migrate_maxed`` reads this, to judge such a task the way its backend
# did. Nothing resolves to these ids any more, and nothing will add to them:
# no code writes ``model`` now, so bumping a max model above never touches it.
_PINNED_MAX_MODELS: dict[str, frozenset[str]] = {
    ENGINE_CLAUDE: frozenset({"claude-fable-5", "claude-fable-5-1"}),
    ENGINE_CODEX: frozenset({"gpt-6-astra"}),
}


def _max_model(engine: str | None) -> MaxModel:
    """The max model of *engine*, resolving it the way a spawn would.

    An absent engine predates the field, and an unrecognised one is driven by
    the Claude backend (see ``Runner._backend_for``), so both answer Claude's.
    """
    return MAX_MODELS.get(engine or DEFAULT_ENGINE, MAX_MODELS[DEFAULT_ENGINE])


def max_model_for(engine: str | None) -> str:
    """The model id a maxed task running on *engine* spawns with."""
    return _max_model(engine).model


def max_tag(engine: str | None, maxed: bool) -> str | None:
    """The tag for a task on *engine*, or ``None`` when it is not maxed.

    This is the predicate behind the tag wherever it appears — ``ilan ls``,
    ``ilan dashboard`` and the web app — so the three cannot disagree about
    which tasks carry one, or about what it says.
    """
    return _max_model(engine).tag if maxed else None


# ── Reasoning levels (``ilan level``) ───────────────────────────────────
# Each task carries one of three reasoning levels. The level is backend
# neutral and every spawn translates it to the flag value of the backend the
# task is on *now*, so switching backends needs no rewrite. New tasks, and
# tasks stored before the field existed, start at ``low``. Codex tops out at
# ``xhigh``, which is what ``max`` means there.
REASONING_LEVELS = ("low", "medium", "max")
DEFAULT_REASONING = "low"
# A branch keeps its parent's level, but an ``ilan btw`` side question always
# starts here, whatever the parent runs at: it is a quick question asked of the
# parent's context, and ``ilan level`` raises it if it turns out to be hard.
BTW_REASONING = "low"

_BACKEND_EFFORTS: dict[str, dict[str, str]] = {
    ENGINE_CLAUDE: {"low": "low", "medium": "medium", "max": "max"},
    ENGINE_CODEX: {"low": "low", "medium": "medium", "max": "xhigh"},
}


def backend_effort(engine: str | None, level: str | None) -> str:
    """The effort value *engine*'s CLI is given for reasoning *level*.

    An unknown level falls back to the default, and an absent or unknown
    engine answers the way the Claude backend would, as ``_max_model`` does.
    """
    efforts = _BACKEND_EFFORTS.get(engine or DEFAULT_ENGINE, _BACKEND_EFFORTS[DEFAULT_ENGINE])
    return efforts.get(level or DEFAULT_REASONING, efforts[DEFAULT_REASONING])


def tag_for_max_model(model: str | None) -> str | None:
    """The tag of whichever backend's max model *model* is, if it is one.

    ``max_tag`` answers for a whole task; this answers for a bare id, which is
    what a command reporting the model the server has just resolved has to go
    on.
    """
    for entry in MAX_MODELS.values():
        if entry.model == model:
            return entry.tag
    return None


STYLE_FOR_STATUS: dict[TaskStatus, str] = {
    TaskStatus.WORKING: "bold cyan",
    TaskStatus.SLEEPING: "bold deep_sky_blue4",
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
    # The most recent time the task became *active*: when it was created, and
    # again every time ``undone`` / ``undiscard`` brought it back. This is what
    # the listing sorts on, so a revived task lands at the bottom next to the
    # work you just picked up rather than back at the position it held before
    # it was closed, which is where ``created_at`` would leave it. Left unset
    # it falls back to ``created_at`` (see ``__post_init__``), which is both
    # what a brand-new task wants and the migration for a store written before
    # this field existed.
    activated_at: str = ""
    session_id: str | None = None
    session_log_path: str | None = None
    pid: int | None = None
    cached_replies: list[str] = field(default_factory=list)
    alias: str | None = None
    # Every name this task has carried before its current one, oldest first.
    # ``Store.rename_task`` appends the outgoing name and never rewrites the
    # list, so renaming back to an earlier name records the round trip instead
    # of collapsing it. ``ilan info`` prints the chain: a task renamed
    # mid-flight is still recognisable by what you used to call it, and a name
    # you remember from an older Gist comment still leads to the task holding
    # it now. Not inherited by a branched child, which starts its own history
    # under its own name.
    former_names: list[str] = field(default_factory=list)
    # Stable handle minted the first time the task reaches DONE or DISCARDED,
    # so it can be revived as ``undone 12`` / ``undiscard 12`` without typing
    # the full name (a closed task has no alias to fall back on). Unlike the
    # alias it is never recycled while the task exists, so reviving and
    # re-closing a task always shows the same number.
    number: int | None = None
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
    # branch topology; recording the removed link lets ``ilan info`` draw a
    # tombstone where the task used to be instead of pretending the child was
    # branched off the grandparent directly.
    deleted_ancestors: list[str] = field(default_factory=list)
    # A free-form reminder the *user* writes with ``ilan notes``, shown in
    # its own column in ``ilan ls`` / ``ilan dashboard``. Distinct from
    # ``summary_one_liner`` below, which the server generates from the
    # agent's latest reply: this one says why the task exists at all, so
    # nothing overwrites it and it survives every status change.
    notes: str | None = None
    summary_one_liner: str | None = None
    # Whether ``ilan max`` has put this task on its backend's max model. Only
    # the choice is stored, never a model id: ``model_override`` resolves it
    # afresh at every spawn, so a newer Fable or Astra reaches every maxed task
    # without re-maxing it, and a backend switch has nothing to translate.
    maxed: bool = False
    # The ``ilan level`` reasoning level (one of ``REASONING_LEVELS``). Like
    # ``maxed`` it stores the choice, not a backend flag value:
    # ``backend_effort`` translates it at every spawn.
    reasoning: str = DEFAULT_REASONING
    # The model that generated the most recent assistant message, cached at
    # reap time so ``ilan tail`` need not rescan the Claude session log. This
    # is the *observed* model, distinct from ``model_override`` (what the next
    # spawn is *told* to run).
    last_assistant_model: str | None = None
    # Reasoning-effort level passed to the most recent agent spawn. Neither
    # backend's session log records the effort, so it is captured here at
    # spawn time (from the task's ``reasoning`` level) and copied to
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
    # comment instead of being posted again. The catch-up renderer reuses this
    # count to place a branch divider after the inherited prefix whenever the
    # log is replayed (backend switch, lost-session reseed), so later spawns
    # still see where the parent's conversation ends and this task's begins.
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
    # Set when this task is branched off ``parent_name`` (every branch carries
    # the child's first assignment); consumed at the next spawn to inject a
    # notice that the inherited conversation is background context rather than
    # work to finish. Without it the child resumes a verbatim copy of the
    # parent's session and reads the parent's in-flight instructions — and its
    # tmux prefix — as its own. Reset once spent.
    awaiting_branch_notice: bool = False

    def __post_init__(self) -> None:
        # Creation is an activation, and for a task that has never been closed
        # and revived it is the only one, so an unset ``activated_at`` means
        # ``created_at``. Doing it here rather than at each call site covers
        # both of them plus ``from_dict``, so a task loaded from a store
        # written before the field existed sorts where it always did.
        if not self.activated_at:
            self.activated_at = self.created_at

    @property
    def model_override(self) -> str | None:
        """The model a spawn runs instead of the configured default, if any.

        A maxed task gets the max model of the backend it is on *now*, looked
        up at the moment of asking; every other task gets ``None`` and so the
        ``model-claude`` / ``model-codex`` default.
        """
        return max_model_for(self.engine) if self.maxed else None

    @property
    def effort(self) -> str:
        """The effort flag value a spawn on the task's current backend gets."""
        return backend_effort(self.engine, self.reasoning)

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

        ``sleep_seconds`` belongs only to ``SLEEPING``. Leaving that state
        drops it so stale metadata cannot leak into a later ordinary reply.
        A terminal status additionally ends any ``reply -t`` cycle: a closed
        task must not be revived by a timer.
        """
        self.status = status
        self.status_changed_at = datetime.now(timezone.utc).isoformat()
        if status is not TaskStatus.SLEEPING:
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
            "activated_at": self.activated_at,
            "session_id": self.session_id,
            "session_log_path": self.session_log_path,
            "pid": self.pid,
            "cached_replies": self.cached_replies,
            "alias": self.alias,
            "former_names": self.former_names,
            "number": self.number,
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
            "notes": self.notes,
            "summary_one_liner": self.summary_one_liner,
            "maxed": self.maxed,
            "reasoning": self.reasoning,
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
            "awaiting_branch_notice": self.awaiting_branch_notice,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Task:
        return cls(
            name=d["name"],
            prompt=d["prompt"],
            status=cls._migrate_status(d["status"], d.get("sleep_seconds")),
            created_at=d.get("created_at", ""),
            status_changed_at=d.get("status_changed_at", d.get("created_at", "")),
            activated_at=d.get("activated_at", ""),
            session_id=d.get("session_id"),
            session_log_path=d.get("session_log_path"),
            pid=d.get("pid"),
            cached_replies=d.get("cached_replies", []),
            alias=d.get("alias"),
            former_names=list(d.get("former_names") or []),
            number=d.get("number"),
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
            notes=d.get("notes"),
            summary_one_liner=d.get("summary_one_liner"),
            maxed=cls._migrate_maxed(d),
            reasoning=(
                d["reasoning"] if d.get("reasoning") in REASONING_LEVELS
                else DEFAULT_REASONING
            ),
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
            awaiting_branch_notice=d.get("awaiting_branch_notice", False),
        )

    @staticmethod
    def _migrate_status(
        value: str, sleep_seconds: int | None = None
    ) -> TaskStatus:
        """Map persisted statuses retired or refined by newer releases.

        ``UNCLAIMED`` tasks were waiting to be scheduled, and now that agents
        spawn immediately the user's next reply is what starts them. Before
        ``SLEEPING`` existed, an active sleep was stored as ``WORKING`` plus
        ``sleep_seconds``; preserve those in-flight sleeps across an upgrade.
        """
        if value == "UNCLAIMED":
            return TaskStatus.NEEDS_ATTENTION
        if value == "WORKING" and sleep_seconds:
            return TaskStatus.SLEEPING
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

    @staticmethod
    def _migrate_maxed(d: dict[str, Any]) -> bool:
        """Read ``maxed``, deciding it for tasks persisted before the flag.

        Those pinned a maxed task to a model id in ``model``. Such a task was
        maxed exactly when that id was a max model of the backend it sat on:
        a stale pin of the *other* backend's, left by a switch from before
        switches translated pins, was ignored and the task ran its default,
        so it migrates as not maxed rather than waking up on the expensive
        model. Dropping the id is the point: a maxed task now follows the
        current max model like any other.
        """
        if "maxed" in d:
            return bool(d["maxed"])
        engine = d.get("engine") or DEFAULT_ENGINE
        pinned = _PINNED_MAX_MODELS.get(engine, _PINNED_MAX_MODELS[DEFAULT_ENGINE])
        return d.get("model") in pinned


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
    # Task alias at the moment this assistant reply was recorded. Persisting
    # the snapshot keeps asynchronously mirrored Gist comments accurate even
    # when the live task is assigned a different alias before the sync runs.
    task_alias: str | None = None
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
        if self.task_alias:
            d["task_alias"] = self.task_alias
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
            task_alias=d.get("task_alias") or None,
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
        task_alias: str | None = None,
    ) -> LogEntry:
        return cls(
            role=role,
            content=content,
            timestamp=datetime.now(timezone.utc).isoformat(),
            model=model,
            effort=effort,
            budget=budget,
            task_alias=task_alias,
            cost_usd=cost_usd,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_input_tokens=cache_read_input_tokens,
        )
