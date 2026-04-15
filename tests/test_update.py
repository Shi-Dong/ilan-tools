"""Tests for ``ilan update`` — pull latest code and reinstall."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from ilan.cli import main, _find_repo_root


def _make_run(returncode: int, stdout: str = "", stderr: str = "") -> "subprocess.CompletedProcess[str]":
    import subprocess
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr=stderr)


class TestFindRepoRoot:
    def test_returns_path(self) -> None:
        root = _find_repo_root()
        assert isinstance(root, Path)
        # The result should be a directory that contains pyproject.toml
        assert (root / "pyproject.toml").exists()


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
