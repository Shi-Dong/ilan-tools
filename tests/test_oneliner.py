"""Tests for ilan.oneliner — Luna-backed one-line summary of a task turn, and
the background thread that writes it off the server lock."""

from __future__ import annotations

import json
import subprocess
import threading
import time
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ilan import config as cfg_mod
from ilan import oneliner
from ilan.models import Task, TaskStatus
from ilan.oneliner import Summarizer
from ilan.server import IlanServer
from ilan.store import Store
from tests.helpers import running_server


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


# ── the background summarizer ────────────────────────────────────────────

@pytest.fixture()
def store(tmp_workdir: Path) -> Store:
    return Store(tmp_workdir)


@pytest.fixture()
def lock() -> threading.Lock:
    return threading.Lock()


@pytest.fixture()
def summarizer(store: Store, lock: threading.Lock) -> Summarizer:
    return Summarizer(store, lock)


def _finished(store: Store, name: str = "t", status: TaskStatus = TaskStatus.AGENT_FINISHED) -> Task:
    """A task whose turn the reaper has just recorded: status set, reply logged,
    summary cleared for the summarizer to fill in."""
    task = Task(name=name, prompt="p")
    store.append_log(name, "user", "please summarize this")
    store.append_log(name, "assistant", "Sure, all set.\n[STATUS: DONE]")
    task.set_status(status)
    store.put_task(task)
    return task


def _summary(store: Store, name: str) -> str | None:
    task = store.get_task(name)
    assert task is not None
    return task.summary_one_liner


class TestSummarizer:
    @pytest.mark.parametrize("status", [TaskStatus.AGENT_FINISHED, TaskStatus.NEEDS_ATTENTION])
    def test_writes_the_summary_of_the_last_exchange(
        self, store: Store, summarizer: Summarizer, status: TaskStatus,
    ) -> None:
        task = _finished(store, status=status)
        with patch("ilan.oneliner.generate_one_liner", return_value="Summary done.") as gen:
            summarizer.summarize(task)
        assert _summary(store, "t") == "Summary done."
        last_user, last_assistant = gen.call_args[0]
        assert last_user == "please summarize this"
        assert last_assistant.startswith("Sure, all set.")

    def test_the_model_is_called_with_the_lock_released(
        self, store: Store, lock: threading.Lock, summarizer: Summarizer,
    ) -> None:
        """The whole point: the seconds the model takes must not be seconds
        during which every listing, tail and reply waits."""
        task = _finished(store)
        held: list[bool] = []

        def gen(user: str, assistant: str) -> str:
            held.append(lock.locked())
            return "Summary done."

        with patch("ilan.oneliner.generate_one_liner", gen):
            summarizer.summarize(task)
        assert held == [False]
        assert _summary(store, "t") == "Summary done."

    def test_no_summary_leaves_the_field_empty(self, store: Store, summarizer: Summarizer) -> None:
        task = _finished(store)
        with patch("ilan.oneliner.generate_one_liner", return_value=None):
            summarizer.summarize(task)
        assert _summary(store, "t") is None

    def test_a_turn_without_a_logged_reply_asks_for_nothing(
        self, store: Store, summarizer: Summarizer,
    ) -> None:
        """An empty reply is never logged, so the log ends on the user's
        message; there is nothing to summarise and the model is not asked."""
        task = Task(name="quiet", prompt="p")
        store.append_log("quiet", "user", "anything?")
        task.set_status(TaskStatus.AGENT_FINISHED)
        store.put_task(task)
        with patch("ilan.oneliner._call_codex_cli") as cli, patch("ilan.oneliner._call_luna") as api:
            summarizer.summarize(task)
        cli.assert_not_called()
        api.assert_not_called()
        assert _summary(store, "quiet") is None

    def test_then_gets_the_task_carrying_the_summary(
        self, store: Store, lock: threading.Lock, summarizer: Summarizer,
    ) -> None:
        task = _finished(store)
        seen: list[tuple[str | None, bool]] = []
        with patch("ilan.oneliner.generate_one_liner", return_value="Summary done."):
            summarizer.summarize(task, then=lambda t: seen.append((t.summary_one_liner, lock.locked())))
        assert seen == [("Summary done.", False)], "then must see the summary, off the lock"

    def test_an_error_finish_takes_no_summary_but_then_still_runs(
        self, store: Store, summarizer: Summarizer,
    ) -> None:
        """The phone is told about errors too, and without a summary there is
        nothing to wait for."""
        task = _finished(store, status=TaskStatus.ERROR)
        seen: list[str] = []
        with patch("ilan.oneliner.generate_one_liner") as gen:
            summarizer.summarize(task, then=lambda t: seen.append(t.status.value))
        gen.assert_not_called()
        assert seen == ["ERROR"]

    def test_a_turn_the_task_has_moved_on_from_is_dropped(
        self, store: Store, summarizer: Summarizer,
    ) -> None:
        """A reply that landed before the job ran has made the summary stale;
        neither it nor the notification may go out for the old turn."""
        task = _finished(store)
        replied = store.get_task("t")
        assert replied is not None
        replied.set_status(TaskStatus.WORKING)
        store.put_task(replied)
        seen: list[Task] = []
        with patch("ilan.oneliner.generate_one_liner") as gen:
            summarizer.summarize(task, then=seen.append)
        gen.assert_not_called()
        assert seen == []
        assert _summary(store, "t") is None

    def test_a_reply_that_lands_during_the_model_call_wins(
        self, store: Store, summarizer: Summarizer,
    ) -> None:
        """The summary is written under the lock only if the turn is still
        the one it was asked for; the new turn's summary comes later."""
        task = _finished(store)
        seen: list[Task] = []

        def gen(user: str, assistant: str) -> str:
            replied = store.get_task("t")
            assert replied is not None
            replied.set_status(TaskStatus.WORKING)
            store.put_task(replied)
            return "Stale summary."

        with patch("ilan.oneliner.generate_one_liner", gen):
            summarizer.summarize(task, then=seen.append)
        assert _summary(store, "t") is None
        assert seen == []

    def test_a_deleted_task_is_dropped(self, store: Store, summarizer: Summarizer) -> None:
        task = _finished(store)
        store.delete_task("t")
        with patch("ilan.oneliner.generate_one_liner") as gen:
            summarizer.summarize(task, then=lambda t: pytest.fail("then ran for a deleted task"))
        gen.assert_not_called()

    def test_the_thread_writes_what_was_queued(self, store: Store, summarizer: Summarizer) -> None:
        task = _finished(store)
        with patch("ilan.oneliner.generate_one_liner", return_value="Summary done."):
            summarizer.start()
            try:
                summarizer.enqueue(task)
                deadline = time.monotonic() + 5
                while _summary(store, "t") is None and time.monotonic() < deadline:
                    time.sleep(0.02)
            finally:
                summarizer.stop()
        assert _summary(store, "t") == "Summary done."

    def test_a_crash_on_one_turn_does_not_stop_the_next(
        self, store: Store, summarizer: Summarizer,
    ) -> None:
        first = _finished(store, name="first")
        second = _finished(store, name="second")
        calls: list[str] = []

        def gen(user: str, assistant: str) -> str:
            calls.append(user)
            if len(calls) == 1:
                raise RuntimeError("boom")
            return "Summary done."

        with patch("ilan.oneliner.generate_one_liner", gen):
            summarizer.start()
            try:
                summarizer.enqueue(first)
                summarizer.enqueue(second)
                deadline = time.monotonic() + 5
                while _summary(store, "second") is None and time.monotonic() < deadline:
                    time.sleep(0.02)
            finally:
                summarizer.stop()
        assert _summary(store, "second") == "Summary done."
        assert _summary(store, "first") is None


class TestEnqueueMissing:
    def test_schedules_every_finished_task_without_a_summary(
        self, store: Store, summarizer: Summarizer,
    ) -> None:
        _finished(store, name="fin-none")
        _finished(store, name="na-none", status=TaskStatus.NEEDS_ATTENTION)
        has = _finished(store, name="fin-has")
        has.summary_one_liner = "Already there."
        store.put_task(has)
        _finished(store, name="err-none", status=TaskStatus.ERROR)
        _finished(store, name="working-none", status=TaskStatus.WORKING)
        _finished(store, name="done-none", status=TaskStatus.DONE)

        assert sorted(summarizer.enqueue_missing()) == ["fin-none", "na-none"]
        assert summarizer._queue.qsize() == 2

    def test_a_summary_lost_to_a_restart_is_written_when_the_server_starts(
        self, tmp_workdir: Path, tmp_config: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The reaper records a finish and the summary follows a few seconds
        later, so a server stopped in between leaves a finished task with no
        summary on disk. The next server writes it — and tells no phone,
        since the finish happened while no server was up. A task that has
        its summary is left alone."""
        cfg_mod.save({**cfg_mod.DEFAULTS, "workdir": str(tmp_workdir)})
        asked: list[str] = []

        def gen(user: str, assistant: str) -> str:
            asked.append(user)
            return "Written at startup."

        monkeypatch.setattr("ilan.oneliner.generate_one_liner", gen)
        server = IlanServer()
        announced: list[Task] = []
        server.push.notify_finished = lambda task: announced.append(task) or True  # type: ignore[method-assign]
        _finished(server.store, name="lost")
        kept = _finished(server.store, name="kept")
        kept.summary_one_liner = "Kept."
        server.store.put_task(kept)

        with running_server(server):
            deadline = time.monotonic() + 5
            while _summary(server.store, "lost") is None and time.monotonic() < deadline:
                time.sleep(0.02)
        assert _summary(server.store, "lost") == "Written at startup."
        assert _summary(server.store, "kept") == "Kept."
        assert asked == ["please summarize this"], "only the task without a summary was summarised"
        assert announced == []
