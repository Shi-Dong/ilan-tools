"""Tests for ilan.tmux and tmux session tracking integration."""

from __future__ import annotations

from unittest.mock import patch, MagicMock

from ilan.tmux import kill_tmux_sessions_by_prefix


class TestKillTmuxSessionsByPrefix:
    def test_kills_matching_sessions(self) -> None:
        mock_list = MagicMock()
        mock_list.returncode = 0
        mock_list.stdout = "abc12345-claude-task1\nabc12345-claude-task1-extra\nother-session\n"

        mock_kill = MagicMock()

        def run_side_effect(cmd, **kwargs):
            if cmd[1] == "list-sessions":
                return mock_list
            return mock_kill

        with patch("ilan.tmux.subprocess.run", side_effect=run_side_effect) as mock_run:
            killed = kill_tmux_sessions_by_prefix("abc12345")

        assert killed == ["abc12345-claude-task1", "abc12345-claude-task1-extra"]
        # 1 list-sessions + 2 kill-session calls
        assert mock_run.call_count == 3

    def test_no_matching_sessions(self) -> None:
        mock_list = MagicMock()
        mock_list.returncode = 0
        mock_list.stdout = "other-session\nanother-session\n"

        with patch("ilan.tmux.subprocess.run", return_value=mock_list):
            killed = kill_tmux_sessions_by_prefix("abc12345")

        assert killed == []

    def test_tmux_not_installed(self) -> None:
        with patch("ilan.tmux.subprocess.run", side_effect=FileNotFoundError):
            killed = kill_tmux_sessions_by_prefix("abc12345")

        assert killed == []

    def test_tmux_list_fails(self) -> None:
        mock_result = MagicMock()
        mock_result.returncode = 1

        with patch("ilan.tmux.subprocess.run", return_value=mock_result):
            killed = kill_tmux_sessions_by_prefix("abc12345")

        assert killed == []

    def test_empty_output(self) -> None:
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = ""

        with patch("ilan.tmux.subprocess.run", return_value=mock_result):
            killed = kill_tmux_sessions_by_prefix("abc12345")

        assert killed == []
