"""YouTube Shorts via the YouTube Data API v3.

OAuth 2.0 with a desktop client. The web app drives the consent flow (see app.py); the worker
only refreshes. Token in tokens/youtube.json.

Quota note: the spec quotes "100 videos.insert per day". The real default is 10,000 units per
day and videos.insert costs 1,600 units, so about six uploads per day per project. The estimate
is logged every run and publishing pauses (slots roll to tomorrow) when the estimate is exhausted.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .. import actions, captions, config, db
from ..timeutil import iso, utcnow
from . import PublishResult

log = logging.getLogger("repurposer.youtube")

SCOPES = ["https://www.googleapis.com/auth/youtube.upload", "https://www.googleapis.com/auth/youtube.readonly"]
TOKEN_PATH = config.TOKEN_DIR / "youtube.json"
DAILY_QUOTA_UNITS = 10_000
INSERT_COST = 1_600
LIST_COST = 1


def client_secret_path() -> Path:
    raw = config.env("YOUTUBE_CLIENT_SECRET_FILE", "tokens/client_secret.json") or ""
    p = Path(raw)
    return p if p.is_absolute() else config.ROOT / p


def load_credentials():
    """Return google Credentials or None. Refreshes and re-saves when expired."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    if not TOKEN_PATH.exists():
        return None
    creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        save_credentials(creds)
    return creds


def save_credentials(creds) -> None:
    config.TOKEN_DIR.mkdir(parents=True, exist_ok=True)
    TOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")


def build_flow(redirect_uri: str):
    """OAuth flow used by the web app's Reconnect button."""
    from google_auth_oauthlib.flow import Flow

    secret = client_secret_path()
    if not secret.exists():
        raise FileNotFoundError(f"YouTube client secret not found at {secret}; see README setup")
    return Flow.from_client_secrets_file(str(secret), scopes=SCOPES, redirect_uri=redirect_uri)


def service(creds):
    from googleapiclient.discovery import build

    return build("youtube", "v3", credentials=creds, cache_discovery=False)


def check_connection(conn: sqlite3.Connection, cfg: dict[str, Any]) -> dict[str, Any]:
    try:
        creds = load_credentials()
        if creds is None:
            raise RuntimeError("not connected: no token. Use Reconnect on the Connections page")
        resp = service(creds).channels().list(part="snippet", mine=True).execute()
        items = resp.get("items") or []
        if not items:
            raise RuntimeError("token works but no channel is attached to this Google account")
        ch = items[0]
        # The access token lasts an hour and refreshes itself; the refresh token has no expiry on a
        # published app, so there is no meaningful date to show.
        with db.tx(conn):
            db.upsert_connection(conn, "youtube", account_name=ch["snippet"]["title"], account_id=ch["id"],
                                 token_expires_at=None, healthy=1, last_error=None)
        return {"healthy": True, "error": None, "account_name": ch["snippet"]["title"]}
    except Exception as exc:  # noqa: BLE001
        msg = f"{type(exc).__name__}: {exc}"
        log.error("YouTube connection unhealthy: %s", msg)
        with db.tx(conn):
            db.upsert_connection(conn, "youtube", healthy=0, last_error=msg[:2000])
        return {"healthy": False, "error": msg, "account_name": None}


def _pacific_midnight() -> datetime:
    now = utcnow().astimezone(ZoneInfo("America/Los_Angeles"))
    return now.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(ZoneInfo("UTC"))


def quota_estimate(conn: sqlite3.Connection) -> tuple[int, int]:
    """(units used since the Pacific-midnight reset, units remaining). An estimate from our own uploads."""
    since = iso(_pacific_midnight())
    r = conn.execute("SELECT COUNT(*) AS n FROM videos WHERE yt_published_at >= ?", (since,)).fetchone()
    failed = conn.execute(
        "SELECT COUNT(*) AS n FROM videos WHERE yt_status = 'failed' AND last_attempt >= ?", (since,)
    ).fetchone()
    used = int(r["n"]) * INSERT_COST + int(failed["n"]) * INSERT_COST + LIST_COST * 4
    return used, max(DAILY_QUOTA_UNITS - used, 0)


def quota_ok(conn: sqlite3.Connection, cfg: dict[str, Any]) -> tuple[bool, str]:
    used, remaining = quota_estimate(conn)
    log.info("YouTube quota estimate: %s used, %s remaining of %s units", used, remaining, DAILY_QUOTA_UNITS)
    if remaining < INSERT_COST:
        return False, f"YouTube daily quota estimate exhausted ({used}/{DAILY_QUOTA_UNITS} units used); resets 08:00 UK"
    return True, f"{remaining} units remaining"


def upload(path: Path, title: str, description: str, extra: dict[str, Any], tags: list[str]) -> str:
    from googleapiclient.errors import HttpError
    from googleapiclient.http import MediaFileUpload

    creds = load_credentials()
    if creds is None:
        raise RuntimeError("YouTube is not connected")
    body = {
        "snippet": {
            "title": title,
            "description": description,
            "categoryId": str(extra.get("category_id", "27")),
            "tags": [t.lstrip("#") for t in tags][:20],
        },
        "status": {
            "privacyStatus": extra.get("privacy", "public"),
            "selfDeclaredMadeForKids": bool(extra.get("made_for_kids", False)),
        },
    }
    media = MediaFileUpload(str(path), mimetype="video/mp4", chunksize=8 * 1024 * 1024, resumable=True)
    request = service(creds).videos().insert(part="snippet,status", body=body, media_body=media)
    response = None
    retries = 0
    while response is None:
        try:
            _status, response = request.next_chunk()
        except HttpError as exc:
            if exc.resp.status in (500, 502, 503, 504) and retries < 5:
                retries += 1
                log.warning("YouTube upload chunk failed (%s), retry %s", exc.resp.status, retries)
                continue
            raise
    return response["id"]


def publish(conn: sqlite3.Connection, video: dict[str, Any], workflow: dict[str, Any], override: dict[str, Any],
            cfg: dict[str, Any]) -> PublishResult:
    if video.get("yt_status") == "uploaded":
        return PublishResult(True, "already uploaded", f"https://youtube.com/shorts/{video.get('yt_video_id')}")
    if video.get("yt_video_id"):
        # A previous run uploaded but crashed before recording success. Never upload twice.
        with db.tx(conn):
            actions.mark_uploaded(conn, video["tiktok_id"], "youtube")
        return PublishResult(True, "recovered earlier upload", f"https://youtube.com/shorts/{video['yt_video_id']}")
    path = Path(video["local_path"] or "")
    if not path.exists():
        return PublishResult(False, f"local file missing: {path}")
    title, description = captions.youtube_snippet(video, workflow, override)
    try:
        video_id = upload(path, title, description, workflow.get("extra") or {}, workflow.get("hashtags") or [])
    except Exception as exc:  # noqa: BLE001
        body = getattr(exc, "content", b"")
        detail = body.decode("utf-8", "replace") if isinstance(body, bytes) else ""
        return PublishResult(False, f"{type(exc).__name__}: {exc}\n{detail}".strip())
    with db.tx(conn):
        actions.mark_uploaded(conn, video["tiktok_id"], "youtube", yt_video_id=video_id)
    return PublishResult(True, "uploaded", f"https://youtube.com/shorts/{video_id}")


def token_days_left() -> float | None:
    if not TOKEN_PATH.exists():
        return None
    try:
        data = json.loads(TOKEN_PATH.read_text())
    except json.JSONDecodeError:
        return None
    exp = data.get("expiry")
    if not exp:
        return None
    dt = datetime.fromisoformat(exp.replace("Z", "+00:00"))
    return (dt - utcnow()).total_seconds() / 86400
