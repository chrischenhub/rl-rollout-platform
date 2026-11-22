"""How the UI shows times: in one display timezone (DISPLAY_TIMEZONE, Pacific by
default), labelled with its abbreviation, so PDT and PST are told apart. The API and
the database keep timestamps in UTC with an explicit offset."""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo


def _local(value: datetime, tz: ZoneInfo) -> datetime:
    return (value if value.tzinfo else value.replace(tzinfo=timezone.utc)).astimezone(tz)


def when(value, tz: ZoneInfo) -> str:
    """A database timestamp as a full local date and time, e.g. 2026-10-29 16:43:38 PDT."""
    return _local(value, tz).strftime("%Y-%m-%d %H:%M:%S %Z") if isinstance(value, datetime) else "—"


def clock(iso: str | None, tz: ZoneInfo) -> str:
    """An ISO timestamp from a transcript as a local time of day, e.g. 16:44:00."""
    try:
        return _local(datetime.fromisoformat(iso), tz).strftime("%H:%M:%S") if iso else ""
    except ValueError:
        return ""


def duration(seconds) -> str:
    """A number of seconds as e.g. 45s, 3m 07s or 1h 05m."""
    if seconds is None:
        return "—"
    s = int(float(seconds))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m {s % 60:02d}s"
    return f"{s // 3600}h {s % 3600 // 60:02d}m"
