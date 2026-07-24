from __future__ import annotations

import json
import os
from pathlib import Path

from ilan import config as cfg
from ilan.backends.base import Backend, ParsedResult

_CLAUDE_STATIC_FLAGS = [
    "--dangerously-skip-permissions",
    "--output-format", "json",
]


def _effective_model(model_override: str | None = None) -> str:
    """Return the model string passed to ``claude --model`` for a spawn.

    *model_override* (a task's ``model``, set via ``ilan max``) takes
    precedence over the configured default; ``None`` falls back to config.
    """
    conf = cfg.load()
    return model_override or str(conf["model-claude"])


def _claude_flags(model_override: str | None = None) -> list[str]:
    """Build claude flags, reading model/effort from config at call time."""
    conf = cfg.load()
    return [
        *_CLAUDE_STATIC_FLAGS,
        "--model", _effective_model(model_override),
        "--effort", str(conf.get("effort", "xhigh")),
    ]


def last_assistant_model(log_path: Path) -> str | None:
    """Return the ``message.model`` of the last assistant entry in a Claude
    Code session log (JSONL), or ``None`` if no such entry exists.

    Claude Code writes one JSON object per line; assistant turns carry
    ``{"message": {"role": "assistant", "model": "...", ...}}``. We scan
    from the end so the cost stays bounded regardless of log length.
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
        message = entry.get("message")
        if not isinstance(message, dict):
            continue
        if message.get("role") != "assistant":
            continue
        model = message.get("model")
        if isinstance(model, str) and model:
            return model
    return None


class ClaudeBackend(Backend):
    """Backend for Anthropic's ``claude -p`` CLI (Claude Code)."""

    def build_command(
        self,
        model_override: str | None,
        *,
        resume: bool,
        session_id: str | None,
    ) -> tuple[list[str], dict[str, str]]:
        # No positional prompt: `claude -p` reads the prompt from stdin.
        cmd = ["claude", "-p", *_claude_flags(model_override)]
        if resume and session_id:
            cmd.extend(["--resume", session_id])

        env = os.environ.copy()
        api_key = str(cfg.load().get("api-key-claude", "")).strip()
        if api_key:
            env["ANTHROPIC_API_KEY"] = api_key
        return cmd, env

    def build_attach_command(
        self, session_id: str, model_override: str | None
    ) -> list[str]:
        conf = cfg.load()
        return [
            "claude",
            "--resume", session_id,
            "--dangerously-skip-permissions",
            "--model", _effective_model(model_override),
            "--effort", str(conf.get("effort", "xhigh")),
        ]

    def parse_output(self, out_path: Path) -> ParsedResult | None:
        try:
            with open(out_path) as f:
                result = json.load(f)
        except (json.JSONDecodeError, FileNotFoundError):
            return None
        usage = result.get("usage") or {}
        return ParsedResult(
            session_id=result.get("session_id"),
            result_text=result.get("result", ""),
            is_error=bool(result.get("is_error")),
            input_tokens=usage.get("input_tokens", 0),
            output_tokens=usage.get("output_tokens", 0),
            cache_read_input_tokens=usage.get("cache_read_input_tokens", 0),
            cost_usd=result.get("total_cost_usd", 0.0),
        )

    def find_session_log(self, session_id: str) -> Path | None:
        """Locate the Claude Code session log for the given session ID."""
        claude_dir = Path.home() / ".claude" / "projects"
        if not claude_dir.is_dir():
            return None
        matches = list(claude_dir.glob(f"*/{session_id}.jsonl"))
        return matches[0] if matches else None

    def last_assistant_model(self, log_path: Path) -> str | None:
        return last_assistant_model(log_path)
