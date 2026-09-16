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
from typing import Any

from .. import actions, db, overrides as ov, scheduler
from ..config import PREFIX
from ..timeutil import utcnow

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


def _defer(conn: sqlite3.Connection, platform: str, rows: list[dict[str, Any]], why: str,
           out: dict[str, Any], now) -> None:
    """Move the given scheduled rows to their next free slot and record it in the result."""
    ids = [r["tiktok_id"] for r in rows if r[f"{PREFIX[platform]}_status"] == "scheduled"]
    if not ids:
        return
    with db.tx(conn):
        moved = scheduler.defer(conn, platform, ids, now=now)
    for tiktok_id in ids:
        out["deferred"].append((tiktok_id, why, moved.get(tiktok_id)))
        log.warning("%s deferred %s (%s) -> %s", platform, tiktok_id, why, moved.get(tiktok_id) or "no free slot yet")


def publish_due(conn: sqlite3.Connection, cfg: dict[str, Any], platform: str, overrides: dict[str, dict[str, Any]],
                *, only: str | None = None, force_manual: bool = False, dry_run: bool = False) -> dict[str, Any]:
    """Publish what is due on one platform, within the burst limits. `only` restricts to one tiktok_id (Publish Now).

    Burst limits (config limits.*), applied to automatic runs only; Publish Now bypasses them:
      slot_grace_minutes   a slot older than this is stale: deferred to the next free slot, never published late
      max_publish_per_run  attempts per platform per worker run (default 1)
      max_publish_per_day  uploads per platform per local day (default: that weekday's slot count)
    Deferred videos are listed in out['deferred'] as (tiktok_id, why, new_slot_or_None).
    """
    out: dict[str, Any] = {"published": [], "failed": [], "skipped": [], "exhausted": [], "rolled": [], "deferred": [],
                           "note": None}
    wf = db.get_workflow(conn, platform)
    if wf is None or not wf["enabled"]:
        out["note"] = "workflow disabled"
        return out
    if not wf["auto_publish"] and not force_manual:
        out["note"] = "manual mode, nothing published"
        return out
    limits = cfg.get("limits", {}) or {}
    retry_runs = int(limits.get("retry_runs", 3))
    grace = int(limits.get("slot_grace_minutes", 90))
    per_run = max(1, int(limits.get("max_publish_per_run", 1)))
    now = utcnow()
    px = PREFIX[platform]
    notes: list[str] = []

    if not only:
        stale = scheduler.stale_scheduled(conn, platform, grace, now)
        if stale and not dry_run:
            _defer(conn, platform, stale, f"slot missed by more than {grace} min", out, now)
            notes.append(f"{len(stale)} missed slot(s) moved to the next free slot")
        elif stale:
            notes.append(f"{len(stale)} missed slot(s) would move to the next free slot")

    rows = scheduler.due(conn, platform, now=now, retry_runs=retry_runs)
    if only:
        rows = [r for r in rows if r["tiktok_id"] == only]
    if not rows:
        out["note"] = "; ".join(notes) or None
        return out
    if dry_run:
        notes.append(f"dry run: {len(rows)} due, none published")
        out["note"] = "; ".join(notes)
        return out
    mod = module_for(platform)
    ok, why = mod.quota_ok(conn, cfg)
    if not ok:
        # Every due slot moves to the next free slot; it is not stacked onto tomorrow's slot.
        for r in rows:
            out["rolled"].append(r["tiktok_id"])
        _defer(conn, platform, rows, "quota", out, now)
        notes.append(f"quota: {why}; {len(rows)} slot(s) moved to the next free slot")
        out["note"] = "; ".join(notes)
        log.warning("%s %s", platform, out["note"])
        return out

    budget = len(rows)
    if not only:
        tz = db.timezone(conn)
        cap = scheduler.day_cap(conn, platform, limits, tz, now)
        done_today = scheduler.uploads_today(conn, platform, tz, now)
        budget = min(per_run, max(0, cap - done_today))
        if budget == 0:
            notes.append(f"daily limit reached ({done_today}/{cap} uploaded today)")

    attempts = 0
    for r in rows:
        if attempts >= budget:
            reason = "daily limit reached" if budget == 0 else f"limit of {per_run} per run"
            _defer(conn, platform, [r], reason, out, now)
            continue
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
        if fresh[f"{px}_status"] == "uploaded":
            continue
        attempts += 1
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
    out["note"] = "; ".join(notes) or None
    return out
