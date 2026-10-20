"""Tests for ``ilan agent``: editing the server's CLAUDE.md / AGENTS.md."""

from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from ilan import config as cfg_mod
from ilan.cli import _agent_confirm_lines, main
from ilan.client import Client
from ilan.server import IlanServer
from tests.helpers import get_json, post_json


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


# ── server routes ───────────────────────────────────────────────────

def test_get_reads_each_backend_file(ilan_server: IlanServer, tmp_workdir: Path) -> None:
    (tmp_workdir / "CLAUDE.md").write_text("claude rules\n")
    (tmp_workdir / "AGENTS.md").write_text("codex rules\n")
    claude = get_json(ilan_server, "/agent-file/claude")
    assert claude["name"] == "CLAUDE.md"
    assert claude["content"] == "claude rules\n"
    assert claude["sha256"] == _sha("claude rules\n")
    assert get_json(ilan_server, "/agent-file/codex")["content"] == "codex rules\n"


def test_default_follows_default_backend(ilan_server: IlanServer, tmp_workdir: Path) -> None:
    (tmp_workdir / "CLAUDE.md").write_text("claude rules")
    (tmp_workdir / "AGENTS.md").write_text("codex rules")
    assert get_json(ilan_server, "/agent-file/default")["name"] == "CLAUDE.md"
    cfg_mod.save({**cfg_mod.load(), "default-backend": "codex"})
    assert get_json(ilan_server, "/agent-file/default")["name"] == "AGENTS.md"


def test_missing_file_is_an_error_and_is_not_created(
    ilan_server: IlanServer, tmp_workdir: Path,
) -> None:
    assert "does not exist" in get_json(ilan_server, "/agent-file/codex")["error"]
    resp = post_json(ilan_server, "/agent-file/codex", {"content": "x", "base_sha256": ""})
    assert "does not exist" in resp["error"]
    assert not (tmp_workdir / "AGENTS.md").exists()


def test_unknown_backend_is_refused(ilan_server: IlanServer) -> None:
    assert "Unknown backend" in get_json(ilan_server, "/agent-file/gemini")["error"]


def test_write_through_symlink_keeps_the_link(
    ilan_server: IlanServer, tmp_workdir: Path, tmp_path: Path,
) -> None:
    target = tmp_path / "elsewhere" / "AGENTS.md"
    target.parent.mkdir()
    target.write_text("old")
    (tmp_workdir / "CLAUDE.md").symlink_to(target)
    got = get_json(ilan_server, "/agent-file/claude")
    assert got["content"] == "old"
    assert got["path"] == str(target.resolve())
    resp = post_json(ilan_server, "/agent-file/claude",
                     {"content": "new", "base_sha256": got["sha256"]})
    assert resp["ok"] is True
    assert (tmp_workdir / "CLAUDE.md").is_symlink()
    assert target.read_text() == "new"


def test_write_refused_when_file_changed_meanwhile(
    ilan_server: IlanServer, tmp_workdir: Path,
) -> None:
    path = tmp_workdir / "CLAUDE.md"
    path.write_text("v1")
    base = get_json(ilan_server, "/agent-file/claude")["sha256"]
    path.write_text("someone else")
    resp = post_json(ilan_server, "/agent-file/claude", {"content": "mine", "base_sha256": base})
    assert "changed on the server" in resp["error"]
    assert path.read_text() == "someone else"


def test_crlf_round_trips_byte_for_byte(ilan_server: IlanServer, tmp_workdir: Path) -> None:
    path = tmp_workdir / "CLAUDE.md"
    path.write_bytes(b"a\r\nb\r\n")
    got = get_json(ilan_server, "/agent-file/claude")
    assert got["content"] == "a\r\nb\r\n"
    assert got["sha256"] == hashlib.sha256(b"a\r\nb\r\n").hexdigest()


# ── CLI, end to end against a real server ───────────────────────────

def _fake_editor(tmp_path: Path, script: str) -> str:
    """A Python 'editor' that runs *script* with the file path as ``p``."""
    editor = tmp_path / "fake-editor"
    editor.write_text(
        f"#!{sys.executable}\nimport sys, time\np = sys.argv[-1]\n{script}\n"
    )
    editor.chmod(0o755)
    return str(editor)


def _invoke(
    ilan_server: IlanServer, editor: str, argv: list[str], *,
    answer: str = "y\n", remote: bool = True,
):
    cfg_mod.save({**cfg_mod.load(), "editor": editor})
    client = Client(base_url=ilan_server._test_url)  # type: ignore[attr-defined]
    client._remote = remote  # type: ignore[attr-defined]
    with patch("ilan.cli._client", return_value=client):
        return CliRunner().invoke(main, ["agent", *argv], input=answer)


@pytest.mark.parametrize(("flag", "name"), [("--claude", "CLAUDE.md"), ("--codex", "AGENTS.md")])
def test_cli_edits_the_server_file(
    ilan_server: IlanServer, tmp_workdir: Path, tmp_path: Path, flag: str, name: str,
) -> None:
    (tmp_workdir / name).write_text("before\n")
    editor = _fake_editor(
        tmp_path,
        "assert p.endswith(%r)\nopen(p, 'a').write('after\\n')" % name,
    )
    result = _invoke(ilan_server, editor, [flag])
    assert result.exit_code == 0, result.output
    assert (tmp_workdir / name).read_text() == "before\nafter\n"
    assert "Saved" in result.output


def test_cli_uploads_each_save_while_the_editor_is_open(
    ilan_server: IlanServer, tmp_workdir: Path, tmp_path: Path,
) -> None:
    server_file = tmp_workdir / "CLAUDE.md"
    server_file.write_text("v0")
    seen = tmp_path / "seen"
    editor = _fake_editor(
        tmp_path,
        "open(p, 'w').write('v1')\n"
        "for _ in range(50):\n"
        f"    if open({str(server_file)!r}).read() == 'v1': break\n"
        "    time.sleep(0.1)\n"
        f"open({str(seen)!r}, 'w').write(open({str(server_file)!r}).read())\n"
        "open(p, 'w').write('v2')",
    )
    result = _invoke(ilan_server, editor, [])
    assert result.exit_code == 0, result.output
    assert seen.read_text() == "v1"
    assert server_file.read_text() == "v2"


def test_cli_missing_file_reports_and_does_nothing(
    ilan_server: IlanServer, tmp_workdir: Path, tmp_path: Path,
) -> None:
    opened = tmp_path / "opened"
    editor = _fake_editor(tmp_path, f"open({str(opened)!r}, 'w').write('x')")
    result = _invoke(ilan_server, editor, ["--codex"])
    assert result.exit_code == 1
    assert "does not exist" in " ".join(result.output.split())
    assert not opened.exists()
    assert not (tmp_workdir / "AGENTS.md").exists()


def test_cli_unchanged_file_is_not_written(
    ilan_server: IlanServer, tmp_workdir: Path, tmp_path: Path,
) -> None:
    path = tmp_workdir / "CLAUDE.md"
    path.write_text("same")
    os.utime(path, (1, 1))
    result = _invoke(ilan_server, _fake_editor(tmp_path, "pass"), [])
    assert result.exit_code == 0, result.output
    assert path.stat().st_mtime == 1
    assert "No changes" in result.output


def test_cli_conflict_keeps_the_local_copy(
    ilan_server: IlanServer, tmp_workdir: Path, tmp_path: Path,
) -> None:
    path = tmp_workdir / "CLAUDE.md"
    path.write_text("v1")
    editor = _fake_editor(
        tmp_path,
        f"open({str(path)!r}, 'w').write('theirs')\nopen(p, 'w').write('mine')",
    )
    result = _invoke(ilan_server, editor, [])
    assert result.exit_code == 1
    assert path.read_text() == "theirs"
    out = " ".join(result.output.split())
    assert "changed on the server" in out
    kept = out.split("kept at ")[1].rstrip(". ")
    assert Path(kept).read_text() == "mine"


@pytest.mark.parametrize(("remote", "where"), [(True, "on the server at http://"), (False, "on this machine")])
def test_cli_names_the_file_and_where_it_is(
    ilan_server: IlanServer, tmp_workdir: Path, tmp_path: Path, remote: bool, where: str,
) -> None:
    (tmp_workdir / "CLAUDE.md").write_text("x")
    result = _invoke(ilan_server, _fake_editor(tmp_path, "pass"), [], remote=remote)
    out = " ".join(result.output.split())
    assert f"About to edit {tmp_workdir / 'CLAUDE.md'} {where}" in out
    assert "Continue? [y/N]" in out


def test_cli_names_the_link_target(
    ilan_server: IlanServer, tmp_workdir: Path, tmp_path: Path,
) -> None:
    target = tmp_path / "real.md"
    target.write_text("x")
    (tmp_workdir / "AGENTS.md").symlink_to(target)
    result = _invoke(ilan_server, _fake_editor(tmp_path, "pass"), ["--codex"])
    assert f"(a link to {target.resolve()})" in " ".join(result.output.split())


def test_cli_declining_opens_nothing(
    ilan_server: IlanServer, tmp_workdir: Path, tmp_path: Path,
) -> None:
    (tmp_workdir / "CLAUDE.md").write_text("x")
    opened = tmp_path / "opened"
    editor = _fake_editor(tmp_path, f"open({str(opened)!r}, 'w').write('x')")
    result = _invoke(ilan_server, editor, [], answer="n\n")
    assert result.exit_code == 0, result.output
    assert not opened.exists()
    assert "Nothing opened" in result.output


def test_cli_yes_skips_the_question(
    ilan_server: IlanServer, tmp_workdir: Path, tmp_path: Path,
) -> None:
    (tmp_workdir / "CLAUDE.md").write_text("x")
    editor = _fake_editor(tmp_path, "open(p, 'w').write('y')")
    result = _invoke(ilan_server, editor, ["-y"], answer="")
    assert result.exit_code == 0, result.output
    assert "Continue?" not in result.output
    assert (tmp_workdir / "CLAUDE.md").read_text() == "y"


def test_short_prompt_is_one_line() -> None:
    about = "About to edit /srv/.ilan/CLAUDE.md on this machine."
    assert _agent_confirm_lines(about, 80) == (None, f"{about} Continue?")


def test_long_prompt_puts_the_path_on_its_own_line() -> None:
    about = "About to edit /" + "x" * 80 + "/CLAUDE.md on this machine."
    assert _agent_confirm_lines(about, 80) == (about, "Continue?")


def test_cli_short_path_prompts_on_one_line(
    ilan_server: IlanServer, tmp_workdir: Path, tmp_path: Path,
) -> None:
    (tmp_workdir / "CLAUDE.md").write_text("x")
    with patch("ilan.cli._agent_file_location", return_value="/s/CLAUDE.md on this machine"):
        result = _invoke(ilan_server, _fake_editor(tmp_path, "pass"), [])
    assert result.output.splitlines()[0] == (
        "About to edit /s/CLAUDE.md on this machine. Continue? [y/N]: y"
    )
