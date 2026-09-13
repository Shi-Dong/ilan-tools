"""Tests for Markdown rendering of `ilan re` / `ilan task tail` output."""

from __future__ import annotations

import json
import re
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

import ilan.config as cfg
from ilan.cli import main


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _squash(s: str) -> str:
    """Strip colour codes and collapse Rich's wrapping so a message matches whole."""
    return " ".join(_ANSI_RE.sub("", s).split())


@pytest.fixture()
def runner() -> CliRunner:
    return CliRunner()


def _make_client() -> MagicMock:
    client = MagicMock()
    client.ensure_server.return_value = {}
    client.version_mismatch = None
    client.is_remote = False
    client.get_last_model.return_value = {"model": "claude-opus-4-8"}
    return client


def _enable_markdown(tmp_config: Path) -> None:
    tmp_config.parent.mkdir(parents=True, exist_ok=True)
    with open(tmp_config, "w") as f:
        json.dump({"markdown": True}, f)


def _enable_line_number_and_markdown(tmp_config: Path) -> None:
    tmp_config.parent.mkdir(parents=True, exist_ok=True)
    with open(tmp_config, "w") as f:
        json.dump({"line-number": True, "markdown": True}, f)


# A pipe table — verifies that Rich's Markdown renderer turns it into a
# boxed table rather than printing raw `|---|---|` source.
_TABLE_MD = (
    "| Pod | Status |\n"
    "|---|---|\n"
    "| pod-0 | Pending |\n"
    "| pod-1 | Running |\n"
)


class TestTailMarkdown:
    def _logs(self, content: str = _TABLE_MD) -> dict:
        return {
            "logs": [
                {"role": "assistant", "content": content,
                 "timestamp": "2026-04-25T00:00:00+00:00"},
            ],
        }

    def test_off_by_default_prints_raw_pipes(
        self, runner: CliRunner, tmp_config: Path
    ) -> None:
        client = _make_client()
        client.get_logs.return_value = self._logs()
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["tail", "my-task", "-n", "1"])
        assert result.exit_code == 0
        # Default: no rendering — the literal pipe-separator row survives.
        assert "|---|---|" in result.output

    def test_flag_renders_table(self, runner: CliRunner, tmp_config: Path) -> None:
        client = _make_client()
        client.get_logs.return_value = self._logs()
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["tail", "my-task", "-n", "1", "-m"])
        assert result.exit_code == 0
        # Rendered: pipe-separator gone, cell values still present.
        assert "|---|---|" not in result.output
        assert "pod-0" in result.output and "pod-1" in result.output

    def test_config_default_renders_table(
        self, runner: CliRunner, tmp_config: Path
    ) -> None:
        _enable_markdown(tmp_config)
        client = _make_client()
        client.get_logs.return_value = self._logs()
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["tail", "my-task", "-n", "1"])
        assert result.exit_code == 0
        assert "|---|---|" not in result.output
        assert "pod-0" in result.output

    def test_re_shortcut_supports_md_flag(
        self, runner: CliRunner, tmp_config: Path
    ) -> None:
        client = _make_client()
        client.get_logs.return_value = self._logs()
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["re", "my-task", "-n", "1", "-m"])
        assert result.exit_code == 0
        assert "|---|---|" not in result.output

    def test_user_messages_are_not_markdown_rendered(
        self, runner: CliRunner, tmp_config: Path
    ) -> None:
        # User content with literal pipes should pass through unchanged
        # (only assistant messages get the Markdown treatment).
        client = _make_client()
        client.get_logs.return_value = {
            "logs": [
                {"role": "user", "content": "raw |---| pipes",
                 "timestamp": "2026-04-25T00:00:00+00:00"},
            ],
        }
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["tail", "my-task", "-n", "1", "-m"])
        assert result.exit_code == 0
        assert "raw |---| pipes" in result.output

    def test_md_with_line_numbers_prefixes_each_visual_line(
        self, runner: CliRunner, tmp_config: Path
    ) -> None:
        """Each rendered visual line of the Markdown output gets a ``[N]`` prefix.

        For a pipe-table the rendered output collapses to one visual row per
        source row, so the prefix counter ranges from ``[1]`` through ``[4]``.
        """
        _enable_line_number_and_markdown(tmp_config)
        client = _make_client()
        client.get_logs.return_value = self._logs()  # the 4-row pipe table
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["tail", "my-task", "-n", "1"])
        assert result.exit_code == 0
        # Table rendered (no raw separator survives) AND each visual row
        # carries a [N] prefix.
        assert "|---|---|" not in result.output
        for n in (1, 2, 3, 4):
            assert f"[{n}]" in result.output
        # The two data rows still show their cell values, alongside the prefix.
        out_lines = result.output.splitlines()
        pod0_line = next(l for l in out_lines if "pod-0" in l)
        pod1_line = next(l for l in out_lines if "pod-1" in l)
        assert "[3]" in pod0_line
        assert "[4]" in pod1_line

    def test_md_with_line_numbers_caches_visual_lines_for_at_refs(
        self, runner: CliRunner, tmp_config: Path
    ) -> None:
        """The ``@N`` cache stores the visual rendered lines (clean, no ANSI)
        so that a subsequent reply's ``@N`` quotes exactly what the user saw."""
        _enable_line_number_and_markdown(tmp_config)
        client = _make_client()
        client.get_logs.return_value = self._logs()  # 4-row pipe table
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["tail", "my-task", "-n", "1"])
        assert result.exit_code == 0
        cached = cfg.load_last_tail("my-task")
        # Four visual rows for four source rows in the rendered table.
        assert len(cached) == 4
        # Cached strings are plain text — no ANSI escape sequences.
        for c in cached:
            assert "\x1b[" not in c
        # ...and no leading/trailing whitespace from Rich's row padding.
        for c in cached:
            assert c == c.strip()
        # Data rows include the cell values verbatim.
        assert any("pod-0" in c and "Pending" in c for c in cached)
        assert any("pod-1" in c and "Running" in c for c in cached)

    def test_md_with_line_numbers_at_ref_in_reply_uses_stripped_form(
        self, runner: CliRunner, tmp_config: Path
    ) -> None:
        """Follow-up `ilan re NAME "@N ..."` should quote the stripped row."""
        _enable_line_number_and_markdown(tmp_config)
        client = _make_client()
        client.get_logs.return_value = self._logs()
        client.reply.return_value = {"message": "ok"}
        with patch("ilan.cli._client", return_value=client):
            # Step 1: render the tail so the @N cache is populated.
            tail_result = runner.invoke(main, ["tail", "my-task", "-n", "1"])
            assert tail_result.exit_code == 0
            # Step 2: send a reply that references row 3 (the pod-0 row).
            reply_result = runner.invoke(main, ["re", "my-task", "look at @3"])
        assert reply_result.exit_code == 0
        # The expanded message Reply received should embed the stripped row,
        # *not* the padded version Rich emits to the screen.
        sent_msg = client.reply.call_args[0][1]
        assert "pod-0  Pending" in sent_msg
        assert "  pod-0" not in sent_msg  # i.e. no leading double-space pad
        # And of course the literal `@3` should be gone after expansion.
        assert "@3" not in sent_msg


class TestSetConfigMarkdown:
    def test_bool_config_roundtrips(self, tmp_config: Path) -> None:
        cfg.save({**cfg.DEFAULTS, "markdown": cfg.parse_bool("true")})
        assert cfg.load()["markdown"] is True
        cfg.save({**cfg.DEFAULTS, "markdown": cfg.parse_bool("false")})
        assert cfg.load()["markdown"] is False

    def test_set_writes_local_not_server(
        self, runner: CliRunner, tmp_config: Path
    ) -> None:
        client = _make_client()
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(
                main,
                ["config", "set", "--yes", "markdown", "true"],
            )
        assert result.exit_code == 0
        client.set_config.assert_not_called()
        assert cfg.load()["markdown"] is True
        assert "client-side" in result.output


# Every command that shows a tail takes the pair; each behaviour is checked
# through all of them rather than trusting that the wiring matches.
_TAIL_PREFIXES = [["tail"], ["task", "tail"], ["re"], ["reply"], ["task", "reply"]]


class TestNoMdFlag:
    """`--no-md` is the off side of `-m/--md`: plain text for one view."""

    def _client(self) -> MagicMock:
        client = _make_client()
        client.get_logs.return_value = {
            "logs": [
                {"role": "assistant", "content": _TABLE_MD,
                 "timestamp": "2026-04-25T00:00:00+00:00"},
            ],
        }
        client.reply.return_value = {"ok": True, "name": "my-task", "message": "ok"}
        return client

    @pytest.mark.parametrize("prefix", _TAIL_PREFIXES)
    def test_no_md_prints_raw_markdown_despite_the_config(
        self, runner: CliRunner, tmp_config: Path, prefix: list[str]
    ) -> None:
        _enable_markdown(tmp_config)
        client = self._client()
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, [*prefix, "my-task", "-n", "1", "--no-md"])
        assert result.exit_code == 0, result.output
        assert "|---|---|" in result.output

    @pytest.mark.parametrize("prefix", _TAIL_PREFIXES)
    def test_md_still_renders_despite_the_config(
        self, runner: CliRunner, tmp_config: Path, prefix: list[str]
    ) -> None:
        client = self._client()  # config left at its default: off
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, [*prefix, "my-task", "-n", "1", "-m"])
        assert result.exit_code == 0, result.output
        assert "|---|---|" not in result.output
        assert "pod-0" in result.output

    def test_the_long_spelling_of_the_on_side(
        self, runner: CliRunner, tmp_config: Path
    ) -> None:
        client = self._client()
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["tail", "my-task", "-n", "1", "--md"])
        assert result.exit_code == 0, result.output
        assert "|---|---|" not in result.output

    @pytest.mark.parametrize("prefix", _TAIL_PREFIXES)
    @pytest.mark.parametrize(
        ("flags", "expected"),
        [([], None), (["-m"], True), (["--md"], True), (["--no-md"], False),
         (["-m", "--no-md"], False), (["--no-md", "-m"], True)],
    )
    def test_the_three_states_reach_the_tail(
        self, runner: CliRunner, tmp_config: Path, prefix: list[str],
        flags: list[str], expected: bool | None,
    ) -> None:
        """None means "ask the config"; True and False override it. The last
        flag given wins, as with any Click on/off pair."""
        client = self._client()
        with patch("ilan.cli._client", return_value=client), \
                patch("ilan.cli._do_tail") as do_tail:
            result = runner.invoke(main, [*prefix, "my-task", *flags])
        assert result.exit_code == 0, result.output
        do_tail.assert_called_once()
        assert do_tail.call_args.kwargs["markdown"] is expected

    @pytest.mark.parametrize("flag", ["-m", "--no-md"])
    def test_refused_with_a_message(
        self, runner: CliRunner, tmp_config: Path, flag: str
    ) -> None:
        """There is no tail once something is sent, so the pair has no meaning."""
        client = self._client()
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["re", "my-task", "hello", flag])
        assert result.exit_code == 1
        assert "cannot be used when a response message is provided" in _squash(result.output)
        client.reply.assert_not_called()

    def test_refused_with_the_editor_flag(
        self, runner: CliRunner, tmp_config: Path
    ) -> None:
        client = self._client()
        with patch("ilan.cli._client", return_value=client), \
                patch("ilan.cli.subprocess.run") as run:
            result = runner.invoke(main, ["re", "my-task", "-e", "--no-md"])
        assert result.exit_code == 1
        assert "cannot be used when a response message is provided" in _squash(result.output)
        run.assert_not_called()
        client.reply.assert_not_called()

    def test_refused_with_update(self, runner: CliRunner, tmp_config: Path) -> None:
        client = self._client()
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["re", "my-task", "-u", "--no-md"])
        assert result.exit_code == 1
        assert "-u only edits the looping prompt" in result.output
        client.set_reply_every_message.assert_not_called()

    def test_refused_with_every_alone(
        self, runner: CliRunner, tmp_config: Path
    ) -> None:
        client = self._client()
        with patch("ilan.cli._client", return_value=client):
            result = runner.invoke(main, ["re", "my-task", "-t", "30m", "--no-md"])
        assert result.exit_code == 1
        assert "apply to the tail" in result.output
        client.retime_reply_every.assert_not_called()

    @pytest.mark.parametrize("prefix", _TAIL_PREFIXES)
    def test_help_lists_the_pair(
        self, runner: CliRunner, tmp_config: Path, prefix: list[str]
    ) -> None:
        result = runner.invoke(main, [*prefix, "--help"])
        assert result.exit_code == 0
        assert "-m, --md / --no-md" in result.output
