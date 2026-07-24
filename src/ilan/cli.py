"""CLI entry-point.  Every command is a thin presentation layer over the
:class:`~ilan.client.Client` HTTP client that talks to the background server.
"""

from __future__ import annotations

import io
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
import webbrowser
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import click
from click.shell_completion import get_completion_class
from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ilan import config as cfg
from ilan.backends import ClaudeBackend
from ilan.client import Client
from ilan.models import (
    DEFAULT_ENGINE,
    ENGINE_CODEX,
    ENGINE_NAME_STYLE,
    FABLE_MODEL,
    STYLE_FOR_STATUS,
    TaskStatus,
    is_fable_model,
)
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


def _format_ts(iso: str, *, seconds: bool = True) -> str:
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
        time_fmt = "%H:%M:%S %Z" if seconds else "%H:%M %Z"
        return f"{date_part} {dt.strftime(time_fmt)}"
    except Exception:
        return iso


def _check_error(resp: dict) -> bool:
    """Print error if present and return True, else False."""
    if "error" in resp:
        console.print(f"[yellow]{resp['error']}[/yellow]")
        return True
    return False


_DURATION_RE = re.compile(r"^(\d+)\s*([hd])$", re.IGNORECASE)

# Matches ``@<digits>`` references used to quote assistant lines in replies
# when line-number mode is on.  The negative lookbehind prevents chewing
# things like email addresses (``user@123.com``).
_AT_REF_RE = re.compile(r"(?<![A-Za-z0-9_])@(\d+)")


def _line_number_enabled() -> bool:
    return cfg.parse_bool(cfg.load().get("line-number", False))


def _markdown_enabled() -> bool:
    return cfg.parse_bool(cfg.load().get("markdown", False))


def _one_liner_enabled() -> bool:
    return cfg.parse_bool(cfg.load().get("one-line-summary", True))


def _render_markdown_visual_lines(content: str, width: int | None = None) -> list[str]:
    """Render ``content`` as Markdown and return the rendered visual lines.

    Each line carries the ANSI escape codes Rich produced, so callers can
    feed the result through :meth:`rich.text.Text.from_ansi` to re-apply the
    styling on top of an extra prefix (e.g. a ``[N]`` line number).

    ``width`` overrides the rendering width — useful when the caller plans
    to embed the result inside a Rich Panel and needs the visual lines to
    fit within the panel's inner width.
    """
    buf = io.StringIO()
    target_width = width if width is not None else (console.width or 80)
    tmp = Console(
        file=buf,
        force_terminal=True,
        color_system="truecolor",
        width=target_width,
    )
    tmp.print(Markdown(content))
    lines = buf.getvalue().rstrip("\n").splitlines()
    # Rich adds vertical padding above/below block elements like tables, which
    # would inflate the [N] counter with invisible rows. Drop leading/trailing
    # whitespace-only lines while preserving any internal blank rows the user
    # actually wrote.
    def _is_blank(s: str) -> bool:
        return Text.from_ansi(s).plain.strip() == ""
    while lines and _is_blank(lines[0]):
        lines.pop(0)
    while lines and _is_blank(lines[-1]):
        lines.pop()
    return lines


def _expand_at_refs(message: str, lines: list[str]) -> str:
    """Replace ``@N`` tokens with the Nth cached assistant line, double-quoted.

    Out-of-range references are left untouched so the user can spot a typo.
    """
    if not lines:
        return message

    def repl(m: re.Match) -> str:
        idx = int(m.group(1))
        if 1 <= idx <= len(lines):
            return f"\"{lines[idx - 1]}\""
        return m.group(0)

    return _AT_REF_RE.sub(repl, message)


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


# ── ping ─────────────────────────────────────────────────────────────


@main.command("ping")
@click.option(
    "-c",
    "--count",
    type=click.IntRange(min=1),
    default=3,
    show_default=True,
    help="Number of pings to send.",
)
def ping(count: int) -> None:
    """Measure round-trip time to the ilan server.

    For a remote server (``ILAN_SERVER_URL`` set), sends ``count`` health
    requests and prints the average round-trip time in milliseconds. For
    a local server, just notes that no network ping is needed.
    """
    c = Client()
    if not c.is_remote:
        console.print("[dim]Server is local — no network ping needed.[/dim]")
        return

    samples_ms: list[float] = []
    for _ in range(count):
        start = time.perf_counter()
        try:
            c.health()
        except Exception:
            continue
        samples_ms.append((time.perf_counter() - start) * 1000)

    if not samples_ms:
        console.print(f"[red]All {count} pings to {c._base_url} failed.[/red]")
        raise SystemExit(1)

    avg = round(sum(samples_ms) / len(samples_ms))
    console.print(f"Average of {len(samples_ms)} pings: [green]{avg} ms[/green]")


# ── config ───────────────────────────────────────────────────────────

@main.group("config")
def config_group() -> None:
    """View or modify ilan configuration."""


def _set_local_config(key: str, value: str) -> object:
    """Write a client-side config key to the local config file and return the coerced value."""
    conf = cfg.load()
    if key in cfg.INT_KEYS:
        conf[key] = int(value)
    elif key in cfg.BOOL_KEYS:
        conf[key] = cfg.parse_bool(value)
    elif key == "time-zone":
        conf[key] = cfg.resolve_time_zone(value)
    else:
        conf[key] = value
    cfg.save(conf)
    return conf[key]


@config_group.command("set")
@click.argument("key", shell_complete=_complete_config_keys)
@click.argument("value")
def config_set(key: str, value: str) -> None:
    """Set a configuration value (e.g. ilan config set model-claude claude-sonnet-4-6).

    Time-zone accepts friendly aliases, e.g. ``ilan config set time-zone
    tokyo`` or ``ilan config set time-zone pacific`` (case-insensitive).
    """
    if key in cfg.CLIENT_SIDE_KEYS:
        if key not in cfg.VALID_KEYS:
            console.print(f"[yellow]Unknown config key: {key}[/yellow]")
            raise SystemExit(1)
        coerced = _set_local_config(key, value)
        console.print(f"[green]Set[/green] {key} = {coerced} [dim](client-side)[/dim]")
        return
    resp = _client().set_config(key, value)
    if _check_error(resp):
        raise SystemExit(1)
    console.print(f"[green]Set[/green] {resp['key']} = {resp['value']}")


@config_group.command("show")
def config_show() -> None:
    """Show current configuration.

    Server-managed keys come from the server; client-side keys (rendering
    toggles like ``line-number``) come from the local config file and
    override whatever the server might report for the same key.

    Secret keys (``api-key-claude``, ``api-key-codex``, …) are masked: only
    the last five characters are shown, preceded by two asterisks.
    """
    resp = _client().get_config()
    conf = dict(resp["config"])
    local = cfg.load()
    for k in cfg.CLIENT_SIDE_KEYS:
        conf[k] = local.get(k, cfg.DEFAULTS.get(k))
    table = Table()
    table.add_column("Key", style="bold")
    table.add_column("Value")
    for k in sorted(conf):
        value = str(conf[k])
        if k in cfg.SECRET_KEYS:
            value = _mask_secret(value)
        table.add_row(k, value)
    console.print(table)


def _mask_secret(value: str) -> str:
    """Mask a secret value: empty → empty, otherwise ``**`` + last 5 chars."""
    if not value:
        return ""
    return f"**{value[-5:]}"


# ── task group ───────────────────────────────────────────────────────

@main.group("task")
def task_group() -> None:
    """Manage tasks."""


# ── task add ─────────────────────────────────────────────────────────

def _do_add(
    name: str, file_path: str | None, description: str | None,
    agent: str | None, max_model: bool = False,
) -> None:
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

    # Fable is an Anthropic model, so --max only makes sense on the Claude
    # backend. Reject the contradictory combination outright, and otherwise
    # pin the task to claude so --max works regardless of the config default.
    if max_model:
        if agent == "codex":
            console.print(
                f"[red]--max runs the task on Fable ({FABLE_MODEL}), a Claude-only "
                "model; it can't be combined with --codex.[/red]"
            )
            raise SystemExit(1)
        agent = "claude"

    prompt = Path(file_path).read_text() if file_path else description
    assert prompt is not None

    resp = _client().add_task(name, prompt, agent, max_model=max_model)
    if _check_error(resp):
        raise SystemExit(1)
    if max_model:
        console.print(
            f"[green]Task [bold]{name}[/bold] added on "
            f"[bold red]FABLE[/bold red] ([cyan]{FABLE_MODEL}[/cyan]).[/green]"
        )
    else:
        console.print(f"[green]Task [bold]{name}[/bold] added.[/green]")


@task_group.command("add")
@click.option("-n", "--name", required=True, help="Short name for the task.")
@click.option("-f", "--file", "file_path", type=click.Path(exists=True), default=None,
              help="Path to a file containing the task prompt.")
@click.option("-d", "--description", default=None, help="Inline task prompt.")
@click.option("--claude", "agent", flag_value="claude", default=None,
              help="Run this task on the Claude backend (the default).")
@click.option("--codex", "agent", flag_value="codex",
              help="Run this task on the Codex backend.")
@click.option("--max", "max_model", is_flag=True, default=False,
              help="Create the task on the Fable model (implies --claude).")
def task_add(
    name: str, file_path: str | None, description: str | None,
    agent: str | None, max_model: bool,
) -> None:
    """Add a new task."""
    _do_add(name, file_path, description, agent, max_model)


# ── task ls ──────────────────────────────────────────────────────────

ALIAS_STYLE = "bold magenta"
TREE_STYLE = "dim"


def _order_tasks_as_forest(rows: list[dict]) -> list[tuple[dict, str]]:
    """Return rows in DFS order with per-row tree-drawing prefixes.

    Parent/child links come from ``parent_name``.  Rows whose parent is
    not in *rows* (filtered out or deleted) are treated as roots.
    Roots preserve the incoming ``rows`` ordering (the server sorts by
    ``created_at`` ascending); siblings under a parent are sorted by
    ``created_at`` ascending so the branching order is easy to read.
    """
    by_name = {r["name"]: r for r in rows}
    present = set(by_name)
    children: dict[str, list[str]] = {}
    for r in rows:
        p = r.get("parent_name")
        if p is not None and p in present:
            children.setdefault(p, []).append(r["name"])
    for siblings in children.values():
        siblings.sort(key=lambda n: by_name[n].get("created_at", ""))

    root_order = [r["name"] for r in rows if r.get("parent_name") not in present]

    result: list[tuple[dict, str]] = []

    def walk(name: str, ancestor_has_next: list[bool]) -> None:
        depth = len(ancestor_has_next)
        if depth == 0:
            prefix = ""
        else:
            parts = ["\u2502  " if flag else "   " for flag in ancestor_has_next[:-1]]
            parts.append("\u251c\u2500 " if ancestor_has_next[-1] else "\u2514\u2500 ")
            prefix = "".join(parts)
        result.append((by_name[name], prefix))
        kids = children.get(name, [])
        for i, kid in enumerate(kids):
            walk(kid, ancestor_has_next + [i < len(kids) - 1])

    for root in root_order:
        walk(root, [])
    return result


def _build_name_cell(row: dict, prefix: str) -> Text:
    """Build the styled "(alias) name" cell with an optional tree prefix.

    ``needs_review`` rows are flagged with a ``!!`` ASCII marker rather
    than the \u26a0\ufe0f emoji, whose unpredictable terminal width breaks
    Rich's Live layout in ``ilan dashboard`` and visually misaligns the
    ``ilan ls`` table.

    Tasks running on the Fable model get a red ``FABLE`` note on a separate
    line beneath the name. Fable is Claude-only, so the note is hidden while
    the task's engine is Codex; every other engine runs on the Claude backend
    (see ``Runner._backend_for``), which honors the Fable override, so it
    keeps the note. The stored model is untouched, so switching back to Claude
    restores it.
    """
    status = TaskStatus(row["status"])
    alias = row.get("alias") or ""
    cell = Text()
    if prefix:
        cell.append(prefix, style=TREE_STYLE)
    if alias:
        cell.append(f"({alias}) ", style=ALIAS_STYLE)
    engine = row.get("engine") or DEFAULT_ENGINE
    name_style = ENGINE_NAME_STYLE.get(engine, "")
    cell.append(row["name"], style=f"bold {name_style}".strip())
    if row.get("needs_review"):
        cell.append(" !!", style="bold yellow")
    if status is TaskStatus.WORKING:
        sleep_suffix = _format_sleep_suffix(row.get("sleep_seconds"))
        if sleep_suffix:
            cell.append(sleep_suffix, style=SLEEP_STYLE)
    if engine != ENGINE_CODEX and is_fable_model(row.get("model")):
        cell.append("\nFABLE", style="bold red")
    return cell


def _build_history_cell(row: dict) -> Text:
    """Build the History cell: a hyperlink over the word ``history``.

    Rich renders the link as an OSC 8 terminal hyperlink, so the long Gist URL
    stays hidden behind the short label. Tasks without a Gist yet (mirroring
    disabled, or the async syncer hasn't created it) show a dim placeholder.
    """
    url = (row.get("gist_url") or "").strip()
    if not url:
        return Text("-", style="dim")
    # Append the styled span instead of using a base Text style: a base style
    # bleeds across the cell's right padding, so the `underline` would run well
    # past the word "history" whenever the column is wider than the label.
    cell = Text()
    cell.append("history", style=f"link {url} blue underline")
    return cell


def _build_status_cell(row: dict, show_one_liner: bool = True) -> Text:
    status = TaskStatus(row["status"])
    style = STYLE_FOR_STATUS.get(status, "")
    # Apply the status style only to the status span (not as a base style on
    # the parent Text), so a `dim` status style (DONE / DISCARDED) doesn't
    # bleed onto the elapsed-time hint or the one-liner and render them
    # darker than the same spans on other rows.
    cell = Text()
    cell.append(status.value, style=style)
    if status == TaskStatus.WORKING and row.get("status_changed_at"):
        elapsed = _format_elapsed(row["status_changed_at"])
        if elapsed:
            cell.append(f" (for {elapsed})", style="dim")
    if show_one_liner:
        one_liner = (row.get("summary_one_liner") or "").strip()
        if one_liner:
            cell.append("\n")
            cell.append(one_liner, style="yellow italic")
    return cell


_ONE_LINER_NO_API_KEY_WARNING = (
    "[yellow]Note: `one-line-summary` is on but the server has no `api-key-claude` "
    "set, so summaries fall back to the server's local `claude` CLI (Claude "
    "Code subscription). This needs `claude` installed and logged in on the "
    "server; otherwise no summaries are generated. Set an `api-key-claude` to use the "
    "Anthropic API instead, or run `ilan config set one-line-summary false` to "
    "hide this note.[/yellow]"
)


def _maybe_warn_one_liner_unconfigured(client: Client) -> None:
    """Print a one-time note if one-line-summary is on but server has no api-key-claude."""
    if not _one_liner_enabled():
        return
    try:
        server_cfg = client.get_config().get("config", {})
    except Exception:
        return
    if not str(server_cfg.get("api-key-claude", "")).strip():
        console.print(_ONE_LINER_NO_API_KEY_WARNING)


# Below this terminal width (in columns) the ``ls`` / ``dashboard`` tables
# drop the lower-priority ``Created`` column so the remaining
# ``Name`` / ``Status`` / ``Last Changed`` / ``History`` columns stay legible
# instead of wrapping into an unreadable mess on a narrow window.
_NARROW_TERMINAL_WIDTH = 120


def _terminal_is_narrow(width: int | None = None) -> bool:
    """Whether the terminal is too narrow to show the Created column."""
    if width is None:
        width = console.width
    return width < _NARROW_TERMINAL_WIDTH


def _do_ls(show_all: bool) -> None:
    client = _client()
    if not show_all:
        _maybe_warn_one_liner_unconfigured(client)
    resp = client.list_tasks(show_all=show_all)
    rows = resp["tasks"]
    if not rows:
        msg = "[dim]No tasks.[/dim]" if show_all else "[dim]No active tasks. Use -a to see all.[/dim]"
        console.print(msg)
        return

    # `-a` lists include DONE/DISCARDED tasks, so the table gets long; the
    # one-liner column would make it even noisier.
    show_one_liner = _one_liner_enabled() and not show_all
    narrow = _terminal_is_narrow()
    table = Table(show_lines=True)
    table.add_column("(Alias) Name", style="bold")
    table.add_column("Status")
    if not narrow:
        table.add_column("Created")
    table.add_column("Last Changed")
    table.add_column("History")
    for row, prefix in _order_tasks_as_forest(rows):
        changed = _format_ts(row["status_changed_at"], seconds=False) if row.get("status_changed_at") else ""
        cells = [
            _build_name_cell(row, prefix),
            _build_status_cell(row, show_one_liner=show_one_liner),
        ]
        if not narrow:
            cells.append(_format_ts(row["created_at"], seconds=False))
        cells.append(changed)
        cells.append(_build_history_cell(row))
        table.add_row(*cells)
    console.print(table)


@task_group.command("ls")
@click.argument("name", required=False, default=None, shell_complete=_complete_task_names)
@click.option("-a", "--all", "show_all", is_flag=True, help="Include DONE and DISCARDED tasks.")
@click.option("-n", "--num", "num", type=int, default=None,
              help="When a task name is given, show the final N messages.")
@click.option("--line-number/--no-line-number", "line_number", default=None,
              help="When a task name is given, override the ``line-number`` "
                   "config for this invocation.")
def task_ls(
    name: str | None, show_all: bool, num: int | None, line_number: bool | None,
) -> None:
    """List tasks, or tail a specific task."""
    if name is not None:
        _do_tail(name, n=num, line_number=line_number)
    else:
        if line_number is not None:
            console.print(
                "[red]--line-number/--no-line-number requires a task name.[/red]"
            )
            raise SystemExit(1)
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


# ── task check-model ─────────────────────────────────────────────────

def _do_check_model(name: str) -> None:
    resp = _client().get_last_model(name)
    if _check_error(resp):
        raise SystemExit(1)
    console.print(resp["model"])


@task_group.command("check-model")
@click.argument("name", shell_complete=_complete_task_names)
def task_check_model(name: str) -> None:
    """Print the model that generated the last assistant message for a task.

    Reads the task's Claude Code session log and reports the ``model``
    field from the last ``role: assistant`` entry.
    """
    _do_check_model(name)


# ── task tail ────────────────────────────────────────────────────────

def _print_reply_hint(handle: str) -> None:
    """Print the ``ilan re <handle>`` reminder shown at the end of tail output."""
    console.print(
        f"[dim]To reply to the task, run [/dim][bright_red]ilan re {handle}[/bright_red]"
    )


def _print_last_model_hint(
    client: Client,
    name: str,
    cached_model: str | None = None,
    cached_effort: str | None = None,
) -> None:
    """Print which model generated the last assistant message, above the reply hint.

    The model and its reasoning effort are normally piggybacked on the tail
    response (*cached_model* / *cached_effort*), so no extra request is
    needed. When the model is absent — e.g. the task was last reaped by an
    older server that didn't cache it — fall back to an explicit
    ``get_last_model`` lookup. If the model still can't be determined (no
    session log yet, no assistant message, server error or unreachable) print
    a warning explaining why, rather than the model line, so attribution is
    never silently dropped. The effort is best-effort: when unknown (older
    server or a task last reaped before efforts were recorded) the line
    simply omits it.
    """
    model = cached_model
    effort = cached_effort
    if not model:
        try:
            resp = client.get_last_model(name)
        except ConnectionError:
            console.print(
                "[yellow]Could not determine the last assistant model: "
                "cannot reach the ilan server.[/yellow]"
            )
            return
        model = resp.get("model")
        effort = resp.get("effort")
        if resp.get("error") or not model:
            reason = resp.get("error") or "no model recorded in the session log"
            console.print(
                f"[yellow]Could not determine the last assistant model: {reason}[/yellow]"
            )
            return
    effort_suffix = (
        f"[dim] (effort: [/dim][yellow]{effort}[/yellow][dim])[/dim]" if effort else ""
    )
    # highlight=False so Rich's auto-highlighter doesn't recolor the digits in
    # model names like ``claude-opus-4-8``; the whole name should be one yellow span.
    console.print(
        f"[dim]The last assistant message is generated by [/dim][yellow]{model}[/yellow]"
        f"{effort_suffix}",
        highlight=False,
    )


def _do_tail(
    name: str,
    n: int | None = None,
    markdown: bool | None = None,
    line_number: bool | None = None,
) -> None:
    client = _client()
    if n is not None:
        # Fetch the full log buffer and slice locally. Trades bandwidth for
        # compatibility with servers that predate `?n=N` on /tail — worth it
        # because `ilan update` does not restart the long-running server.
        resp = client.get_logs(name)
        if _check_error(resp):
            raise SystemExit(1)
        reply_handle = resp.get("alias") or resp.get("name") or name
        if resp.get("warning"):
            console.print(f"[yellow]{resp['warning']}[/yellow]")
        entries = resp.get("logs", [])
        if not entries:
            if not resp.get("warning"):
                console.print("[yellow]No logs yet.[/yellow]")
            _print_reply_hint(reply_handle)
            return
        entries = entries[-n:]
    else:
        resp = client.get_tail(name)
        if _check_error(resp):
            raise SystemExit(1)
        reply_handle = resp.get("alias") or resp.get("name") or name
        if resp.get("warning"):
            console.print(f"[yellow]{resp['warning']}[/yellow]")
            _print_reply_hint(reply_handle)
            return
        entries = resp["entries"]

    line_number_on = _line_number_enabled() if line_number is None else line_number
    if markdown is None:
        markdown = _markdown_enabled()

    # Each entry is rendered inside a Rich Panel, so any pre-rendered
    # markdown visuals (line-number + markdown mode) must fit within the
    # panel's inner width. ``_PANEL_CHROME`` accounts for the two border
    # columns + the one column of left/right padding we pass below.
    _PANEL_CHROME = 4
    # Reserve some columns for the ``[N] `` line-number prefix so prefixed
    # visuals don't wrap inside the panel. 5 is enough for line counts up
    # to 99 (``[NN] ``); larger counts may wrap by a column or two.
    _PREFIX_BUDGET = 5
    panel_inner_width = max((console.width or 80) - _PANEL_CHROME, 20)
    md_render_width = max(panel_inner_width - _PREFIX_BUDGET, 20)

    # When both modes are on, the line-number scheme indexes the *visual*
    # rendered lines, not the raw source. We render once up-front so the
    # cache (used by ``@N`` reply expansion) stores exactly what the user
    # saw on screen, and re-use the same rendered lines when printing.
    md_visuals_by_entry: dict[int, list[str]] = {}
    numbered_lines: list[str] = []
    if line_number_on:
        for i, entry in enumerate(entries):
            if entry["role"] != "assistant":
                continue
            if markdown:
                visuals = _render_markdown_visual_lines(
                    entry["content"], width=md_render_width
                )
                md_visuals_by_entry[i] = visuals
                # Cache the ANSI-stripped, edge-trimmed text. Stripping the
                # whitespace Rich pads each visual row with keeps `@N` reply
                # expansion tidy: e.g. `@3` → `"pod-0  Pending"` instead of
                # `"  pod-0  Pending  "`.
                for vl in visuals:
                    numbered_lines.append(Text.from_ansi(vl).plain.strip())
            else:
                numbered_lines.extend(entry["content"].splitlines())
        cfg.save_last_tail(name, numbered_lines)
    width = max(len(str(len(numbered_lines))), 1)

    console.print()
    line_idx = 0
    for i, entry in enumerate(entries):
        is_assistant = entry["role"] == "assistant"
        label = "Assistant" if is_assistant else "User"
        border_style = "cyan" if is_assistant else "green"
        ts = _format_ts(entry["timestamp"]) if entry.get("timestamp") else ""
        title = Text()
        title.append(label, style=f"bold {border_style}")
        if ts:
            title.append(f"  ({ts})", style="dim")

        body: object
        if markdown and line_number_on and is_assistant:
            text = Text()
            for j, vl in enumerate(md_visuals_by_entry.get(i, [])):
                line_idx += 1
                if j > 0:
                    text.append("\n")
                text.append(f"[{line_idx:>{width}}]", style="yellow")
                text.append(" ")
                text.append(Text.from_ansi(vl))
            body = text
        elif markdown and is_assistant:
            body = Markdown(entry["content"])
        elif line_number_on and is_assistant:
            lines = entry["content"].splitlines()
            if not lines:
                body = Text(entry["content"])
            else:
                text = Text()
                for j, line in enumerate(lines):
                    line_idx += 1
                    if j > 0:
                        text.append("\n")
                    text.append(f"[{line_idx:>{width}}]", style="yellow")
                    text.append(" ")
                    text.append(line)
                body = text
        else:
            body = Text(entry["content"])

        if line_number_on:
            console.print(
                Panel(
                    body,
                    title=title,
                    title_align="left",
                    border_style=border_style,
                    padding=(0, 1),
                )
            )
        else:
            # Line numbers off → drop the Panel chrome too so the output is
            # easy to copy straight out of the terminal without picking up
            # box-drawing characters. Keep the role/timestamp as a plain
            # header line to preserve attribution when ``-n`` shows more
            # than one entry.
            console.print(title)
            console.print(body)
        console.print()

    _print_last_model_hint(
        client, name,
        resp.get("last_assistant_model"), resp.get("last_assistant_effort"),
    )
    _print_reply_hint(reply_handle)


@task_group.command("tail")
@click.argument("name", shell_complete=_complete_task_names)
@click.option("-n", "--num", "num", type=int, default=None,
              help="Show the final N messages (assistant + user combined).")
@click.option("-m", "--md", "markdown", is_flag=True, default=False,
              help="Render assistant messages as Markdown (overrides the "
                   "``markdown`` config key for this invocation).")
@click.option("--line-number/--no-line-number", "line_number", default=None,
              help="Override the ``line-number`` config for this invocation.")
def task_tail(name: str, num: int | None, markdown: bool, line_number: bool | None) -> None:
    """Show the last assistant message, the user prompt that elicited it,
    and any user messages after it.

    With -n N, show the final N messages (assistant + user combined).
    """
    _do_tail(name, n=num, markdown=markdown or None, line_number=line_number)


# ── task reply ───────────────────────────────────────────────────────

def _model_switch_needed(name: str, max_: bool) -> bool:
    """Return False when the task's model is already in the requested state."""
    resp = _client().get_task(name)
    if _check_error(resp):
        raise SystemExit(1)
    model = resp.get("task", {}).get("model")
    return not is_fable_model(model) if max_ else model is not None


def _do_reply(
    name: str, message: str, max_: bool = False, unmax: bool = False
) -> None:
    # Switch the model first so this reply's own turn (and every one after
    # it) already runs on the new model. On a codex task the max endpoint
    # warns and leaves the model untouched; the reply is still posted. If
    # the model is already in the requested state the switch is silently
    # skipped.
    if max_ and _model_switch_needed(name, max_=True):
        _do_max(name)
    elif unmax and _model_switch_needed(name, max_=False):
        _do_unmax(name)
    if _line_number_enabled():
        message = _expand_at_refs(message, cfg.load_last_tail(name))
    resp = _client().reply(name, message)
    if _check_error(resp):
        raise SystemExit(1)
    if resp.get("warning"):
        console.print(f"[yellow]{resp['warning']}[/yellow]")
    elif resp.get("message"):
        console.print(f"[green]{resp['message']}[/green]")


def _check_reply_model_flags(
    message: str | None, max_: bool, unmax: bool
) -> None:
    """Validate the ``--max``/``--unmax`` reply flags, exiting on misuse."""
    if max_ and unmax:
        console.print("[red]--max and --unmax cannot be used together.[/red]")
        raise SystemExit(1)
    if (max_ or unmax) and message is None:
        console.print(
            "[red]--max/--unmax require a response message (to only switch "
            "the model, use ilan max / ilan unmax).[/red]"
        )
        raise SystemExit(1)


@task_group.command("reply")
@click.argument("name", shell_complete=_complete_task_names)
@click.argument("message", required=False, default=None)
@click.option("-n", "--num", "num", type=int, default=None,
              help="When no message is given, show the final N messages.")
@click.option("-m", "--md", "markdown", is_flag=True, default=False,
              help="When no message is given, render assistant messages as Markdown.")
@click.option("--line-number/--no-line-number", "line_number", default=None,
              help="When no message is given, override the ``line-number`` "
                   "config for this invocation.")
@click.option("--max", "max_", is_flag=True, default=False,
              help="Switch the task to the Fable model before posting the "
                   "reply; the model persists for all subsequent messages. "
                   "Claude backend only (a codex task warns and replies "
                   "unchanged).")
@click.option("--unmax", "unmax", is_flag=True, default=False,
              help="Reset the task's model to the config default before "
                   "posting the reply.")
def task_reply(
    name: str, message: str | None, num: int | None, markdown: bool,
    line_number: bool | None, max_: bool, unmax: bool,
) -> None:
    """Send a response to a task. If no message is given, show the tail instead."""
    _check_reply_model_flags(message, max_, unmax)
    if message is None:
        _do_tail(name, n=num, markdown=markdown or None, line_number=line_number)
    else:
        if line_number is not None:
            console.print(
                "[red]--line-number/--no-line-number cannot be used when a "
                "response message is provided.[/red]"
            )
            raise SystemExit(1)
        _do_reply(name, message, max_=max_, unmax=unmax)


# ── task tap ─────────────────────────────────────────────────────────

TAP_MESSAGE = "How are things now? Pause what you are doing and give me a quick summary of the current situation."


TAP_ALLOWED_STATUSES = (
    TaskStatus.WORKING,
    TaskStatus.AGENT_FINISHED,
    TaskStatus.NEEDS_ATTENTION,
    TaskStatus.ERROR,
)


def _do_tap(name: str) -> None:
    client = _client()
    resp = client.get_task(name)
    if _check_error(resp):
        raise SystemExit(1)
    t = resp["task"]
    task_name = t["name"]
    status = TaskStatus(t["status"])
    if status not in TAP_ALLOWED_STATUSES:
        allowed = ", ".join(s.value for s in TAP_ALLOWED_STATUSES)
        console.print(
            f"[yellow]Task [bold]{task_name}[/bold] is {status.value}. "
            f"Tap only works on tasks whose status is one of: {allowed}.[/yellow]"
        )
        return
    _do_reply(task_name, TAP_MESSAGE)


@task_group.command("tap")
@click.argument("name", shell_complete=_complete_task_names)
def task_tap(name: str) -> None:
    """Ask a WORKING / AGENT_FINISHED / NEEDS_ATTENTION / ERROR agent for a status update."""
    _do_tap(name)


# ── task sleep ───────────────────────────────────────────────────────

SLEEP_STYLE = "yellow"


def _format_sleep_suffix(sleep_seconds: int | None) -> str | None:
    """Return ``(sleeping for Xs)`` for a task that has an active sleep."""
    if not sleep_seconds or sleep_seconds <= 0:
        return None
    return f" (sleeping for {int(sleep_seconds)}s)"


_SLEEP_SECOND_UNITS = frozenset({"s", "sec", "second", "seconds"})
_SLEEP_MINUTE_UNITS = frozenset({"m", "min", "mins", "minute", "minutes"})
_SLEEP_HOUR_UNITS = frozenset({"h", "hr", "hrs", "hour", "hours"})
_SLEEP_DURATION_RE = re.compile(r"^(\d+\.?\d*|\.\d+)([A-Za-z]*)$")


def _parse_sleep_duration(value: str) -> int:
    """Parse a sleep duration like ``300s``, ``5m``, ``2h``, or ``1.5h``.

    The number may be an integer or a decimal (e.g. ``1.5h`` → 5400s). No
    whitespace is allowed between the number and the unit. A bare number
    is interpreted as seconds. Returns the duration in whole seconds
    (rounded to the nearest second).
    """
    match = _SLEEP_DURATION_RE.match(value)
    if not match:
        raise ValueError(
            f"invalid duration {value!r}: expected e.g. '300', '300s', '5m', '1.5h'"
        )
    number = float(match.group(1))
    unit = match.group(2).lower()
    if unit == "" or unit in _SLEEP_SECOND_UNITS:
        multiplier = 1
    elif unit in _SLEEP_MINUTE_UNITS:
        multiplier = 60
    elif unit in _SLEEP_HOUR_UNITS:
        multiplier = 3600
    else:
        raise ValueError(
            f"invalid duration unit {match.group(2)!r} in {value!r}: "
            "use s/sec/second(s), m/min(s)/minute(s), or h/hr(s)/hour(s)"
        )
    return int(round(number * multiplier))


def _do_sleep(name: str, duration: str) -> None:
    try:
        seconds = _parse_sleep_duration(duration)
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise SystemExit(1)
    if seconds <= 0:
        console.print("[red]duration must be positive[/red]")
        raise SystemExit(1)
    resp = _client().sleep_task(name, seconds)
    if _check_error(resp):
        raise SystemExit(1)
    task_name = resp.get("name", name)
    console.print(
        f"[green]Told [bold]{task_name}[/bold] to sleep {seconds}s and report back.[/green]"
    )


@task_group.command("sleep")
@click.argument("name", shell_complete=_complete_task_names)
@click.argument("duration")
def task_sleep(name: str, duration: str) -> None:
    """Tell a NEEDS_ATTENTION / AGENT_FINISHED task to sleep for DURATION and report back.

    DURATION accepts an integer or decimal with an optional unit suffix
    (no whitespace): e.g. ``300``, ``300s``, ``5m``, ``2h``, ``1.5h``.
    Bare numbers are seconds. Unit aliases: seconds = s/sec/second/seconds,
    minutes = m/min/mins/minute/minutes, hours = h/hr/hrs/hour/hours.
    """
    _do_sleep(name, duration)


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

def _do_rename(old_name: str, new_name: str, description: str | None = None) -> None:
    resp = _client().rename_task(old_name, new_name)
    if _check_error(resp):
        raise SystemExit(1)
    console.print(
        f"[green]Renamed [bold]{resp['old_name']}[/bold] → [bold]{resp['new_name']}[/bold][/green]"
    )
    if description is not None:
        _do_reply(resp["new_name"], description)


@task_group.command("rename")
@click.argument("old_name", shell_complete=_complete_task_names)
@click.argument("new_name")
@click.option("-d", "--description", default=None,
              help="Reply to send to the task right after renaming.")
def task_rename(old_name: str, new_name: str, description: str | None) -> None:
    """Rename a task."""
    _do_rename(old_name, new_name, description)


# ── task alias ──────────────────────────────────────────────────────

def _do_set_alias(name: str, new_alias: str) -> None:
    resp = _client().set_alias(name, new_alias)
    if _check_error(resp):
        raise SystemExit(1)
    console.print(
        f"[green]Alias for [bold]{resp['name']}[/bold] set to "
        f"[bold]{resp['alias']}[/bold].[/green]"
    )


@task_group.command("alias")
@click.argument("name", shell_complete=_complete_task_names)
@click.argument("new_alias")
def task_alias(name: str, new_alias: str) -> None:
    """Set the two-letter alias of an active task."""
    _do_set_alias(name, new_alias)


# ── task branch ─────────────────────────────────────────────────────

def _do_branch(
    old_name: str,
    new_name: str,
    file_path: str | None,
    description: str | None,
) -> None:
    if file_path is not None and description is not None:
        console.print("[red]Pass at most one of --file / --description.[/red]")
        raise SystemExit(1)
    message: str | None = None
    if file_path is not None:
        message = Path(file_path).read_text()
    elif description is not None:
        message = description

    if message is not None and _line_number_enabled():
        message = _expand_at_refs(message, cfg.load_last_tail(old_name))

    resp = _client().branch_task(old_name, new_name, message)
    if _check_error(resp):
        raise SystemExit(1)
    child = resp.get("name", new_name)
    parent = resp.get("parent_name", old_name)
    console.print(
        f"[green]Branched [bold]{child}[/bold] from [bold]{parent}[/bold].[/green]"
    )


@task_group.command("branch")
@click.argument("old_name", shell_complete=_complete_task_names)
@click.option("-n", "--name", "new_name", required=True,
              help="Short name for the branched (new) task.")
@click.option("-f", "--file", "file_path", type=click.Path(exists=True), default=None,
              help="Path to a file containing the first reply to the child task.")
@click.option("-d", "--description", default=None,
              help="Inline first reply to the child task.")
def task_branch(
    old_name: str, new_name: str, file_path: str | None, description: str | None
) -> None:
    """Branch a new task from OLD_NAME, inheriting its full context."""
    _do_branch(old_name, new_name, file_path, description)


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

    engine = t.get("engine") or DEFAULT_ENGINE
    if engine == ENGINE_CODEX:
        console.print(
            f"[yellow]Task [bold]{t['name']}[/bold] runs on the Codex backend; "
            f"ilan attach only supports Claude Code sessions. "
            f"Resume it directly with [bold]codex resume {session_id}[/bold], or switch "
            f"the task back with [bold]ilan switch-backend {t['name']}[/bold] first.[/yellow]"
        )
        raise SystemExit(1)

    if not ClaudeBackend().find_session_log(session_id):
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
        "--model", str(conf["model-claude"]),
        "--effort", str(conf.get("effort", "xhigh")),
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


# ── task open ────────────────────────────────────────────────────────

def _do_open(name: str) -> None:
    resp = _client().get_history_url(name)
    if _check_error(resp):
        raise SystemExit(1)
    url = resp.get("url")
    if not url:
        console.print(
            f"[yellow]Task '{name}' has no history page yet.[/yellow]"
        )
        return
    console.print(f"Opening {url}")
    webbrowser.open(url)


@task_group.command("open")
@click.argument("name", shell_complete=_complete_task_names)
def task_open(name: str) -> None:
    """Open the task's Gist history page in the default browser."""
    _do_open(name)


# ── task rm ──────────────────────────────────────────────────────────

@task_group.command("rm")
@click.argument("names", nargs=-1, required=True, shell_complete=_complete_task_names)
@click.option("-y", "--yes", is_flag=True, help="Skip confirmation.")
@click.option("-f", "--force", is_flag=True,
              help="Delete even if the task has active (non-terminal) descendants.")
def task_rm(names: tuple[str, ...], yes: bool, force: bool) -> None:
    """Remove one or more tasks and all their data.

    Refuses if any target has active descendants that are not also in the
    batch — e.g. ``ilan task rm parent child`` is allowed when parent's only
    active descendant is child, but ``ilan task rm parent`` on its own is not.
    ``-f`` overrides the check entirely.
    """
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

    if not force:
        all_rows = client.list_tasks(show_all=True).get("tasks", [])
        by_name = {r["name"]: r for r in all_rows}
        children_map: dict[str, list[str]] = {}
        for r in all_rows:
            parent = r.get("parent_name")
            if parent:
                children_map.setdefault(parent, []).append(r["name"])

        batch = set(found)
        blockers: dict[str, list[str]] = {}
        for target in found:
            outside_active: list[str] = []
            stack = list(children_map.get(target, []))
            seen: set[str] = set()
            while stack:
                d = stack.pop()
                if d in seen:
                    continue
                seen.add(d)
                stack.extend(children_map.get(d, []))
                if d in batch:
                    continue  # caller is deleting this too
                info = by_name.get(d)
                if info is None:
                    continue
                if not TaskStatus(info["status"]).is_terminal:
                    outside_active.append(d)
            if outside_active:
                blockers[target] = sorted(outside_active)

        if blockers:
            console.print(
                "[yellow]Refusing to delete: some targets have active "
                "descendants outside this batch.[/yellow]"
            )
            for target, outside in blockers.items():
                console.print(f"  [bold]{target}[/bold] → {', '.join(outside)}")
            console.print("[dim]Re-run with [bold]-f[/bold] to force delete.[/dim]")
            raise SystemExit(1)

    if not yes:
        if not click.confirm(f"Remove {len(found)} task(s): {', '.join(found)}?"):
            return

    # We've already validated the batch above (or the user passed -f), so
    # pass force=True to the server to bypass its per-task descendant check
    # — otherwise deleting the outer ancestor in a parent+child batch would
    # trip the single-task guard on the server side.
    removed: list[str] = []
    failed: list[str] = []
    for n in found:
        resp = client.delete_task(n, force=True)
        if "error" in resp:
            console.print(f"[yellow]{resp['error']}[/yellow]")
            failed.append(n)
        else:
            removed.append(n)
    if removed:
        console.print(f"[green]Removed: {', '.join(removed)}[/green]")
    if failed:
        raise SystemExit(1)


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


# ── task unread ──────────────────────────────────────────────────────

def _do_unread(names: tuple[str, ...]) -> None:
    client = _client()
    failed = False
    for name in names:
        resp = client.mark_unread(name)
        if _check_error(resp):
            failed = True
        else:
            task_name = resp.get("name", name)
            console.print(f"[yellow]Task [bold]{task_name}[/bold] marked unread.[/yellow]")
    if failed:
        raise SystemExit(1)


@task_group.command("unread")
@click.argument("names", nargs=-1, required=True, shell_complete=_complete_task_names)
def task_unread(names: tuple[str, ...]) -> None:
    """Restore the unread marker on one or more tasks."""
    _do_unread(names)


# ── task max / unmax ─────────────────────────────────────────────────

def _do_max(name: str) -> None:
    resp = _client().max_task(name)
    if _check_error(resp):
        raise SystemExit(1)
    warning = resp.get("warning")
    if warning:
        console.print(f"[yellow]{warning}[/yellow]")
        return
    task_name = resp.get("name", name)
    model = resp.get("model", "")
    console.print(
        f"[green]Task [bold]{task_name}[/bold] set to [bold red]FABLE[/bold red] "
        f"([cyan]{model}[/cyan]).[/green]"
    )


def _do_unmax(name: str) -> None:
    resp = _client().unmax_task(name)
    if _check_error(resp):
        raise SystemExit(1)
    task_name = resp.get("name", name)
    console.print(
        f"[green]Task [bold]{task_name}[/bold] reset to the default model.[/green]"
    )


@task_group.command("max")
@click.argument("name", shell_complete=_complete_task_names)
def task_max(name: str) -> None:
    """Run a task on the Fable model (claude-fable-5)."""
    _do_max(name)


@task_group.command("unmax")
@click.argument("name", shell_complete=_complete_task_names)
def task_unmax(name: str) -> None:
    """Reset a task's model back to the config default."""
    _do_unmax(name)


# ── task switch-backend ──────────────────────────────────────────────

def _do_switch_backend(name: str) -> None:
    resp = _client().switch_backend(name)
    if _check_error(resp):
        raise SystemExit(1)
    task_name = resp.get("name", name)
    from_engine = resp.get("from_engine", "")
    engine = resp.get("engine", "")
    from_style = ENGINE_NAME_STYLE.get(from_engine, "")
    to_style = ENGINE_NAME_STYLE.get(engine, "")
    console.print(
        f"[green]Task [bold]{task_name}[/bold] switched backend "
        f"[bold {from_style}]{from_engine}[/bold {from_style}] "
        f"-> [bold {to_style}]{engine}[/bold {to_style}].[/green]"
    )


@task_group.command("switch-backend")
@click.argument("name", shell_complete=_complete_task_names)
def task_switch_backend(name: str) -> None:
    """Toggle a task's agent backend (claude <-> codex).

    The switch is lazy: it only rewires which backend the task uses on its
    next spawn, and the task catches up on its next turn. A WORKING task
    cannot be switched — wait for the agent to finish (or kill it) first.
    """
    _do_switch_backend(name)


# ── top-level shorthands ─────────────────────────────────────────────

@main.command("add")
@click.option("-n", "--name", required=True, help="Short name for the task.")
@click.option("-f", "--file", "file_path", type=click.Path(exists=True), default=None,
              help="Path to a file containing the task prompt.")
@click.option("-d", "--description", default=None, help="Inline task prompt.")
@click.option("--claude", "agent", flag_value="claude", default=None,
              help="Run this task on the Claude backend (the default).")
@click.option("--codex", "agent", flag_value="codex",
              help="Run this task on the Codex backend.")
@click.option("--max", "max_model", is_flag=True, default=False,
              help="Create the task on the Fable model (implies --claude).")
def shortcut_add(
    name: str, file_path: str | None, description: str | None,
    agent: str | None, max_model: bool,
) -> None:
    """Shorthand for 'ilan task add'."""
    _do_add(name, file_path, description, agent, max_model)


@main.command("ls")
@click.argument("name", required=False, default=None, shell_complete=_complete_task_names)
@click.option("-a", "--all", "show_all", is_flag=True, help="Include DONE and DISCARDED tasks.")
@click.option("-n", "--num", "num", type=int, default=None,
              help="When a task name is given, show the final N messages.")
@click.option("--line-number/--no-line-number", "line_number", default=None,
              help="When a task name is given, override the ``line-number`` "
                   "config for this invocation.")
def shortcut_ls(
    name: str | None, show_all: bool, num: int | None, line_number: bool | None,
) -> None:
    """Shorthand for 'ilan task ls'. If a task name is given, acts as 'ilan tail'."""
    if name is not None:
        _do_tail(name, n=num, line_number=line_number)
    else:
        if line_number is not None:
            console.print(
                "[red]--line-number/--no-line-number requires a task name.[/red]"
            )
            raise SystemExit(1)
        _do_ls(show_all)


@main.command("tail")
@click.argument("name", shell_complete=_complete_task_names)
@click.option("-n", "--num", "num", type=int, default=None,
              help="Show the final N messages (assistant + user combined).")
@click.option("-m", "--md", "markdown", is_flag=True, default=False,
              help="Render assistant messages as Markdown (overrides the "
                   "``markdown`` config key for this invocation).")
@click.option("--line-number/--no-line-number", "line_number", default=None,
              help="Override the ``line-number`` config for this invocation.")
def shortcut_tail(
    name: str, num: int | None, markdown: bool, line_number: bool | None,
) -> None:
    """Shorthand for 'ilan task tail'."""
    _do_tail(name, n=num, markdown=markdown or None, line_number=line_number)


@main.command("reply")
@click.argument("name", shell_complete=_complete_task_names)
@click.argument("message", required=False, default=None)
@click.option("-n", "--num", "num", type=int, default=None,
              help="When no message is given, show the final N messages.")
@click.option("-m", "--md", "markdown", is_flag=True, default=False,
              help="When no message is given, render assistant messages as Markdown.")
@click.option("--line-number/--no-line-number", "line_number", default=None,
              help="When no message is given, override the ``line-number`` "
                   "config for this invocation.")
@click.option("--max", "max_", is_flag=True, default=False,
              help="Switch the task to the Fable model before posting the "
                   "reply; the model persists for all subsequent messages. "
                   "Claude backend only (a codex task warns and replies "
                   "unchanged).")
@click.option("--unmax", "unmax", is_flag=True, default=False,
              help="Reset the task's model to the config default before "
                   "posting the reply.")
def shortcut_reply(
    name: str, message: str | None, num: int | None, markdown: bool,
    line_number: bool | None, max_: bool, unmax: bool,
) -> None:
    """Shorthand for 'ilan task reply'."""
    _check_reply_model_flags(message, max_, unmax)
    if message is None:
        _do_tail(name, n=num, markdown=markdown or None, line_number=line_number)
    else:
        if line_number is not None:
            console.print(
                "[red]--line-number/--no-line-number cannot be used when a "
                "response message is provided.[/red]"
            )
            raise SystemExit(1)
        _do_reply(name, message, max_=max_, unmax=unmax)


@main.command("re")
@click.argument("name", shell_complete=_complete_task_names)
@click.argument("message", required=False, default=None)
@click.option("-n", "--num", "num", type=int, default=None,
              help="When no message is given, show the final N messages.")
@click.option("-m", "--md", "markdown", is_flag=True, default=False,
              help="When no message is given, render assistant messages as Markdown.")
@click.option("--line-number/--no-line-number", "line_number", default=None,
              help="When no message is given, override the ``line-number`` "
                   "config for this invocation.")
@click.option("--max", "max_", is_flag=True, default=False,
              help="Switch the task to the Fable model before posting the "
                   "reply; the model persists for all subsequent messages. "
                   "Claude backend only (a codex task warns and replies "
                   "unchanged).")
@click.option("--unmax", "unmax", is_flag=True, default=False,
              help="Reset the task's model to the config default before "
                   "posting the reply.")
def shortcut_re(
    name: str, message: str | None, num: int | None, markdown: bool,
    line_number: bool | None, max_: bool, unmax: bool,
) -> None:
    """Shorthand for 'ilan task reply'."""
    _check_reply_model_flags(message, max_, unmax)
    if message is None:
        _do_tail(name, n=num, markdown=markdown or None, line_number=line_number)
    else:
        if line_number is not None:
            console.print(
                "[red]--line-number/--no-line-number cannot be used when a "
                "response message is provided.[/red]"
            )
            raise SystemExit(1)
        _do_reply(name, message, max_=max_, unmax=unmax)


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


@main.command("unread")
@click.argument("names", nargs=-1, required=True, shell_complete=_complete_task_names)
def shortcut_unread(names: tuple[str, ...]) -> None:
    """Shorthand for 'ilan task unread'."""
    _do_unread(names)


@main.command("max")
@click.argument("name", shell_complete=_complete_task_names)
def shortcut_max(name: str) -> None:
    """Shorthand for 'ilan task max'."""
    _do_max(name)


@main.command("unmax")
@click.argument("name", shell_complete=_complete_task_names)
def shortcut_unmax(name: str) -> None:
    """Shorthand for 'ilan task unmax'."""
    _do_unmax(name)


@main.command("switch-backend")
@click.argument("name", shell_complete=_complete_task_names)
def shortcut_switch_backend(name: str) -> None:
    """Shorthand for 'ilan task switch-backend'."""
    _do_switch_backend(name)


@main.command("rename")
@click.argument("old_name", shell_complete=_complete_task_names)
@click.argument("new_name")
@click.option("-d", "--description", default=None,
              help="Reply to send to the task right after renaming.")
def shortcut_rename(old_name: str, new_name: str, description: str | None) -> None:
    """Shorthand for 'ilan task rename'."""
    _do_rename(old_name, new_name, description)


@main.command("alias")
@click.argument("name", shell_complete=_complete_task_names)
@click.argument("new_alias")
def shortcut_alias(name: str, new_alias: str) -> None:
    """Shorthand for 'ilan task alias'."""
    _do_set_alias(name, new_alias)


@main.command("branch")
@click.argument("old_name", shell_complete=_complete_task_names)
@click.option("-n", "--name", "new_name", required=True,
              help="Short name for the branched (new) task.")
@click.option("-f", "--file", "file_path", type=click.Path(exists=True), default=None,
              help="Path to a file containing the first reply to the child task.")
@click.option("-d", "--description", default=None,
              help="Inline first reply to the child task.")
def shortcut_branch(
    old_name: str, new_name: str, file_path: str | None, description: str | None
) -> None:
    """Shorthand for 'ilan task branch'."""
    _do_branch(old_name, new_name, file_path, description)


@main.command("tap")
@click.argument("name", shell_complete=_complete_task_names)
def shortcut_tap(name: str) -> None:
    """Shorthand for 'ilan task tap'."""
    _do_tap(name)


@main.command("sleep")
@click.argument("name", shell_complete=_complete_task_names)
@click.argument("duration")
def shortcut_sleep(name: str, duration: str) -> None:
    """Shorthand for 'ilan task sleep'."""
    _do_sleep(name, duration)


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


@main.command("open")
@click.argument("name", shell_complete=_complete_task_names)
def shortcut_open(name: str) -> None:
    """Shorthand for 'ilan task open'."""
    _do_open(name)


@main.command("check-model")
@click.argument("name", shell_complete=_complete_task_names)
def shortcut_check_model(name: str) -> None:
    """Shorthand for 'ilan task check-model'."""
    _do_check_model(name)


# ── dashboard ────────────────────────────────────────────────────────

def _build_dashboard_table(
    rows: list[dict], tz: ZoneInfo, show_one_liner: bool = True,
    narrow: bool = False,
) -> Table:
    """Build a Rich Table from task rows, reusing the _do_ls format.

    When ``narrow`` is set (terminal below ``_NARROW_TERMINAL_WIDTH``), the
    ``Created`` column is dropped so the remaining columns stay legible.
    """
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

    # Column sizing depends on whether the Haiku one-line summary is
    # rendered inside the Status cell:
    #   * one-liner ON  → Status needs a much wider slot to fit the summary
    #     under the status label, so we give it 16/34 of the proportional
    #     space and shrink Name to 10/34.
    #   * one-liner OFF → Status only holds a short label + duration
    #     suffix, so we keep the original 14:7:7:4 ratios.
    # In both cases overlong name cells fold within the column instead of
    # pushing it wider.
    table = Table(title=header, expand=True, show_lines=True)
    if show_one_liner:
        table.add_column("(Alias) Name", style="bold", ratio=10)
        table.add_column("Status", ratio=16)
        if not narrow:
            table.add_column("Created", ratio=6)
        table.add_column("Last Changed", ratio=6)
        table.add_column("History", ratio=4)
    else:
        table.add_column("(Alias) Name", style="bold", ratio=14)
        table.add_column("Status", ratio=8)
        if not narrow:
            table.add_column("Created", ratio=7)
        table.add_column("Last Changed", ratio=7)
        table.add_column("History", ratio=4)

    if not rows:
        table.add_row(*(Text("No active tasks.", style="dim"),
                        *[""] * (len(table.columns) - 1)))
        return table

    for row, prefix in _order_tasks_as_forest(rows):
        changed = _format_ts(row["status_changed_at"], seconds=False) if row.get("status_changed_at") else ""
        cells = [
            _build_name_cell(row, prefix),
            _build_status_cell(row, show_one_liner=show_one_liner),
        ]
        if not narrow:
            cells.append(_format_ts(row["created_at"], seconds=False))
        cells.append(changed)
        cells.append(_build_history_cell(row))
        table.add_row(*cells)
    return table


def _do_dashboard() -> None:
    """Full-screen real-time task dashboard (like htop)."""
    client = _client()
    _maybe_warn_one_liner_unconfigured(client)
    interval = max(1, int(cfg.load().get("dashboard-interval", 1)))

    def fetch_and_render() -> Table:
        # Re-read the time-zone (and one-line-summary) on each render so the
        # dashboard stays in sync with `_format_ts` and the cell builders.
        # Otherwise editing `time-zone` or `one-line-summary` while the
        # dashboard is running leaves it stuck on the values loaded at startup.
        tz = ZoneInfo(str(cfg.load().get("time-zone", "US/Pacific")))
        show_one_liner = _one_liner_enabled()
        narrow = _terminal_is_narrow()
        try:
            resp = client.list_tasks(show_all=False)
            rows = resp["tasks"]
        except Exception:
            rows = []
        return _build_dashboard_table(
            rows, tz, show_one_liner=show_one_liner, narrow=narrow,
        )

    def _refresh(live: Live) -> None:
        live.update(fetch_and_render())

    # Put terminal in raw mode to capture single keypresses.
    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        with Live(fetch_and_render(), console=console, screen=True, refresh_per_second=1) as live:
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

    parent_names = {r["parent_name"] for r in rows if r.get("parent_name")}

    stale: list[dict] = []
    skipped_with_children: list[str] = []
    for r in rows:
        changed = r.get("status_changed_at") or r.get("created_at", "")
        if not changed:
            continue
        ts = datetime.fromisoformat(changed)
        if ts >= cutoff:
            continue
        if r["name"] in parent_names:
            skipped_with_children.append(r["name"])
            continue
        stale.append(r)

    if skipped_with_children:
        console.print(
            "[dim]Skipped (has children — use [bold]ilan task rm -f[/bold] to drop the whole subtree):"
            f" {', '.join(skipped_with_children)}[/dim]"
        )

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


def _branch_in_other_worktree(repo: Path, branch: str) -> Path | None:
    """Return the path of another worktree where ``branch`` is checked out.

    Returns ``None`` if ``branch`` is not checked out anywhere, or if it is
    only checked out in ``repo`` itself. ``git checkout`` (with or without
    ``-B``) refuses to act on a branch that is checked out in another
    worktree, so we detect that case to fall back to a detached checkout.
    """
    result = subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        cwd=repo, capture_output=True, text=True,
    )
    if result.returncode != 0:
        return None
    target_ref = f"refs/heads/{branch}"
    try:
        repo_resolved = repo.resolve()
    except OSError:
        repo_resolved = repo
    current_worktree: str | None = None
    for line in result.stdout.splitlines():
        if line.startswith("worktree "):
            current_worktree = line[len("worktree "):].strip()
        elif line.startswith("branch "):
            ref = line[len("branch "):].strip()
            if ref == target_ref and current_worktree is not None:
                wt_path = Path(current_worktree)
                try:
                    wt_resolved = wt_path.resolve()
                except OSError:
                    wt_resolved = wt_path
                if wt_resolved != repo_resolved:
                    return wt_path
    return None


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

        other_wt = _branch_in_other_worktree(repo, branch)
        if other_wt is not None:
            # Branch is checked out in another worktree — `git checkout` (even
            # with -B) would refuse, and we don't want to disturb that
            # worktree's HEAD anyway. Detach onto origin/<branch> so the
            # working directory matches the latest fetched code.
            console.print(
                f"[yellow]Branch [bold]{branch}[/bold] is checked out at "
                f"{other_wt}; using detached HEAD at "
                f"[bold]origin/{branch}[/bold].[/yellow]"
            )
            checkout = subprocess.run(
                ["git", "checkout", "--detach", f"origin/{branch}"],
                cwd=repo, capture_output=True, text=True,
            )
            if checkout.returncode != 0:
                console.print(f"[red]git checkout failed:[/red]\n{checkout.stderr.strip()}")
                raise SystemExit(1)
        else:
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
            # Pull latest for the checked-out branch. (Skipped in the
            # detached-HEAD path above because `checkout --detach
            # origin/<branch>` already lands us at the freshly-fetched tip.)
            pull = subprocess.run(
                ["git", "pull", "origin", branch],
                cwd=repo, capture_output=True, text=True,
            )
            if pull.returncode != 0:
                console.print(f"[red]git pull failed:[/red]\n{pull.stderr.strip()}")
                raise SystemExit(1)
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
