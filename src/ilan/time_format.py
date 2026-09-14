"""Time and duration text shared by terminal task views."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from ilan import config as cfg

SLEEP_HOUR_SUFFIX_THRESHOLD_SECONDS = 1800


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


def _format_compact_duration(seconds: int) -> str:
    """Render a positive duration compactly: ``5m``, ``30m``, ``1.5h``."""
    seconds = int(seconds)
    if seconds >= SLEEP_HOUR_SUFFIX_THRESHOLD_SECONDS:
        unit, unit_seconds = "h", 3600
    else:
        unit, unit_seconds = "m", 60
    # Truncate rather than format with `:.1f`, which rounds to nearest and would
    # show a 1799s duration as `30m`. Clamp to one tenth so a duration shorter
    # than 6s still reads as nonzero.
    tenths = max(1, seconds * 10 // unit_seconds)
    whole, remainder = divmod(tenths, 10)
    shown = f"{whole}.{remainder}" if remainder else str(whole)
    return f"{shown}{unit}"


def _format_sleep_suffix(sleep_seconds: int | None) -> str | None:
    """Return ``(sleeping for Xm)`` / ``(sleeping for X.Yh)`` for an active sleep."""
    if not sleep_seconds or sleep_seconds <= 0:
        return None
    return f" (sleeping for {_format_compact_duration(sleep_seconds)})"


def _format_reply_every_suffix(reply_every_seconds: int | None) -> str | None:
    """Return ``(responding every Xm)`` for an active ``reply -t`` cycle."""
    if not reply_every_seconds or reply_every_seconds <= 0:
        return None
    return f" (responding every {_format_compact_duration(reply_every_seconds)})"
