"""Tests for task-name shell completion."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import ilan.cli as cli_mod
from ilan.cli import _complete_active_task_names, _complete_task_names, main
from ilan.models import TaskStatus

_STATUSES = {
    "alpha": TaskStatus.WORKING,
    "apple": TaskStatus.DONE,
    "april": TaskStatus.DISCARDED,
    "arch": TaskStatus.NEEDS_ATTENTION,
}


def _local(names: dict[str, TaskStatus]) -> tuple[MagicMock, MagicMock]:
    client = MagicMock(is_remote=False)
    store = MagicMock()
    store.return_value.load_tasks.return_value = {
        n: MagicMock(status=s) for n, s in names.items()
    }
    return client, store


def _remote(names: dict[str, TaskStatus]) -> MagicMock:
    client = MagicMock(is_remote=True)
    client.list_tasks.return_value = {
        "tasks": [{"name": n, "status": s.value} for n, s in names.items()]
    }
    return client


def _param(command_name: str) -> object:
    command = main.commands[command_name]
    return next(p for p in command.params if p.name == "name")


class TestLocalCompletion:
    def test_all_names_by_default(self) -> None:
        client, store = _local(_STATUSES)
        with patch.object(cli_mod, "Client", return_value=client), patch.object(cli_mod, "Store", store):
            assert _complete_task_names(None, None, "a") == ["alpha", "apple", "april", "arch"]

    def test_active_names_skip_done_and_discarded(self) -> None:
        client, store = _local(_STATUSES)
        with patch.object(cli_mod, "Client", return_value=client), patch.object(cli_mod, "Store", store):
            assert _complete_active_task_names(None, None, "a") == ["alpha", "arch"]


class TestRemoteCompletion:
    def test_active_names_skip_done_and_discarded(self) -> None:
        with patch.object(cli_mod, "Client", return_value=_remote(_STATUSES)):
            assert _complete_active_task_names(None, None, "a") == ["alpha", "arch"]

    def test_all_names_by_default(self) -> None:
        with patch.object(cli_mod, "Client", return_value=_remote(_STATUSES)):
            assert _complete_task_names(None, None, "ap") == ["apple", "april"]


def test_reply_and_shorthands_complete_active_tasks_only() -> None:
    for name in ("reply", "re"):
        assert _param(name)._custom_shell_complete is _complete_active_task_names
    assert main.commands["task"].commands["reply"].params[0]._custom_shell_complete is _complete_active_task_names
    # Commands that act on closed tasks still see every name.
    assert _param("undone")._custom_shell_complete is _complete_task_names
