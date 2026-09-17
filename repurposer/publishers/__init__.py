"""Publisher interface shared by youtube.py and instagram.py.

Each module exposes:
  check_connection(conn, cfg) -> dict   updates the connections table, returns {'healthy', 'error', 'account_name'}
  quota_ok(conn, cfg) -> (bool, str)    whether we may publish this run, and why not
  publish(conn, video, workflow, override, cfg) -> PublishResult
"""
from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from .. import actions, db, overrides as ov, scheduler
from ..config import PREFIX
from ..timeutil import iso, parse, utcnow

log = logging.getLogger("repurposer.publish")


@dataclass
class PublishResult:
    ok: bool
    message: str
    url: str | None = None


def module_for(platform: str):
    if platform == "youtube":
        from . import youtube
        return youtube
    if platform == "instagram":
        from . import instagram
        return instagram
    raise ValueError(platform)


def _throttle(conn: sqlite3.Connection, cfg: dict[str, Any], platform: str, rows: list[dict[str, Any]],
              out: dict[str, Any]) -> list[dict[str, Any]]:
    """Burst guard. When the worker has not run for a while (Mac asleep, laptop shut) several slots
    fall due at once. Slots older than limits.max_slot_lag_minutes go back to the queue for a fresh
    future slot, and at most limits.max_publish_per_run videos publish per run; the rest are requeued.
    Nothing is dropped: every requeued video is reassigned by the scheduler on the next run."""
    limits = cfg.get("limits", {})
    lag = int(limits.get("max_slot_lag_minutes", 120))
    cap = int(limits.get("max_publish_per_run", 1))
    px = PREFIX[platform]
    now = utcnow()
    cutoff = now - timedelta(minutes=lag)
    stale = [r for r in rows if (parse(r[f"{px}_scheduled_for"]) or now) < cutoff]
    fresh = [r for r in rows if r not in stale]
    over = fresh[cap:] if cap > 0 else []
    fresh = fresh[:cap] if cap > 0 else fresh
    notes = []
    if stale:
        notes.append(f"{len(stale)} slot(s) more than {lag} min overdue requeued for a fresh slot")
    if over:
        notes.append(f"{len(over)} due beyond the {cap} per run cap requeued")
    if stale or over:
        with db.tx(conn):
            for r in stale + over:
                actions.requeue(conn, r["tiktok_id"], platform)
                out["rolled"].append(r["tiktok_id"])
        out["note"] = "burst guard: " + "; ".join(notes)
        log.warning("%s %s", platform, out["note"])
    return fresh


def publish_due(conn: sqlite3.Connection, cfg: dict[str, Any], platform: str, overrides: dict[str, dict[str, Any]],
                *, only: str | None = None, force_manual: bool = False, dry_run: bool = False) -> dict[str, Any]:
    """Publish everything due on one platform. `only` restricts to a single tiktok_id (Publish Now)."""
    out: dict[str, Any] = {"published": [], "failed": [], "skipped": [], "exhausted": [], "rolled": [], "note": None}
    wf = db.get_workflow(conn, platform)
    if wf is None or not wf["enabled"]:
        out["note"] = "workflow disabled"
        return out
    if not wf["auto_publish"] and not force_manual:
        out["note"] = "manual mode, nothing published"
        return out
    retry_runs = int(cfg.get("limits", {}).get("retry_runs", 3))
    rows = scheduler.due(conn, platform, retry_runs=retry_runs)
    if only:
        rows = [r for r in rows if r["tiktok_id"] == only]
    if not rows:
        return out
    if dry_run:
        out["note"] = f"dry run: {len(rows)} due, none published"
        return out
    mod = module_for(platform)
    ok, why = mod.quota_ok(conn, cfg)
    if not ok:
        # Roll every due slot to the same time tomorrow and say why in the summary.
        px = PREFIX[platform]
        with db.tx(conn):
            for r in rows:
                when = parse(r[f"{px}_scheduled_for"]) or utcnow()
                db.update_video(conn, r["tiktok_id"], **{f"{px}_scheduled_for": iso(when + timedelta(days=1))})
                out["rolled"].append(r["tiktok_id"])
        out["note"] = f"quota: {why}; {len(rows)} slot(s) rolled to tomorrow"
        log.warning("%s %s", platform, out["note"])
        return out
    if not only:
        rows = _throttle(conn, cfg, platform, rows, out)
    for r in rows:
        override = ov.override_for(overrides, r["tiktok_id"])
        action = override.get("action")
        if action == "hold":
            with db.tx(conn):
                actions.hold(conn, r["tiktok_id"], "held by overrides.yaml")
            out["skipped"].append((r["tiktok_id"], "held by overrides.yaml"))
            continue
        if action == "skip":
            with db.tx(conn):
                actions.skip(conn, r["tiktok_id"], "skipped by overrides.yaml")
            out["skipped"].append((r["tiktok_id"], "skipped by overrides.yaml"))
            continue
        # Idempotency guard: re-read the row inside the run in case another process moved it.
        fresh = db.get_video(conn, r["tiktok_id"]) or r
        if fresh[f"{PREFIX[platform]}_status"] == "uploaded":
            continue
        try:
            result = mod.publish(conn, fresh, wf, override, cfg)
        except Exception as exc:  # noqa: BLE001 - every failure is recorded, never swallowed
            log.exception("%s publish crashed for %s", platform, r["tiktok_id"])
            result = PublishResult(False, f"{type(exc).__name__}: {exc}")
        if result.ok:
            out["published"].append((r["tiktok_id"], result.url or result.message))
            log.info("%s published %s -> %s", platform, r["tiktok_id"], result.url)
        else:
            with db.tx(conn):
                exhausted = actions.mark_failed(conn, r["tiktok_id"], platform, result.message, retry_runs)
            out["failed"].append((r["tiktok_id"], result.message))
            if exhausted:
                out["exhausted"].append(r["tiktok_id"])
            log.error("%s failed %s: %s", platform, r["tiktok_id"], result.message[:300])
    return out
