"""Instagram Reels via the Instagram API with Instagram Login (no Facebook Page needed).

Token in tokens/instagram.json: {"access_token", "user_id", "username", "expires_at"}.
Resumable upload: create a REELS container with upload_type=resumable, POST the bytes to the
rupload endpoint, poll the container until FINISHED, then media_publish. The media_publish step
only ever runs when a container ID exists and no media ID exists, so a crash mid-way cannot
double-post.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from datetime import timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import requests

from .. import actions, captions, config, db
from ..timeutil import iso, parse, utcnow
from . import PublishResult

log = logging.getLogger("repurposer.instagram")

API_VERSION = "v23.0"
GRAPH = f"https://graph.instagram.com/{API_VERSION}"
RUPLOAD = f"https://rupload.facebook.com/ig-api-upload/{API_VERSION}"
AUTH_URL = "https://www.instagram.com/oauth/authorize"
TOKEN_URL = "https://api.instagram.com/oauth/access_token"
SCOPES = ["instagram_business_basic", "instagram_business_content_publish"]
TOKEN_PATH = config.TOKEN_DIR / "instagram.json"
REFRESH_IF_DAYS_LEFT = 10
POLL_EVERY_S = 15
POLL_MAX_S = 600
TIMEOUT = 60


class InstagramError(RuntimeError):
    pass


def load_token() -> dict[str, Any] | None:
    if not TOKEN_PATH.exists():
        return None
    try:
        return json.loads(TOKEN_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise InstagramError(f"{TOKEN_PATH.name} is not valid JSON: {exc}") from exc


def save_token(data: dict[str, Any]) -> None:
    config.TOKEN_DIR.mkdir(parents=True, exist_ok=True)
    TOKEN_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _check(resp: requests.Response) -> dict[str, Any]:
    try:
        data = resp.json()
    except ValueError:
        data = {"raw": resp.text[:1000]}
    if resp.status_code >= 400 or "error" in data:
        err = data.get("error") or data
        raise InstagramError(f"HTTP {resp.status_code}: {json.dumps(err)[:1500]}")
    return data


# ---------- OAuth (used by the web app) ----------

def authorize_url(state: str) -> str:
    app_id = config.env("IG_APP_ID")
    redirect = config.env("IG_REDIRECT_URI", "https://bami-oladipupo.github.io/repurposer/oauth/instagram/callback/")
    if not app_id:
        raise InstagramError("IG_APP_ID is not set in .env")
    q = {"client_id": app_id, "redirect_uri": redirect, "scope": ",".join(SCOPES), "response_type": "code",
         "state": state, "enable_fb_login": "0", "force_authentication": "1"}
    return f"{AUTH_URL}?{urlencode(q)}"


def exchange_code(code: str) -> dict[str, Any]:
    app_id, secret = config.env("IG_APP_ID"), config.env("IG_APP_SECRET")
    redirect = config.env("IG_REDIRECT_URI", "https://bami-oladipupo.github.io/repurposer/oauth/instagram/callback/")
    if not app_id or not secret:
        raise InstagramError("IG_APP_ID / IG_APP_SECRET missing from .env")
    short = _check(requests.post(TOKEN_URL, data={
        "client_id": app_id, "client_secret": secret, "grant_type": "authorization_code",
        "redirect_uri": redirect, "code": code}, timeout=TIMEOUT))
    long_lived = _check(requests.get(f"https://graph.instagram.com/access_token", params={
        "grant_type": "ig_exchange_token", "client_secret": secret, "access_token": short["access_token"]}, timeout=TIMEOUT))
    expires_at = utcnow() + timedelta(seconds=int(long_lived.get("expires_in", 60 * 86400)))
    me = _check(requests.get(f"{GRAPH}/me", params={"fields": "id,username,account_type",
                                                  "access_token": long_lived["access_token"]}, timeout=TIMEOUT))
    data = {"access_token": long_lived["access_token"], "user_id": str(me["id"]), "username": me.get("username"),
            "account_type": me.get("account_type"), "expires_at": iso(expires_at), "obtained_at": iso(utcnow())}
    save_token(data)
    return data


def refresh_token(tok: dict[str, Any]) -> dict[str, Any]:
    data = _check(requests.get(f"{GRAPH}/refresh_access_token", params={
        "grant_type": "ig_refresh_token", "access_token": tok["access_token"]}, timeout=TIMEOUT))
    tok = dict(tok, access_token=data["access_token"],
               expires_at=iso(utcnow() + timedelta(seconds=int(data.get("expires_in", 60 * 86400)))),
               obtained_at=iso(utcnow()))
    save_token(tok)
    return tok


# ---------- connection health ----------

def check_connection(conn: sqlite3.Connection, cfg: dict[str, Any]) -> dict[str, Any]:
    try:
        tok = load_token()
        if tok is None:
            raise InstagramError("not connected: no token. Use Reconnect on the Connections page")
        expires = parse(tok.get("expires_at"))
        if expires is not None:
            days_left = (expires - utcnow()).total_seconds() / 86400
            if days_left < REFRESH_IF_DAYS_LEFT:
                try:
                    tok = refresh_token(tok)
                    log.info("Instagram token refreshed; new expiry %s", tok["expires_at"])
                except InstagramError as exc:
                    raise InstagramError(f"token expires in {days_left:.0f} days and refresh failed: {exc}") from exc
        me = _check(requests.get(f"{GRAPH}/me", params={"fields": "id,username,account_type",
                                                      "access_token": tok["access_token"]}, timeout=TIMEOUT))
        if me.get("account_type") not in (None, "BUSINESS", "MEDIA_CREATOR", "CREATOR"):
            raise InstagramError(f"account type is {me.get('account_type')}; convert to a Creator or Business account")
        with db.tx(conn):
            db.upsert_connection(conn, "instagram", account_name=me.get("username"), account_id=str(me["id"]),
                                 token_expires_at=tok.get("expires_at"), healthy=1, last_error=None)
        return {"healthy": True, "error": None, "account_name": me.get("username")}
    except Exception as exc:  # noqa: BLE001
        msg = f"{type(exc).__name__}: {exc}"
        log.error("Instagram connection unhealthy: %s", msg)
        with db.tx(conn):
            db.upsert_connection(conn, "instagram", healthy=0, last_error=msg[:2000])
        return {"healthy": False, "error": msg, "account_name": None}


def quota_ok(conn: sqlite3.Connection, cfg: dict[str, Any]) -> tuple[bool, str]:
    tok = load_token()
    if tok is None:
        return False, "Instagram not connected"
    try:
        data = _check(requests.get(f"{GRAPH}/{tok['user_id']}/content_publishing_limit",
                                   params={"fields": "quota_usage,config", "access_token": tok["access_token"]},
                                   timeout=TIMEOUT))
    except InstagramError as exc:
        return False, f"could not read publishing limit: {exc}"
    entry = (data.get("data") or [{}])[0]
    used = int(entry.get("quota_usage", 0))
    total = int((entry.get("config") or {}).get("quota_total", 100))
    log.info("Instagram publishing limit: %s/%s used", used, total)
    if used >= total:
        return False, f"Instagram publishing limit reached ({used}/{total} in 24h)"
    return True, f"{total - used} publishes remaining"


# ---------- publishing ----------

def _create_container(tok: dict[str, Any], caption: str, share_to_feed: bool) -> dict[str, Any]:
    return _check(requests.post(f"{GRAPH}/{tok['user_id']}/media", data={
        "media_type": "REELS", "upload_type": "resumable", "caption": caption,
        "share_to_feed": "true" if share_to_feed else "false", "access_token": tok["access_token"]}, timeout=TIMEOUT))


def _upload_bytes(tok: dict[str, Any], container_id: str, path: Path) -> None:
    size = path.stat().st_size
    headers = {"Authorization": f"OAuth {tok['access_token']}", "offset": "0", "file_size": str(size),
               "Content-Type": "application/octet-stream"}
    with path.open("rb") as fh:
        resp = requests.post(f"{RUPLOAD}/{container_id}", headers=headers, data=fh, timeout=600)
    data = _check(resp)
    if not data.get("success", True):
        raise InstagramError(f"rupload did not report success: {data}")


def _wait_for_container(tok: dict[str, Any], container_id: str) -> None:
    waited = 0
    while waited <= POLL_MAX_S:
        data = _check(requests.get(f"{GRAPH}/{container_id}", params={"fields": "status_code,status",
                                                                     "access_token": tok["access_token"]}, timeout=TIMEOUT))
        code = data.get("status_code")
        if code == "FINISHED":
            return
        if code in ("ERROR", "EXPIRED"):
            raise InstagramError(f"container {container_id} {code}: {data.get('status')}")
        time.sleep(POLL_EVERY_S)
        waited += POLL_EVERY_S
    raise InstagramError(f"container {container_id} not FINISHED after {POLL_MAX_S}s (last status {data.get('status')})")


def _media_publish(tok: dict[str, Any], container_id: str) -> str:
    data = _check(requests.post(f"{GRAPH}/{tok['user_id']}/media_publish", data={
        "creation_id": container_id, "access_token": tok["access_token"]}, timeout=TIMEOUT))
    return str(data["id"])


def _permalink(tok: dict[str, Any], media_id: str) -> str | None:
    try:
        data = _check(requests.get(f"{GRAPH}/{media_id}", params={"fields": "permalink",
                                                                 "access_token": tok["access_token"]}, timeout=TIMEOUT))
        return data.get("permalink")
    except InstagramError:
        return None


def publish(conn: sqlite3.Connection, video: dict[str, Any], workflow: dict[str, Any], override: dict[str, Any],
            cfg: dict[str, Any]) -> PublishResult:
    if video.get("ig_status") == "uploaded" or video.get("ig_media_id"):
        if video.get("ig_status") != "uploaded":
            with db.tx(conn):
                actions.mark_uploaded(conn, video["tiktok_id"], "instagram")
        return PublishResult(True, "already published", None)
    tok = load_token()
    if tok is None:
        return PublishResult(False, "Instagram is not connected")
    path = Path(video["local_path"] or "")
    if not path.exists():
        return PublishResult(False, f"local file missing: {path}")
    caption = captions.instagram_caption(video, workflow, override)
    share = bool((workflow.get("extra") or {}).get("share_to_feed", True))
    try:
        container_id = video.get("ig_container_id")
        if container_id:
            # Resume: if the old container is still good we reuse it, otherwise start again.
            try:
                _wait_for_container(tok, container_id)
            except InstagramError as exc:
                log.warning("previous container %s unusable (%s); creating a new one", container_id, exc)
                container_id = None
        if not container_id:
            created = _create_container(tok, caption, share)
            container_id = str(created["id"])
            with db.tx(conn):
                db.update_video(conn, video["tiktok_id"], ig_container_id=container_id)
            _upload_bytes(tok, container_id, path)
            _wait_for_container(tok, container_id)
        media_id = _media_publish(tok, container_id)
    except Exception as exc:  # noqa: BLE001
        return PublishResult(False, f"{type(exc).__name__}: {exc}")
    link = _permalink(tok, media_id)
    with db.tx(conn):
        actions.mark_uploaded(conn, video["tiktok_id"], "instagram", ig_media_id=media_id)
    return PublishResult(True, "published", link or f"instagram media {media_id}")
