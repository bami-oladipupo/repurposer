"""State transitions shared by the worker and the web UI.

Every write from either side goes through these functions so the transitions are identical
whichever side triggers them. Platform-specific columns are addressed by prefix (yt_ / ig_).
"""
from __future__ import annotations

import sqlite3
from typing import Any

from . import db
from .config import PLATFORMS, PREFIX
from .timeutil import iso, utcnow

TERMINAL_PLATFORM = {"uploaded", "skipped", "cancelled"}
ACTIVE_VIDEO = {"new", "downloaded", "ready"}


def p(platform: str) -> str:
    if platform not in PREFIX:
        raise ValueError(f"unknown platform '{platform}'")
    return PREFIX[platform]


def _stage_status(video: dict[str, Any]) -> str:
    """Where a released video goes back to, based on what has already happened to its file."""
    if video.get("local_path") and video.get("duration_s") is not None:
        return "ready"
    if video.get("local_path"):
        return "downloaded"
    return "new"


def hold(conn: sqlite3.Connection, tiktok_id: str, reason: str = "held from UI") -> None:
    """Nothing publishes while held. Scheduled slots are released so other content can use them."""
    fields: dict[str, Any] = {"status": "held", "status_reason": reason}
    for plat in PLATFORMS:
        px = p(plat)
        v = db.get_video(conn, tiktok_id) or {}
        if v.get(f"{px}_status") == "scheduled":
            fields[f"{px}_status"] = "queued"
            fields[f"{px}_scheduled_for"] = None
    db.update_video(conn, tiktok_id, **fields)


def release(conn: sqlite3.Connection, tiktok_id: str) -> None:
    """Clear a hold. The scheduler picks the video up again on the next run."""
    v = db.get_video(conn, tiktok_id)
    if v is None:
        return
    if v["status"] != "held":
        return
    db.update_video(conn, tiktok_id, status=_stage_status(v), status_reason=None)


def skip(conn: sqlite3.Connection, tiktok_id: str, reason: str) -> None:
    fields: dict[str, Any] = {"status": "skipped", "status_reason": reason}
    v = db.get_video(conn, tiktok_id) or {}
    for plat in PLATFORMS:
        px = p(plat)
        if v.get(f"{px}_status") not in {"uploaded"}:
            fields[f"{px}_status"] = "skipped"
            fields[f"{px}_scheduled_for"] = None
            fields[f"{px}_error"] = reason
    db.update_video(conn, tiktok_id, **fields)


def skip_platform(conn: sqlite3.Connection, tiktok_id: str, platform: str, reason: str) -> None:
    px = p(platform)
    db.update_video(conn, tiktok_id, **{f"{px}_status": "skipped", f"{px}_scheduled_for": None, f"{px}_error": reason})
    refresh_done(conn, tiktok_id)


def cancel(conn: sqlite3.Connection, tiktok_id: str, platform: str) -> None:
    px = p(platform)
    v = db.get_video(conn, tiktok_id) or {}
    if v.get(f"{px}_status") == "uploaded":
        return
    db.update_video(conn, tiktok_id, **{f"{px}_status": "cancelled", f"{px}_scheduled_for": None})
    refresh_done(conn, tiktok_id)


def requeue(conn: sqlite3.Connection, tiktok_id: str, platform: str) -> None:
    """Re-add to schedule: back to the queue, next free slot, not the original one."""
    px = p(platform)
    v = db.get_video(conn, tiktok_id) or {}
    if v.get(f"{px}_status") == "uploaded":
        return
    fields = {f"{px}_status": "queued", f"{px}_scheduled_for": None, f"{px}_error": None, f"{px}_attempts": 0}
    if v.get("status") in {"done", "failed"}:
        fields["status"] = _stage_status(v)
        fields["status_reason"] = None
    db.update_video(conn, tiktok_id, **fields)


def schedule(conn: sqlite3.Connection, tiktok_id: str, platform: str, when: str) -> None:
    px = p(platform)
    db.update_video(conn, tiktok_id, **{f"{px}_status": "scheduled", f"{px}_scheduled_for": when})


def reschedule(conn: sqlite3.Connection, tiktok_id: str, platform: str, when: str) -> None:
    """Calendar drag: move a scheduled item to a new time. Cancelled or queued items become scheduled."""
    px = p(platform)
    v = db.get_video(conn, tiktok_id) or {}
    if v.get(f"{px}_status") in {"uploaded", "skipped"}:
        raise ValueError(f"{platform} post for {tiktok_id} is {v.get(f'{px}_status')} and cannot be moved")
    db.update_video(conn, tiktok_id, **{f"{px}_status": "scheduled", f"{px}_scheduled_for": when, f"{px}_error": None})


def publish_now(conn: sqlite3.Connection, tiktok_id: str, platform: str) -> int:
    """Schedule for now, release any hold, and raise a job so the worker runs immediately."""
    px = p(platform)
    v = db.get_video(conn, tiktok_id)
    if v is None:
        raise ValueError(f"unknown video {tiktok_id}")
    if v.get(f"{px}_status") == "uploaded":
        raise ValueError(f"{platform} post already uploaded")
    fields: dict[str, Any] = {
        f"{px}_status": "scheduled", f"{px}_scheduled_for": iso(utcnow()), f"{px}_error": None, f"{px}_attempts": 0,
    }
    if v["status"] in {"held", "skipped", "done", "failed"}:
        fields["status"] = _stage_status(v)
        fields["status_reason"] = None
    db.update_video(conn, tiktok_id, **fields)
    return enqueue_job(conn, "publish_now", tiktok_id, platform)


def set_text(conn: sqlite3.Connection, tiktok_id: str, *, yt_title: str | None = None,
             yt_description: str | None = None, ig_caption: str | None = None) -> None:
    fields = {}
    if yt_title is not None:
        fields["yt_title"] = yt_title.strip() or None
    if yt_description is not None:
        fields["yt_description"] = yt_description.strip() or None
    if ig_caption is not None:
        fields["ig_caption"] = ig_caption.strip() or None
    db.update_video(conn, tiktok_id, **fields)


# ---------- worker-side transitions ----------

def mark_uploaded(conn: sqlite3.Connection, tiktok_id: str, platform: str, **ids: Any) -> None:
    px = p(platform)
    fields = {f"{px}_status": "uploaded", f"{px}_error": None, f"{px}_published_at": iso(utcnow()), **ids}
    db.update_video(conn, tiktok_id, **fields)
    refresh_done(conn, tiktok_id)


def mark_failed(conn: sqlite3.Connection, tiktok_id: str, platform: str, error: str, retry_runs: int) -> bool:
    """Record the failure. Returns True when the video has exhausted its retries (needs manual attention)."""
    px = p(platform)
    v = db.get_video(conn, tiktok_id) or {}
    attempts = int(v.get(f"{px}_attempts") or 0) + 1
    db.update_video(conn, tiktok_id, **{
        f"{px}_status": "failed", f"{px}_error": error[:4000], f"{px}_attempts": attempts,
        "last_attempt": iso(utcnow()), "attempts": int(v.get("attempts") or 0) + 1,
    })
    exhausted = attempts > retry_runs
    refresh_done(conn, tiktok_id, retry_runs=retry_runs)
    return exhausted


def refresh_done(conn: sqlite3.Connection, tiktok_id: str, retry_runs: int = 3) -> None:
    """Video-level status follows the platform statuses of the enabled workflows."""
    v = db.get_video(conn, tiktok_id)
    if v is None or v["status"] in {"held", "skipped"}:
        return
    wfs = db.all_workflows(conn)
    enabled = [plat for plat, wf in wfs.items() if wf["enabled"]] or list(PLATFORMS)
    statuses = {plat: v[f"{p(plat)}_status"] for plat in enabled}
    attempts = {plat: int(v[f"{p(plat)}_attempts"] or 0) for plat in enabled}
    all_terminal = all(s in TERMINAL_PLATFORM or (s == "failed" and attempts[plat] > retry_runs) for plat, s in statuses.items())
    if not all_terminal:
        if v["status"] in {"done", "failed"}:
            db.update_video(conn, tiktok_id, status=_stage_status(v))
        return
    if any(s == "uploaded" for s in statuses.values()):
        db.update_video(conn, tiktok_id, status="done", status_reason=None)
    elif any(s == "failed" for s in statuses.values()):
        db.update_video(conn, tiktok_id, status="failed", status_reason="publish failed after retries")
    elif all(s == "skipped" for s in statuses.values()):
        db.update_video(conn, tiktok_id, status="skipped")


# ---------- jobs and workflow toggles ----------

def enqueue_job(conn: sqlite3.Connection, kind: str, tiktok_id: str | None = None, platform: str | None = None,
                arg: str | None = None) -> int:
    cur = conn.execute(
        "INSERT INTO jobs(kind, tiktok_id, platform, created_at, arg) VALUES (?,?,?,?,?)",
        (kind, tiktok_id, platform, iso(utcnow()), arg),
    )
    return int(cur.lastrowid)


def set_auto_publish(conn: sqlite3.Connection, platform: str, on: bool) -> None:
    """Manual keeps every slot; nothing publishes until Auto is switched back on."""
    db.save_workflow(conn, platform, auto_publish=int(bool(on)))
