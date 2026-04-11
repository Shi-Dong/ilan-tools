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
