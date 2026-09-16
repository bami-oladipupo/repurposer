"""Assign queued videos to publish times.

Rules (section 8 step 4 of the spec):
  * mode 'asap': new-origin videos publish at max(now, published_at + min_age + delay).
  * mode 'schedule': fill empty future slots. New content claims the earliest free slot it is
    eligible for; existing content fills what remains, from existing_start_from onwards, in the
    configured order. A video never holds two slots on the same platform.
  * Existing (backfill) content always goes through slots, even in asap mode, so a 300-video
    catalogue never floods a channel.
  * Manual (auto_publish off) leaves everything exactly where it is.

Guard rails against bursts (added after five Shorts went out in one run on 2026-09-15, when the
worker had not run for two days and every missed slot was still "due"):
  * A slot that passed more than slot_grace_minutes ago is stale. The video is not published late;
    it goes back to the queue and takes the next free slot (defer_stale / defer).
  * publish_due publishes at most max_publish_per_run videos per platform per run, and at most
    max_publish_per_day per local day (default: the number of slots on that weekday).
"""
from __future__ import annotations

import sqlite3
from datetime import date, timedelta, datetime
from typing import Any

from . import db
from .config import PLATFORMS, PREFIX
from .timeutil import iso, local_to_utc, parse, to_local, utcnow

HORIZON_DAYS = 14
MIN_SLOT_GAP_MINUTES = 120
MAX_SLOTS_PER_DAY = 5


def validate_slots(times: list[str]) -> str | None:
    """Return an error message if a day's slot list breaks the limits, else None."""
    if len(times) > MAX_SLOTS_PER_DAY:
        return f"at most {MAX_SLOTS_PER_DAY} slots per day"
    mins = []
    for t in times:
        try:
            hh, mm = (int(x) for x in t.split(":"))
        except ValueError:
            return f"'{t}' is not a HH:MM time"
        if not (0 <= hh < 24 and 0 <= mm < 60):
            return f"'{t}' is not a valid time"
        mins.append(hh * 60 + mm)
    mins.sort()
    for a, b in zip(mins, mins[1:]):
        if b - a < MIN_SLOT_GAP_MINUTES:
            return "slots must be at least two hours apart"
    return None


def future_slots(conn: sqlite3.Connection, platform: str, tz: str, start: datetime, horizon_days: int = HORIZON_DAYS) -> list[datetime]:
    slots = db.get_slots(conn, platform)
    by_weekday: dict[int, list[str]] = {}
    for s in slots:
        by_weekday.setdefault(int(s["weekday"]), []).append(s["local_time"])
    out: list[datetime] = []
    local_start = to_local(start, tz)
    for offset in range(horizon_days + 1):
        d: date = (local_start + timedelta(days=offset)).date()
        for t in sorted(by_weekday.get(d.weekday(), [])):
            when = local_to_utc(d, t, tz)
            if when >= start:
                out.append(when)
    return out


def taken_slots(conn: sqlite3.Connection, platform: str) -> set[str]:
    px = PREFIX[platform]
    rows = conn.execute(
        f"SELECT {px}_scheduled_for AS w FROM videos WHERE {px}_status = 'scheduled' AND {px}_scheduled_for IS NOT NULL"
    ).fetchall()
    return {r["w"] for r in rows}


def earliest_allowed(video: dict[str, Any], min_age_minutes: int, delay_minutes: int) -> datetime:
    published = parse(video.get("published_at")) or parse(video.get("first_seen")) or utcnow()
    return published + timedelta(minutes=min_age_minutes + delay_minutes)


def _candidates(conn: sqlite3.Connection, platform: str, workflow: dict[str, Any]) -> tuple[list[dict], list[dict]]:
    px = PREFIX[platform]
    base = (
        f"SELECT * FROM videos WHERE {px}_status = 'queued' AND status IN ('new','downloaded','ready') AND origin = ?"
    )
    new = db.rows(conn, base + " ORDER BY published_at ASC", ("new",))
    existing: list[dict] = []
    if workflow.get("content_scope") == "new_and_existing":
        order = "DESC" if workflow.get("existing_order", "newest_first") == "newest_first" else "ASC"
        sql = base
        params: list[Any] = ["existing"]
        if workflow.get("existing_include_before"):
            sql += " AND published_at < ?"
            params.append(workflow["existing_include_before"])
        existing = db.rows(conn, sql + " ORDER BY published_at DESC", params)
        limit = workflow.get("existing_limit")
        if limit:
            # Newest N of the whole catalogue (scheduled and uploaded ones count towards N), so the
            # cap never creeps further down the list on later runs. Photo posts and other skips do not count.
            newest = db.rows(conn, "SELECT tiktok_id FROM videos WHERE origin='existing' AND status NOT IN ('skipped','held') "
                                   "ORDER BY published_at DESC LIMIT ?", (int(limit),))
            allowed = {r["tiktok_id"] for r in newest}
            existing = [v for v in existing if v["tiktok_id"] in allowed]
        if order == "ASC":
            existing = list(reversed(existing))
    return new, existing


def assign(conn: sqlite3.Connection, platform: str, *, now: datetime | None = None) -> list[tuple[str, str]]:
    """Assign publish times for one platform. Returns [(tiktok_id, iso_time)] of new assignments."""
    now = now or utcnow()
    wf = db.get_workflow(conn, platform)
    if wf is None or not wf["enabled"]:
        return []
    tz = db.timezone(conn)
    min_age = int(db.get_setting(conn, "min_age_minutes", 60))
    delay = int(wf.get("delay_minutes") or 0)
    new, existing = _candidates(conn, platform, wf)
    assigned: list[tuple[str, str]] = []

    if wf["mode"] == "asap":
        for v in new:
            when = max(now, earliest_allowed(v, min_age, delay))
            db.update_video(conn, v["tiktok_id"], **{f"{PREFIX[platform]}_status": "scheduled",
                                                     f"{PREFIX[platform]}_scheduled_for": iso(when)})
            assigned.append((v["tiktok_id"], iso(when)))
        new = []

    if not new and not existing:
        return assigned

    start_from = None
    if wf.get("existing_start_from"):
        try:
            start_from = local_to_utc(date.fromisoformat(wf["existing_start_from"]), "00:00", tz)
        except ValueError:
            start_from = None

    taken = taken_slots(conn, platform)
    free = [s for s in future_slots(conn, platform, tz, now) if iso(s) not in taken]
    new_queue = list(new)
    existing_queue = list(existing)
    for slot in free:
        if not new_queue and not existing_queue:
            break
        chosen = None
        # New content first: the earliest new video that is old enough for this slot.
        for i, v in enumerate(new_queue):
            if earliest_allowed(v, min_age, delay) <= slot:
                chosen = new_queue.pop(i)
                break
        if chosen is None and existing_queue and (start_from is None or slot >= start_from):
            chosen = existing_queue.pop(0)
        if chosen is None:
            continue
        px = PREFIX[platform]
        db.update_video(conn, chosen["tiktok_id"], **{f"{px}_status": "scheduled", f"{px}_scheduled_for": iso(slot)})
        assigned.append((chosen["tiktok_id"], iso(slot)))
    return assigned


def assign_all(conn: sqlite3.Connection, now: datetime | None = None) -> dict[str, list[tuple[str, str]]]:
    out = {}
    for plat in PLATFORMS:
        with db.tx(conn):
            out[plat] = assign(conn, plat, now=now)
    return out


def next_scheduled(conn: sqlite3.Connection, platform: str) -> str | None:
    px = PREFIX[platform]
    r = conn.execute(
        f"SELECT MIN({px}_scheduled_for) AS w FROM videos WHERE {px}_status = 'scheduled' AND {px}_scheduled_for >= ?",
        (iso(utcnow()),),
    ).fetchone()
    return r["w"] if r else None


def due(conn: sqlite3.Connection, platform: str, now: datetime | None = None, retry_runs: int = 3) -> list[dict[str, Any]]:
    """Videos whose slot has arrived: scheduled and ready, or failed with retries left."""
    now = now or utcnow()
    px = PREFIX[platform]
    return db.rows(
        conn,
        f"""SELECT * FROM videos
            WHERE status = 'ready'
              AND {px}_scheduled_for IS NOT NULL AND {px}_scheduled_for <= ?
              AND ({px}_status = 'scheduled' OR ({px}_status = 'failed' AND {px}_attempts <= ?))
            ORDER BY {px}_scheduled_for ASC""",
        (iso(now), retry_runs),
    )


# ---------- burst guards ----------

def stale_scheduled(conn: sqlite3.Connection, platform: str, grace_minutes: int, now: datetime | None = None) -> list[dict[str, Any]]:
    """Scheduled rows whose slot passed more than grace_minutes ago, whatever their download state."""
    now = now or utcnow()
    px = PREFIX[platform]
    cutoff = iso(now - timedelta(minutes=int(grace_minutes)))
    return db.rows(
        conn,
        f"""SELECT * FROM videos
            WHERE {px}_status = 'scheduled' AND {px}_scheduled_for IS NOT NULL AND {px}_scheduled_for < ?
              AND status IN ('new','downloaded','ready')
            ORDER BY {px}_scheduled_for ASC""",
        (cutoff,),
    )


def defer(conn: sqlite3.Connection, platform: str, tiktok_ids: list[str], *, now: datetime | None = None) -> dict[str, str | None]:
    """Release the slots of the given videos and hand each the next free future slot.

    Returns {tiktok_id: new_iso_time_or_None}. Must be called inside db.tx(). Only the platform
    columns change: the video keeps its download state, error text and attempt count.
    """
    if not tiktok_ids:
        return {}
    px = PREFIX[platform]
    for tiktok_id in tiktok_ids:
        db.update_video(conn, tiktok_id, **{f"{px}_status": "queued", f"{px}_scheduled_for": None})
    assigned = dict(assign(conn, platform, now=now))
    return {tiktok_id: assigned.get(tiktok_id) for tiktok_id in tiktok_ids}


def local_day_start(tz: str, now: datetime | None = None) -> datetime:
    """UTC instant of local midnight for the day containing `now`."""
    now = now or utcnow()
    local = to_local(now, tz)
    return local_to_utc(local.date(), "00:00", tz)


def uploads_today(conn: sqlite3.Connection, platform: str, tz: str, now: datetime | None = None) -> int:
    px = PREFIX[platform]
    r = conn.execute(f"SELECT COUNT(*) AS n FROM videos WHERE {px}_published_at >= ?",
                     (iso(local_day_start(tz, now)),)).fetchone()
    return int(r["n"])


def day_cap(conn: sqlite3.Connection, platform: str, limits: dict[str, Any], tz: str, now: datetime | None = None) -> int:
    """Uploads allowed on the current local day. Config wins; otherwise the weekday's slot count, at least 1."""
    configured = limits.get("max_publish_per_day")
    if configured:
        return max(1, int(configured))
    now = now or utcnow()
    weekday = to_local(now, tz).weekday()
    n = sum(1 for s in db.get_slots(conn, platform) if int(s["weekday"]) == weekday)
    return max(1, n)
