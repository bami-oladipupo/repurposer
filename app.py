#!/usr/bin/env python3
"""TikTok Repurposer web UI. FastAPI + Jinja2 + HTMX. Reads and writes the database only.

Publishing never happens in this process: Publish Now and Run Now raise a job and spawn the worker.
The only platform calls made here are the OAuth handshakes for the Connections page.
No login screen: bind to 127.0.0.1 (default) or put Tailscale / a basic-auth proxy in front.
"""
from __future__ import annotations

import json
import logging
import secrets
import sqlite3
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile  # noqa: E402
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, RedirectResponse, Response  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from fastapi.templating import Jinja2Templates  # noqa: E402

from repurposer import actions, captions, config, db, logsetup, rewrite, scheduler  # noqa: E402
from repurposer.config import PLATFORMS, PLATFORM_LABEL, PREFIX  # noqa: E402
from repurposer.timeutil import fmt_local, iso, local_to_utc, parse, to_local, utcnow  # noqa: E402

ROOT = config.ROOT
log = logging.getLogger("repurposer.web")
app = FastAPI(title="TikTok Repurposer", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=str(ROOT / "static")), name="static")
templates = Jinja2Templates(directory=str(ROOT / "templates"))

WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
_oauth_states: dict[str, dict[str, Any]] = {}


# ---------- helpers ----------

def get_conn() -> sqlite3.Connection:
    conn = db.open_db()
    try:
        db.seed_from_config(conn, config.load_config())
    except config.ConfigError as exc:
        log.error("config error: %s", exc)
    return conn


def tz_of(conn: sqlite3.Connection) -> str:
    return db.timezone(conn)


def _fmt(value: Any, fmt: str = "%a %d %b %H:%M") -> str:
    return fmt_local(value, templates.env.globals.get("tz", "Europe/London"), fmt)


def _rel(value: Any) -> str:
    dt = parse(value) if isinstance(value, str) else value
    if not dt:
        return ""
    delta = dt - utcnow()
    s = int(delta.total_seconds())
    past = s < 0
    s = abs(s)
    if s < 60:
        txt = "now"
    elif s < 3600:
        txt = f"{s // 60} min"
    elif s < 86400:
        txt = f"{s // 3600} h"
    else:
        txt = f"{s // 86400} d"
    if txt == "now":
        return txt
    return f"{txt} ago" if past else f"in {txt}"


templates.env.filters["local"] = _fmt
templates.env.filters["rel"] = _rel
templates.env.filters["short"] = lambda s, n=80: (s or "")[:n] + ("…" if s and len(s) > n else "")
STATUS_LABEL = {"uploaded": "published", "queued": "queued", "scheduled": "scheduled", "failed": "failed",
                "held": "held", "skipped": "skipped", "cancelled": "cancelled"}
RUN_KIND = {"cycle": "scheduled run", "dry-run": "dry run", "publish-now": "publish now", "publish_now": "publish now",
            "run_now": "run now", "republish": "republish", "import": "catalogue import"}
YT_CATEGORIES = [("27", "Education"), ("22", "People and Blogs"), ("28", "Science and Technology"), ("26", "Howto and Style"),
                 ("24", "Entertainment"), ("23", "Comedy"), ("25", "News and Politics"), ("10", "Music"), ("20", "Gaming"),
                 ("1", "Film and Animation")]
def asset_version() -> str:
    """mtime of the static files, so browsers pick up a new stylesheet after an edit."""
    return str(int(max(p.stat().st_mtime for p in (ROOT / "static").glob("*"))))


templates.env.globals.update(asset_v=asset_version,
                             PLATFORMS=PLATFORMS, LABEL=PLATFORM_LABEL, PREFIX=PREFIX, WEEKDAYS=WEEKDAYS,
                             STATUS_LABEL=STATUS_LABEL, RUN_KIND=RUN_KIND, YT_CATEGORIES=YT_CATEGORIES)


def banner(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Unhealthy connections for enabled workflows drive the red banner on every page."""
    out = []
    for plat, wf in db.all_workflows(conn).items():
        if not wf["enabled"]:
            continue
        c = db.get_connection(conn, plat)
        if c is None or not c["healthy"]:
            out.append({"platform": plat, "error": (c or {}).get("last_error") or "not connected"})
    return out


def attention(conn: sqlite3.Connection) -> int:
    """Failed posts on enabled workflows drive the count next to Content in the nav."""
    total = 0
    for plat, wf in db.all_workflows(conn).items():
        if wf["enabled"]:
            total += conn.execute(f"SELECT COUNT(*) FROM videos WHERE {PREFIX[plat]}_status='failed'").fetchone()[0]
    return total


def render(request: Request, name: str, conn: sqlite3.Connection, **ctx: Any) -> HTMLResponse:
    templates.env.globals["tz"] = tz_of(conn)
    ctx.setdefault("banner", banner(conn))
    ctx.setdefault("attention", attention(conn))
    try:
        ctx.setdefault("handle", config.load_config()["source"]["tiktok_handle"])
    except (config.ConfigError, KeyError):
        ctx.setdefault("handle", "")
    ctx.setdefault("page", name.split(".")[0])
    ctx.setdefault("now", utcnow())
    return templates.TemplateResponse(request, name, ctx)


def spawn_worker(*args: str) -> subprocess.Popen:
    cmd = [sys.executable, str(ROOT / "worker.py"), *args]
    log.info("spawning %s", " ".join(cmd))
    return subprocess.Popen(cmd, cwd=str(ROOT), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)


def platform_or_404(platform: str) -> str:
    if platform not in PLATFORMS:
        raise HTTPException(404, f"unknown platform {platform}")
    return platform


def counts(conn: sqlite3.Connection, platform: str) -> dict[str, int]:
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


def video_view(conn: sqlite3.Connection, v: dict[str, Any], platform: str) -> dict[str, Any]:
    """Row model for the Content table and the edit drawer."""
    px = PREFIX[platform]
    local = Path(v["local_path"]) if v.get("local_path") else None
    thumb = ROOT / "media" / f"{v['tiktok_id']}.jpg"
    pstatus = v[f"{px}_status"]
    if v["status"] in ("held", "skipped") and pstatus not in ("uploaded",):
        pill = v["status"]
    else:
        pill = pstatus
    link = None
    if platform == "youtube" and v.get("yt_video_id"):
        link = f"https://youtube.com/shorts/{v['yt_video_id']}"
    elif platform == "instagram" and v.get("ig_media_id"):
        link = f"https://www.instagram.com/reel/{v['ig_media_id']}/"
    job = conn.execute(
        "SELECT 1 FROM jobs WHERE tiktok_id=? AND platform=? AND finished_at IS NULL", (v["tiktok_id"], platform)
    ).fetchone()
    return {
        **v,
        "platform": platform, "px": px, "pill": pill,
        "p_status": pstatus, "p_scheduled_for": v[f"{px}_scheduled_for"], "p_error": v[f"{px}_error"],
        "p_attempts": v[f"{px}_attempts"], "p_published_at": v[f"{px}_published_at"],
        "link": link, "has_file": bool(local and local.exists()),
        "thumb": f"/media/{v['tiktok_id']}.jpg" if thumb.exists() else v.get("thumbnail_url"),
        "polling": bool(job) or (pstatus == "scheduled" and v[f"{px}_scheduled_for"] and v[f"{px}_scheduled_for"] <= iso(utcnow())),
        "can_publish": pstatus not in ("uploaded",) and v["status"] not in ("skipped",),
        "can_cancel": pstatus in ("scheduled", "queued", "failed"),
        "can_requeue": pstatus in ("cancelled", "failed", "skipped") and v["status"] not in ("skipped",),
        "can_hold": v["status"] in ("new", "downloaded", "ready") and pstatus not in ("uploaded",),
        "can_release": v["status"] == "held",
    }


# ---------- workflows ----------

@app.get("/", include_in_schema=False)
def home() -> RedirectResponse:
    return RedirectResponse("/workflows", status_code=302)


@app.get("/workflows", response_class=HTMLResponse)
def workflows(request: Request):
    conn = get_conn()
    cards = [_card_ctx(conn, p) for p in PLATFORMS]
    last_run = db.one(conn, "SELECT * FROM runs ORDER BY run_id DESC LIMIT 1")
    return render(request, "workflows.html", conn, cards=cards, last_run=last_run)


def _card_ctx(conn: sqlite3.Connection, platform: str) -> dict[str, Any]:
    wf = db.get_workflow(conn, platform) or {}
    return {"platform": platform, "wf": wf, "counts": counts(conn, platform),
            "next": scheduler.next_scheduled(conn, platform), "connection": db.get_connection(conn, platform)}


@app.post("/workflows/{platform}/auto", response_class=HTMLResponse)
def toggle_auto(request: Request, platform: str, on: str = Form("")):
    platform = platform_or_404(platform)
    conn = get_conn()
    with db.tx(conn):
        actions.set_auto_publish(conn, platform, on == "1")
    return render(request, "partials/workflow_card.html", conn, card=_card_ctx(conn, platform))


@app.post("/workflows/{platform}/enabled", response_class=HTMLResponse)
def toggle_enabled(request: Request, platform: str, on: str = Form("")):
    platform = platform_or_404(platform)
    conn = get_conn()
    with db.tx(conn):
        db.save_workflow(conn, platform, enabled=int(on == "1"))
    return render(request, "partials/workflow_card.html", conn, card=_card_ctx(conn, platform))


@app.post("/workflows/run-now")
def run_now():
    conn = get_conn()
    with db.tx(conn):
        job_id = actions.enqueue_job(conn, "run_now")
    spawn_worker("--job", str(job_id))
    return Response(status_code=204, headers={"HX-Trigger": json.dumps({"toast": "Worker started. The Runs page shows progress."})})


@app.post("/workflows/import")
def import_catalogue(limit: str = Form("")):
    """Import the back catalogue. `limit` = newest N videos on the profile; blank means everything."""
    limit_s = (limit or "").strip()
    n: int | None = None
    if limit_s:
        try:
            n = int(limit_s)
        except ValueError:
            return Response(status_code=204, headers={"HX-Trigger": json.dumps({"toast": "How many videos must be a whole number"})})
        if n < 1:
            return Response(status_code=204, headers={"HX-Trigger": json.dumps({"toast": "How many videos must be at least 1"})})
    conn = get_conn()
    with db.tx(conn):
        job_id = actions.enqueue_job(conn, "import", arg=str(n) if n else None)
    spawn_worker("--job", str(job_id))
    msg = f"Importing the newest {n} videos. Watch Runs for the result." if n else "Full catalogue import started. It can take several minutes."
    return Response(status_code=204, headers={"HX-Trigger": json.dumps({"toast": msg})})


@app.get("/workflows/{platform}/settings", response_class=HTMLResponse)
def workflow_settings(request: Request, platform: str, saved: str = "", error: str = ""):
    platform = platform_or_404(platform)
    conn = get_conn()
    wf = db.get_workflow(conn, platform)
    grid = {wd: [] for wd in range(7)}
    for s in db.get_slots(conn, platform):
        grid[int(s["weekday"])].append(s["local_time"])
    return render(request, "settings.html", conn, platform=platform, wf=wf, grid=grid, saved=saved, error=error,
                  page="workflows")


@app.post("/workflows/{platform}/settings")
async def save_workflow_settings(request: Request, platform: str):
    platform = platform_or_404(platform)
    form = await request.form()
    conn = get_conn()
    slots: list[tuple[int, str]] = []
    for wd in range(7):
        times = [t.strip() for t in form.getlist(f"slot_{wd}") if t and t.strip()]
        err = scheduler.validate_slots(times)
        if err:
            return RedirectResponse(f"/workflows/{platform}/settings?error={WEEKDAYS[wd]}: {err}", status_code=303)
        slots += [(wd, t) for t in times]
    lines = lambda key: [ln.strip() for ln in (form.get(key) or "").splitlines() if ln.strip()]  # noqa: E731
    extra = (db.get_workflow(conn, platform) or {}).get("extra") or {}
    if platform == "youtube":
        extra.update({"privacy": form.get("privacy") or "public", "category_id": (form.get("category_id") or "27").strip(),
                      "made_for_kids": form.get("made_for_kids") == "1"})
    else:
        extra.update({"share_to_feed": form.get("share_to_feed") == "1"})
    extra["rewrite"] = form.get("rewrite") == "1"
    delay = form.get("delay_minutes") or "0"
    try:
        delay_i = max(0, int(delay))
    except ValueError:
        return RedirectResponse(f"/workflows/{platform}/settings?error=delay must be a whole number of minutes", status_code=303)
    start = (form.get("existing_start_from") or "").strip() or None
    limit_raw = (form.get("existing_limit") or "").strip()
    try:
        existing_limit = int(limit_raw) if limit_raw else None
        if existing_limit is not None and existing_limit < 1:
            raise ValueError
    except ValueError:
        return RedirectResponse(f"/workflows/{platform}/settings?error=newest N must be a whole number", status_code=303)
    before = (form.get("existing_include_before") or "").strip() or None
    for label, val in (("start date", start), ("include before", before)):
        if val:
            try:
                date.fromisoformat(val)
            except ValueError:
                return RedirectResponse(f"/workflows/{platform}/settings?error={label} must be YYYY-MM-DD", status_code=303)
    with db.tx(conn):
        db.save_workflow(
            conn, platform,
            mode="asap" if form.get("mode") == "asap" else "schedule",
            content_scope="new_and_existing" if form.get("content_scope") == "new_and_existing" else "new",
            existing_start_from=start, existing_include_before=before, existing_limit=existing_limit,
            existing_order="oldest_first" if form.get("existing_order") == "oldest_first" else "newest_first",
            delay_minutes=delay_i,
            title_template=(form.get("title_template") or "").strip() or None,
            caption_template=(form.get("caption_template") or "").strip() or None,
            hashtags=lines("hashtags"), exclude_keywords=lines("exclude_keywords"), extra=extra,
        )
        db.replace_slots(conn, platform, slots)
    return RedirectResponse(f"/workflows/{platform}/settings?saved=1", status_code=303)


# ---------- global settings ----------

@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, saved: str = "", error: str = ""):
    conn = get_conn()
    cfg = config.load_config()
    values = {
        "timezone": db.get_setting(conn, "timezone", "Europe/London"),
        "min_age_minutes": db.get_setting(conn, "min_age_minutes", 60),
        "lookback_days": db.get_setting(conn, "lookback_days", 7),
    }
    return render(request, "global_settings.html", conn, values=values, cfg=cfg, saved=saved, error=error,
                  handle=cfg["source"]["tiktok_handle"])


@app.post("/settings")
def save_settings(timezone: str = Form(...), min_age_minutes: int = Form(...), lookback_days: int = Form(...)):
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    conn = get_conn()
    try:
        ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError):
        return RedirectResponse(f"/settings?error=unknown timezone {timezone}", status_code=303)
    with db.tx(conn):
        db.set_setting(conn, "timezone", timezone)
        db.set_setting(conn, "min_age_minutes", max(0, min_age_minutes))
        db.set_setting(conn, "lookback_days", max(1, lookback_days))
    return RedirectResponse("/settings?saved=1", status_code=303)


# ---------- content ----------

STATUS_FILTERS = ["queued", "scheduled", "uploaded", "failed", "held", "skipped", "cancelled"]


@app.get("/content", response_class=HTMLResponse)
def content(request: Request, platform: str = "youtube", status: str = "", origin: str = "", q: str = "", limit: int = 100, edit: str = ""):
    platform = platform_or_404(platform)
    conn = get_conn()
    px = PREFIX[platform]
    sql = "SELECT * FROM videos WHERE 1=1"
    params: list[Any] = []
    if status in ("held", "skipped"):
        sql += " AND status = ?"
        params.append(status)
    elif status in STATUS_FILTERS:
        sql += f" AND {px}_status = ? AND status NOT IN ('held','skipped')"
        params.append(status)
    if origin in ("new", "existing"):
        sql += " AND origin = ?"
        params.append(origin)
    if q:
        sql += " AND (tiktok_caption LIKE ? OR tiktok_id LIKE ?)"
        params += [f"%{q}%", f"%{q}%"]
    sql += f" ORDER BY CASE WHEN {px}_status='scheduled' THEN 0 ELSE 1 END, {px}_scheduled_for ASC, COALESCE(published_at, first_seen) DESC LIMIT ?"
    params.append(limit)
    rows = [video_view(conn, v, platform) for v in db.rows(conn, sql, params)]
    total = conn.execute("SELECT COUNT(*) FROM videos").fetchone()[0]
    last_run = db.one(conn, "SELECT * FROM runs ORDER BY run_id DESC LIMIT 1")
    return render(request, "content.html", conn, platform=platform, rows=rows, status=status, origin=origin, q=q,
                  filters=STATUS_FILTERS, total=total, counts=counts(conn, platform), edit=edit,
                  workflows=db.all_workflows(conn), connections={p: db.get_connection(conn, p) for p in PLATFORMS},
                  min_age=db.get_setting(conn, "min_age_minutes", 60), last_run=last_run)


@app.get("/content/row/{tiktok_id}", response_class=HTMLResponse)
def content_row(request: Request, tiktok_id: str, platform: str = "youtube"):
    platform = platform_or_404(platform)
    conn = get_conn()
    v = db.get_video(conn, tiktok_id)
    if v is None:
        raise HTTPException(404)
    return render(request, "partials/content_row.html", conn, row=video_view(conn, v, platform))


def _row_response(request: Request, conn: sqlite3.Connection, tiktok_id: str, platform: str, toast: str | None = None):
    v = db.get_video(conn, tiktok_id)
    if v is None:
        raise HTTPException(404)
    resp = render(request, "partials/content_row.html", conn, row=video_view(conn, v, platform))
    if toast:
        resp.headers["HX-Trigger"] = json.dumps({"toast": toast})
    return resp


@app.post("/videos/{tiktok_id}/{platform}/publish-now", response_class=HTMLResponse)
def video_publish_now(request: Request, tiktok_id: str, platform: str):
    platform = platform_or_404(platform)
    conn = get_conn()
    try:
        with db.tx(conn):
            job_id = actions.publish_now(conn, tiktok_id, platform)
    except ValueError as exc:
        return _row_response(request, conn, tiktok_id, platform, toast=str(exc))
    spawn_worker("--job", str(job_id))
    return _row_response(request, conn, tiktok_id, platform, toast=f"Publishing to {PLATFORM_LABEL[platform]} now")


@app.post("/videos/{tiktok_id}/{platform}/cancel", response_class=HTMLResponse)
def video_cancel(request: Request, tiktok_id: str, platform: str):
    platform = platform_or_404(platform)
    conn = get_conn()
    with db.tx(conn):
        actions.cancel(conn, tiktok_id, platform)
    return _row_response(request, conn, tiktok_id, platform, toast="Cancelled")


@app.post("/videos/{tiktok_id}/{platform}/requeue", response_class=HTMLResponse)
def video_requeue(request: Request, tiktok_id: str, platform: str):
    platform = platform_or_404(platform)
    conn = get_conn()
    with db.tx(conn):
        actions.requeue(conn, tiktok_id, platform)
    return _row_response(request, conn, tiktok_id, platform, toast="Back in the queue. Next worker run assigns a slot.")


@app.post("/videos/{tiktok_id}/{platform}/hold", response_class=HTMLResponse)
def video_hold(request: Request, tiktok_id: str, platform: str):
    platform = platform_or_404(platform)
    conn = get_conn()
    with db.tx(conn):
        actions.hold(conn, tiktok_id, "held from UI")
    return _row_response(request, conn, tiktok_id, platform, toast="Held on both platforms")


@app.post("/videos/{tiktok_id}/{platform}/release", response_class=HTMLResponse)
def video_release(request: Request, tiktok_id: str, platform: str):
    platform = platform_or_404(platform)
    conn = get_conn()
    with db.tx(conn):
        actions.release(conn, tiktok_id)
    return _row_response(request, conn, tiktok_id, platform, toast="Released")


@app.get("/videos/{tiktok_id}/edit", response_class=HTMLResponse)
def video_edit(request: Request, tiktok_id: str, platform: str = "youtube"):
    platform = platform_or_404(platform)
    conn = get_conn()
    v = db.get_video(conn, tiktok_id)
    if v is None:
        raise HTTPException(404)
    wf = db.get_workflow(conn, platform) or {}
    if platform == "youtube":
        title, description = captions.youtube_snippet(v, wf)
        fields = {"yt_title": v.get("yt_title") or title, "yt_description": v.get("yt_description") or description}
    else:
        fields = {"ig_caption": v.get("ig_caption") or captions.instagram_caption(v, wf)}
    row = video_view(conn, v, platform)
    sched_local = to_local(row["p_scheduled_for"], tz_of(conn)) if row["p_scheduled_for"] else None
    return render(request, "partials/edit_drawer.html", conn, row=row, fields=fields,
                  sched_value=sched_local.strftime("%Y-%m-%dT%H:%M") if sched_local else "",
                  rewrite_on=platform in rewrite.enabled_platforms(conn), rewrite_key=bool(config.env("ANTHROPIC_API_KEY")))


@app.post("/videos/{tiktok_id}/rewrite", response_class=HTMLResponse)
def video_rewrite(request: Request, tiktok_id: str, platform: str = "youtube"):
    """Regenerate the text for one platform with Claude, then re-render the drawer."""
    platform = platform_or_404(platform)
    conn = get_conn()
    v = db.get_video(conn, tiktok_id)
    if v is None:
        raise HTTPException(404)
    with db.tx(conn):
        res = rewrite.rewrite_video(conn, v, config.load_config(), platforms=[platform], force=True)
    resp = video_edit(request, tiktok_id, platform)
    resp.headers["HX-Trigger"] = json.dumps({"toast": f"Rewrite failed: {res['error'][:160]}" if res["error"] else "Rewritten with Claude"})
    return resp


@app.post("/videos/{tiktok_id}/edit", response_class=HTMLResponse)
async def video_save(request: Request, tiktok_id: str):
    form = await request.form()
    platform = platform_or_404(form.get("platform") or "youtube")
    conn = get_conn()
    with db.tx(conn):
        actions.set_text(conn, tiktok_id, yt_title=form.get("yt_title"), yt_description=form.get("yt_description"),
                         ig_caption=form.get("ig_caption"))
        when = (form.get("scheduled_for") or "").strip()
        if when:
            local = datetime.fromisoformat(when)
            utc = local_to_utc(local.date(), local.strftime("%H:%M"), tz_of(conn))
            try:
                actions.reschedule(conn, tiktok_id, platform, iso(utc))
            except ValueError as exc:
                return _row_response(request, conn, tiktok_id, platform, toast=str(exc))
    toast = "Saved"
    if form.get("publish_now") == "1":
        with db.tx(conn):
            job_id = actions.publish_now(conn, tiktok_id, platform)
        spawn_worker("--job", str(job_id))
        toast = f"Saved and publishing to {PLATFORM_LABEL[platform]} now"
    resp = _row_response(request, conn, tiktok_id, platform, toast=toast)
    resp.headers["HX-Retarget"] = f"#row-{tiktok_id}"
    resp.headers["HX-Reswap"] = "outerHTML"
    return resp


# ---------- calendar ----------

@app.get("/calendar", response_class=HTMLResponse)
def calendar(request: Request, view: str = "month", d: str = ""):
    conn = get_conn()
    tz = tz_of(conn)
    today = to_local(utcnow(), tz).date()
    try:
        anchor = date.fromisoformat(d) if d else today
    except ValueError:
        anchor = today
    if view == "day":
        start, end = anchor, anchor + timedelta(days=1)
        prev_d, next_d = anchor - timedelta(days=1), anchor + timedelta(days=1)
        title = anchor.strftime("%A %d %B %Y")
    elif view == "week":
        start = anchor - timedelta(days=anchor.weekday())
        end = start + timedelta(days=7)
        prev_d, next_d = start - timedelta(days=7), start + timedelta(days=7)
        title = f"Week of {start.strftime('%d %b %Y')}"
    else:
        view = "month"
        first = anchor.replace(day=1)
        start = first - timedelta(days=first.weekday())
        nxt = (first + timedelta(days=32)).replace(day=1)
        end = nxt + timedelta(days=(7 - nxt.weekday()) % 7)
        prev_d, next_d = (first - timedelta(days=1)).replace(day=1), nxt
        title = first.strftime("%B %Y")
    start_utc, end_utc = local_to_utc(start, "00:00", tz), local_to_utc(end, "00:00", tz)
    entries: dict[date, list[dict[str, Any]]] = {}
    for plat in PLATFORMS:
        px = PREFIX[plat]
        rows = db.rows(
            conn,
            f"""SELECT * FROM videos WHERE
                ({px}_status = 'scheduled' AND {px}_scheduled_for >= ? AND {px}_scheduled_for < ?)
             OR ({px}_status = 'uploaded' AND {px}_published_at >= ? AND {px}_published_at < ?)
             OR ({px}_status = 'failed' AND {px}_scheduled_for >= ? AND {px}_scheduled_for < ?)""",
            (iso(start_utc), iso(end_utc)) * 3,
        )
        for v in rows:
            vv = video_view(conn, v, plat)
            when = vv["p_published_at"] if vv["p_status"] == "uploaded" else vv["p_scheduled_for"]
            local = to_local(when, tz)
            if local is None:
                continue
            vv["when_local"] = local
            entries.setdefault(local.date(), []).append(vv)
    for lst in entries.values():
        lst.sort(key=lambda e: e["when_local"])
    days = [start + timedelta(days=i) for i in range((end - start).days)]
    return render(request, "calendar.html", conn, view=view, days=days, entries=entries, title=title, today=today,
                  anchor=anchor, prev_d=prev_d.isoformat(), next_d=next_d.isoformat(), month=anchor.month)


@app.post("/videos/{tiktok_id}/{platform}/reschedule")
def video_reschedule(tiktok_id: str, platform: str, day: str = Form(...), time: str = Form("")):
    """Calendar drag: keep the time of day, move to the dropped day."""
    platform = platform_or_404(platform)
    conn = get_conn()
    tz = tz_of(conn)
    v = db.get_video(conn, tiktok_id)
    if v is None:
        raise HTTPException(404)
    try:
        new_day = date.fromisoformat(day)
    except ValueError:
        raise HTTPException(400, "bad day")
    current = to_local(v[f"{PREFIX[platform]}_scheduled_for"], tz)
    hhmm = time or (current.strftime("%H:%M") if current else "10:00")
    when = local_to_utc(new_day, hhmm, tz)
    if when < utcnow():
        return Response(status_code=204, headers={"HX-Trigger": json.dumps({"toast": "That time is in the past"})})
    try:
        with db.tx(conn):
            actions.reschedule(conn, tiktok_id, platform, iso(when))
    except ValueError as exc:
        return Response(status_code=204, headers={"HX-Trigger": json.dumps({"toast": str(exc)})})
    return Response(status_code=204, headers={"HX-Trigger": json.dumps({"toast": f"Moved to {fmt_local(when, tz)}", "refresh": True})})


# ---------- connections ----------

@app.get("/connections", response_class=HTMLResponse)
def connections(request: Request, msg: str = "", error: str = ""):
    from repurposer.publishers import youtube
    conn = get_conn()
    yt_secret = youtube.client_secret_path()
    rows = []
    for plat in PLATFORMS:
        c = db.get_connection(conn, plat) or {"platform": plat, "healthy": 0}
        wf = db.get_workflow(conn, plat) or {}
        exp = parse(c.get("token_expires_at")) if c.get("token_expires_at") else None
        rows.append({**c, "platform": plat, "enabled": wf.get("enabled", 0),
                     "days_left": (exp - utcnow()).days if exp else None})
    return render(request, "connections.html", conn, rows=rows, msg=msg, error=error,
                  yt_secret_present=yt_secret.exists(), yt_secret_path=str(yt_secret),
                  ig_app_configured=bool(config.env("IG_APP_ID") and config.env("IG_APP_SECRET")))


@app.post("/connections/youtube/client-secret")
async def upload_client_secret(file: UploadFile = File(...)):
    """Drop the JSON downloaded from Google Cloud here instead of moving it by hand."""
    from repurposer.publishers import youtube
    raw = await file.read()
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return RedirectResponse("/connections?error=That file is not the JSON Google gave you", status_code=303)
    block = data.get("installed") or data.get("web")
    if not block or not block.get("client_id") or not block.get("client_secret"):
        return RedirectResponse("/connections?error=JSON has no OAuth client in it. Download the client's JSON from Credentials, not the project file", status_code=303)
    if "installed" not in data:
        return RedirectResponse("/connections?error=This is a Web client. Create the OAuth client as type Desktop app and download that one", status_code=303)
    target = youtube.client_secret_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(raw)
    return RedirectResponse("/connections?msg=Client secret saved. Now press Connect for YouTube", status_code=303)


@app.post("/connections/check")
def connections_check():
    spawn_worker("--check-connections")
    return Response(status_code=204, headers={"HX-Trigger": json.dumps({"toast": "Checking connections. Refresh in a few seconds."})})


@app.get("/connect/youtube")
def connect_youtube(request: Request):
    from repurposer.publishers import youtube
    redirect = str(request.url_for("oauth_youtube_callback"))
    try:
        flow = youtube.build_flow(redirect)
    except FileNotFoundError as exc:
        return RedirectResponse(f"/connections?error={exc}", status_code=303)
    state = secrets.token_urlsafe(16)
    url, _ = flow.authorization_url(access_type="offline", prompt="consent", state=state, include_granted_scopes="true")
    # The library uses PKCE: the code verifier generated here must be handed to the callback's flow.
    _oauth_states[state] = {"platform": "youtube", "code_verifier": flow.code_verifier}
    return RedirectResponse(url, status_code=303)


@app.get("/oauth/youtube/callback", name="oauth_youtube_callback")
def oauth_youtube_callback(request: Request, code: str = "", state: str = "", error: str = ""):
    from repurposer.publishers import youtube
    if error:
        return RedirectResponse(f"/connections?error=Google returned: {error}", status_code=303)
    entry = _oauth_states.pop(state, None)
    if not entry or entry.get("platform") != "youtube":
        return RedirectResponse("/connections?error=OAuth state mismatch; try Reconnect again", status_code=303)
    redirect = str(request.url_for("oauth_youtube_callback"))
    try:
        flow = youtube.build_flow(redirect)
        flow.code_verifier = entry.get("code_verifier")
        flow.fetch_token(code=code)
        youtube.save_credentials(flow.credentials)
        res = youtube.check_connection(get_conn(), config.load_config())
    except Exception as exc:  # noqa: BLE001
        log.exception("YouTube OAuth failed")
        return RedirectResponse(f"/connections?error={type(exc).__name__}: {exc}", status_code=303)
    if not res["healthy"]:
        return RedirectResponse(f"/connections?error={res['error']}", status_code=303)
    return RedirectResponse(f"/connections?msg=YouTube connected as {res['account_name']}", status_code=303)


@app.get("/connect/instagram")
def connect_instagram():
    from repurposer.publishers import instagram
    state = secrets.token_urlsafe(16)
    _oauth_states[state] = {"platform": "instagram"}
    try:
        return RedirectResponse(instagram.authorize_url(state), status_code=303)
    except instagram.InstagramError as exc:
        return RedirectResponse(f"/connections?error={exc}", status_code=303)


@app.get("/oauth/instagram/callback")
def oauth_instagram_callback(code: str = "", state: str = "", error: str = "", error_description: str = ""):
    from repurposer.publishers import instagram
    if error:
        return RedirectResponse(f"/connections?error=Instagram returned: {error_description or error}", status_code=303)
    entry = _oauth_states.pop(state, None)
    if not entry or entry.get("platform") != "instagram":
        return RedirectResponse("/connections?error=OAuth state mismatch; try Reconnect again", status_code=303)
    try:
        instagram.exchange_code(code)
        res = instagram.check_connection(get_conn(), config.load_config())
    except Exception as exc:  # noqa: BLE001
        log.exception("Instagram OAuth failed")
        return RedirectResponse(f"/connections?error={type(exc).__name__}: {exc}", status_code=303)
    if not res["healthy"]:
        return RedirectResponse(f"/connections?error={res['error']}", status_code=303)
    return RedirectResponse(f"/connections?msg=Instagram connected as @{res['account_name']}", status_code=303)


# ---------- runs ----------

@app.get("/runs", response_class=HTMLResponse)
def runs(request: Request, limit: int = 50):
    conn = get_conn()
    rows = db.rows(conn, "SELECT * FROM runs ORDER BY run_id DESC LIMIT ?", (limit,))
    for r in rows:
        try:
            r["detail"] = json.loads(r["summary"]) if r["summary"] else {}
        except json.JSONDecodeError:
            r["detail"] = {"raw": r["summary"]}
        r["log_name"] = Path(r["log_path"]).name if r.get("log_path") else None
    jobs = db.rows(conn, "SELECT * FROM jobs ORDER BY id DESC LIMIT 20")
    return render(request, "runs.html", conn, rows=rows, jobs=jobs)


@app.get("/logs/{name}", response_class=PlainTextResponse)
def log_file(name: str, tail: int = 400):
    path = (config.LOG_DIR / name).resolve()
    if config.LOG_DIR.resolve() not in path.parents or not path.exists():
        raise HTTPException(404)
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(lines[-tail:])


@app.get("/media/{name}")
def media_file(name: str):
    path = (config.MEDIA_DIR / name).resolve()
    if config.MEDIA_DIR.resolve() not in path.parents or not path.exists():
        raise HTTPException(404)
    return FileResponse(str(path))


@app.get("/health", response_class=PlainTextResponse)
def health():
    return "ok"


if __name__ == "__main__":
    import uvicorn

    config.ensure_dirs()
    logsetup.setup("web")
    uvicorn.run(app, host=config.env("WEB_HOST", "127.0.0.1"), port=int(config.env("WEB_PORT", "8080") or 8080), log_level="info")
