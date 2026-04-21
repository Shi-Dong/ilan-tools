"""Tests for :mod:`ilan.summarize`."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from ilan import summarize as sm
from ilan.models import Task, TaskStatus
from ilan.store import Store


def _seed_task_with_log(workdir: Path, name: str = "my-task") -> Task:
    store = Store(workdir)
    now = datetime.now(timezone.utc).isoformat()
    task = Task(
        name=name,
        prompt="Do the thing.",
        status=TaskStatus.AGENT_FINISHED,
        created_at=now,
        status_changed_at=now,
        task_hash="deadbeef",
    )
    store.put_task(task)
    store.append_log(name, "user", "Do the thing.")
    store.append_log(name, "assistant", "I did the thing. PR: https://github.com/a/b/pull/1")
    return task


def test_summarize_writes_file_and_caches(
    tmp_workdir: Path,
    tmp_config: Path,
    env_with_mock_claude,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """First run generates a summary; second run without log changes reuses it."""
    import ilan.config as cfg_mod

    cfg_mod.save({**cfg_mod.DEFAULTS, "workdir": str(tmp_workdir)})
    monkeypatch.setenv("MOCK_CLAUDE_RESPONSE", "## Summary\n\nAll good.\n")

    task = _seed_task_with_log(tmp_workdir)

    result1 = sm.summarize(task.name)
    assert result1.summary_path.exists()
    assert result1.reused is False
    assert "All good." in result1.summary_text
    body = result1.summary_path.read_text()
    assert result1.summary_text == body

    # Metadata sidecar should exist next to the summary.
    meta = sm.meta_path_for(Store(tmp_workdir).log_path(task.name))
    assert meta.exists()

    # Second run, log unchanged → should reuse cached summary (claude not
    # re-invoked; we prove this by changing the mock response and verifying
    # the file stays the same).
    monkeypatch.setenv("MOCK_CLAUDE_RESPONSE", "## Summary\n\nDIFFERENT.\n")
    result2 = sm.summarize(task.name)
    assert result2.reused is True
    assert result2.summary_text == body
    assert result2.summary_path.read_text() == body


def test_summarize_regenerates_after_log_change(
    tmp_workdir: Path,
    tmp_config: Path,
    env_with_mock_claude,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ilan.config as cfg_mod

    cfg_mod.save({**cfg_mod.DEFAULTS, "workdir": str(tmp_workdir)})
    monkeypatch.setenv("MOCK_CLAUDE_RESPONSE", "first summary")

    task = _seed_task_with_log(tmp_workdir)
    first = sm.summarize(task.name)
    assert first.reused is False

    # Append a new log entry so the log hash changes.
    Store(tmp_workdir).append_log(task.name, "user", "Also check wandb.")

    monkeypatch.setenv("MOCK_CLAUDE_RESPONSE", "second summary")
    second = sm.summarize(task.name)
    assert second.reused is False
    assert second.summary_text.startswith("second summary")
    assert second.summary_path.read_text().startswith("second summary")


def test_summarize_unknown_task_raises(
    tmp_workdir: Path,
    tmp_config: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ilan.config as cfg_mod

    cfg_mod.save({**cfg_mod.DEFAULTS, "workdir": str(tmp_workdir)})
    with pytest.raises(ValueError, match="not found"):
        sm.summarize("does-not-exist")


def test_summarize_no_logs_raises(
    tmp_workdir: Path,
    tmp_config: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ilan.config as cfg_mod

    cfg_mod.save({**cfg_mod.DEFAULTS, "workdir": str(tmp_workdir)})

    store = Store(tmp_workdir)
    now = datetime.now(timezone.utc).isoformat()
    store.put_task(Task(name="empty-task", prompt="x", created_at=now, status_changed_at=now))

    with pytest.raises(ValueError, match="no log entries"):
        sm.summarize("empty-task")


def test_summary_path_lives_next_to_log(tmp_workdir: Path) -> None:
    """The summary file must live alongside the task's JSONL log."""
    log_path = Store(tmp_workdir).log_path("foo")
    summary = sm.summary_path_for(log_path)
    meta = sm.meta_path_for(log_path)
    assert summary.parent == log_path.parent
    assert meta.parent == log_path.parent
    assert summary.name == "foo.summary.md"
    assert meta.name == "foo.summary.meta.json"


def test_mock_claude_handles_summarize_flags(mock_claude_bin: Path) -> None:
    """Mock claude accepts the flags ilan.summarize passes."""
    import subprocess

    proc = subprocess.run(
        [str(mock_claude_bin), "-p", "hello",
         "--model", "sonnet", "--effort", "medium",
         "--dangerously-skip-permissions",
         "--output-format", "json"],
        capture_output=True, text=True, env={
            **__import__("os").environ,
            "MOCK_CLAUDE_RESPONSE": "test summary body",
        },
    )
    assert proc.returncode == 0
    import json as _json
    payload = _json.loads(proc.stdout)
    assert payload["result"] == "test summary body"
