"""Tests for ilan.oneliner — Haiku-backed one-line summary of a task turn."""

from __future__ import annotations

import json
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ilan import oneliner


@pytest.fixture()
def with_api_key(tmp_config: Path) -> None:
    import ilan.config as cfg_mod
    cfg_mod.save({**cfg_mod.DEFAULTS, "api-key": "sk-test-key"})


@pytest.fixture()
def without_api_key(tmp_config: Path) -> None:
    import ilan.config as cfg_mod
    cfg_mod.save({**cfg_mod.DEFAULTS, "api-key": ""})


def _mock_response(text: str) -> MagicMock:
    """Build a fake urlopen context manager yielding a Haiku-style JSON body."""
    payload = json.dumps({
        "content": [{"type": "text", "text": text}],
    }).encode()
    resp = MagicMock()
    resp.read.return_value = payload
    cm = MagicMock()
    cm.__enter__.return_value = resp
    cm.__exit__.return_value = False
    return cm


class TestGenerateOneLiner:
    def test_returns_none_without_api_key(self, without_api_key: None) -> None:
        result = oneliner.generate_one_liner("user msg", "assistant reply")
        assert result is None

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
        # The request body should carry the newest Haiku model id.
        req = mock_open.call_args[0][0]
        body = json.loads(req.data.decode())
        assert body["model"] == oneliner.HAIKU_MODEL
        assert body["messages"][0]["role"] == "user"

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
        assert req.get_header("X-api-key") == "sk-test-key"
        assert req.get_header("Anthropic-version") == oneliner.ANTHROPIC_API_VERSION

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
        assert len(body["messages"][0]["content"]) < len(long_msg) * 2
        assert "[truncated]" in body["messages"][0]["content"]
