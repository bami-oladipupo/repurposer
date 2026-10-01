"""Push alerts to the IV Repost iPhone app through Apple's push service (APNs).

Needs four values in .env: APNS_KEY_FILE (the .p8 auth key), APNS_KEY_ID, APNS_TEAM_ID and
APNS_BUNDLE_ID. Phones register their device token through /api/app/devices. A token Apple reports
as dead is disabled, never retried; every other failure is recorded on the device row and returned.
"""
from __future__ import annotations

import base64
import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import Any

from . import config, db
from .timeutil import iso, utcnow

log = logging.getLogger("repurposer.push")
HOSTS = {"sandbox": "https://api.sandbox.push.apple.com", "production": "https://api.push.apple.com"}
DEAD_TOKEN_REASONS = {"BadDeviceToken", "Unregistered", "DeviceTokenNotForTopic"}


class PushError(RuntimeError):
    pass


def _key_path() -> Path | None:
    raw = config.env("APNS_KEY_FILE")
    if not raw:
        return None
    path = Path(raw).expanduser()
    return path if path.is_absolute() else config.ROOT / path


def configured() -> bool:
    path = _key_path()
    return bool(path and path.exists() and config.env("APNS_KEY_ID") and config.env("APNS_TEAM_ID")
                and config.env("APNS_BUNDLE_ID"))


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _jwt() -> str:
    """Provider token: ES256 over header.claims, signed with the .p8 key."""
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

    key = serialization.load_pem_private_key(_key_path().read_bytes(), password=None)
    head = _b64(json.dumps({"alg": "ES256", "kid": config.env("APNS_KEY_ID")}).encode())
    claims = _b64(json.dumps({"iss": config.env("APNS_TEAM_ID"), "iat": int(time.time())}).encode())
    r, s = decode_dss_signature(key.sign(f"{head}.{claims}".encode(), ec.ECDSA(hashes.SHA256())))
    return f"{head}.{claims}.{_b64(r.to_bytes(32, 'big') + s.to_bytes(32, 'big'))}"


def register(conn: sqlite3.Connection, token: str, environment: str) -> None:
    now = iso(utcnow())
    conn.execute(
        """INSERT INTO devices(token, environment, created_at, last_seen) VALUES (?,?,?,?)
           ON CONFLICT(token) DO UPDATE SET environment=excluded.environment, last_seen=excluded.last_seen,
                                            disabled_at=NULL, last_error=NULL""",
        (token, environment, now, now),
    )


def device_count(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM devices WHERE disabled_at IS NULL").fetchone()[0])


def send_all(conn: sqlite3.Connection, title: str, body: str, *, route: str | None = None) -> tuple[int, list[str]]:
    """Send one alert to every registered phone. Returns (sent, errors)."""
    devices = db.rows(conn, "SELECT * FROM devices WHERE disabled_at IS NULL")
    if not devices:
        return 0, []
    if not configured():
        raise PushError("APNS_KEY_FILE, APNS_KEY_ID, APNS_TEAM_ID and APNS_BUNDLE_ID are required in .env")
    import httpx

    payload: dict[str, Any] = {"aps": {"alert": {"title": title, "body": body[:900]}, "sound": "default"}}
    if route:
        payload["route"] = route
    headers = {"authorization": f"bearer {_jwt()}", "apns-topic": config.env("APNS_BUNDLE_ID"),
               "apns-push-type": "alert", "apns-priority": "10"}
    sent, errors = 0, []
    with httpx.Client(http2=True, timeout=20) as client:
        for d in devices:
            try:
                resp = client.post(f"{HOSTS[d['environment']]}/3/device/{d['token']}", headers=headers, json=payload)
            except httpx.HTTPError as exc:
                errors.append(f"could not reach Apple push: {exc}")
                continue
            if resp.status_code == 200:
                sent += 1
                continue
            try:
                reason = resp.json().get("reason", "")
            except ValueError:
                reason = resp.text[:200]
            err = f"Apple push {resp.status_code}: {reason}"
            errors.append(err)
            log.error("push to %s… failed: %s", d["token"][:8], err)
            with db.tx(conn):
                conn.execute("UPDATE devices SET last_error=?, disabled_at=? WHERE token=?",
                             (err, iso(utcnow()) if reason in DEAD_TOKEN_REASONS else None, d["token"]))
    return sent, errors


def summarise(run: dict[str, Any]) -> tuple[str, str]:
    """Short (title, body) for a run that published something or hit a problem."""
    published = run.get("published") or []
    problems = len(run.get("failed") or []) + len(run.get("alerts") or []) + len(run.get("stage_errors") or [])
    names = {"youtube": "YouTube", "instagram": "Instagram"}
    lines = []
    if published:
        plats = " and ".join(dict.fromkeys(names.get(p, p) for p, _, _ in published))
        lines.append(f"Posted to {plats}")
    for stage, tiktok_id, err in run.get("failed") or []:
        lines.append(f"{names.get(stage, stage)} failed: {err.strip().splitlines()[0][:140]}")
    lines += [str(a)[:160] for a in run.get("alerts") or []]
    lines += [f"{stage} crashed" for stage, _ in run.get("stage_errors") or []]
    title = "Needs a look" if problems else "Posted"
    return title, "\n".join(lines) or "Run finished"
