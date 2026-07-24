"""Tests for ilan.oneliner — Luna-backed one-line summary of a task turn."""

from __future__ import annotations

import json
import subprocess
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ilan import oneliner


@pytest.fixture()
def with_api_key(tmp_config: Path) -> None:
    import ilan.config as cfg_mod
    cfg_mod.save({**cfg_mod.DEFAULTS, "api-key-codex": "sk-test-key"})


@pytest.fixture()
def without_api_key(tmp_config: Path) -> None:
    import ilan.config as cfg_mod
    cfg_mod.save({**cfg_mod.DEFAULTS, "api-key-codex": ""})


def _mock_response(text: str) -> MagicMock:
    """Build a fake urlopen context manager yielding an OpenAI-style JSON body."""
    payload = json.dumps({
        "choices": [{"message": {"role": "assistant", "content": text}}],
    }).encode()
    resp = MagicMock()
    resp.read.return_value = payload
    cm = MagicMock()
    cm.__enter__.return_value = resp
    cm.__exit__.return_value = False
    return cm


def _codex_stream(text: str) -> str:
    """Render a minimal ``codex exec --json`` event stream carrying *text*."""
    return "\n".join([
        json.dumps({"type": "thread.started", "thread_id": "t1"}),
        json.dumps({
            "type": "item.completed",
            "item": {"type": "agent_message", "text": text},
        }),
        json.dumps({"type": "turn.completed", "usage": {}}),
    ])


def _mock_cli_result(stdout: str, returncode: int = 0, stderr: str = "") -> MagicMock:
    """Build a fake ``subprocess.run`` CompletedProcess-like result."""
    result = MagicMock()
    result.stdout = stdout
    result.stderr = stderr
    result.returncode = returncode
    return result


class TestGenerateOneLiner:
    def test_returns_none_for_empty_assistant(self, with_api_key: None) -> None:
        with patch("urllib.request.urlopen") as mock_open:
            result = oneliner.generate_one_liner("hi", "")
        assert result is None
        mock_open.assert_not_called()

    def test_happy_path(self, with_api_key: None) -> None:
        with patch(
            "urllib.request.urlopen",
            return_value=_mock_response("Wrote a feature flag and pushed PR."),
        ) as mock_open:
            result = oneliner.generate_one_liner("Please add a flag.", "Done.")
        assert result == "Wrote a feature flag and pushed PR."
        # The request body should carry the Luna model id.
        req = mock_open.call_args[0][0]
        body = json.loads(req.data.decode())
        assert body["model"] == oneliner.ONELINER_MODEL
        assert body["messages"][0]["role"] == "system"
        assert body["messages"][1]["role"] == "user"
        # Luna rejects the legacy `max_tokens` parameter.
        assert "max_tokens" not in body
        assert body["max_completion_tokens"] > 0

    def test_clips_to_20_words(self, with_api_key: None) -> None:
        long_reply = " ".join(f"word{i}" for i in range(40))
        with patch(
            "urllib.request.urlopen", return_value=_mock_response(long_reply),
        ):
            result = oneliner.generate_one_liner("u", "a")
        assert result is not None
        # The trimmed result keeps at most 20 words plus an ellipsis marker.
        kept = result.rstrip("…").split()
        assert len(kept) <= 20

    def test_collapses_whitespace(self, with_api_key: None) -> None:
        with patch(
            "urllib.request.urlopen",
            return_value=_mock_response("multi\n\n   line\treply  "),
        ):
            result = oneliner.generate_one_liner("u", "a")
        assert result == "multi line reply"

    def test_network_error_returns_none(self, with_api_key: None) -> None:
        with patch(
            "urllib.request.urlopen",
            side_effect=urllib.error.URLError("boom"),
        ):
            result = oneliner.generate_one_liner("u", "a")
        assert result is None

    def test_timeout_returns_none(self, with_api_key: None) -> None:
        with patch("urllib.request.urlopen", side_effect=TimeoutError):
            result = oneliner.generate_one_liner("u", "a")
        assert result is None

    def test_unexpected_exception_returns_none(self, with_api_key: None) -> None:
        with patch("urllib.request.urlopen", side_effect=RuntimeError("oops")):
            result = oneliner.generate_one_liner("u", "a")
        assert result is None

    def test_empty_text_block_returns_none(self, with_api_key: None) -> None:
        with patch(
            "urllib.request.urlopen", return_value=_mock_response(""),
        ):
            result = oneliner.generate_one_liner("u", "a")
        assert result is None

    def test_request_includes_api_key_header(self, with_api_key: None) -> None:
        with patch(
            "urllib.request.urlopen", return_value=_mock_response("hi"),
        ) as mock_open:
            oneliner.generate_one_liner("u", "a")
        req = mock_open.call_args[0][0]
        assert req.get_header("Authorization") == "Bearer sk-test-key"
        assert req.full_url == oneliner.OPENAI_API_URL

    def test_no_choices_returns_none(self, with_api_key: None) -> None:
        resp = MagicMock()
        resp.read.return_value = json.dumps({"choices": []}).encode()
        cm = MagicMock()
        cm.__enter__.return_value = resp
        cm.__exit__.return_value = False
        with patch("urllib.request.urlopen", return_value=cm):
            assert oneliner.generate_one_liner("u", "a") is None

    def test_truncates_long_inputs(self, with_api_key: None) -> None:
        """Pathologically long messages must be clipped before sending."""
        long_msg = "x" * (oneliner._MAX_INPUT_CHARS * 5)
        with patch(
            "urllib.request.urlopen", return_value=_mock_response("ok"),
        ) as mock_open:
            oneliner.generate_one_liner(long_msg, long_msg)
        req = mock_open.call_args[0][0]
        body = json.loads(req.data.decode())
        # Sent prompt should be far shorter than the raw inputs combined.
        assert len(body["messages"][1]["content"]) < len(long_msg) * 2
        assert "[truncated]" in body["messages"][1]["content"]


class TestCodexCliFallback:
    """Without an api-key-codex, generation falls back to the local `codex` CLI."""

    def test_uses_cli_and_never_calls_http(self, without_api_key: None) -> None:
        with patch(
            "subprocess.run",
            return_value=_mock_cli_result(
                _codex_stream("Wrote a feature flag and pushed PR.")
            ),
        ) as mock_run, patch("urllib.request.urlopen") as mock_open:
            result = oneliner.generate_one_liner("Please add a flag.", "Done.")
        assert result == "Wrote a feature flag and pushed PR."
        mock_open.assert_not_called()
        # The CLI is invoked non-interactively against the Luna model.
        argv = mock_run.call_args[0][0]
        assert argv[:2] == ["codex", "exec"]
        assert oneliner.ONELINER_MODEL in argv
        # codex has no --system-prompt flag: the instructions ride on stdin.
        assert oneliner.SYSTEM_PROMPT in mock_run.call_args[1]["input"]

    def test_cli_is_not_granted_a_sandbox_bypass(self, without_api_key: None) -> None:
        """Summarising text needs no tools, so the agent stays sandboxed."""
        with patch(
            "subprocess.run", return_value=_mock_cli_result(_codex_stream("ok")),
        ) as mock_run:
            oneliner.generate_one_liner("u", "a")
        argv = mock_run.call_args[0][0]
        assert not any("bypass" in arg for arg in argv)

    def test_non_json_cli_noise_is_ignored(self, without_api_key: None) -> None:
        """Stray non-JSON lines in the stream must not break parsing."""
        noisy = "warning: something\n" + _codex_stream("Fixed the flaky test.")
        with patch("subprocess.run", return_value=_mock_cli_result(noisy)):
            assert oneliner.generate_one_liner("u", "a") == "Fixed the flaky test."

    def test_stream_without_agent_message_returns_none(
        self, without_api_key: None
    ) -> None:
        stream = json.dumps({"type": "thread.started", "thread_id": "t1"})
        with patch("subprocess.run", return_value=_mock_cli_result(stream)):
            assert oneliner.generate_one_liner("u", "a") is None

    def test_cli_output_is_trimmed_to_20_words(self, without_api_key: None) -> None:
        long_reply = " ".join(f"word{i}" for i in range(40))
        with patch(
            "subprocess.run", return_value=_mock_cli_result(_codex_stream(long_reply)),
        ):
            result = oneliner.generate_one_liner("u", "a")
        assert result is not None
        kept = result.rstrip("…").split()
        assert len(kept) <= 20

    def test_cli_whitespace_is_collapsed(self, without_api_key: None) -> None:
        with patch(
            "subprocess.run",
            return_value=_mock_cli_result(_codex_stream("multi   line\treply  ")),
        ):
            result = oneliner.generate_one_liner("u", "a")
        assert result == "multi line reply"

    def test_nonzero_exit_returns_none(self, without_api_key: None) -> None:
        with patch(
            "subprocess.run",
            return_value=_mock_cli_result("", returncode=1, stderr="boom"),
        ):
            result = oneliner.generate_one_liner("u", "a")
        assert result is None

    def test_missing_binary_returns_none(self, without_api_key: None) -> None:
        with patch("subprocess.run", side_effect=FileNotFoundError("no codex")):
            result = oneliner.generate_one_liner("u", "a")
        assert result is None

    def test_cli_timeout_returns_none(self, without_api_key: None) -> None:
        with patch(
            "subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="codex", timeout=60),
        ):
            result = oneliner.generate_one_liner("u", "a")
        assert result is None

    def test_empty_cli_output_returns_none(self, without_api_key: None) -> None:
        with patch("subprocess.run", return_value=_mock_cli_result("   ")):
            result = oneliner.generate_one_liner("u", "a")
        assert result is None
