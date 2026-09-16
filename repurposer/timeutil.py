"""All datetimes are stored in the database as ISO 8601 UTC strings ('YYYY-MM-DDTHH:MM:SS+00:00').

Local times (slots, calendar display) are converted through the configured timezone.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone, date, time
from zoneinfo import ZoneInfo

UTC = timezone.utc


def utcnow() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).replace(microsecond=0).isoformat()


def parse(value: str | None) -> datetime | None:
    if not value:
        return None
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def to_local(dt: datetime | str | None, tz: str) -> datetime | None:
    if isinstance(dt, str):
        dt = parse(dt)
    if dt is None:
        return None
    return dt.astimezone(ZoneInfo(tz))


def local_to_utc(d: date, hhmm: str, tz: str) -> datetime:
    hh, mm = (int(x) for x in hhmm.split(":"))
    local = datetime.combine(d, time(hh, mm), tzinfo=ZoneInfo(tz))
    return local.astimezone(UTC)


def fmt_local(dt: datetime | str | None, tz: str, fmt: str = "%a %d %b %H:%M") -> str:
    local = to_local(dt, tz)
    return local.strftime(fmt) if local else ""


def minutes_ago(dt: datetime | str | None) -> float | None:
    dt = parse(dt) if isinstance(dt, str) else dt
    if dt is None:
        return None
    return (utcnow() - dt).total_seconds() / 60


__all__ = ["utcnow", "iso", "parse", "to_local", "local_to_utc", "fmt_local", "minutes_ago", "timedelta", "UTC"]
