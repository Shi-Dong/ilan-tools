"""Tests for ``ilan update`` — pull latest code and reinstall."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from ilan.cli import main, _find_repo_root, _branch_in_other_worktree


def _make_run(returncode: int, stdout: str = "", stderr: str = "") -> "subprocess.CompletedProcess[str]":
    import subprocess
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr=stderr)


class TestFindRepoRoot:
    def test_returns_path(self) -> None:
        root = _find_repo_root()
        assert isinstance(root, Path)
        # The result should be a directory that contains pyproject.toml
        assert (root / "pyproject.toml").exists()


class TestBranchInOtherWorktree:
    def test_branch_in_separate_worktree(self) -> None:
        with patch("ilan.cli.subprocess.run") as mock_run:
            mock_run.return_value = _make_run(
                0,
                stdout=(
                    "worktree /tmp/main-repo\n"
                    "HEAD aaaa\n"
                    "branch refs/heads/main\n"
                    "\n"
                    "worktree /tmp/feature-wt\n"
                    "HEAD bbbb\n"
                    "branch refs/heads/feat\n"
                    "\n"
                ),
            )
            with patch("pathlib.Path.resolve", lambda self: self):
                got = _branch_in_other_worktree(Path("/tmp/main-repo"), "feat")
        assert got == Path("/tmp/feature-wt")

    def test_branch_only_in_current_worktree(self) -> None:
        with patch("ilan.cli.subprocess.run") as mock_run:
            mock_run.return_value = _make_run(
                0,
                stdout=(
                    "worktree /tmp/main-repo\n"
                    "HEAD aaaa\n"
                    "branch refs/heads/feat\n"
                    "\n"
                ),
            )
            with patch("pathlib.Path.resolve", lambda self: self):
                got = _branch_in_other_worktree(Path("/tmp/main-repo"), "feat")
        assert got is None

    def test_branch_not_checked_out_anywhere(self) -> None:
        with patch("ilan.cli.subprocess.run") as mock_run:
            mock_run.return_value = _make_run(
                0,
                stdout=(
                    "worktree /tmp/main-repo\n"
                    "HEAD aaaa\n"
                    "branch refs/heads/main\n"
                    "\n"
                ),
            )
            with patch("pathlib.Path.resolve", lambda self: self):
                got = _branch_in_other_worktree(Path("/tmp/main-repo"), "feat")
        assert got is None

    def test_git_worktree_failure_returns_none(self) -> None:
        with patch("ilan.cli.subprocess.run") as mock_run:
            mock_run.return_value = _make_run(128, stderr="not a git repo")
            got = _branch_in_other_worktree(Path("/tmp/x"), "feat")
        assert got is None


class TestUpdateCommand:
    def test_dirty_repo_aborts(self) -> None:
        runner = CliRunner()
        with patch("ilan.cli.subprocess.run") as mock_run:
            mock_run.return_value = _make_run(0, stdout=" M src/ilan/cli.py\n")
            result = runner.invoke(main, ["update"])
        assert result.exit_code != 0
        assert "Uncommitted changes" in result.output

    def test_git_status_failure(self) -> None:
        runner = CliRunner()
        with patch("ilan.cli.subprocess.run") as mock_run:
            mock_run.return_value = _make_run(128, stderr="not a git repo")
            result = runner.invoke(main, ["update"])
        assert result.exit_code != 0
        assert "git status failed" in result.output

    def test_git_pull_failure(self) -> None:
        runner = CliRunner()
        with patch("ilan.cli.subprocess.run") as mock_run:
            def side_effect(cmd, **kw):
                if cmd[1] == "status":
                    return _make_run(0, stdout="")
                if cmd[1] == "pull":
                    return _make_run(1, stderr="Could not resolve host")
                return _make_run(0)
            mock_run.side_effect = side_effect
            result = runner.invoke(main, ["update"])
        assert result.exit_code != 0
        assert "git pull failed" in result.output

    def test_install_failure(self) -> None:
        runner = CliRunner()
        with patch("ilan.cli.subprocess.run") as mock_run:
            def side_effect(cmd, **kw):
                if cmd[0] == "git" and cmd[1] == "status":
                    return _make_run(0, stdout="")
                if cmd[0] == "git" and cmd[1] == "pull":
                    return _make_run(0, stdout="Already up to date.")
                if cmd[0] == "uv":
                    return _make_run(1, stderr="error: package not found")
                return _make_run(0)
            mock_run.side_effect = side_effect
            result = runner.invoke(main, ["update"])
        assert result.exit_code != 0
        assert "uv pip install failed" in result.output

    def test_success(self) -> None:
        runner = CliRunner()
        with patch("ilan.cli.subprocess.run") as mock_run:
            def side_effect(cmd, **kw):
                if cmd[0] == "git" and cmd[1] == "status":
                    return _make_run(0, stdout="?? untracked.txt\n")
                if cmd[0] == "git" and cmd[1] == "pull":
                    return _make_run(0, stdout="Already up to date.")
                if cmd[0] == "uv":
                    return _make_run(0, stdout="Installed 1 package")
                return _make_run(0)
            mock_run.side_effect = side_effect
            result = runner.invoke(main, ["update"])
        assert result.exit_code == 0
        assert "updated successfully" in result.output

    def test_untracked_files_ignored(self) -> None:
        """Untracked files (lines starting with ??) should NOT block the update."""
        runner = CliRunner()
        with patch("ilan.cli.subprocess.run") as mock_run:
            def side_effect(cmd, **kw):
                if cmd[0] == "git" and cmd[1] == "status":
                    return _make_run(0, stdout="?? new-file.txt\n?? another.log\n")
                if cmd[0] == "git" and cmd[1] == "pull":
                    return _make_run(0, stdout="Already up to date.")
                if cmd[0] == "uv":
                    return _make_run(0)
                return _make_run(0)
            mock_run.side_effect = side_effect
            result = runner.invoke(main, ["update"])
        assert result.exit_code == 0


class TestUpdateWithBranch:
    def test_branch_fetch_failure(self) -> None:
        runner = CliRunner()
        with patch("ilan.cli.subprocess.run") as mock_run:
            def side_effect(cmd, **kw):
                if cmd[0] == "git" and cmd[1] == "status":
                    return _make_run(0, stdout="")
                if cmd[0] == "git" and cmd[1] == "fetch":
                    return _make_run(1, stderr="fatal: couldn't find remote ref shi/nope")
                return _make_run(0)
            mock_run.side_effect = side_effect
            result = runner.invoke(main, ["update", "shi/nope"])
        assert result.exit_code != 0
        assert "git fetch failed" in result.output

    def test_branch_checkout_failure(self) -> None:
        runner = CliRunner()
        with patch("ilan.cli.subprocess.run") as mock_run:
            def side_effect(cmd, **kw):
                if cmd[0] == "git" and cmd[1] == "status":
                    return _make_run(0, stdout="")
                if cmd[0] == "git" and cmd[1] == "fetch":
                    return _make_run(0)
                if cmd[0] == "git" and cmd[1] == "checkout":
                    return _make_run(1, stderr="error: pathspec 'bad' did not match")
                return _make_run(0)
            mock_run.side_effect = side_effect
            result = runner.invoke(main, ["update", "bad"])
        assert result.exit_code != 0
        assert "git checkout failed" in result.output

    def test_branch_success(self) -> None:
        runner = CliRunner()
        with patch("ilan.cli.subprocess.run") as mock_run:
            def side_effect(cmd, **kw):
                if cmd[0] == "git" and cmd[1] == "status":
                    return _make_run(0, stdout="")
                if cmd[0] == "git" and cmd[1] == "fetch":
                    return _make_run(0)
                if cmd[0] == "git" and cmd[1] == "checkout":
                    return _make_run(0, stdout="Switched to branch 'shi/add-dashboard'")
                if cmd[0] == "git" and cmd[1] == "pull":
                    return _make_run(0, stdout="Already up to date.")
                if cmd[0] == "uv":
                    return _make_run(0, stdout="Installed 1 package")
                return _make_run(0)
            mock_run.side_effect = side_effect
            result = runner.invoke(main, ["update", "shi/add-dashboard"])
        assert result.exit_code == 0
        assert "updated successfully" in result.output
        assert "shi/add-dashboard" in result.output

    def test_branch_checkout_falls_back_to_tracking(self) -> None:
        """When 'git checkout <branch>' fails, try creating a local tracking branch."""
        runner = CliRunner()
        call_log: list[list[str]] = []
        with patch("ilan.cli.subprocess.run") as mock_run:
            def side_effect(cmd, **kw):
                call_log.append(list(cmd))
                if cmd[0] == "git" and cmd[1] == "status":
                    return _make_run(0, stdout="")
                if cmd[0] == "git" and cmd[1] == "fetch":
                    return _make_run(0)
                if cmd[0] == "git" and cmd[1] == "checkout" and "-B" not in cmd:
                    return _make_run(1, stderr="error: pathspec did not match")
                if cmd[0] == "git" and cmd[1] == "checkout" and "-B" in cmd:
                    return _make_run(0, stdout="Switched to a new branch 'shi/new-feature'")
                if cmd[0] == "git" and cmd[1] == "pull":
                    return _make_run(0, stdout="Already up to date.")
                if cmd[0] == "uv":
                    return _make_run(0)
                return _make_run(0)
            mock_run.side_effect = side_effect
            result = runner.invoke(main, ["update", "shi/new-feature"])
        assert result.exit_code == 0
        assert "updated successfully" in result.output
        # Verify the fallback checkout was attempted with -B (not -b).
        tracking_cmds = [c for c in call_log if c[:2] == ["git", "checkout"] and "-B" in c]
        assert len(tracking_cmds) == 1

    def test_branch_checkout_existing_local_branch(self) -> None:
        """When 'git checkout <branch>' fails and the branch already exists locally,
        the -B flag should reset it to track origin instead of failing."""
        runner = CliRunner()
        call_log: list[list[str]] = []
        with patch("ilan.cli.subprocess.run") as mock_run:
            def side_effect(cmd, **kw):
                call_log.append(list(cmd))
                if cmd[0] == "git" and cmd[1] == "status":
                    return _make_run(0, stdout="")
                if cmd[0] == "git" and cmd[1] == "fetch":
                    return _make_run(0)
                if cmd[0] == "git" and cmd[1] == "checkout" and "-B" not in cmd:
                    # First checkout fails (e.g. conflict with untracked files)
                    return _make_run(1, stderr="error: cannot checkout")
                if cmd[0] == "git" and cmd[1] == "checkout" and "-B" in cmd:
                    # -B succeeds even though the branch already exists
                    return _make_run(0, stdout="Reset branch 'shi/dashboard'")
                if cmd[0] == "git" and cmd[1] == "pull":
                    return _make_run(0, stdout="Already up to date.")
                if cmd[0] == "uv":
                    return _make_run(0)
                return _make_run(0)
            mock_run.side_effect = side_effect
            result = runner.invoke(main, ["update", "shi/dashboard"])
        assert result.exit_code == 0
        assert "updated successfully" in result.output

    def test_branch_checked_out_in_other_worktree_uses_detached(self) -> None:
        """If <branch> is checked out in another worktree, fall back to a
        detached checkout of origin/<branch> instead of failing."""
        runner = CliRunner()
        call_log: list[list[str]] = []
        with patch("ilan.cli.subprocess.run") as mock_run:
            def side_effect(cmd, **kw):
                call_log.append(list(cmd))
                if cmd[0] == "git" and cmd[1] == "status":
                    return _make_run(0, stdout="")
                if cmd[0] == "git" and cmd[1] == "fetch":
                    return _make_run(0)
                if cmd[0] == "git" and cmd[1] == "worktree" and cmd[2] == "list":
                    # Simulate: shi/dashboard is checked out at /tmp/wt-dashboard.
                    return _make_run(
                        0,
                        stdout=(
                            "worktree /tmp/main-repo\n"
                            "HEAD aaaaaaaa\n"
                            "branch refs/heads/main\n"
                            "\n"
                            "worktree /tmp/wt-dashboard\n"
                            "HEAD bbbbbbbb\n"
                            "branch refs/heads/shi/dashboard\n"
                            "\n"
                        ),
                    )
                if cmd[0] == "git" and cmd[1] == "checkout" and "--detach" in cmd:
                    return _make_run(0, stdout="HEAD is now at bbbbbbbb")
                if cmd[0] == "uv":
                    return _make_run(0)
                return _make_run(0)
            mock_run.side_effect = side_effect
            result = runner.invoke(main, ["update", "shi/dashboard"])
        assert result.exit_code == 0, result.output
        assert "updated successfully" in result.output
        # Detached checkout was used, not plain or -B checkout.
        detached = [c for c in call_log if c[:2] == ["git", "checkout"] and "--detach" in c]
        assert len(detached) == 1
        assert detached[0][-1] == "origin/shi/dashboard"
        plain = [
            c for c in call_log
            if c[:2] == ["git", "checkout"] and "--detach" not in c
        ]
        assert plain == []
        # No `git pull` either — detached checkout already lands at the tip.
        assert not any(c[:2] == ["git", "pull"] for c in call_log)

    def test_branch_in_same_worktree_does_not_trigger_detach(self) -> None:
        """If the branch is only checked out in the current repo (same path),
        regular checkout/pull flow is used."""
        runner = CliRunner()
        call_log: list[list[str]] = []
        with patch("ilan.cli.subprocess.run") as mock_run, \
                patch("ilan.cli._find_repo_root") as mock_root:
            mock_root.return_value = Path("/tmp/main-repo")

            def side_effect(cmd, **kw):
                call_log.append(list(cmd))
                if cmd[0] == "git" and cmd[1] == "status":
                    return _make_run(0, stdout="")
                if cmd[0] == "git" and cmd[1] == "fetch":
                    return _make_run(0)
                if cmd[0] == "git" and cmd[1] == "worktree" and cmd[2] == "list":
                    # Branch refs/heads/shi/dashboard lives in the main repo
                    # itself, so no other worktree owns it.
                    return _make_run(
                        0,
                        stdout=(
                            "worktree /tmp/main-repo\n"
                            "HEAD aaaaaaaa\n"
                            "branch refs/heads/shi/dashboard\n"
                            "\n"
                        ),
                    )
                if cmd[0] == "git" and cmd[1] == "checkout":
                    return _make_run(0)
                if cmd[0] == "git" and cmd[1] == "pull":
                    return _make_run(0)
                if cmd[0] == "uv":
                    return _make_run(0)
                return _make_run(0)
            mock_run.side_effect = side_effect
            result = runner.invoke(main, ["update", "shi/dashboard"])
        assert result.exit_code == 0, result.output
        # No detached checkout in the same-worktree case.
        assert not any(
            c[:2] == ["git", "checkout"] and "--detach" in c
            for c in call_log
        )
        # Pull does run.
        assert any(c[:2] == ["git", "pull"] for c in call_log)

    def test_branch_pull_failure(self) -> None:
        runner = CliRunner()
        with patch("ilan.cli.subprocess.run") as mock_run:
            def side_effect(cmd, **kw):
                if cmd[0] == "git" and cmd[1] == "status":
                    return _make_run(0, stdout="")
                if cmd[0] == "git" and cmd[1] == "fetch":
                    return _make_run(0)
                if cmd[0] == "git" and cmd[1] == "checkout":
                    return _make_run(0)
                if cmd[0] == "git" and cmd[1] == "pull":
                    return _make_run(1, stderr="Could not resolve host")
                return _make_run(0)
            mock_run.side_effect = side_effect
            result = runner.invoke(main, ["update", "shi/some-branch"])
        assert result.exit_code != 0
        assert "git pull failed" in result.output
