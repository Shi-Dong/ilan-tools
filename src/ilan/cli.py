"""CLI entry-point.  Every command is a thin presentation layer over the
:class:`~ilan.client.Client` HTTP client that talks to the background server.
"""

from __future__ import annotations

import subprocess
import tempfile
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import click
from click.shell_completion import get_completion_class
from rich.console import Console
from rich.table import Table
from rich.text import Text

from . import config as cfg
from .client import Client
from .models import STYLE_FOR_STATUS, TaskStatus
from .server import read_server_info
from .store import Store

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


def _format_ts(iso: str) -> str:
    """Convert a UTC ISO timestamp to the configured time-zone."""
    try:
        tz = ZoneInfo(str(cfg.load().get("time-zone", "US/Pacific")))
        dt = datetime.fromisoformat(iso).astimezone(tz)
        return dt.strftime("%Y-%m-%d %H:%M:%S %Z")
    except Exception:
        return iso


def _check_error(resp: dict) -> bool:
    """Print error if present and return True, else False."""
    if "error" in resp:
        console.print(f"[yellow]{resp['error']}[/yellow]")
        return True
    return False


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

def _do_ls(show_all: bool) -> None:
    resp = _client().list_tasks(show_all=show_all)
    rows = resp["tasks"]
    if not rows:
        msg = "[dim]No tasks.[/dim]" if show_all else "[dim]No active tasks. Use -a to see all.[/dim]"
        console.print(msg)
        return

    table = Table()
    table.add_column("Name", style="bold")
    table.add_column("Status")
    table.add_column("Created")
    for r in rows:
        status = TaskStatus(r["status"])
        style = STYLE_FOR_STATUS.get(status, "")
        table.add_row(r["name"], Text(status.value, style=style), _format_ts(r["created_at"]))
    console.print(table)


@task_group.command("ls")
@click.option("-a", "--all", "show_all", is_flag=True, help="Include DONE and DISCARDED tasks.")
def task_ls(show_all: bool) -> None:
    """List tasks."""
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
@click.argument("message")
def task_reply(name: str, message: str) -> None:
    """Send a response to a task."""
    _do_reply(name, message)


# ── task kill ────────────────────────────────────────────────────────

@task_group.command("kill")
@click.argument("name", shell_complete=_complete_task_names)
def task_kill(name: str) -> None:
    """Kill a WORKING agent and move its task to ERROR."""
    resp = _client().kill_task(name)
    if _check_error(resp):
        raise SystemExit(1)
    console.print(f"[green]Agent for [bold]{name}[/bold] killed. Task set to ERROR.[/green]")


# ── task log / logs ──────────────────────────────────────────────────

def _open_log(name: str) -> None:
    resp = _client().get_logs(name)
    if _check_error(resp):
        raise SystemExit(1)

    logs = resp["logs"]
    if not logs:
        console.print("[yellow]No logs yet for this task.[/yellow]")
        return

    lines: list[str] = []
    for entry in logs:
        label = "User" if entry["role"] == "user" else "Assistant"
        ts = _format_ts(entry["timestamp"]) if entry.get("timestamp") else ""
        lines.append(f"--- {label} ({ts}) ---")
        lines.append(entry["content"])
        lines.append("")

    editor = str(cfg.load().get("editor", "emacs"))
    readonly_flags: dict[str, list[str]] = {
        "vim": ["-R"], "vi": ["-R"], "nvim": ["-R"],
        "nano": ["-v"],
    }

    with tempfile.NamedTemporaryFile(mode="w", suffix=f"-{name}.log", delete=False) as tmp:
        tmp.write("\n".join(lines))
        tmp_path = tmp.name

    cmd = [editor, *readonly_flags.get(editor, []), tmp_path]
    subprocess.run(cmd)


@task_group.command("log")
@click.argument("name", shell_complete=_complete_task_names)
def task_log(name: str) -> None:
    """Open task logs in the configured editor."""
    _open_log(name)


@task_group.command("logs")
@click.argument("name", shell_complete=_complete_task_names)
def task_logs(name: str) -> None:
    """Alias for 'ilan task log'."""
    _open_log(name)


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
        (found if "task" in resp else missing).append(n)

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

@task_group.command("done")
@click.argument("names", nargs=-1, required=True, shell_complete=_complete_task_names)
def task_done(names: tuple[str, ...]) -> None:
    """Mark one or more tasks as DONE."""
    client = _client()
    failed = False
    for name in names:
        resp = client.mark_done(name)
        if _check_error(resp):
            failed = True
        else:
            console.print(f"[green]Task [bold]{name}[/bold] marked DONE.[/green]")
    if failed:
        raise SystemExit(1)


@task_group.command("discard")
@click.argument("names", nargs=-1, required=True, shell_complete=_complete_task_names)
def task_discard(names: tuple[str, ...]) -> None:
    """Mark one or more tasks as DISCARDED."""
    client = _client()
    failed = False
    for name in names:
        resp = client.mark_discard(name)
        if _check_error(resp):
            failed = True
        else:
            console.print(f"[dim]Task [bold]{name}[/bold] discarded.[/dim]")
    if failed:
        raise SystemExit(1)


# ── task undone / undiscard ──────────────────────────────────────────

@task_group.command("undone")
@click.argument("name", shell_complete=_complete_task_names)
def task_undone(name: str) -> None:
    """Move a DONE task back to NEEDS_ATTENTION."""
    resp = _client().undone(name)
    if _check_error(resp):
        raise SystemExit(1)
    console.print(f"[green]Task [bold]{name}[/bold] moved to NEEDS_ATTENTION.[/green]")


@task_group.command("undiscard")
@click.argument("name", shell_complete=_complete_task_names)
def task_undiscard(name: str) -> None:
    """Move a DISCARDED task back to NEEDS_ATTENTION."""
    resp = _client().undiscard(name)
    if _check_error(resp):
        raise SystemExit(1)
    console.print(f"[green]Task [bold]{name}[/bold] moved to NEEDS_ATTENTION.[/green]")


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
@click.option("-a", "--all", "show_all", is_flag=True, help="Include DONE and DISCARDED tasks.")
def shortcut_ls(show_all: bool) -> None:
    """Shorthand for 'ilan task ls'."""
    _do_ls(show_all)


@main.command("tail")
@click.argument("name", shell_complete=_complete_task_names)
def shortcut_tail(name: str) -> None:
    """Shorthand for 'ilan task tail'."""
    _do_tail(name)


@main.command("reply")
@click.argument("name", shell_complete=_complete_task_names)
@click.argument("message")
def shortcut_reply(name: str, message: str) -> None:
    """Shorthand for 'ilan task reply'."""
    _do_reply(name, message)


@main.command("re")
@click.argument("name", shell_complete=_complete_task_names)
@click.argument("message")
def shortcut_re(name: str, message: str) -> None:
    """Shorthand for 'ilan task reply'."""
    _do_reply(name, message)


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
