from __future__ import annotations

import json
import random
import shutil
from pathlib import Path

from ilan.models import ALIAS_POOL, LogEntry, Task


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

    def get_task_by_name_or_alias(self, name_or_alias: str) -> Task | None:
        """Look up a task by name first, then by alias."""
        tasks = self.load_tasks()
        if name_or_alias in tasks:
            return tasks[name_or_alias]
        for task in tasks.values():
            if task.alias == name_or_alias:
                return task
        return None

    def next_available_alias(self) -> str | None:
        """Return a random unused alias from the pool, or None if exhausted."""
        tasks = self.load_tasks()
        used = {t.alias for t in tasks.values() if t.alias}
        available = [alias for alias in ALIAS_POOL if alias not in used]
        if not available:
            return None
        return random.choice(available)

    def put_task(self, task: Task) -> None:
        tasks = self.load_tasks()
        tasks[task.name] = task
        self.save_tasks(tasks)

    def branch_task(
        self,
        parent: Task,
        new_name: str,
        *,
        alias: str,
        task_hash: str,
        now: str,
    ) -> Task:
        """Create a child task that inherits *parent*'s Claude Code session.

        Copies the parent's ilan conversation log so ``tail``/``log`` on the
        child show the full inherited history; after this point the two logs
        diverge.  The parent task is not modified.
        """
        child = Task(
            name=new_name,
            prompt=parent.prompt,
            created_at=now,
            status_changed_at=now,
            session_id=parent.session_id,
            session_log_path=parent.session_log_path,
            alias=alias,
            task_hash=task_hash,
            parent_name=parent.name,
        )
        self.put_task(child)

        parent_log = self.log_path(parent.name)
        if parent_log.exists():
            shutil.copyfile(parent_log, self.log_path(new_name))

        return child

    @staticmethod
    def build_children_map(tasks: dict[str, Task]) -> dict[str, list[str]]:
        """Return ``{parent_name: [child_name, ...]}`` over *tasks*."""
        children: dict[str, list[str]] = {}
        for t in tasks.values():
            if t.parent_name:
                children.setdefault(t.parent_name, []).append(t.name)
        return children

    def collect_descendants(self, name: str, tasks: dict[str, Task] | None = None) -> set[str]:
        """Return all transitive descendants of *name* in *tasks* (or the store)."""
        if tasks is None:
            tasks = self.load_tasks()
        children = self.build_children_map(tasks)
        result: set[str] = set()
        stack = list(children.get(name, []))
        while stack:
            n = stack.pop()
            if n in result:
                continue
            result.add(n)
            stack.extend(children.get(n, []))
        return result

    def rename_task(self, old_name: str, new_name: str) -> Task:
        """Rename a task, updating the tasks dict, log file, and output file."""
        tasks = self.load_tasks()
        task = tasks.pop(old_name)
        task.name = new_name
        tasks[new_name] = task
        for other in tasks.values():
            if other.parent_name == old_name:
                other.parent_name = new_name
        self.save_tasks(tasks)

        old_log = self.log_path(old_name)
        if old_log.exists():
            old_log.rename(self.log_path(new_name))

        old_output = self.output_path(old_name)
        if old_output.exists():
            old_output.rename(self.output_path(new_name))

        return task

    def delete_task(self, name: str) -> None:
        tasks = self.load_tasks()
        removed = tasks.pop(name, None)
        if removed is not None:
            # Re-parent surviving children onto their grandparent so the
            # branch tree stays connected after a mid-branch delete.
            new_parent = removed.parent_name
            for other in tasks.values():
                if other.parent_name == name:
                    other.parent_name = new_parent
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
