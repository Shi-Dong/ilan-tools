from __future__ import annotations

import json
import shutil
from pathlib import Path

from .models import LogEntry, Task


class Store:
    """Persists tasks (JSON) and per-task conversation logs (JSONL)."""

    def __init__(self, workdir: Path) -> None:
        self.workdir = workdir
        self._tasks_file = workdir / "tasks.json"
        self._logs_dir = workdir / "logs"
        self._output_dir = workdir / "output"

        for d in (workdir, self._logs_dir, self._output_dir):
            d.mkdir(parents=True, exist_ok=True)

    # ── task CRUD ────────────────────────────────────────────────────

    def load_tasks(self) -> dict[str, Task]:
        if not self._tasks_file.exists():
            return {}
        with open(self._tasks_file) as f:
            data = json.load(f)
        return {name: Task.from_dict(t) for name, t in data.items()}

    def save_tasks(self, tasks: dict[str, Task]) -> None:
        with open(self._tasks_file, "w") as f:
            json.dump({n: t.to_dict() for n, t in tasks.items()}, f, indent=2)

    def get_task(self, name: str) -> Task | None:
        return self.load_tasks().get(name)

    def put_task(self, task: Task) -> None:
        tasks = self.load_tasks()
        tasks[task.name] = task
        self.save_tasks(tasks)

    def delete_task(self, name: str) -> None:
        tasks = self.load_tasks()
        tasks.pop(name, None)
        self.save_tasks(tasks)
        self.log_path(name).unlink(missing_ok=True)
        self.output_path(name).unlink(missing_ok=True)

    def delete_all(self) -> None:
        """Remove tasks, logs, and output but preserve workdir-level files (PID, config, server log)."""
        self._tasks_file.unlink(missing_ok=True)
        if self._logs_dir.exists():
            shutil.rmtree(self._logs_dir)
        if self._output_dir.exists():
            shutil.rmtree(self._output_dir)
        self._logs_dir.mkdir(parents=True, exist_ok=True)
        self._output_dir.mkdir(parents=True, exist_ok=True)

    # ── conversation logs ────────────────────────────────────────────

    def log_path(self, task_name: str) -> Path:
        return self._logs_dir / f"{task_name}.jsonl"

    def append_log(self, task_name: str, role: str, content: str) -> None:
        entry = LogEntry.now(role, content)
        with open(self.log_path(task_name), "a") as f:
            f.write(json.dumps(entry.to_dict()) + "\n")

    def read_logs(self, task_name: str) -> list[LogEntry]:
        path = self.log_path(task_name)
        if not path.exists():
            return []
        entries: list[LogEntry] = []
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line:
                    entries.append(LogEntry.from_dict(json.loads(line)))
        return entries

    # ── claude process output ────────────────────────────────────────

    def output_path(self, task_name: str) -> Path:
        return self._output_dir / f"{task_name}.json"
