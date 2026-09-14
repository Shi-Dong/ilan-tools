"""Rich labels, cells and styles shared by terminal task views."""

from __future__ import annotations

from rich.text import Text

from ilan.models import (
    DEFAULT_ENGINE,
    ENGINE_NAME_STYLE,
    TaskStatus,
    display_status,
    max_tag,
)
from ilan.time_format import (
    _format_elapsed,
    _format_reply_every_suffix,
    _format_sleep_suffix,
)


SLEEP_STYLE = "yellow"
# Same foreground as the sleep suffix, distinguished by the background fill,
# which _build_name_cell also paints under the row's pin/alias/name.
REPLY_EVERY_BG = "grey27"
REPLY_EVERY_STYLE = f"{SLEEP_STYLE} on {REPLY_EVERY_BG}"

# The style the `FABLE` / `ASTRA` tag is drawn in: the `ilan max` and
# `ilan add --max` confirmations print the tag in it, and it is what the tag
# looked like on its own line beneath the name while it was still part of the
# listings. `red` is the terminal theme's own red (ANSI colour 1) rather than
# a fixed palette entry, so it follows the theme like the confirmation does.
MAX_TAG_STYLE = "bold red"
# The alias is the handle you type, so it is bold either way. A maxed task is
# marked twice over: by the alias's *shape* — `[GK]`, capitals inside square
# brackets, where an ordinary task shows `(gk)` — and by its colour, the tag's
# red in place of pink, so the alias carries the mark the tag used to. The
# shape holds the fact on its own where colour is lost (`NO_COLOR`, a
# plain-text paste). See :func:`_format_alias` and :func:`_alias_style`.
ALIAS_STYLE = "bold pink1"
ALIAS_MAXED_STYLE = MAX_TAG_STYLE
NUMBER_STYLE = "dim"
PIN_STYLE = "bold yellow"
PIN_MARKER = "→ "
UNREAD_STYLE = "bold yellow"
UNREAD_MARKER = "!!"
# The note sits on its own line under the alias and name, so it needs to
# read as a different kind of text from both: italic sets it apart from the
# name above it, and light green from the yellow-italic summary in the Status
# cell beside it and from the plain `green` of AGENT_FINISHED. `not bold`
# because the whole Name column is bold and the note would inherit it — a
# reminder should sit quieter than the name it hangs under, not match it.
NOTES_STYLE = "not bold italic light_green"
# The agent's one-line summary, wherever it is shown: under the status label
# in the listings, and in its own `ilan info` field. One constant so the two
# cannot drift apart.
ONE_LINER_STYLE = "yellow italic"
# The dashboard splits its flexible space equally between Name and Status;
# the timestamp columns are pinned. Both are prose columns now — Name carries
# the note beneath the alias and name, Status the one-line summary beneath
# the label — and neither has a claim on more room than the other.
NAME_TO_STATUS = (1, 1)
# `_format_ts(..., seconds=False)` is longest as "Yesterday 21:38 CEST": nine
# for the day word, a space, five for the time, a space, and up to four for a
# zone abbreviation. At 20 no stamp folds onto a second line in any common
# zone. Pinned, because under `expand=True` a ratio share grew with the
# terminal, far past anything a timestamp needs. `Last Changed` is the only
# column it applies to now — see `PROSE_MAX_WIDTH`.
TIMESTAMP_COLUMN_WIDTH = 20
# `ilan ls` sizes its columns to their contents. The two prose columns share
# one cap, so a long note or summary folds within its cell instead of pushing
# the table off the right edge, and the two come out equal whenever both are
# full. Capped rather than pinned: a listing of short names and short
# statuses should not reserve room prose would have needed.
#
# 52 is the 42 the cap held while `Created` was still a column plus half of
# that column's 20 characters — the same even split `NAME_TO_STATUS` gives
# the dashboard's flexible space, so between them the two prose columns
# reclaim all of it rather than leaving it blank to the right of the table.
# The dashboard needs no cap of its own: its prose columns are ratios under
# `expand=True`, so they absorbed `Created`'s width the moment it left.
PROSE_MAX_WIDTH = 52


def _append_task_number(text: Text, row: dict) -> None:
    """Prefix *text* with the task number when *row* is a closed task.

    Only DONE / DISCARDED rows carry the number: it is the handle ``undone`` /
    ``undiscard`` accept, and neither applies to a live task, so showing it on
    one would advertise a reference that does not resolve.
    """
    if TaskStatus(row["status"]).is_terminal and (number := row.get("number")):
        text.append(f"{number} ", style=NUMBER_STYLE)


def _name_style(row: dict) -> str:
    """Style for a task-name span: bold engine color, linked to its Gist.

    The name doubles as an OSC 8 terminal hyperlink to the task's secret-Gist
    conversation mirror, which keeps the long URL out of the table without
    spending a column on a link label. The underline is what marks a name as
    clickable, so it also says at a glance which tasks have been mirrored: a
    task with no Gist yet (mirroring disabled, or the async syncer hasn't
    created it) renders plain and unlinked.

    ``underline`` is placed *before* ``link`` because Rich's style parser
    reads the word after ``link`` as the URL; keeping the URL last means no
    attribute can be swallowed by it.
    """
    engine = row.get("engine") or DEFAULT_ENGINE
    style = f"bold {ENGINE_NAME_STYLE.get(engine, '')}".strip()
    if url := (row.get("gist_url") or "").strip():
        style = f"{style} underline link {url}"
    return style


def _is_maxed(row: dict) -> bool:
    """Whether the task is pinned to the max model of the backend it is on.

    This is :func:`max_tag`'s call — the same predicate the web app's tag
    uses, so the two views cannot disagree about which tasks are maxed. A
    stale foreign pin left over from before a backend switch does not count,
    just as it never earned the tag.
    """
    engine = row.get("engine") or DEFAULT_ENGINE
    return bool(max_tag(engine, row.get("model")))


def _format_alias(row: dict) -> str:
    """The alias as the listings print it: ``(gk)``, or ``[GK]`` once maxed.

    The shape, with the red of :func:`_alias_style`, is all the mark a
    maxed task carries in ``ilan ls``, ``ilan dashboard``, the concise line
    and ``ilan info``; the ``FABLE`` / ``ASTRA`` line it used to get beneath
    its name is gone, since a whole extra line for one word cost every row
    height for a fact the alias can carry itself. Capitals *and* brackets, so
    the mark holds even where case is easy to miss; the lookup accepts either
    case, so an alias copied from the listing resolves as typed.
    """
    alias = row["alias"]
    return f"[{alias.upper()}]" if _is_maxed(row) else f"({alias})"


def _alias_style(row: dict) -> str:
    """Style for a task's alias: pink, or the tag's red once maxed.

    The red is :data:`MAX_TAG_STYLE`, the style the ``FABLE`` / ``ASTRA`` tag
    is drawn in — see the note on :data:`ALIAS_MAXED_STYLE` — so the alias
    inherits the mark the tag carried, on top of its ``[GK]`` shape.
    """
    return ALIAS_MAXED_STYLE if _is_maxed(row) else ALIAS_STYLE


def _build_name_label(row: dict) -> Text:
    """Build the styled "number (alias) name" label, without the note.

    ``needs_review`` rows are flagged with a ``!!`` ASCII marker rather
    than the \u26a0\ufe0f emoji, whose unpredictable terminal width breaks
    Rich's Live layout in ``ilan dashboard`` and visually misaligns the
    ``ilan ls`` table. Pinned rows get a leading ``\u2192`` for the same reason:
    a pushpin emoji would be the obvious marker but has the same width
    problem, whereas a bare arrow glyph occupies a single cell.

    A maxed task is told apart by its alias: ``[GK]`` in the red the tag
    used, rather than ``(gk)`` in pink — see :func:`_format_alias` and
    :func:`_alias_style`.

    The name itself links to the task's Gist conversation mirror — see
    :func:`_name_style`.

    Split out of :func:`_build_name_cell` for ``ilan info``, which gives the
    note a labelled field of its own and so wants the label alone.
    """
    status = TaskStatus(row["status"])
    cell = Text()
    if row.get("pinned"):
        cell.append(PIN_MARKER, style=PIN_STYLE)
    _append_task_number(cell, row)
    if row.get("alias"):
        cell.append(f"{_format_alias(row)} ", style=_alias_style(row))
    cell.append(row["name"], style=_name_style(row))
    if row.get("needs_review"):
        cell.append(f" {UNREAD_MARKER}", style=UNREAD_STYLE)
    if status is TaskStatus.WORKING and (
        sleep_suffix := _format_sleep_suffix(row.get("sleep_seconds"))
    ):
        cell.append(sleep_suffix, style=SLEEP_STYLE)
    # No status filter: unlike a sleep (which only means anything while the
    # agent is WORKING), a reply-every cycle re-fires from any live status.
    if reply_every_suffix := _format_reply_every_suffix(row.get("reply_every_seconds")):
        cell.append(reply_every_suffix, style=REPLY_EVERY_STYLE)
        # Extend the background under the pin/alias/name so the whole label is
        # highlighted; the note line is appended later and stays unfilled.
        cell.stylize(f"on {REPLY_EVERY_BG}")
    return cell


def _build_name_cell(row: dict) -> Text:
    """Build the Name cell: :func:`_build_name_label`, with the note beneath.

    The task's note, if it has one, is the cell's last line, in
    :data:`NOTES_STYLE`: it belongs with the name because it says what the
    task is *for*, where the Status cell says what the agent just did.
    """
    cell = _build_name_label(row)
    if note := (row.get("notes") or "").strip():
        cell.append("\n")
        cell.append(note, style=NOTES_STYLE)
    return cell


def _build_status_cell(row: dict, show_one_liner: bool = True) -> Text:
    """Build the Status cell: the label, how long it has been there, and the
    agent's one-line summary beneath.
    """
    status = TaskStatus(row["status"])
    label, style = display_status(status, row.get("reply_every_seconds"))
    # Apply the status style only to the status span (not as a base style on
    # the parent Text), so a `dim` status style (DONE / DISCARDED) doesn't
    # bleed onto the elapsed-time hint or the one-liner and render them
    # darker than the same spans on other rows.
    cell = Text()
    cell.append(label, style=style)
    if (
        status == TaskStatus.WORKING
        and row.get("status_changed_at")
        and (elapsed := _format_elapsed(row["status_changed_at"]))
    ):
        cell.append(f" (for {elapsed})", style="dim")
    if show_one_liner and (
        one_liner := (row.get("summary_one_liner") or "").strip()
    ):
        cell.append("\n")
        cell.append(one_liner, style=ONE_LINER_STYLE)
    return cell


def _build_concise_task_line(row: dict) -> Text:
    """Build a styled ``→ number (alias) name !! STATUS`` concise line.

    The name links to the task's Gist conversation mirror, same as in the full
    table — see :func:`_name_style`.
    """
    status = TaskStatus(row["status"])
    line = Text()
    if row.get("pinned"):
        line.append(PIN_MARKER, style=PIN_STYLE)
    _append_task_number(line, row)
    if row.get("alias"):
        line.append(f"{_format_alias(row)} ", style=_alias_style(row))
    line.append(row["name"], style=_name_style(row))
    if row.get("needs_review"):
        line.append(f" {UNREAD_MARKER}", style=UNREAD_STYLE)
    line.append(" ")
    label, style = display_status(status, row.get("reply_every_seconds"))
    line.append(label, style=style)
    if reply_every_suffix := _format_reply_every_suffix(
        row.get("reply_every_seconds")
    ):
        line.append(reply_every_suffix, style=REPLY_EVERY_STYLE)
        # Mirror _build_name_cell: paint the background under the whole line
        # so cycling tasks are as easy to spot here as in the full table.
        line.stylize(f"on {REPLY_EVERY_BG}")
    return line
