"""Which account pays for a spawn: a subscription plan, or an API key.

Neither backend records the billing source in its session log, so — like the
reasoning effort — it is resolved locally at spawn time and cached on the task.
Both CLIs pick their credentials the same way: an API key in the environment
wins, otherwise the OAuth login stored on disk is used. So the answer is an
API-key check followed by a lookup of the stored login's plan name.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
from pathlib import Path
from typing import Any

from ilan import config as cfg
from ilan.models import API, ENGINE_CLAUDE, ENGINE_CODEX

# Env vars that override the stored OAuth login for each CLI. ANTHROPIC_AUTH_TOKEN
# counts too: it is how a third-party gateway (via ANTHROPIC_BASE_URL) gets
# billed, which is likewise not the Claude subscription.
_CLAUDE_KEY_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
_CODEX_KEY_VARS = ("OPENAI_API_KEY",)

_CLAUDE_CREDENTIALS_FILE = Path("~/.claude/.credentials.json")
# On macOS, Claude Code keeps the same JSON in the login keychain instead of
# the file above.
_CLAUDE_KEYCHAIN_SERVICE = "Claude Code-credentials"
# A keychain read can block on an authorization dialog if the item's ACL does
# not already cover the `security` binary. Cap the wait so resolving the budget
# can never stall a spawn on a prompt nobody is there to answer.
_KEYCHAIN_TIMEOUT_SECONDS = 5

_CODEX_AUTH_FILE = Path("~/.codex/auth.json")
# Namespaced claim in the Codex id_token holding the ChatGPT plan.
_CODEX_AUTH_CLAIM = "https://api.openai.com/auth"


def _read_json_file(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.expanduser().read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _read_keychain_json() -> dict[str, Any] | None:
    try:
        proc = subprocess.run(
            ["security", "find-generic-password", "-s", _CLAUDE_KEYCHAIN_SERVICE, "-w"],
            capture_output=True,
            text=True,
            timeout=_KEYCHAIN_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _plan_label(plan: Any) -> str | None:
    """Normalize a vendor plan name (``"team"``) for display (``"Team"``)."""
    if not isinstance(plan, str) or not plan.strip():
        return None
    return plan.strip().title()


def _has_api_key(config_key: str, env_vars: tuple[str, ...]) -> bool:
    """Whether a spawn would authenticate with an API key.

    Mirrors the backends' own precedence: they inject the configured key into a
    copy of the current environment, so either source means key-based billing.
    """
    if str(cfg.load().get(config_key, "")).strip():
        return True
    return any(os.environ.get(var, "").strip() for var in env_vars)


def _claude_oauth() -> dict[str, Any] | None:
    """Return Claude Code's stored OAuth record, from file or keychain."""
    # The file is also where the MCP OAuth records live, so it can exist while
    # holding no login at all; fall through to the keychain on a miss rather
    # than treating a present-but-loginless file as the answer.
    for stored in (_read_json_file(_CLAUDE_CREDENTIALS_FILE), _read_keychain_json()):
        oauth = (stored or {}).get("claudeAiOauth")
        if isinstance(oauth, dict):
            return oauth
    return None


def _claude_budget() -> str | None:
    if _has_api_key("api-key-claude", _CLAUDE_KEY_VARS):
        return API
    return _plan_label((_claude_oauth() or {}).get("subscriptionType"))


def _codex_plan(tokens: dict[str, Any]) -> str | None:
    """Read the ChatGPT plan out of the id_token's payload segment."""
    id_token = tokens.get("id_token")
    if not isinstance(id_token, str):
        return None
    parts = id_token.split(".")
    if len(parts) < 2:
        return None
    padded = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        claims = json.loads(base64.urlsafe_b64decode(padded))
    except (ValueError, json.JSONDecodeError):
        return None
    auth = claims.get(_CODEX_AUTH_CLAIM) if isinstance(claims, dict) else None
    if not isinstance(auth, dict):
        return None
    return _plan_label(auth.get("chatgpt_plan_type"))


def _codex_budget() -> str | None:
    if _has_api_key("api-key-codex", _CODEX_KEY_VARS):
        return API
    auth = _read_json_file(_CODEX_AUTH_FILE)
    if auth is None:
        return None
    # `codex login` records which credential it stored; an API-key login also
    # leaves the key itself in this file.
    if auth.get("auth_mode") == "apikey" or str(auth.get("OPENAI_API_KEY") or "").strip():
        return API
    tokens = auth.get("tokens")
    return _codex_plan(tokens) if isinstance(tokens, dict) else None


def detect(engine: str) -> str | None:
    """Return the budget label for a spawn on *engine*, or ``None`` if unknown.

    ``None`` means the credential store could not be read (no login yet, or a
    keychain we are not allowed to open); callers omit the field rather than
    guess, so attribution is never wrong.
    """
    if engine == ENGINE_CLAUDE:
        return _claude_budget()
    if engine == ENGINE_CODEX:
        return _codex_budget()
    return None
