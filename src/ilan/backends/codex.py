from __future__ import annotations

import json
import os
from pathlib import Path

from ilan import config as cfg
from ilan.backends.base import Backend, ParsedResult

_CODEX_STATIC_FLAGS = [
    "--json",
    "--skip-git-repo-check",
    "--dangerously-bypass-approvals-and-sandbox",
]

# Strongest Codex model currently available; used when a task has no per-task
# model override. ``gpt-5.6-sol`` is OpenAI's flagship (the ``gpt-5.6`` alias
# routes to it) and handles coding/tool-heavy workflows.
_CODEX_DEFAULT_MODEL = "gpt-5.6-sol"


class CodexBackend(Backend):
    """Backend for OpenAI's ``codex exec`` CLI.

    Codex streams a JSONL event log to stdout (``--json``): a ``thread.started``
    event carries the session id (``thread_id``), ``item.completed`` events of
    type ``agent_message`` carry the assistant text, and a ``turn.completed``
    event carries token usage. Resuming a thread (``codex exec resume <id>``)
    re-emits the *same* ``thread_id``, so a task's session survives switching
    backends away and back with no context loss.
    """

    def build_command(
        self,
        prompt: str,
        model_override: str | None,
        *,
        resume: bool,
        session_id: str | None,
    ) -> tuple[list[str], dict[str, str]]:
        cmd = ["codex", "exec"]
        if resume and session_id:
            cmd += ["resume", session_id]
        cmd += list(_CODEX_STATIC_FLAGS)
        cmd += ["--model", model_override or _CODEX_DEFAULT_MODEL]
        cmd.append(prompt)

        env = os.environ.copy()
        api_key = str(cfg.load().get("api-key-codex", "")).strip()
        if api_key:
            env["OPENAI_API_KEY"] = api_key
        return cmd, env

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
                usage = event.get("usage") or {}
                total_in = usage.get("input_tokens", 0)
                cached = usage.get("cached_input_tokens", 0)
                # Report uncached input separately from cache reads so the
                # accounting matches the Claude backend's disjoint convention.
                input_tokens += max(0, total_in - cached)
                cache_read += cached
                output_tokens += usage.get("output_tokens", 0)
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
