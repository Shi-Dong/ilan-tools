"""Tests for ``ilan task tree`` — forest building, tombstones, and rendering."""

from __future__ import annotations

import re
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner
from rich.console import Console

import ilan.cli as cli_mod
from ilan.cli import _build_branch_forest, _TreeNode, main


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _strip_ansi(s: str) -> str:
    return _ANSI_RE.sub("", s)


def _row(
    name: str,
    parent: str | None = None,
    *,
    hour: int = 0,
    status: str = "WORKING",
    alias: str | None = None,
    deleted_ancestors: list[str] | None = None,
) -> dict:
    """Build one ``/tasks`` row; *hour* drives the creation order."""
    ts = f"2026-07-28T{hour:02d}:00:00+00:00"
    return {
        "name": name,
        "status": status,
        "created_at": ts,
        "status_changed_at": ts,
        "alias": alias,
        "needs_review": False,
        "parent_name": parent,
        "deleted_ancestors": deleted_ancestors or [],
    }


def _shape(nodes: list[_TreeNode]) -> list:
    """Flatten a forest into nested ``(label, children)`` tuples.

    Tombstones are marked with a trailing ``!`` so structural asserts can tell
    a synthetic node from a live task of the same name.
    """
    return [
        (n.name if n.row is not None else f"{n.name}!", _shape(n.children))
        for n in nodes
    ]


@pytest.fixture()
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture()
def wide_console(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep Rich from wrapping tree labels mid-assert."""
    monkeypatch.setattr(cli_mod, "console", Console(width=200, force_terminal=False))


# ── _build_branch_forest ────────────────────────────────────────────────


class TestBuildBranchForest:
    def test_intact_tree(self) -> None:
        rows = [
            _row("root", hour=0),
            _row("kid-a", "root", hour=1),
            _row("kid-b", "root", hour=2),
            _row("grandkid", "kid-a", hour=3),
        ]
        assert _shape(_build_branch_forest(rows)) == [
            ("root", [
                ("kid-a", [("grandkid", [])]),
                ("kid-b", []),
            ]),
        ]

    def test_separate_roots_stay_separate(self) -> None:
        rows = [_row("a", hour=0), _row("b", hour=1)]
        assert _shape(_build_branch_forest(rows)) == [("a", []), ("b", [])]

    def test_children_keep_creation_order(self) -> None:
        rows = [
            _row("root", hour=0),
            _row("late", "root", hour=5),
            _row("later", "root", hour=6),
        ]
        # Rows arrive created_at ascending, so siblings render in that order.
        assert _shape(_build_branch_forest(rows)) == [
            ("root", [("late", []), ("later", [])]),
        ]

    def test_deleted_middle_node_becomes_a_tombstone(self) -> None:
        rows = [
            _row("root", hour=0),
            _row("grandkid", "root", hour=2, deleted_ancestors=["kid"]),
        ]
        assert _shape(_build_branch_forest(rows)) == [
            ("root", [("kid!", [("grandkid", [])])]),
        ]

    def test_siblings_orphaned_by_one_delete_share_a_tombstone(self) -> None:
        rows = [
            _row("root", hour=0),
            _row("gk-a", "root", hour=2, deleted_ancestors=["kid"]),
            _row("gk-b", "root", hour=3, deleted_ancestors=["kid"]),
        ]
        assert _shape(_build_branch_forest(rows)) == [
            ("root", [("kid!", [("gk-a", []), ("gk-b", [])])]),
        ]

    def test_same_name_under_different_parents_gets_its_own_tombstone(self) -> None:
        rows = [
            _row("root-a", hour=0),
            _row("root-b", hour=1),
            _row("kid-a", "root-a", hour=2, deleted_ancestors=["gone"]),
            _row("kid-b", "root-b", hour=3, deleted_ancestors=["gone"]),
        ]
        # Two unrelated deletes can't be merged just because the names match.
        assert _shape(_build_branch_forest(rows)) == [
            ("root-a", [("gone!", [("kid-a", [])])]),
            ("root-b", [("gone!", [("kid-b", [])])]),
        ]

    def test_chained_deletes_nest_nearest_first(self) -> None:
        rows = [
            _row("root", hour=0),
            _row("kid", "root", hour=3, deleted_ancestors=["mid", "upper"]),
        ]
        # ``deleted_ancestors`` is nearest-first, so *upper* sits above *mid*.
        assert _shape(_build_branch_forest(rows)) == [
            ("root", [("upper!", [("mid!", [("kid", [])])])]),
        ]

    def test_deleted_root_becomes_a_tombstone_root(self) -> None:
        rows = [_row("kid", None, hour=1, deleted_ancestors=["root"])]
        assert _shape(_build_branch_forest(rows)) == [("root!", [("kid", [])])]

    def test_unknown_parent_is_treated_as_a_root(self) -> None:
        # ``ilan ls`` without -a hides terminal tasks, so a listed child can
        # point at a parent that isn't in *rows* at all.
        rows = [_row("kid", "hidden-parent", hour=1)]
        assert _shape(_build_branch_forest(rows)) == [("kid", [])]

    def test_tombstone_sorts_at_its_earliest_survivor(self) -> None:
        rows = [
            _row("root", hour=0),
            _row("early", "root", hour=1),
            _row("orphan", "root", hour=2, deleted_ancestors=["kid"]),
            _row("late", "root", hour=3),
        ]
        assert _shape(_build_branch_forest(rows)) == [
            ("root", [
                ("early", []),
                ("kid!", [("orphan", [])]),
                ("late", []),
            ]),
        ]


# ── ilan task tree ──────────────────────────────────────────────────────


def _client_with(rows: list[dict]) -> MagicMock:
    client = MagicMock()
    client.list_tasks.return_value = {"tasks": rows}
    return client


def _invoke(runner: CliRunner, rows: list[dict], *args: str):
    client = _client_with(rows)
    with patch("ilan.cli._client", return_value=client):
        result = runner.invoke(main, list(args))
    return result, client


class TestTreeCommand:
    def test_shows_whole_tree_from_a_leaf(
        self, runner: CliRunner, tmp_config, wide_console,
    ) -> None:
        rows = [
            _row("root", hour=0, alias="aa", status="NEEDS_ATTENTION"),
            _row("kid-a", "root", hour=1, alias="bb", status="DONE"),
            _row("kid-b", "root", hour=2, alias="cc"),
        ]
        result, client = _invoke(runner, rows, "task", "tree", "kid-b")
        assert result.exit_code == 0
        out = _strip_ansi(result.output)
        # Terminal tasks belong in a tree even though ls hides them.
        client.list_tasks.assert_called_once_with(show_all=True)
        assert "(aa) root" in out
        assert "kid-a" in out
        assert "NEEDS_ATTENTION" in out
        # The focused task is called out, and only it.
        assert out.count("← this task") == 1
        assert "kid-b" in out.split("← this task")[0].splitlines()[-1]

    def test_indents_children_under_their_parent(
        self, runner: CliRunner, tmp_config, wide_console,
    ) -> None:
        rows = [
            _row("root", hour=0),
            _row("kid", "root", hour=1),
            _row("grandkid", "kid", hour=2),
        ]
        result, _ = _invoke(runner, rows, "task", "tree", "root")
        assert result.exit_code == 0
        out = _strip_ansi(result.output)
        offsets = {
            name: next(line.index(name) for line in out.splitlines() if name in line)
            for name in ("root", "kid", "grandkid")
        }
        assert offsets["root"] < offsets["kid"] < offsets["grandkid"]

    def test_deleted_task_renders_as_a_tombstone(
        self, runner: CliRunner, tmp_config, wide_console,
    ) -> None:
        rows = [
            _row("root", hour=0),
            _row("orphan", "root", hour=2, deleted_ancestors=["gone-kid"]),
        ]
        result, _ = _invoke(runner, rows, "task", "tree", "orphan")
        assert result.exit_code == 0
        out = _strip_ansi(result.output)
        assert "gone-kid (deleted)" in out
        # The tombstone sits between the root and the orphan it used to own.
        assert out.index("root") < out.index("gone-kid") < out.index("orphan")
        lines = out.splitlines()
        gone_at = next(l.index("gone-kid") for l in lines if "gone-kid" in l)
        orphan_at = next(l.index("orphan") for l in lines if "orphan" in l)
        assert gone_at < orphan_at

    def test_tree_rooted_at_a_tombstone(
        self, runner: CliRunner, tmp_config, wide_console,
    ) -> None:
        rows = [_row("kid", None, hour=1, deleted_ancestors=["gone-root"])]
        result, _ = _invoke(runner, rows, "task", "tree", "kid")
        assert result.exit_code == 0
        out = _strip_ansi(result.output)
        assert "gone-root (deleted)" in out
        assert "kid" in out

    def test_accepts_an_alias(
        self, runner: CliRunner, tmp_config, wide_console,
    ) -> None:
        rows = [
            _row("root", hour=0, alias="aa"),
            _row("kid", "root", hour=1, alias="bb"),
        ]
        result, _ = _invoke(runner, rows, "task", "tree", "bb")
        assert result.exit_code == 0
        out = _strip_ansi(result.output)
        assert "(bb) kid" in out
        assert "kid" in out.split("← this task")[0].splitlines()[-1]

    def test_name_wins_over_a_colliding_alias(
        self, runner: CliRunner, tmp_config, wide_console,
    ) -> None:
        rows = [
            _row("aa", hour=0),
            _row("other", hour=1, alias="aa"),
        ]
        result, _ = _invoke(runner, rows, "task", "tree", "aa")
        assert result.exit_code == 0
        out = _strip_ansi(result.output)
        assert "other" not in out

    def test_unknown_task_exits_nonzero(
        self, runner: CliRunner, tmp_config, wide_console,
    ) -> None:
        result, _ = _invoke(runner, [_row("root", hour=0)], "task", "tree", "nope")
        assert result.exit_code == 1
        assert "not found" in _strip_ansi(result.output)

    def test_shorthand_matches_the_task_subcommand(
        self, runner: CliRunner, tmp_config, wide_console,
    ) -> None:
        rows = [
            _row("root", hour=0, alias="aa"),
            _row("kid", "root", hour=1, alias="bb"),
        ]
        long_form, _ = _invoke(runner, rows, "task", "tree", "kid")
        short_form, _ = _invoke(runner, rows, "tree", "kid")
        assert short_form.exit_code == long_form.exit_code == 0
        assert short_form.output == long_form.output
