"""Weekly digest: what went out, what is queued, what needs attention."""
from __future__ import annotations

import shutil
import sqlite3
from datetime import timedelta
from typing import Any

from . import config, db
from .config import PLATFORMS, PLATFORM_LABEL, PREFIX
from .timeutil import fmt_local, iso, parse, utcnow


def _link(plat: str, v: dict[str, Any]) -> str:
    if plat == "youtube" and v.get("yt_video_id"):
        return f"https://youtube.com/shorts/{v['yt_video_id']}"
    if plat == "instagram" and v.get("ig_media_id"):
        return f"https://www.instagram.com/reel/{v['ig_media_id']}/"
    return ""


def build_digest(conn: sqlite3.Connection, cfg: dict[str, Any], *, days: int = 7) -> str:
    now = utcnow()
    tz = db.timezone(conn)
    since, until = iso(now - timedelta(days=days)), iso(now + timedelta(days=days))
    lines = [f"Repurposer weekly digest ({fmt_local(now, tz, '%d %b')})"]
    wfs = db.all_workflows(conn)
    retry_runs = int(cfg.get("limits", {}).get("retry_runs", 3))

    for plat in PLATFORMS:
        wf = wfs.get(plat) or {}
        px = PREFIX[plat]
        if not wf.get("enabled"):
            continue
        published = db.rows(conn, f"SELECT * FROM videos WHERE {px}_status='uploaded' AND {px}_published_at >= ? ORDER BY {px}_published_at", (since,))
        upcoming = db.rows(conn, f"SELECT * FROM videos WHERE {px}_status='scheduled' AND {px}_scheduled_for BETWEEN ? AND ? ORDER BY {px}_scheduled_for", (iso(now), until))
        queued = conn.execute(f"SELECT COUNT(*) FROM videos WHERE {px}_status='queued' AND status IN ('new','downloaded','ready')").fetchone()[0]
        failed = db.rows(conn, f"SELECT tiktok_id, {px}_error AS err, {px}_attempts AS n FROM videos WHERE {px}_status='failed'")
        mode = "Manual (paused)" if not wf.get("auto_publish") else ("ASAP" if wf.get("mode") == "asap" else "On schedule")
        lines.append("")
        lines.append(f"{PLATFORM_LABEL[plat]} ({mode})")
        lines.append(f"  Published last {days} days: {len(published)}")
        for v in published[:10]:
            lines.append(f"    {fmt_local(v[f'{px}_published_at'], tz, '%a %d %b')}  {(v['tiktok_caption'] or v['tiktok_id'])[:50]}  {_link(plat, v)}".rstrip())
        lines.append(f"  Scheduled next {days} days: {len(upcoming)}")
        for v in upcoming[:10]:
            lines.append(f"    {fmt_local(v[f'{px}_scheduled_for'], tz, '%a %d %b %H:%M')}  {(v['tiktok_caption'] or v['tiktok_id'])[:50]}")
        lines.append(f"  Waiting for a slot: {queued}")
        if failed:
            lines.append(f"  Failed: {len(failed)}")
            for f in failed[:10]:
                tag = "needs manual attention" if int(f["n"] or 0) > retry_runs else "retrying"
                lines.append(f"    {f['tiktok_id']} ({tag}): {(f['err'] or '').splitlines()[0][:120]}")

    held = db.rows(conn, "SELECT tiktok_id, status_reason FROM videos WHERE status='held'")
    if held:
        lines.append("")
        lines.append(f"Held ({len(held)}), release from the Content page when ready")
        for h in held[:10]:
            lines.append(f"  {h['tiktok_id']}: {h['status_reason'] or ''}")

    rw_failed = db.rows(conn, "SELECT tiktok_id, rewrite_error FROM videos WHERE rewrite_status='failed'")
    if rw_failed:
        lines.append("")
        lines.append(f"Caption rewrite failed ({len(rw_failed)}), templates used")
        for r in rw_failed[:5]:
            lines.append(f"  {r['tiktok_id']}: {(r['rewrite_error'] or '')[:120]}")

    lines.append("")
    lines.append("Connections")
    for plat in PLATFORMS:
        if not (wfs.get(plat) or {}).get("enabled"):
            continue
        c = db.get_connection(conn, plat) or {}
        exp = parse(c.get("token_expires_at")) if c.get("token_expires_at") else None
        days_left = f", token {int((exp - now).total_seconds() // 86400)} days left" if exp else (", token refreshes automatically" if plat == "youtube" else "")
        state = "healthy" if c.get("healthy") else f"UNHEALTHY: {(c.get('last_error') or 'not connected')[:100]}"
        lines.append(f"  {PLATFORM_LABEL[plat]}: {state}{days_left}")

    runs = db.rows(conn, "SELECT COUNT(*) AS n, SUM(CASE WHEN ok=0 THEN 1 ELSE 0 END) AS bad FROM runs WHERE started >= ?", (since,))
    free_gb = shutil.disk_usage(str(config.ROOT)).free / (1024 ** 3)
    lines.append("")
    lines.append(f"Worker runs last {days} days: {runs[0]['n']} ({runs[0]['bad'] or 0} with problems). Disk free: {free_gb:.0f} GB.")
    return "\n".join(lines)
