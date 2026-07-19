from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path


@dataclass
class ParsedResult:
    """Backend-agnostic view of one finished agent turn.

    Every backend parses its own native output format (a single JSON object
    for Claude Code, a JSONL event stream for Codex) down to this common
    shape so the reaper in ``Runner`` never has to know which CLI produced it.
    """

    session_id: str | None
    result_text: str
    is_error: bool
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cost_usd: float = 0.0


class Backend(ABC):
    """Interface every agent CLI (Claude Code, Codex, …) must implement.

    A backend owns everything CLI-specific: how to build the argv + env for a
    spawn, how to parse the process output, and how to locate/inspect the
    persisted session transcript. ``Runner`` stays backend-agnostic and drives
    these hooks.
    """

    @abstractmethod
    def build_command(
        self,
        prompt: str,
        model_override: str | None,
        *,
        resume: bool,
        session_id: str | None,
    ) -> tuple[list[str], dict[str, str]]:
        """Return ``(argv, env)`` for spawning the agent on *prompt*.

        *prompt* is already augmented (tmux instruction + status suffix) by
        the caller. When *resume* is true and *session_id* is set, the argv
        must resume that native session rather than starting fresh.
        """

    @abstractmethod
    def parse_output(self, out_path: Path) -> ParsedResult | None:
        """Parse the process output file, or ``None`` if it is unreadable."""

    @abstractmethod
    def find_session_log(self, session_id: str) -> Path | None:
        """Locate the on-disk session transcript for *session_id*."""

    @abstractmethod
    def last_assistant_model(self, log_path: Path) -> str | None:
        """Return the model id of the last assistant turn in the transcript."""
