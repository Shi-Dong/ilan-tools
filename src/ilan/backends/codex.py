from __future__ import annotations

import json
import os
from pathlib import Path

from ilan import config as cfg
from ilan.backends.base import Backend, ParsedResult, TokenUsage
from ilan.models import is_fable_model

_CODEX_STATIC_FLAGS = [
    "--json",
    "--skip-git-repo-check",
    "--dangerously-bypass-approvals-and-sandbox",
]


def _token_usage(raw: object) -> TokenUsage | None:
    """Normalize Codex counters into disjoint categories."""
    if not isinstance(raw, dict):
        return None
    total_input = raw.get("input_tokens", 0)
    cached_input = raw.get("cached_input_tokens", 0)
    output = raw.get("output_tokens", 0)
    values = (total_input, cached_input, output)
    if any(
        not isinstance(value, int) or isinstance(value, bool) or value < 0
        for value in values
    ):
        return None
    return TokenUsage(
        input_tokens=max(0, total_input - cached_input),
        output_tokens=output,
        cache_read_input_tokens=cached_input,
    )


def _usage_delta(start: TokenUsage, end: TokenUsage) -> TokenUsage:
    """Subtract cumulative counters, treating a counter reset as a fresh run."""
    start_values = (
        start.input_tokens,
        start.output_tokens,
        start.cache_read_input_tokens,
    )
    end_values = (
        end.input_tokens,
        end.output_tokens,
        end.cache_read_input_tokens,
    )
    if any(after < before for before, after in zip(start_values, end_values)):
        return end
    return TokenUsage(
        input_tokens=end.input_tokens - start.input_tokens,
        output_tokens=end.output_tokens - start.output_tokens,
        cache_read_input_tokens=(
            end.cache_read_input_tokens - start.cache_read_input_tokens
        ),
    )


class CodexBackend(Backend):
    """Backend for OpenAI's ``codex exec`` CLI.

    Codex streams a JSONL event log to stdout (``--json``): a ``thread.started``
    event carries the session id (``thread_id``), ``item.completed`` events of
    type ``agent_message`` carry the assistant text, and a ``turn.completed``
    event carries cumulative thread token usage. Resuming a thread
    (``codex exec resume <id>``) re-emits the *same* ``thread_id``, so a task's
    session survives switching backends away and back with no context loss.
    """

    def build_command(
        self,
        model_override: str | None,
        *,
        resume: bool,
        session_id: str | None,
    ) -> tuple[list[str], dict[str, str]]:
        conf = cfg.load()
        cmd = ["codex", "exec"]
        if resume and session_id:
            cmd += ["resume", session_id]
        cmd += list(_CODEX_STATIC_FLAGS)
        # Mirror the Claude backend's --effort flag. Codex has no dedicated
        # CLI flag; the knob is the model_reasoning_effort config key. The
        # value is quoted so it parses as a TOML string.
        effort = str(conf.get("effort", "max")).strip()
        if effort:
            cmd += ["-c", f'model_reasoning_effort="{effort}"']
        # A task's ``model`` override only makes sense for the engine that set
        # it. ``ilan max`` pins a task to Fable (a Claude-only model); if such a
        # task is later switched to codex the stale override would spawn
        # ``codex exec --model claude-fable-5``, which codex can't load. Ignore
        # any Claude-only override here and fall back to the codex default.
        model = None if is_fable_model(model_override) else model_override
        cmd += ["--model", model or str(conf["model-codex"])]
        # `-` makes codex read the prompt from stdin.
        cmd.append("-")

        env = os.environ.copy()
        api_key = str(conf.get("api-key-codex", "")).strip()
        env.pop("OPENAI_API_KEY", None)
        if not cfg.parse_bool(conf.get("api-key-mode", False)):
            api_key = ""
        if api_key:
            env["OPENAI_API_KEY"] = api_key
        return cmd, env

    def build_attach_command(
        self, session_id: str, model_override: str | None
    ) -> list[str]:
        # Same Fable guard as build_command: a Claude-only override must not
        # reach `codex --model`.
        model = None if is_fable_model(model_override) else model_override
        return [
            "codex", "resume", session_id,
            "--dangerously-bypass-approvals-and-sandbox",
            "--model", model or str(cfg.load()["model-codex"]),
        ]

    def parse_output(self, out_path: Path) -> ParsedResult | None:
        """Parse Codex's JSONL event stream into a ``ParsedResult``.

        Returns ``None`` only when the stream is unreadable or contains no
        parseable events at all (treated as a hard failure by ``Runner``).
        """
        try:
            with open(out_path) as f:
                raw_lines = f.readlines()
        except FileNotFoundError:
            return None

        session_id: str | None = None
        result_text = ""
        is_error = False
        input_tokens = 0
        output_tokens = 0
        cache_read = 0
        saw_event = False

        for raw in raw_lines:
            line = raw.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            saw_event = True
            etype = event.get("type", "")

            if etype == "thread.started":
                tid = event.get("thread_id")
                if isinstance(tid, str) and tid:
                    session_id = tid
            elif etype == "item.completed":
                item = event.get("item") or {}
                if item.get("type") == "agent_message":
                    text = item.get("text")
                    if isinstance(text, str):
                        result_text = text
            elif etype == "turn.completed":
                # Codex reports a cumulative snapshot for the resumed thread,
                # not a delta for this invocation. Keep the latest snapshot;
                # Runner replaces it with the transcript-derived turn delta.
                usage = _token_usage(event.get("usage") or {})
                if usage is not None:
                    input_tokens = usage.input_tokens
                    cache_read = usage.cache_read_input_tokens
                    output_tokens = usage.output_tokens
            elif etype in ("error", "turn.failed", "thread.error"):
                is_error = True

        if not saw_event:
            return None

        return ParsedResult(
            session_id=session_id,
            result_text=result_text,
            is_error=is_error,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_input_tokens=cache_read,
            cost_usd=0.0,
        )

    def find_session_log(self, session_id: str) -> Path | None:
        """Locate the Codex rollout transcript for the given session id.

        Codex writes ``~/.codex/sessions/YYYY/MM/DD/rollout-<ts>-<uuid>.jsonl``
        where ``<uuid>`` is the ``thread_id`` we store as the session id.
        """
        sessions_dir = Path.home() / ".codex" / "sessions"
        if not sessions_dir.is_dir():
            return None
        matches = list(sessions_dir.glob(f"*/*/*/rollout-*-{session_id}.jsonl"))
        return matches[0] if matches else None

    def last_assistant_model(self, log_path: Path) -> str | None:
        """Return the model recorded for the last turn in a rollout transcript.

        Codex records the active model on each ``turn_context`` entry
        (``payload.model``); we scan from the end so cost stays bounded.
        """
        try:
            with open(log_path, "rb") as f:
                lines = f.readlines()
        except OSError:
            return None
        for raw in reversed(lines):
            line = raw.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("type") != "turn_context":
                continue
            payload = entry.get("payload")
            if not isinstance(payload, dict):
                continue
            model = payload.get("model")
            if isinstance(model, str) and model:
                return model
        return None

    def last_turn_token_usage(self, log_path: Path) -> TokenUsage | None:
        """Return usage between the last task's start and completion.

        ``codex exec resume --json`` emits lifetime thread totals in
        ``turn.completed.usage``. The rollout transcript also records
        ``task_started`` / ``task_complete`` boundaries and cumulative
        ``token_count`` snapshots, so subtracting the boundary totals yields
        the counters for the single invocation that produced the Ilan reply.
        """
        try:
            with open(log_path) as f:
                lines = f.readlines()
        except OSError:
            return None

        last_usage = TokenUsage()
        turn_start: TokenUsage | None = None
        turn_end: TokenUsage | None = None
        completed_usage: TokenUsage | None = None
        turn_open = False

        for raw in lines:
            try:
                entry = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if entry.get("type") != "event_msg":
                continue
            payload = entry.get("payload")
            if not isinstance(payload, dict):
                continue
            event_type = payload.get("type")

            if event_type == "task_started":
                turn_start = last_usage
                turn_end = last_usage
                completed_usage = None
                turn_open = True
            elif event_type == "token_count":
                info = payload.get("info")
                if not isinstance(info, dict):
                    continue
                usage = _token_usage(info.get("total_token_usage"))
                if usage is None:
                    continue
                last_usage = usage
                if turn_open:
                    turn_end = usage
            elif event_type == "task_complete" and turn_open:
                if turn_start is not None and turn_end is not None:
                    completed_usage = _usage_delta(turn_start, turn_end)
                turn_open = False

        # Never reuse the preceding task's usage for an incomplete latest task.
        return None if turn_open else completed_usage

    def last_assistant_token_usage(self, log_path: Path) -> TokenUsage | None:
        """Return ``last_token_usage`` for the final agent message.

        A Codex task can make many model calls for reasoning and tool use.
        ``agent_message`` identifies calls that emitted assistant text, and
        the following ``token_count`` carries that call's ``last_token_usage``.
        The last such pair before ``task_complete`` produced Ilan's final reply.
        """
        try:
            with open(log_path) as f:
                lines = f.readlines()
        except OSError:
            return None

        pending_message = False
        message_usage: TokenUsage | None = None
        completed_usage: TokenUsage | None = None
        turn_open = False

        for raw in lines:
            try:
                entry = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if entry.get("type") != "event_msg":
                continue
            payload = entry.get("payload")
            if not isinstance(payload, dict):
                continue
            event_type = payload.get("type")

            if event_type == "task_started":
                pending_message = False
                message_usage = None
                completed_usage = None
                turn_open = True
            elif event_type == "agent_message" and turn_open:
                pending_message = True
            elif event_type == "token_count" and turn_open:
                info = payload.get("info")
                if pending_message and isinstance(info, dict):
                    usage = _token_usage(info.get("last_token_usage"))
                    if usage is not None:
                        message_usage = usage
                pending_message = False
            elif event_type == "task_complete" and turn_open:
                completed_usage = message_usage
                turn_open = False

        return None if turn_open else completed_usage
