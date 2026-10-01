"""JSON API for the IV Repost iPhone app, mounted under /api/app.

Every answer is `{"success": true, "data": ...}` or `{"success": false, "error": "..."}`. Times are
UTC ISO strings; the app shows them in the phone's time zone. Writes go through repurposer.actions,
the same transitions the web UI uses. Publishing never happens here: Publish Now and Run Now raise a
job and spawn the worker, exactly like the web UI.

Access is guarded in app.py: every /api/app request needs `Authorization: Bearer <APP_TOKEN>`.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any, Callable

from fastapi import APIRouter, Body, HTTPException
from fastapi.responses import FileResponse

from . import actions, captions, config, db, push, rewrite, scheduler
from .config import PLATFORM_LABEL, PLATFORMS, PREFIX
from .timeutil import iso, local_to_utc, parse, to_local, utcnow

# A healthy worker runs every 15 minutes; past this it has stopped (Mac asleep, job unloaded).
WORKER_STALE_MINUTES = 40


def ok(data: Any = None) -> dict[str, Any]:
    return {"success": True, "data": data if data is not None else {}}


def _platform(platform: str) -> str:
    if platform not in PLATFORMS:
        raise HTTPException(404, f"unknown platform {platform}")
    return platform


def _video(conn: sqlite3.Connection, tiktok_id: str) -> dict[str, Any]:
    v = db.get_video(conn, tiktok_id)
    if v is None:
        raise HTTPException(404, f"unknown video {tiktok_id}")
    return v


def _link(conn: sqlite3.Connection, v: dict[str, Any], platform: str) -> str | None:
    if platform == "youtube" and v.get("yt_video_id"):
        return f"https://youtube.com/shorts/{v['yt_video_id']}"
    if platform == "instagram" and v.get("ig_media_id"):
        # Only the media ID is stored on the video; the Reel's real address (a shortcode permalink)
        # is in the summary of the run that published it.
        r = conn.execute("SELECT summary FROM runs WHERE summary LIKE ? ORDER BY run_id DESC LIMIT 1",
                         (f'%["instagram", "{v["tiktok_id"]}", "http%',)).fetchone()
        if r:
            for plat, tiktok_id, link in json.loads(r["summary"]).get("published") or []:
                if plat == "instagram" and tiktok_id == v["tiktok_id"]:
                    return link
    return None


def post_view(conn: sqlite3.Connection, v: dict[str, Any], platform: str) -> dict[str, Any]:
    """One video on one platform: what the app shows as a row and may do to it."""
    px = PREFIX[platform]
    pstatus = v[f"{px}_status"]
    status = v["status"] if v["status"] in ("held", "skipped") and pstatus != "uploaded" else pstatus
    when = v[f"{px}_published_at"] if pstatus == "uploaded" else v[f"{px}_scheduled_for"]
    busy = conn.execute("SELECT 1 FROM jobs WHERE tiktok_id=? AND platform=? AND finished_at IS NULL",
                        (v["tiktok_id"], platform)).fetchone()
    title = v.get("yt_title") if platform == "youtube" else captions.first_line(v.get("ig_caption"))
    return {
        "id": f"{v['tiktok_id']}:{platform}",
        "tiktok_id": v["tiktok_id"],
        "platform": platform,
        "title": title or captions.first_line(v.get("tiktok_caption")) or f"TikTok {v['tiktok_id']}",
        "status": status,
        "status_reason": v.get("status_reason") if status in ("held", "skipped") else None,
        "when": when,
        "error": v[f"{px}_error"] if pstatus == "failed" else None,
        "attempts": int(v[f"{px}_attempts"] or 0),
        "link": _link(conn, v, platform),
        "origin": v["origin"],
        "has_thumb": (config.MEDIA_DIR / f"{v['tiktok_id']}.jpg").exists(),
        "busy": bool(busy),
        "can_publish": pstatus != "uploaded" and v["status"] != "skipped",
        "can_cancel": pstatus in ("scheduled", "queued", "failed"),
        "can_requeue": pstatus in ("cancelled", "failed", "skipped") and v["status"] != "skipped",
        "can_hold": v["status"] in ("new", "downloaded", "ready") and pstatus != "uploaded",
        "can_release": v["status"] == "held",
        "can_reschedule": pstatus not in ("uploaded", "skipped") and v["status"] not in ("held", "skipped"),
    }


def _counts(conn: sqlite3.Connection, platform: str) -> dict[str, int]:
    px = PREFIX[platform]
    r = conn.execute(
        f"""SELECT
            SUM(CASE WHEN {px}_status='queued' AND status IN ('new','downloaded','ready') THEN 1 ELSE 0 END) AS queued,
            SUM(CASE WHEN {px}_status='scheduled' THEN 1 ELSE 0 END) AS scheduled,
            SUM(CASE WHEN {px}_status='uploaded' THEN 1 ELSE 0 END) AS published,
            SUM(CASE WHEN {px}_status='failed' THEN 1 ELSE 0 END) AS failed,
            SUM(CASE WHEN status='held' THEN 1 ELSE 0 END) AS held
           FROM videos"""
    ).fetchone()
    return {k: int(r[k] or 0) for k in ("queued", "scheduled", "published", "failed", "held")}


def run_view(r: dict[str, Any]) -> dict[str, Any]:
    try:
        d = json.loads(r["summary"]) if r.get("summary") else {}
    except json.JSONDecodeError:
        d = {}
    problems = [f"{stage} {tid}: {err}" for stage, tid, err in d.get("failed") or []]
    problems += list(d.get("alerts") or [])
    problems += [f"{stage}: {err.splitlines()[0]}" for stage, err in d.get("stage_errors") or []]
    return {
        "run_id": r["run_id"], "kind": r["kind"], "started": r["started"], "finished": r["finished"],
        "ok": None if r["ok"] is None else bool(r["ok"]),
        "videos_seen": int(r["videos_seen"] or 0),
        "published": [{"platform": p, "tiktok_id": t, "link": link} for p, t, link in d.get("published") or []],
        "problems": [p[:400] for p in problems],
        "held": [f"{t}: {why}" for t, why in d.get("held") or []],
        "notes": list(d.get("notes") or []),
    }


def _worker(conn: sqlite3.Connection) -> dict[str, Any]:
    last = db.one(conn, "SELECT started, finished, ok FROM runs WHERE kind = 'cycle' ORDER BY run_id DESC LIMIT 1")
    started = parse(last["started"]) if last else None
    minutes = int((utcnow() - started).total_seconds() // 60) if started else None
    return {"last_run_at": last["started"] if last else None,
            "last_ok": None if not last or last["ok"] is None else bool(last["ok"]),
            "minutes_since": minutes,
            "stale": minutes is None or minutes > WORKER_STALE_MINUTES}


def _platform_card(conn: sqlite3.Connection, platform: str) -> dict[str, Any]:
    wf = db.get_workflow(conn, platform) or {}
    c = db.get_connection(conn, platform) or {}
    exp = parse(c.get("token_expires_at")) if c.get("token_expires_at") else None
    return {
        "platform": platform, "label": PLATFORM_LABEL[platform],
        "enabled": bool(wf.get("enabled")), "auto_publish": bool(wf.get("auto_publish")),
        "counts": _counts(conn, platform), "next_at": scheduler.next_scheduled(conn, platform),
        "connection": {"healthy": bool(c.get("healthy")), "account_name": c.get("account_name"),
                       "error": c.get("last_error"), "days_left": (exp - utcnow()).days if exp else None},
    }


def build_router(get_conn: Callable[[], sqlite3.Connection], spawn_worker: Callable[..., Any]) -> APIRouter:
    router = APIRouter(prefix="/api/app")

    @router.get("/status")
    def status():
        cfg = config.load_config()
        conn = get_conn()
        return ok({"handle": cfg["source"]["tiktok_handle"], "timezone": db.timezone(conn),
                   "apns_configured": push.configured(), "devices": push.device_count(conn)})

    @router.get("/home")
    def home():
        conn = get_conn()
        now = utcnow()
        tz = db.timezone(conn)
        day_start = local_to_utc(to_local(now, tz).date(), "00:00", tz)
        today, up_next, attention = [], [], []
        for plat in PLATFORMS:
            px = PREFIX[plat]
            for v in db.rows(conn, f"SELECT * FROM videos WHERE {px}_status='uploaded' AND {px}_published_at >= ?",
                             (iso(day_start),)):
                today.append(post_view(conn, v, plat))
            for v in db.rows(conn, f"""SELECT * FROM videos WHERE {px}_status='scheduled' AND status NOT IN ('held','skipped')
                                       ORDER BY {px}_scheduled_for LIMIT 6"""):
                up_next.append(post_view(conn, v, plat))
            for v in db.rows(conn, f"SELECT * FROM videos WHERE {px}_status='failed'"):
                attention.append(post_view(conn, v, plat))
        today.sort(key=lambda p: p["when"] or "", reverse=True)
        up_next.sort(key=lambda p: p["when"] or "")
        last = db.one(conn, "SELECT * FROM runs ORDER BY run_id DESC LIMIT 1")
        return ok({
            "generated_at": iso(now),
            "handle": config.load_config()["source"]["tiktok_handle"],
            "worker": _worker(conn),
            "platforms": [_platform_card(conn, p) for p in PLATFORMS],
            "published_today": today, "up_next": up_next[:6], "attention": attention,
            "last_run": run_view(last) if last else None,
        })

    @router.get("/posts")
    def posts(start: str = "", end: str = "", status: str = "", limit: int = 200):
        """Posts in a UTC time window (scheduled, published or failed inside it), or every post in one status."""
        conn = get_conn()
        out: list[dict[str, Any]] = []
        for plat in PLATFORMS:
            px = PREFIX[plat]
            if status in ("held", "skipped"):
                rows = db.rows(conn, "SELECT * FROM videos WHERE status = ? ORDER BY COALESCE(published_at, first_seen) DESC LIMIT ?",
                               (status, limit))
            elif status:
                rows = db.rows(conn, f"""SELECT * FROM videos WHERE {px}_status = ? AND status NOT IN ('held','skipped')
                                         ORDER BY COALESCE({px}_scheduled_for, published_at, first_seen) LIMIT ?""", (status, limit))
            else:
                if not parse(start) or not parse(end):
                    raise HTTPException(400, "start and end must be ISO times")
                rows = db.rows(
                    conn,
                    f"""SELECT * FROM videos WHERE
                        ({px}_status IN ('scheduled','failed') AND {px}_scheduled_for >= ? AND {px}_scheduled_for < ?)
                     OR ({px}_status = 'uploaded' AND {px}_published_at >= ? AND {px}_published_at < ?)""",
                    (start, end) * 2,
                )
            out += [post_view(conn, v, plat) for v in rows]
        out.sort(key=lambda p: p["when"] or "")
        return ok({"posts": out})

    @router.get("/videos/{tiktok_id}")
    def video(tiktok_id: str):
        conn = get_conn()
        v = _video(conn, tiktok_id)
        wfs = db.all_workflows(conn)
        title, description = captions.youtube_snippet(v, wfs.get("youtube") or {})
        return ok({
            "tiktok_id": v["tiktok_id"], "tiktok_url": v.get("tiktok_url"), "caption": v.get("tiktok_caption") or "",
            "published_at": v.get("published_at"), "origin": v["origin"], "status": v["status"],
            "status_reason": v.get("status_reason"), "duration_s": v.get("duration_s"),
            "posts": [post_view(conn, v, p) for p in PLATFORMS if (wfs.get(p) or {}).get("enabled")],
            "yt_title": title, "yt_description": description,
            "ig_caption": captions.instagram_caption(v, wfs.get("instagram") or {}),
            "rewrite_available": bool(config.env("ANTHROPIC_API_KEY")),
        })

    @router.get("/thumb/{tiktok_id}")
    def thumb(tiktok_id: str):
        path = (config.MEDIA_DIR / f"{tiktok_id}.jpg").resolve()
        if config.MEDIA_DIR.resolve() not in path.parents or not path.exists():
            raise HTTPException(404, "no thumbnail")
        return FileResponse(str(path))

    # ---------- actions ----------

    @router.post("/videos/{tiktok_id}/{platform}/publish-now")
    def publish_now(tiktok_id: str, platform: str):
        platform = _platform(platform)
        conn = get_conn()
        _video(conn, tiktok_id)
        try:
            with db.tx(conn):
                job_id = actions.publish_now(conn, tiktok_id, platform)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        spawn_worker("--job", str(job_id))
        return ok({"message": f"Publishing to {PLATFORM_LABEL[platform]} now"})

    @router.post("/videos/{tiktok_id}/{platform}/cancel")
    def cancel(tiktok_id: str, platform: str):
        platform = _platform(platform)
        conn = get_conn()
        _video(conn, tiktok_id)
        with db.tx(conn):
            actions.cancel(conn, tiktok_id, platform)
        return ok({"message": f"Cancelled on {PLATFORM_LABEL[platform]}"})

    @router.post("/videos/{tiktok_id}/{platform}/requeue")
    def requeue(tiktok_id: str, platform: str):
        platform = _platform(platform)
        conn = get_conn()
        _video(conn, tiktok_id)
        with db.tx(conn):
            actions.requeue(conn, tiktok_id, platform)
        return ok({"message": "Back in the queue. The next run gives it a slot."})

    @router.post("/videos/{tiktok_id}/{platform}/reschedule")
    def reschedule(tiktok_id: str, platform: str, when: str = Body(..., embed=True)):
        platform = _platform(platform)
        conn = get_conn()
        _video(conn, tiktok_id)
        at = parse(when)
        if at is None:
            raise HTTPException(400, "when must be an ISO time")
        if at < utcnow():
            raise HTTPException(409, "That time is in the past")
        try:
            with db.tx(conn):
                actions.reschedule(conn, tiktok_id, platform, iso(at))
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        return ok({"message": "Moved", "when": iso(at)})

    @router.post("/videos/{tiktok_id}/hold")
    def hold(tiktok_id: str):
        conn = get_conn()
        _video(conn, tiktok_id)
        with db.tx(conn):
            actions.hold(conn, tiktok_id, "held from the app")
        return ok({"message": "Held on both platforms"})

    @router.post("/videos/{tiktok_id}/release")
    def release(tiktok_id: str):
        conn = get_conn()
        _video(conn, tiktok_id)
        with db.tx(conn):
            actions.release(conn, tiktok_id)
        return ok({"message": "Released. The next run gives it a slot."})

    @router.post("/videos/{tiktok_id}/text")
    def set_text(tiktok_id: str, body: dict[str, Any] = Body(...)):
        conn = get_conn()
        _video(conn, tiktok_id)
        fields = {k: body[k] for k in ("yt_title", "yt_description", "ig_caption") if isinstance(body.get(k), str)}
        with db.tx(conn):
            actions.set_text(conn, tiktok_id, **fields)
        return ok({"message": "Saved"})

    @router.post("/videos/{tiktok_id}/{platform}/rewrite")
    def rewrite_text(tiktok_id: str, platform: str):
        """Regenerate one platform's text with Claude. Slow (a model call); the app waits for it."""
        platform = _platform(platform)
        conn = get_conn()
        v = _video(conn, tiktok_id)
        with db.tx(conn):
            res = rewrite.rewrite_video(conn, v, config.load_config(), platforms=[platform], force=True)
        if res["error"]:
            raise HTTPException(502, f"Rewrite failed: {res['error'][:300]}")
        return ok({"message": "Rewritten with Claude"})

    @router.post("/workflows/{platform}/auto")
    def set_auto(platform: str, on: bool = Body(..., embed=True)):
        platform = _platform(platform)
        conn = get_conn()
        with db.tx(conn):
            actions.set_auto_publish(conn, platform, on)
        return ok({"message": f"{PLATFORM_LABEL[platform]} {'resumed' if on else 'paused'}"})

    @router.post("/run-now")
    def run_now():
        conn = get_conn()
        with db.tx(conn):
            job_id = actions.enqueue_job(conn, "run_now")
        spawn_worker("--job", str(job_id))
        return ok({"message": "Worker started"})

    @router.get("/runs")
    def runs(limit: int = 40, problems_only: bool = False):
        conn = get_conn()
        where = "WHERE ok = 0" if problems_only else ""
        rows = db.rows(conn, f"SELECT * FROM runs {where} ORDER BY run_id DESC LIMIT ?", (min(max(limit, 1), 200),))
        return ok({"runs": [run_view(r) for r in rows], "worker": _worker(conn)})

    # ---------- push ----------

    @router.post("/devices")
    def register_device(token: str = Body(..., embed=True), environment: str = Body("production", embed=True)):
        if not token or len(token) > 200 or not all(c in "0123456789abcdef" for c in token.lower()):
            raise HTTPException(400, "bad device token")
        conn = get_conn()
        with db.tx(conn):
            push.register(conn, token.lower(), "sandbox" if environment == "sandbox" else "production")
        return ok({"message": "Device registered"})

    @router.post("/devices/test")
    def test_push():
        conn = get_conn()
        if not push.configured():
            raise HTTPException(409, "Push is not set up on the Mac yet (APNS_* missing from .env)")
        sent, errors = push.send_all(conn, "IV Repost", "Test alert from your Mac. Push is working.")
        if not sent:
            raise HTTPException(502, errors[0] if errors else "No phone has registered for alerts yet")
        return ok({"message": f"Sent to {sent} device{'s' if sent != 1 else ''}"})

    return router


__all__ = ["build_router", "post_view", "run_view", "ok"]
