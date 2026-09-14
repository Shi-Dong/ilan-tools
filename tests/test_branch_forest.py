"""Tests for the branch forest ``ilan info`` draws — shape and tombstones.

The rendering of it, and the CLI that reaches it, are covered in
``tests/test_task_info.py``.
"""

from __future__ import annotations

from ilan.cli import _build_branch_forest, _TreeNode


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
