"""Download TikTok videos watermark-free with yt-dlp.

New content is downloaded as soon as it is old enough (min_age_minutes). Backfill content is
downloaded lazily, only when a platform slot is within backfill_download_hours, so disk use stays
flat and a TikTok deleted in the meantime is caught before it is reposted.
"""
from __future__ import annotations

import logging
import shutil
import sqlite3
from datetime import timedelta
from pathlib import Path
from typing import Any

from . import actions, config, db
from .timeutil import iso, minutes_ago, utcnow

log = logging.getLogger("repurposer.downloader")

# TikTok exposes watermarked ("download_addr", format_note contains 'watermarked') and clean
# ("play_addr") variants. Prefer anything not marked watermarked, fall back to best.
FORMAT = "bv*[format_note!*=watermark]+ba/b[format_note!*=watermark]/bv*+ba/b"

UNAVAILABLE_MARKERS = ("Video not available", "unavailable", "private", "removed", "404", "not found", "Unable to extract")


class DownloadError(RuntimeError):
    pass


class VideoGone(DownloadError):
    """The TikTok no longer exists or is private; never repost it."""


def free_gb(path: Path) -> float:
    usage = shutil.disk_usage(str(path))
    return usage.free / (1024 ** 3)


def download(video: dict[str, Any], media_dir: Path | None = None) -> Path:
    import yt_dlp

    media_dir = media_dir or config.MEDIA_DIR
    media_dir.mkdir(parents=True, exist_ok=True)
    target = media_dir / f"{video['tiktok_id']}.mp4"
    if target.exists() and target.stat().st_size > 0:
        return target
    url = video.get("tiktok_url") or f"https://www.tiktok.com/@_/video/{video['tiktok_id']}"
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "format": FORMAT,
        "outtmpl": str(media_dir / f"{video['tiktok_id']}.%(ext)s"),
        "merge_output_format": "mp4",
        "noplaylist": True,
        "retries": 3,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.extract_info(url, download=True)
    except yt_dlp.utils.DownloadError as exc:
        msg = str(exc)
        if any(m.lower() in msg.lower() for m in UNAVAILABLE_MARKERS):
            raise VideoGone(f"TikTok {video['tiktok_id']} is no longer available: {msg[:300]}") from exc
        raise DownloadError(f"yt-dlp failed for {video['tiktok_id']}: {msg[:500]}") from exc
    if not target.exists():
        # yt-dlp may have written a different extension when merging was not needed.
        candidates = sorted(media_dir.glob(f"{video['tiktok_id']}.*"))
        if not candidates:
            raise DownloadError(f"yt-dlp reported success but no file for {video['tiktok_id']}")
        candidates[0].rename(target)
    return target


def select_pending(conn: sqlite3.Connection, cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """Rows in status 'new' that should be downloaded now."""
    min_age = int(db.get_setting(conn, "min_age_minutes", cfg["source"].get("min_age_minutes", 60)))
    hours = float(cfg.get("limits", {}).get("backfill_download_hours", 24))
    soon = iso(utcnow() + timedelta(hours=hours))
    rows = db.rows(
        conn,
        """SELECT * FROM videos WHERE status = 'new' AND (
               origin = 'new'
               OR (yt_status = 'scheduled' AND yt_scheduled_for <= ?)
               OR (ig_status = 'scheduled' AND ig_scheduled_for <= ?)
           ) ORDER BY published_at DESC""",
        (soon, soon),
    )
    out = []
    for r in rows:
        if r["origin"] == "new":
            age = minutes_ago(r.get("published_at") or r.get("first_seen"))
            if age is not None and age < min_age:
                continue
        out.append(r)
    return out


def run(conn: sqlite3.Connection, cfg: dict[str, Any]) -> dict[str, Any]:
    """Download everything pending. Returns counts plus a list of (tiktok_id, error) failures."""
    limits = cfg.get("limits", {})
    min_free = float(limits.get("min_free_disk_gb", 2))
    stats: dict[str, Any] = {"downloaded": 0, "gone": 0, "failed": [], "disk_alert": None}
    pending = select_pending(conn, cfg)
    for v in pending:
        gb = free_gb(config.MEDIA_DIR if config.MEDIA_DIR.exists() else config.ROOT)
        if gb < min_free:
            stats["disk_alert"] = f"only {gb:.1f} GB free (minimum {min_free:.0f} GB); downloads paused"
            log.error(stats["disk_alert"])
            break
        try:
            path = download(v)
        except VideoGone as exc:
            with db.tx(conn):
                actions.skip(conn, v["tiktok_id"], "removed from TikTok before repost")
            stats["gone"] += 1
            log.warning("%s", exc)
            continue
        except DownloadError as exc:
            log.error("download failed for %s: %s", v["tiktok_id"], exc)
            with db.tx(conn):
                db.update_video(conn, v["tiktok_id"], status_reason=str(exc)[:1000], last_attempt=iso(utcnow()),
                                attempts=int(v.get("attempts") or 0) + 1)
            stats["failed"].append((v["tiktok_id"], str(exc)))
            continue
        with db.tx(conn):
            db.update_video(conn, v["tiktok_id"], local_path=str(path), status="downloaded", status_reason=None)
        stats["downloaded"] += 1
    return stats


def cleanup(conn: sqlite3.Connection, cfg: dict[str, Any]) -> dict[str, Any]:
    """Delete local video files for videos that are finished everywhere. Thumbnails are kept for the UI."""
    stats: dict[str, Any] = {"removed": 0, "freed_mb": 0.0, "failed": []}
    rows = db.rows(conn, "SELECT tiktok_id, local_path FROM videos WHERE local_path IS NOT NULL AND status IN ('done','skipped','failed')")
    for r in rows:
        path = Path(r["local_path"])
        try:
            size = path.stat().st_size if path.exists() else 0
            if path.exists():
                path.unlink()
            with db.tx(conn):
                db.update_video(conn, r["tiktok_id"], local_path=None)
            stats["removed"] += 1
            stats["freed_mb"] += size / (1024 * 1024)
        except OSError as exc:
            log.error("could not remove %s: %s", path, exc)
            stats["failed"].append((r["tiktok_id"], str(exc)))
    return stats
