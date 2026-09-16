"""List TikTok videos with yt-dlp (metadata only) and record unseen ones.

Two entry points:
  poll()             recent videos since the newest we know about (or lookback_days on first run)
  import_catalogue() the whole profile, inserted as origin='existing', sponsored ones held
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta
from typing import Any, Callable, Iterable

from . import captions, config, db
from .timeutil import UTC, iso, parse, utcnow

log = logging.getLogger("repurposer.poller")


class PollError(RuntimeError):
    pass


def _ydl_opts(extra: dict[str, Any] | None = None) -> dict[str, Any]:
    opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "ignoreerrors": True,      # one broken video must not kill the whole listing
        "extract_flat": False,     # we need caption + timestamp for every entry
        "noplaylist": False,
        "lazy_playlist": True,
        "socket_timeout": 60,      # TikTok is slow from time to time; 20s default drops videos from the listing
        "extractor_retries": 3,
        "retries": 3,
    }
    if extra:
        opts.update(extra)
    return opts


def _entry_to_video(entry: dict[str, Any]) -> dict[str, Any] | None:
    vid = entry.get("id")
    if not vid:
        return None
    ts = entry.get("timestamp")
    published = datetime.fromtimestamp(ts, tz=UTC) if ts else None
    if published is None and entry.get("upload_date"):
        published = datetime.strptime(entry["upload_date"], "%Y%m%d").replace(tzinfo=UTC)
    caption = entry.get("description") or entry.get("title") or ""
    formats = entry.get("formats") or []
    # TikTok photo carousels come through yt-dlp as audio-only formats; they cannot be Shorts or Reels.
    is_photo = bool(formats) and all((f.get("vcodec") in (None, "none")) for f in formats)
    if "/photo/" in (entry.get("webpage_url") or ""):
        is_photo = True
    return {
        "is_photo": is_photo,
        "tiktok_id": str(vid),
        "tiktok_url": entry.get("webpage_url") or entry.get("original_url") or entry.get("url"),
        "tiktok_caption": caption,
        "thumbnail_url": entry.get("thumbnail"),
        "published_at": iso(published) if published else None,
    }


def list_videos(profile_url: str, *, after: datetime | None = None, max_items: int | None = None,
                progress: Callable[[int, dict], None] | None = None) -> Iterable[dict[str, Any]]:
    """Yield video dicts for a profile, newest first as TikTok lists them.

    Pinned videos come first on a TikTok profile and can be old, so we never rely on list order
    to stop early. Instead the poll caps the listing at `max_items` and filters by `after` here.
    """
    import yt_dlp

    extra: dict[str, Any] = {}
    if max_items:
        extra["playlistend"] = int(max_items)
    with yt_dlp.YoutubeDL(_ydl_opts(extra)) as ydl:
        try:
            info = ydl.extract_info(profile_url, download=False)
        except yt_dlp.utils.DownloadError as exc:
            raise PollError(f"yt-dlp could not list {profile_url}: {exc}") from exc
    if not info:
        raise PollError(f"yt-dlp returned nothing for {profile_url}")
    entries = info.get("entries") or ([info] if info.get("id") else [])
    n = 0
    for entry in entries:
        if not entry:
            continue
        v = _entry_to_video(entry)
        if v is None:
            continue
        if after is not None and v["published_at"] and parse(v["published_at"]) < after:
            continue
        n += 1
        if progress:
            progress(n, v)
        yield v


def _newest_known(conn: sqlite3.Connection) -> datetime | None:
    r = conn.execute("SELECT MAX(published_at) AS m FROM videos WHERE origin = 'new'").fetchone()
    return parse(r["m"]) if r and r["m"] else None


def _insert(conn: sqlite3.Connection, v: dict[str, Any], origin: str, exclude_keywords: list[str]) -> bool:
    kw = captions.matches_exclusion(v["tiktok_caption"], exclude_keywords)
    is_photo = bool(v.get("is_photo"))
    fields = dict({k: val for k, val in v.items() if k != "is_photo"}, origin=origin, status="new", first_seen=iso(utcnow()))
    if is_photo:
        fields.update(status="skipped", status_reason="photo post, no video stream", yt_status="skipped", ig_status="skipped")
    elif kw:
        fields["status"] = "held"
        fields["status_reason"] = f"caption contains '{kw}'"
    return db.insert_video(conn, **fields)


def _exclusions(conn: sqlite3.Connection) -> list[str]:
    kws: list[str] = []
    for wf in db.all_workflows(conn).values():
        for k in wf.get("exclude_keywords") or []:
            if k not in kws:
                kws.append(k)
    return kws


def poll(conn: sqlite3.Connection, cfg: dict[str, Any]) -> dict[str, Any]:
    """Detect new videos. Returns {'seen': n, 'inserted': n, 'held': n}."""
    url = config.tiktok_profile_url(cfg)
    newest = _newest_known(conn)
    if newest is None:
        lookback = int(db.get_setting(conn, "lookback_days", cfg["source"].get("lookback_days", 7)))
        after = utcnow() - timedelta(days=lookback)
    else:
        after = newest - timedelta(hours=1)  # small overlap so a clock skew never loses a video
    exclude = _exclusions(conn)
    max_items = int(cfg["source"].get("poll_max_items", 30))
    seen = inserted = held = 0
    with db.tx(conn):
        for v in list_videos(url, after=after, max_items=max_items):
            seen += 1
            if conn.execute("SELECT 1 FROM videos WHERE tiktok_id = ?", (v["tiktok_id"],)).fetchone():
                continue
            if _insert(conn, v, "new", exclude):
                inserted += 1
                if captions.matches_exclusion(v["tiktok_caption"], exclude):
                    held += 1
                log.info("new TikTok %s (%s)", v["tiktok_id"], (v["tiktok_caption"] or "")[:60])
    return {"seen": seen, "inserted": inserted, "held": held}


def import_catalogue(conn: sqlite3.Connection, cfg: dict[str, Any], *, limit: int | None = None,
                     progress: Callable[[str], None] | None = None) -> dict[str, Any]:
    """Walk the profile newest-first, insert as existing. Idempotent: a second run adds zero rows.

    `limit` caps the walk at the newest N entries as TikTok lists them. Pinned videos sit at the top
    of a profile, so they count towards N whatever their date.
    """
    url = config.tiktok_profile_url(cfg)
    exclude = _exclusions(conn)
    seen = inserted = held = 0

    def _p(n: int, v: dict[str, Any]) -> None:
        if progress and (n % 10 == 0 or n == 1):
            progress(f"  listed {n} videos so far (latest: {v['tiktok_id']} {v['published_at'] or ''})")

    with db.tx(conn):
        for v in list_videos(url, max_items=limit or None, progress=_p):
            seen += 1
            if _insert(conn, v, "existing", exclude):
                inserted += 1
                if captions.matches_exclusion(v["tiktok_caption"], exclude):
                    held += 1
    return {"seen": seen, "inserted": inserted, "held": held}
