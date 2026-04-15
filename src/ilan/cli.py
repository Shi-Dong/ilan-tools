"""CLI entry-point.  Every command is a thin presentation layer over the
:class:`~ilan.client.Client` HTTP client that talks to the background server.
"""

from __future__ import annotations

import os
import re
import select
import shutil
import subprocess
import sys
import tempfile
import termios
import time
import tty
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import click
from click.shell_completion import get_completion_class
from rich.console import Console
from rich.live import Live
from rich.table import Table
from rich.text import Text

from ilan import config as cfg
from ilan.client import Client
from ilan.models import STYLE_FOR_STATUS, TaskStatus
from ilan.runner import Runner
from ilan.server import read_server_info
from ilan.store import Store

console = Console()

_SHELL_RC: dict[str, str] = {"bash": "~/.bashrc", "zsh": "~/.zshrc"}


# ── helpers ──────────────────────────────────────────────────────────

def _client() -> Client:
    """Return a Client connected to the server (auto-starting if needed)."""
    c = Client()
    try:
        c.ensure_server()
    except RuntimeError as exc:
        console.print(f"[red]{exc}[/red]")
        raise SystemExit(1)
    if c.version_mismatch:
        console.print(f"[yellow]Warning: ilan commit mismatch ({c.version_mismatch})[/yellow]")
    return c


def _format_elapsed(iso: str) -> str:
    """Return a human-readable elapsed duration like ``01h23m06s``."""
    try:
        dt = datetime.fromisoformat(iso)
        delta = datetime.now(timezone.utc) - dt.astimezone(timezone.utc)
        total = int(delta.total_seconds())
        if total < 0:
            total = 0
        h, rem = divmod(total, 3600)
        m, s = divmod(rem, 60)
        return f"{h:02d}h{m:02d}m{s:02d}s"
    except Exception:
        return ""


def _format_ts(iso: str) -> str:
    """Convert a UTC ISO timestamp to the configured time-zone."""
    try:
        tz = ZoneInfo(str(cfg.load().get("time-zone", "US/Pacific")))
        dt = datetime.fromisoformat(iso).astimezone(tz)
        today = datetime.now(tz).date()
        day = dt.date()
        if day == today:
            date_part = "Today"
        elif day == today - timedelta(days=1):
            date_part = "Yesterday"
        else:
            date_part = dt.strftime("%m-%d")
        return f"{date_part} {dt.strftime('%H:%M:%S %Z')}"
    except Exception:
        return iso


def _check_error(resp: dict) -> bool:
    """Print error if present and return True, else False."""
    if "error" in resp:
        console.print(f"[yellow]{resp['error']}[/yellow]")
        return True
    return False


_DURATION_RE = re.compile(r"^(\d+)\s*([hd])$", re.IGNORECASE)


def _parse_duration(spec: str) -> timedelta:
    """Parse a human duration like ``5h`` or ``3d`` into a timedelta."""
    m = _DURATION_RE.match(spec.strip())
    if not m:
        raise click.BadParameter(
            f"Invalid duration {spec!r}. Use a number followed by 'h' (hours) or 'd' (days), e.g. 5h or 3d."
        )
    value, unit = int(m.group(1)), m.group(2).lower()
    if unit == "h":
        return timedelta(hours=value)
    return timedelta(days=value)


def _complete_task_names(ctx: click.Context, param: click.Parameter, incomplete: str) -> list[str]:
    """Shell completion for task names.

    When connected to a remote server (ILAN_SERVER_URL), queries the server API
    so that the client can discover task names that only exist on the remote host.
    Falls back to reading the local tasks.json when the server is unreachable.
    """
    try:
        c = Client()
        if c.is_remote:
            resp = c.list_tasks(show_all=True)
            names = [t["name"] for t in resp.get("tasks", [])]
            return sorted(n for n in names if n.startswith(incomplete))
    except Exception:
        pass

    # Local fallback: read tasks.json directly (avoids starting a server just
    # for tab-completion).
    try:
        tasks = Store(cfg.get_workdir()).load_tasks()
        return sorted(n for n in tasks if n.startswith(incomplete))
    except Exception:
        return []


def _complete_config_keys(ctx: click.Context, param: click.Parameter, incomplete: str) -> list[str]:
    return sorted(k for k in cfg.VALID_KEYS if k.startswith(incomplete))


def _install_completion(ctx: click.Context, _param: click.Parameter, shell: str | None) -> None:
    """Eager callback: generate and install the Click tab-completion script."""
    if shell is None:
        return

    cls = get_completion_class(shell)
    if cls is None:
        console.print(f"[red]Unsupported shell: {shell}[/red]")
        ctx.exit(1)

    comp = cls(cli=main, ctx_args={}, prog_name="ilan", complete_var="_ILAN_COMPLETE")
    script = comp.source()

    if shell == "fish":
        script_path = Path("~/.config/fish/completions/ilan.fish").expanduser()
        script_path.parent.mkdir(parents=True, exist_ok=True)
        script_path.write_text(script)
        console.print(f"[green]Completion installed:[/green] {script_path}")
    else:
        script_path = Path(f"~/.ilan/completion.{shell}").expanduser()
        script_path.parent.mkdir(parents=True, exist_ok=True)
        script_path.write_text(script)

        rc_path = Path(_SHELL_RC[shell]).expanduser()
        source_line = f". {script_path}"

        if rc_path.exists() and str(script_path) in rc_path.read_text():
            console.print(f"[green]Completion already installed in {rc_path}[/green]")
            ctx.exit(0)

        with open(rc_path, "a") as f:
            f.write(f"\n{source_line}\n")
        console.print(f"[green]Completion installed. Restart your shell or run:[/green]")
        console.print(f"  {source_line}")

    ctx.exit(0)


# ── root group ───────────────────────────────────────────────────────

@click.group()
@click.option(
    "--install-completion",
    type=click.Choice(["bash", "zsh", "fish"]),
    default=None,
    is_eager=True,
    expose_value=False,
    callback=_install_completion,
    help="Install shell tab-completion and exit.",
)
def main() -> None:
    """Ilan CLI — manage a swarm of Claude Code agents."""


# ── server ───────────────────────────────────────────────────────────

@main.group("server")
def server_group() -> None:
    """Manage the ilan background server."""


@server_group.command("stop")
def server_stop() -> None:
    """Stop the background server."""
    c = Client()
    if c.is_remote:
        try:
            c.stop_server()
            console.print(f"[green]Remote server stopped.[/green]  ({c._base_url})")
        except Exception:
            console.print(f"[yellow]Remote server did not respond.[/yellow]  ({c._base_url})")
        return
    info = read_server_info()
    if info is None:
        console.print("[dim]Server is not running.[/dim]")
        return
    try:
        Client(port=info["port"]).stop_server()
        console.print("[green]Server stopped.[/green]")
    except Exception:
        console.print("[yellow]Server did not respond. It may already be stopped.[/yellow]")


@server_group.command("restart")
def server_restart() -> None:
    """Restart the background server (picks up code changes)."""
    c = Client()
    if c.is_remote:
        console.print("[yellow]Cannot restart a remote server from this machine.[/yellow]")
        raise SystemExit(1)
    info = read_server_info()
    if info is not None:
        try:
            Client(port=info["port"]).stop_server()
        except Exception:
            pass
        time.sleep(0.3)
    try:
        c.ensure_server()
        new_info = read_server_info()
        console.print(
            f"[green]Server restarted[/green]  "
            f"(pid={new_info['pid']}, port={new_info['port']})"
        )
    except RuntimeError as exc:
        console.print(f"[red]{exc}[/red]")
        raise SystemExit(1)


@server_group.command("status")
def server_status() -> None:
    """Show whether the background server is running."""
    c = Client()
    if c.is_remote:
        try:
            c.health()
            console.print(f"[green]Remote server is reachable[/green]  ({c._base_url})")
        except Exception:
            console.print(f"[yellow]Remote server is not reachable[/yellow]  ({c._base_url})")
        return
    info = read_server_info()
    if info is None:
        console.print("[dim]Server is not running.[/dim]")
        return
    try:
        Client(port=info["port"]).health()
        console.print(f"[green]Server is running[/green]  (pid={info['pid']}, port={info['port']})")
    except Exception:
        console.print("[yellow]PID file exists but server is not responding.[/yellow]")


# ── config ───────────────────────────────────────────────────────────

@main.group("config")
def config_group() -> None:
    """View or modify ilan configuration."""


@config_group.command("set")
@click.argument("key", shell_complete=_complete_config_keys)
@click.argument("value")
def config_set(key: str, value: str) -> None:
    """Set a configuration value (e.g. ilan config set num-agents 3)."""
    resp = _client().set_config(key, value)
    if _check_error(resp):
        raise SystemExit(1)
    console.print(f"[green]Set[/green] {resp['key']} = {resp['value']}")


@config_group.command("show")
def config_show() -> None:
    """Show current configuration."""
    resp = _client().get_config()
    conf = resp["config"]
    table = Table()
    table.add_column("Key", style="bold")
    table.add_column("Value")
    for k in sorted(conf):
        table.add_row(k, str(conf[k]))
    console.print(table)


# ── task group ───────────────────────────────────────────────────────

@main.group("task")
def task_group() -> None:
    """Manage tasks."""


# ── task add ─────────────────────────────────────────────────────────

def _do_add(name: str, file_path: str | None, description: str | None) -> None:
    if shutil.which("tmux") is None:
        console.print(
            "[red]tmux is required but not found on PATH.[/red]\n"
            "ilan uses tmux to isolate agent terminal sessions. "
            "Please install tmux and try again."
        )
        raise SystemExit(1)

    if (file_path is None) == (description is None):
        console.print("[red]Exactly one of --file / --description must be provided.[/red]")
        raise SystemExit(1)

    prompt = Path(file_path).read_text() if file_path else description
    assert prompt is not None

    resp = _client().add_task(name, prompt)
    if _check_error(resp):
        raise SystemExit(1)
    console.print(f"[green]Task [bold]{name}[/bold] added.[/green]")


@task_group.command("add")
@click.option("-n", "--name", required=True, help="Short name for the task.")
@click.option("-f", "--file", "file_path", type=click.Path(exists=True), default=None,
              help="Path to a file containing the task prompt.")
@click.option("-d", "--description", default=None, help="Inline task prompt.")
def task_add(name: str, file_path: str | None, description: str | None) -> None:
    """Add a new task."""
    _do_add(name, file_path, description)


# ── task ls ──────────────────────────────────────────────────────────

ALIAS_STYLE = "bold magenta"


def _do_ls(show_all: bool) -> None:
    resp = _client().list_tasks(show_all=show_all)
    rows = resp["tasks"]
    if not rows:
        msg = "[dim]No tasks.[/dim]" if show_all else "[dim]No active tasks. Use -a to see all.[/dim]"
        console.print(msg)
        return

    table = Table()
    table.add_column("(Alias) Name", style="bold")
    table.add_column("Status")
    table.add_column("Cost", justify="right")
    table.add_column("Created")
    table.add_column("Last Changed")
    for r in rows:
        status = TaskStatus(r["status"])
        style = STYLE_FOR_STATUS.get(status, "")
        alias = r.get("alias") or ""
        name_cell = Text()
        if alias:
            name_cell.append(f"({alias}) ", style=ALIAS_STYLE)
        name_cell.append(r["name"], style="bold")
        if r.get("needs_review"):
            name_cell.append(" \u26a0\ufe0f")
        status_cell = Text(status.value, style=style)
        if status == TaskStatus.WORKING and r.get("status_changed_at"):
            elapsed = _format_elapsed(r["status_changed_at"])
            if elapsed:
                status_cell.append(f" (for {elapsed})", style="dim")
        changed = _format_ts(r["status_changed_at"]) if r.get("status_changed_at") else ""
        cost = r.get("cost_usd", 0.0)
        cost_cell = f"${cost:.2f}" if cost else "[dim]-[/dim]"
        table.add_row(
            name_cell,
            status_cell,
            cost_cell,
            _format_ts(r["created_at"]),
            changed,
        )
    console.print(table)


@task_group.command("ls")
@click.argument("name", required=False, default=None, shell_complete=_complete_task_names)
@click.option("-a", "--all", "show_all", is_flag=True, help="Include DONE and DISCARDED tasks.")
def task_ls(name: str | None, show_all: bool) -> None:
    """List tasks, or tail a specific task."""
    if name is not None:
        _do_tail(name)
    else:
        _do_ls(show_all)


# ── task show ────────────────────────────────────────────────────────

@task_group.command("show")
@click.argument("name", shell_complete=_complete_task_names)
def task_show(name: str) -> None:
    """Show the full prompt of a task."""
    resp = _client().get_task(name)
    if _check_error(resp):
        raise SystemExit(1)
    t = resp["task"]
    console.print(f"[bold]Task: {t['name']}[/bold]  (status: {t['status']})\n")
    console.print(t["prompt"])


# ── task path ────────────────────────────────────────────────────────

@task_group.command("path")
@click.argument("name", shell_complete=_complete_task_names)
def task_path(name: str) -> None:
    """Print the Claude Code session log path for a task."""
    resp = _client().get_path(name)
    if _check_error(resp):
        raise SystemExit(1)
    console.print(resp["path"])


# ── task tail ────────────────────────────────────────────────────────

def _do_tail(name: str) -> None:
    resp = _client().get_tail(name)
    if _check_error(resp):
        raise SystemExit(1)

    if resp.get("warning"):
        console.print(f"[yellow]{resp['warning']}[/yellow]")
        return

    for entry in resp["entries"]:
        label = "Assistant" if entry["role"] == "assistant" else "User"
        style = "bold cyan" if entry["role"] == "assistant" else "bold green"
        ts = _format_ts(entry["timestamp"]) if entry.get("timestamp") else ""
        console.print(f"[{style}]{label}[/{style}] [dim]({ts})[/dim]")
        console.print(entry["content"])
        console.print()


@task_group.command("tail")
@click.argument("name", shell_complete=_complete_task_names)
def task_tail(name: str) -> None:
    """Show the last assistant message and any user messages after it."""
    _do_tail(name)


# ── task reply ───────────────────────────────────────────────────────

def _do_reply(name: str, message: str) -> None:
    resp = _client().reply(name, message)
    if _check_error(resp):
        raise SystemExit(1)
    if resp.get("warning"):
        console.print(f"[yellow]{resp['warning']}[/yellow]")
    elif resp.get("message"):
        console.print(f"[green]{resp['message']}[/green]")


@task_group.command("reply")
@click.argument("name", shell_complete=_complete_task_names)
@click.argument("message", required=False, default=None)
def task_reply(name: str, message: str | None) -> None:
    """Send a response to a task. If no message is given, show the tail instead."""
    if message is None:
        _do_tail(name)
    else:
        _do_reply(name, message)


# ── task tap ─────────────────────────────────────────────────────────

TAP_MESSAGE = "How are things now? Give me a summary of the current situation."


def _do_tap(name: str) -> None:
    client = _client()
    resp = client.get_task(name)
    if _check_error(resp):
        raise SystemExit(1)
    t = resp["task"]
    task_name = t["name"]
    status = TaskStatus(t["status"])
    if status != TaskStatus.WORKING:
        console.print(
            f"[yellow]Task [bold]{task_name}[/bold] is {status.value}, not WORKING. "
            f"Tap only works on WORKING tasks.[/yellow]"
        )
        return
    _do_reply(task_name, TAP_MESSAGE)


@task_group.command("tap")
@click.argument("name", shell_complete=_complete_task_names)
def task_tap(name: str) -> None:
    """Ask a WORKING agent for a status update."""
    _do_tap(name)


# ── task kill ────────────────────────────────────────────────────────

@task_group.command("kill")
@click.argument("name", shell_complete=_complete_task_names)
def task_kill(name: str) -> None:
    """Kill a WORKING agent and move its task to ERROR."""
    resp = _client().kill_task(name)
    if _check_error(resp):
        raise SystemExit(1)
    task_name = resp.get("name", name)
    console.print(f"[green]Agent for [bold]{task_name}[/bold] killed. Task set to ERROR.[/green]")


# ── task rename ─────────────────────────────────────────────────────

def _do_rename(old_name: str, new_name: str) -> None:
    resp = _client().rename_task(old_name, new_name)
    if _check_error(resp):
        raise SystemExit(1)
    console.print(
        f"[green]Renamed [bold]{resp['old_name']}[/bold] → [bold]{resp['new_name']}[/bold][/green]"
    )


@task_group.command("rename")
@click.argument("old_name", shell_complete=_complete_task_names)
@click.argument("new_name")
def task_rename(old_name: str, new_name: str) -> None:
    """Rename a task."""
    _do_rename(old_name, new_name)


# ── task attach ─────────────────────────────────────────────────────

def _do_attach(name: str) -> None:
    client = _client()

    if client.is_remote:
        console.print(
            "[red]ilan attach can only be run from the host machine "
            "where the ilan server is running.[/red]"
        )
        raise SystemExit(1)

    resp = client.get_task(name)
    if _check_error(resp):
        raise SystemExit(1)

    t = resp["task"]
    session_id = t.get("session_id")
    if not session_id:
        console.print(f"[yellow]Task [bold]{t['name']}[/bold] has no session yet.[/yellow]")
        raise SystemExit(1)

    status = TaskStatus(t["status"])
    if status == TaskStatus.WORKING:
        console.print(
            f"[yellow]Task [bold]{t['name']}[/bold] is WORKING. "
            f"Kill the agent first with [bold]ilan task kill {t['name']}[/bold].[/yellow]"
        )
        raise SystemExit(1)

    if not Runner._find_session_log(session_id):
        console.print(
            f"[yellow]Session [bold]{session_id}[/bold] for task [bold]{t['name']}[/bold] "
            f"not found on disk. The session may have been lost when the agent was killed.[/yellow]"
        )
        raise SystemExit(1)

    conf = cfg.load()
    workdir = cfg.get_workdir()
    console.print(f"Attaching to session [bold]{session_id}[/bold] for task [bold]{t['name']}[/bold]…")
    os.chdir(workdir)
    os.execvp("claude", [
        "claude",
        "--resume", session_id,
        "--dangerously-skip-permissions",
        "--model", str(conf.get("model", "opus")),
        "--effort", str(conf.get("effort", "high")),
    ])


@task_group.command("attach")
@click.argument("name", shell_complete=_complete_task_names)
def task_attach(name: str) -> None:
    """Attach to a task's Claude Code session in interactive mode."""
    _do_attach(name)


# ── task log / logs ──────────────────────────────────────────────────

def _print_log_path(name: str) -> None:
    resp = _client().get_log_path(name)
    if _check_error(resp):
        raise SystemExit(1)
    click.echo(resp["path"])


def _open_log(name: str, *, path: bool = False) -> None:
    if path:
        _print_log_path(name)
        return

    client = _client()
    task_resp = client.get_task(name)
    if _check_error(task_resp):
        raise SystemExit(1)
    task_name = task_resp["task"]["name"]

    resp = client.get_logs(name)
    if _check_error(resp):
        raise SystemExit(1)

    logs = resp["logs"]
    if not logs:
        console.print("[yellow]No logs yet for this task.[/yellow]")
        return

    lines: list[str] = [f"# Task: {task_name}", ""]
    for i, entry in enumerate(logs):
        label = "User" if entry["role"] == "user" else "Assistant"
        ts = _format_ts(entry["timestamp"]) if entry.get("timestamp") else ""
        if i > 0:
            lines.append("---")
            lines.append("")
        lines.append(f"## {label} — {ts}")
        lines.append("")
        lines.append(entry["content"])
        lines.append("")

    editor = str(cfg.load().get("editor", "emacs"))
    readonly_flags: dict[str, list[str]] = {
        "vim": ["-R"], "vi": ["-R"], "nvim": ["-R"],
        "nano": ["-v"],
    }

    with tempfile.NamedTemporaryFile(mode="w", suffix=f"-{task_name}.md", delete=False) as tmp:
        tmp.write("\n".join(lines))
        tmp_path = tmp.name

    cmd = [editor, *readonly_flags.get(editor, []), tmp_path]
    subprocess.run(cmd)


@task_group.command("log")
@click.argument("name", shell_complete=_complete_task_names)
@click.option("-p", "--path", is_flag=True, help="Print the log file path instead of opening it.")
def task_log(name: str, path: bool) -> None:
    """Open task logs in the configured editor."""
    _open_log(name, path=path)


@task_group.command("logs")
@click.argument("name", shell_complete=_complete_task_names)
@click.option("-p", "--path", is_flag=True, help="Print the log file path instead of opening it.")
def task_logs(name: str, path: bool) -> None:
    """Alias for 'ilan task log'."""
    _open_log(name, path=path)


# ── task rm ──────────────────────────────────────────────────────────

@task_group.command("rm")
@click.argument("names", nargs=-1, required=True, shell_complete=_complete_task_names)
@click.option("-y", "--yes", is_flag=True, help="Skip confirmation.")
def task_rm(names: tuple[str, ...], yes: bool) -> None:
    """Remove one or more tasks and all their data."""
    client = _client()

    found: list[str] = []
    missing: list[str] = []
    for n in names:
        resp = client.get_task(n)
        if "task" in resp:
            found.append(resp["task"]["name"])
        else:
            missing.append(n)

    if missing:
        console.print(f"[yellow]Not found: {', '.join(missing)}[/yellow]")
    if not found:
        return

    if not yes:
        if not click.confirm(f"Remove {len(found)} task(s): {', '.join(found)}?"):
            return

    for n in found:
        client.delete_task(n)
    console.print(f"[green]Removed: {', '.join(found)}[/green]")


# ── task done / discard ──────────────────────────────────────────────


def _do_done(names: tuple[str, ...]) -> None:
    client = _client()
    failed = False
    for name in names:
        resp = client.mark_done(name)
        if _check_error(resp):
            failed = True
        else:
            task_name = resp.get("name", name)
            console.print(f"[green]Task [bold]{task_name}[/bold] marked DONE.[/green]")
    if failed:
        raise SystemExit(1)


def _do_discard(names: tuple[str, ...]) -> None:
    client = _client()
    failed = False
    for name in names:
        resp = client.mark_discard(name)
        if _check_error(resp):
            failed = True
        else:
            task_name = resp.get("name", name)
            console.print(f"[dim]Task [bold]{task_name}[/bold] discarded.[/dim]")
    if failed:
        raise SystemExit(1)


@task_group.command("done")
@click.argument("names", nargs=-1, required=True, shell_complete=_complete_task_names)
def task_done(names: tuple[str, ...]) -> None:
    """Mark one or more tasks as DONE."""
    _do_done(names)


@task_group.command("discard")
@click.argument("names", nargs=-1, required=True, shell_complete=_complete_task_names)
def task_discard(names: tuple[str, ...]) -> None:
    """Mark one or more tasks as DISCARDED."""
    _do_discard(names)


# ── task undone / undiscard ──────────────────────────────────────────

def _do_undone(name: str) -> None:
    resp = _client().undone(name)
    if _check_error(resp):
        raise SystemExit(1)
    task_name = resp.get("name", name)
    console.print(f"[green]Task [bold]{task_name}[/bold] moved to NEEDS_ATTENTION.[/green]")


def _do_undiscard(name: str) -> None:
    resp = _client().undiscard(name)
    if _check_error(resp):
        raise SystemExit(1)
    task_name = resp.get("name", name)
    console.print(f"[green]Task [bold]{task_name}[/bold] moved to NEEDS_ATTENTION.[/green]")


@task_group.command("undone")
@click.argument("name", shell_complete=_complete_task_names)
def task_undone(name: str) -> None:
    """Move a DONE task back to NEEDS_ATTENTION."""
    _do_undone(name)


@task_group.command("undiscard")
@click.argument("name", shell_complete=_complete_task_names)
def task_undiscard(name: str) -> None:
    """Move a DISCARDED task back to NEEDS_ATTENTION."""
    _do_undiscard(name)


# ── top-level shorthands ─────────────────────────────────────────────

@main.command("add")
@click.option("-n", "--name", required=True, help="Short name for the task.")
@click.option("-f", "--file", "file_path", type=click.Path(exists=True), default=None,
              help="Path to a file containing the task prompt.")
@click.option("-d", "--description", default=None, help="Inline task prompt.")
def shortcut_add(name: str, file_path: str | None, description: str | None) -> None:
    """Shorthand for 'ilan task add'."""
    _do_add(name, file_path, description)


@main.command("ls")
@click.argument("name", required=False, default=None, shell_complete=_complete_task_names)
@click.option("-a", "--all", "show_all", is_flag=True, help="Include DONE and DISCARDED tasks.")
def shortcut_ls(name: str | None, show_all: bool) -> None:
    """Shorthand for 'ilan task ls'. If a task name is given, acts as 'ilan tail'."""
    if name is not None:
        _do_tail(name)
    else:
        _do_ls(show_all)


@main.command("tail")
@click.argument("name", shell_complete=_complete_task_names)
def shortcut_tail(name: str) -> None:
    """Shorthand for 'ilan task tail'."""
    _do_tail(name)


@main.command("reply")
@click.argument("name", shell_complete=_complete_task_names)
@click.argument("message", required=False, default=None)
def shortcut_reply(name: str, message: str | None) -> None:
    """Shorthand for 'ilan task reply'."""
    if message is None:
        _do_tail(name)
    else:
        _do_reply(name, message)


@main.command("re")
@click.argument("name", shell_complete=_complete_task_names)
@click.argument("message", required=False, default=None)
def shortcut_re(name: str, message: str | None) -> None:
    """Shorthand for 'ilan task reply'."""
    if message is None:
        _do_tail(name)
    else:
        _do_reply(name, message)


@main.command("done")
@click.argument("names", nargs=-1, required=True, shell_complete=_complete_task_names)
def shortcut_done(names: tuple[str, ...]) -> None:
    """Shorthand for 'ilan task done'."""
    _do_done(names)


@main.command("discard")
@click.argument("names", nargs=-1, required=True, shell_complete=_complete_task_names)
def shortcut_discard(names: tuple[str, ...]) -> None:
    """Shorthand for 'ilan task discard'."""
    _do_discard(names)


@main.command("undone")
@click.argument("name", shell_complete=_complete_task_names)
def shortcut_undone(name: str) -> None:
    """Shorthand for 'ilan task undone'."""
    _do_undone(name)


@main.command("undiscard")
@click.argument("name", shell_complete=_complete_task_names)
def shortcut_undiscard(name: str) -> None:
    """Shorthand for 'ilan task undiscard'."""
    _do_undiscard(name)


@main.command("rename")
@click.argument("old_name", shell_complete=_complete_task_names)
@click.argument("new_name")
def shortcut_rename(old_name: str, new_name: str) -> None:
    """Shorthand for 'ilan task rename'."""
    _do_rename(old_name, new_name)


@main.command("tap")
@click.argument("name", shell_complete=_complete_task_names)
def shortcut_tap(name: str) -> None:
    """Shorthand for 'ilan task tap'."""
    _do_tap(name)


@main.command("attach")
@click.argument("name", shell_complete=_complete_task_names)
def shortcut_attach(name: str) -> None:
    """Shorthand for 'ilan task attach'."""
    _do_attach(name)


@main.command("log")
@click.argument("name", shell_complete=_complete_task_names)
@click.option("-p", "--path", is_flag=True, help="Print the log file path instead of opening it.")
def shortcut_log(name: str, path: bool) -> None:
    """Shorthand for 'ilan task log'."""
    _open_log(name, path=path)


@main.command("logs")
@click.argument("name", shell_complete=_complete_task_names)
@click.option("-p", "--path", is_flag=True, help="Print the log file path instead of opening it.")
def shortcut_logs(name: str, path: bool) -> None:
    """Shorthand for 'ilan task logs'."""
    _open_log(name, path=path)


# ── dashboard ────────────────────────────────────────────────────────

def _build_dashboard_table(rows: list[dict], tz: ZoneInfo) -> Table:
    """Build a Rich Table from task rows, reusing the _do_ls format."""
    now = datetime.now(tz)
    header = Text()
    header.append("ilan dashboard", style="bold")
    header.append("  —  refreshed at ", style="dim")
    header.append(now.strftime("%H:%M:%S %Z"), style="bold green")
    header.append("  —  ", style="dim")
    header.append("q", style="bold")
    header.append(" quit  ", style="dim")
    header.append("r", style="bold")
    header.append(" refresh", style="dim")

    table = Table(title=header, expand=True)
    table.add_column("(Alias) Name", style="bold")
    table.add_column("Status")
    table.add_column("Cost", justify="right")
    table.add_column("Created")
    table.add_column("Last Changed")

    if not rows:
        table.add_row(Text("No active tasks.", style="dim"), "", "", "", "")
        return table

    for r in rows:
        status = TaskStatus(r["status"])
        style = STYLE_FOR_STATUS.get(status, "")
        alias = r.get("alias") or ""
        name_cell = Text()
        if alias:
            name_cell.append(f"({alias}) ", style=ALIAS_STYLE)
        name_cell.append(r["name"], style="bold")
        if r.get("needs_review"):
            # Use an ASCII marker instead of the ⚠️ emoji.  The emoji
            # (U+26A0 + VS16) has unpredictable terminal width that
            # causes table misalignment in Rich's Live display.
            name_cell.append(" !!", style="bold yellow")
        status_cell = Text(status.value, style=style)
        if status == TaskStatus.WORKING and r.get("status_changed_at"):
            elapsed = _format_elapsed(r["status_changed_at"])
            if elapsed:
                status_cell.append(f" (for {elapsed})", style="dim")
        changed = _format_ts(r["status_changed_at"]) if r.get("status_changed_at") else ""
        cost = r.get("cost_usd", 0.0)
        cost_cell = f"${cost:.2f}" if cost else "[dim]-[/dim]"
        table.add_row(
            name_cell,
            status_cell,
            cost_cell,
            _format_ts(r["created_at"]),
            changed,
        )
    return table


def _do_dashboard() -> None:
    """Full-screen real-time task dashboard (like htop)."""
    client = _client()
    conf = cfg.load()
    tz = ZoneInfo(str(conf.get("time-zone", "US/Pacific")))
    interval = max(1, int(conf.get("dashboard-interval", 1)))

    # Snapshot of previous statuses for change detection.
    prev_statuses: dict[str, str] = {}

    def fetch_and_render() -> tuple[Table, bool]:
        """Poll tasks and build the table.

        Returns ``(table, should_bell)`` — the bell flag is True when a
        task's status changed or a new task appeared since the last poll.
        The caller is responsible for ringing the bell *after*
        ``live.update()`` so the raw ``\\a`` byte doesn't corrupt Rich's
        escape-sequence stream.
        """
        nonlocal prev_statuses
        try:
            resp = client.list_tasks(show_all=False)
            rows = resp["tasks"]
        except Exception:
            rows = []

        # Detect status changes.
        cur_statuses = {r["name"]: r["status"] for r in rows}
        bell = False
        if prev_statuses:
            for name, status in cur_statuses.items():
                old = prev_statuses.get(name)
                if old is not None and old != status:
                    bell = True
                    break
            else:
                # Also bell if a brand-new task appeared.
                for name in cur_statuses:
                    if name not in prev_statuses:
                        bell = True
                        break
        prev_statuses = cur_statuses

        return _build_dashboard_table(rows, tz), bell

    def _refresh(live: Live) -> None:
        table, bell = fetch_and_render()
        live.update(table)
        if bell:
            # Write directly to the underlying fd so the bell byte is
            # emitted *after* Live has finished its screen update.
            os.write(sys.stdout.fileno(), b"\a")

    # Put terminal in raw mode to capture single keypresses.
    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        initial_table, _ = fetch_and_render()
        with Live(initial_table, console=console, screen=True, refresh_per_second=1) as live:
            last_poll = time.monotonic()
            while True:
                # Check for keypress (non-blocking).
                rlist, _, _ = select.select([sys.stdin], [], [], 0.1)
                if rlist:
                    ch = sys.stdin.read(1)
                    if ch in ("q", "Q", "\x03"):  # q or Ctrl-C
                        break
                    if ch in ("r", "R"):
                        _refresh(live)
                        last_poll = time.monotonic()
                        continue

                # Auto-refresh at the configured interval.
                if time.monotonic() - last_poll >= interval:
                    _refresh(live)
                    last_poll = time.monotonic()
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)


@main.command("dashboard")
def dashboard() -> None:
    """Full-screen, real-time task dashboard (like htop)."""
    _do_dashboard()


# ── clean ────────────────────────────────────────────────────────────

@main.command("clean")
@click.argument("duration")
@click.option("-y", "--yes", is_flag=True, help="Skip confirmation.")
def clean(duration: str, yes: bool) -> None:
    """Delete tasks whose last change is older than DURATION (e.g. 5h, 3d)."""
    delta = _parse_duration(duration)
    cutoff = datetime.now(timezone.utc) - delta

    client = _client()
    resp = client.list_tasks(show_all=True)
    rows = resp["tasks"]

    stale: list[dict] = []
    for r in rows:
        changed = r.get("status_changed_at") or r.get("created_at", "")
        if not changed:
            continue
        ts = datetime.fromisoformat(changed)
        if ts < cutoff:
            stale.append(r)

    if not stale:
        console.print(f"[dim]No tasks older than {duration}.[/dim]")
        return

    table = Table(title=f"Tasks older than {duration}")
    table.add_column("Name", style="bold")
    table.add_column("Status")
    table.add_column("Last Changed")
    for r in stale:
        status = TaskStatus(r["status"])
        style = STYLE_FOR_STATUS.get(status, "")
        changed = _format_ts(r["status_changed_at"]) if r.get("status_changed_at") else ""
        table.add_row(r["name"], Text(status.value, style=style), changed)
    console.print(table)

    if not yes:
        if not click.confirm(f"Delete {len(stale)} task(s)?"):
            console.print("[dim]Aborted.[/dim]")
            return

    for r in stale:
        client.delete_task(r["name"])
    console.print(f"[green]Deleted {len(stale)} task(s).[/green]")


# ── clear-everything ─────────────────────────────────────────────────

@main.command("clear-everything")
def clear_everything() -> None:
    """Remove ALL tasks, logs, and data. Cannot be bypassed with -y."""
    if not click.confirm(
        "This will permanently delete ALL ilan data. Are you sure?", default=False
    ):
        console.print("[dim]Aborted.[/dim]")
        return

    resp = _client().clear_everything()
    if _check_error(resp):
        raise SystemExit(1)
    console.print("[green]All data cleared.[/green]")


# ── update ──────────────────────────────────────────────────────────

def _find_repo_root() -> Path:
    """Return the git repo root for the ilan-tools source tree."""
    src_dir = Path(__file__).resolve().parent
    try:
        root = subprocess.check_output(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=src_dir,
            stderr=subprocess.DEVNULL,
        ).decode().strip()
        return Path(root)
    except Exception:
        return src_dir.parent.parent  # fallback: src/ilan -> src -> repo root


@main.command("update")
@click.argument("branch", required=False, default=None)
def update(branch: str | None) -> None:
    """Pull the latest ilan-tools from remote and reinstall.

    Optionally pass a BRANCH name to fetch and checkout that branch
    instead of pulling the current one (defaults to main).
    """
    repo = _find_repo_root()
    console.print(f"[bold]Updating ilan-tools[/bold]  ({repo})")

    # Check for uncommitted changes that would block git pull.
    result = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=repo, capture_output=True, text=True,
    )
    if result.returncode != 0:
        console.print(f"[red]git status failed:[/red] {result.stderr.strip()}")
        raise SystemExit(1)
    dirty = [ln for ln in result.stdout.splitlines() if ln and not ln.startswith("??")]
    if dirty:
        console.print("[red]Uncommitted changes in ilan-tools — commit or stash them first.[/red]")
        for ln in dirty:
            console.print(f"  {ln}")
        raise SystemExit(1)

    if branch is not None:
        # Fetch the specific branch and check it out.
        console.print(f"[dim]Fetching branch [bold]{branch}[/bold]…[/dim]")
        fetch = subprocess.run(
            ["git", "fetch", "origin", branch],
            cwd=repo, capture_output=True, text=True,
        )
        if fetch.returncode != 0:
            console.print(f"[red]git fetch failed:[/red]\n{fetch.stderr.strip()}")
            raise SystemExit(1)
        console.print(f"[dim]Checking out [bold]{branch}[/bold]…[/dim]")
        checkout = subprocess.run(
            ["git", "checkout", branch],
            cwd=repo, capture_output=True, text=True,
        )
        if checkout.returncode != 0:
            # Branch may only exist on remote, or may exist locally but be
            # stale.  Use -B so the command works whether or not a local
            # branch with this name already exists.
            checkout = subprocess.run(
                ["git", "checkout", "-B", branch, f"origin/{branch}"],
                cwd=repo, capture_output=True, text=True,
            )
            if checkout.returncode != 0:
                console.print(f"[red]git checkout failed:[/red]\n{checkout.stderr.strip()}")
                raise SystemExit(1)
        # Pull latest for the checked-out branch.
        pull = subprocess.run(
            ["git", "pull", "origin", branch],
            cwd=repo, capture_output=True, text=True,
        )
        if pull.returncode != 0:
            console.print(f"[red]git pull failed:[/red]\n{pull.stderr.strip()}")
            raise SystemExit(1)
        console.print(pull.stdout.strip())
    else:
        # Default: pull current branch (main).
        console.print("[dim]Pulling latest changes…[/dim]")
        pull = subprocess.run(
            ["git", "pull"],
            cwd=repo, capture_output=True, text=True,
        )
        if pull.returncode != 0:
            console.print(f"[red]git pull failed:[/red]\n{pull.stderr.strip()}")
            raise SystemExit(1)
        console.print(pull.stdout.strip())

    # reinstall
    console.print("[dim]Reinstalling…[/dim]")
    install = subprocess.run(
        ["uv", "pip", "install", "-e", "."],
        cwd=repo, capture_output=True, text=True,
    )
    if install.returncode != 0:
        console.print(f"[red]uv pip install failed:[/red]\n{install.stderr.strip()}")
        raise SystemExit(1)
    console.print("[green]ilan-tools updated successfully.[/green]")
