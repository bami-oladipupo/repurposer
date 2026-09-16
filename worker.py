#!/usr/bin/env python3
"""TikTok Repurposer worker. Run every 15 minutes (launchd/cron) or by hand.

  python worker.py                      full cycle
  python worker.py --dry-run            poll, schedule, download, transform; publish nothing
  python worker.py --status             last 20 videos with both platform statuses
  python worker.py --import-catalogue   one-off: insert the TikTok back catalogue as existing content
  python worker.py --import-catalogue --limit 50   only the newest 50 videos on the profile
  python worker.py --republish ID --platform youtube|instagram
  python worker.py --check-connections  refresh connection health only
  python worker.py --test-notify        send a test message through the configured alert method
  python worker.py --digest             send the weekly digest (launchd runs this Monday 08:00)
  python worker.py --job N              internal: run the job the web UI raised (Publish Now / Run Now)

Every stage is wrapped so one failure never hides another. A runs row is written whether or not
anything succeeded. Exit code is non-zero if any stage failed.
"""
from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
import traceback
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from repurposer import actions, config, db, digest, downloader, logsetup, notify, poller, rewrite, scheduler, transform  # noqa: E402
from repurposer import overrides as ov  # noqa: E402
from repurposer.config import PLATFORMS, PREFIX  # noqa: E402
from repurposer.publishers import module_for, publish_due  # noqa: E402
from repurposer.timeutil import fmt_local, iso, utcnow  # noqa: E402

log = logging.getLogger("repurposer.worker")
PLATFORM_ALIASES = {"yt": "youtube", "youtube": "youtube", "ig": "instagram", "instagram": "instagram"}


class Run:
    """Accumulates everything the notifier and the runs row need."""

    def __init__(self, conn: sqlite3.Connection, kind: str, log_path: Path | None) -> None:
        self.conn = conn
        self.kind = kind
        self.started = utcnow()
        self.stage_errors: list[tuple[str, str]] = []
        self.published: list[tuple[str, str, str | None]] = []
        self.failed: list[tuple[str, str, str]] = []
        self.held: list[tuple[str, str]] = []
        self.rolled: list[tuple[str, str, str]] = []
        self.alerts: list[str] = []
        self.needs_attention: list[tuple[str, str, str]] = []
        self.videos_seen = 0
        self.notes: list[str] = []
        with db.tx(conn):
            cur = conn.execute("INSERT INTO runs(started, kind, log_path) VALUES (?,?,?)",
                               (iso(self.started), kind, str(log_path) if log_path else None))
            self.run_id = int(cur.lastrowid)

    def stage(self, name: str, fn, *args, **kwargs):
        """Run one stage; record an exception instead of letting it stop the cycle."""
        try:
            return fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001
            log.exception("stage %s failed", name)
            self.stage_errors.append((name, f"{type(exc).__name__}: {exc}\n{traceback.format_exc()[-1500:]}"))
            return None

    @property
    def ok(self) -> bool:
        return not self.stage_errors and not self.failed and not self.alerts

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id, "kind": self.kind, "videos_seen": self.videos_seen,
            "published": self.published, "failed": self.failed, "held": self.held, "rolled": self.rolled,
            "alerts": self.alerts, "needs_attention": self.needs_attention, "stage_errors": self.stage_errors,
            "notes": self.notes,
        }

    def finish(self) -> None:
        with db.tx(self.conn):
            self.conn.execute(
                "UPDATE runs SET finished=?, videos_seen=?, videos_published=?, failures=?, ok=?, summary=? WHERE run_id=?",
                (iso(utcnow()), self.videos_seen, len(self.published),
                 len(self.failed) + len(self.stage_errors) + len(self.alerts), int(self.ok),
                 json.dumps(self.as_dict()), self.run_id),
            )


# ---------- stages ----------

def stage_overrides(conn: sqlite3.Connection, cfg: dict[str, Any], run: Run) -> dict[str, dict[str, Any]]:
    overrides = ov.load_overrides(cfg)
    with db.tx(conn):
        for tiktok_id, entry in overrides.items():
            v = db.get_video(conn, tiktok_id)
            if v is None:
                continue
            action = entry.get("action")
            if action == "hold" and v["status"] not in {"held", "done"}:
                actions.hold(conn, tiktok_id, "held by overrides.yaml")
                run.held.append((tiktok_id, "overrides.yaml"))
            elif action == "skip" and v["status"] != "skipped":
                actions.skip(conn, tiktok_id, "skipped by overrides.yaml")
            elif action == "publish" and v["status"] == "held" and (v.get("status_reason") or "").endswith("overrides.yaml"):
                actions.release(conn, tiktok_id)
    return overrides


def stage_connections(conn: sqlite3.Connection, cfg: dict[str, Any], run: Run) -> dict[str, dict[str, Any]]:
    results = {}
    for plat in PLATFORMS:
        wf = db.get_workflow(conn, plat)
        if wf is None or not wf["enabled"]:
            continue
        res = module_for(plat).check_connection(conn, cfg)
        results[plat] = res
        if not res["healthy"]:
            run.alerts.append(f"{plat} connection unhealthy: {res['error']}")
    return results


def stage_poll(conn: sqlite3.Connection, cfg: dict[str, Any], run: Run) -> None:
    stats = poller.poll(conn, cfg)
    run.videos_seen = stats["seen"]
    run.notes.append(f"poll: {stats['seen']} seen, {stats['inserted']} new, {stats['held']} held")
    for v in db.rows(conn, "SELECT tiktok_id, status_reason FROM videos WHERE status='held' AND first_seen >= ?",
                     (iso(run.started),)):
        run.held.append((v["tiktok_id"], v["status_reason"] or "held"))


def stage_schedule(conn: sqlite3.Connection, run: Run) -> None:
    assigned = scheduler.assign_all(conn)
    n = sum(len(v) for v in assigned.values())
    if n:
        run.notes.append("scheduled: " + ", ".join(f"{p} {len(v)}" for p, v in assigned.items() if v))


def stage_download(conn: sqlite3.Connection, cfg: dict[str, Any], run: Run) -> None:
    stats = downloader.run(conn, cfg)
    run.notes.append(f"download: {stats['downloaded']} ok, {stats['gone']} gone, {len(stats['failed'])} failed")
    for tiktok_id, err in stats["failed"]:
        run.failed.append(("download", tiktok_id, err))
    if stats.get("disk_alert"):
        run.alerts.append(stats["disk_alert"])


def stage_transform(conn: sqlite3.Connection, cfg: dict[str, Any], run: Run) -> None:
    limits = cfg.get("limits", {})
    n = 0
    for v in db.rows(conn, "SELECT * FROM videos WHERE status = 'downloaded'"):
        try:
            with db.tx(conn):
                result = transform.process(conn, v, limits)
            n += 1
            if result == "skipped":
                fresh = db.get_video(conn, v["tiktok_id"]) or {}
                run.held.append((v["tiktok_id"], f"skipped: {fresh.get('status_reason')}"))
        except Exception as exc:  # noqa: BLE001
            log.exception("transform failed for %s", v["tiktok_id"])
            with db.tx(conn):
                db.update_video(conn, v["tiktok_id"], status_reason=f"transform: {exc}"[:1000])
            run.failed.append(("transform", v["tiktok_id"], f"{type(exc).__name__}: {exc}"))
    if n:
        run.notes.append(f"transform: {n} processed")


def stage_rewrite(conn: sqlite3.Connection, cfg: dict[str, Any], run: Run) -> None:
    stats = rewrite.run(conn, cfg)
    if not stats["platforms"]:
        return
    if stats["rewritten"] or stats["failed"]:
        run.notes.append(f"rewrite: {stats['rewritten']} rewritten, {len(stats['failed'])} failed")
    for tiktok_id, err in stats["failed"]:
        run.failed.append(("rewrite", tiktok_id, err))
    if stats["alert"]:
        run.alerts.append(stats["alert"])


def stage_publish(conn: sqlite3.Connection, cfg: dict[str, Any], run: Run, overrides: dict[str, dict[str, Any]],
                  *, only: str | None = None, only_platform: str | None = None, force_manual: bool = False,
                  dry_run: bool = False) -> None:
    for plat in PLATFORMS:
        if only_platform and plat != only_platform:
            continue
        res = run.stage(f"publish:{plat}", publish_due, conn, cfg, plat, overrides, only=only,
                        force_manual=force_manual, dry_run=dry_run)
        if res is None:
            continue
        for tiktok_id, link in res["published"]:
            run.published.append((plat, tiktok_id, link))
        for tiktok_id, err in res["failed"]:
            run.failed.append((plat, tiktok_id, err))
        for tiktok_id in res["rolled"]:
            run.rolled.append((plat, tiktok_id, res["note"] or "quota"))
        for tiktok_id, why in res["skipped"]:
            run.held.append((tiktok_id, why))
        if res["note"]:
            run.notes.append(f"{plat}: {res['note']}")


def stage_cleanup(conn: sqlite3.Connection, cfg: dict[str, Any], run: Run) -> None:
    stats = downloader.cleanup(conn, cfg)
    if stats["removed"]:
        run.notes.append(f"cleanup: {stats['removed']} file(s) removed, {stats['freed_mb']:.0f} MB freed")
    for tiktok_id, err in stats["failed"]:
        run.failed.append(("cleanup", tiktok_id, err))


def stage_attention(conn: sqlite3.Connection, cfg: dict[str, Any], run: Run) -> None:
    retry_runs = int(cfg.get("limits", {}).get("retry_runs", 3))
    for plat in PLATFORMS:
        px = PREFIX[plat]
        for v in db.rows(conn, f"SELECT tiktok_id, {px}_error AS err FROM videos WHERE {px}_status='failed' AND {px}_attempts > ?",
                         (retry_runs,)):
            run.needs_attention.append((plat, v["tiktok_id"], v["err"] or ""))


def stage_notify(cfg: dict[str, Any], run: Run) -> None:
    text, must_send = notify.build_summary(run.as_dict())
    quiet = bool((cfg.get("notify") or {}).get("quiet_when_idle", True))
    if must_send or not quiet:
        notify.send(cfg, text)
    else:
        log.info("nothing to report; no notification sent")


# ---------- commands ----------

def cmd_cycle(conn: sqlite3.Connection, cfg: dict[str, Any], log_path: Path, *, dry_run: bool = False) -> int:
    run = Run(conn, "dry-run" if dry_run else "cycle", log_path)
    overrides = run.stage("overrides", stage_overrides, conn, cfg, run) or {}
    run.stage("connections", stage_connections, conn, cfg, run)
    run.stage("poll", stage_poll, conn, cfg, run)
    run.stage("schedule", stage_schedule, conn, run)
    run.stage("download", stage_download, conn, cfg, run)
    run.stage("transform", stage_transform, conn, cfg, run)
    run.stage("rewrite", stage_rewrite, conn, cfg, run)
    stage_publish(conn, cfg, run, overrides, dry_run=dry_run)
    run.stage("cleanup", stage_cleanup, conn, cfg, run)
    run.stage("attention", stage_attention, conn, cfg, run)
    run.stage("notify", stage_notify, cfg, run)
    run.finish()
    print_summary(run)
    return 0 if run.ok else 1


def cmd_job(conn: sqlite3.Connection, cfg: dict[str, Any], log_path: Path, job_id: int) -> int:
    job = db.one(conn, "SELECT * FROM jobs WHERE id = ?", (job_id,))
    if job is None:
        print(f"no job {job_id}", file=sys.stderr)
        return 2
    with db.tx(conn):
        conn.execute("UPDATE jobs SET started_at = ? WHERE id = ?", (iso(utcnow()), job_id))
    code = 1
    err = None
    try:
        if job["kind"] == "publish_now":
            code = cmd_single(conn, cfg, log_path, job["tiktok_id"], job["platform"], kind="publish-now")
        elif job["kind"] == "run_now":
            code = cmd_cycle(conn, cfg, log_path)
        elif job["kind"] == "import":
            code = cmd_import(conn, cfg, log_path, limit=int(job["arg"]) if job.get("arg") else None)
        else:
            err = f"unknown job kind {job['kind']}"
    except Exception as exc:  # noqa: BLE001
        err = f"{type(exc).__name__}: {exc}"
        log.exception("job %s crashed", job_id)
    with db.tx(conn):
        conn.execute("UPDATE jobs SET finished_at = ?, ok = ?, error = ? WHERE id = ?",
                     (iso(utcnow()), int(code == 0 and err is None), err, job_id))
    return code if err is None else 1


def cmd_single(conn: sqlite3.Connection, cfg: dict[str, Any], log_path: Path, tiktok_id: str, platform: str,
               *, kind: str = "republish") -> int:
    """Download, transform and publish one video on one platform, ignoring Manual mode."""
    platform = PLATFORM_ALIASES.get(platform, platform)
    v = db.get_video(conn, tiktok_id)
    if v is None:
        print(f"unknown video {tiktok_id}", file=sys.stderr)
        return 2
    run = Run(conn, kind, log_path)
    overrides = run.stage("overrides", stage_overrides, conn, cfg, run) or {}
    px = PREFIX[platform]
    with db.tx(conn):
        if kind == "republish":
            actions.requeue(conn, tiktok_id, platform)
        actions.schedule(conn, tiktok_id, platform, iso(utcnow()))
        v = db.get_video(conn, tiktok_id) or v
        if v["status"] == "held":
            actions.release(conn, tiktok_id)
    v = db.get_video(conn, tiktok_id) or v
    if v["status"] == "new" or not v.get("local_path"):
        def _dl():
            path = downloader.download(v)
            with db.tx(conn):
                db.update_video(conn, tiktok_id, local_path=str(path), status="downloaded", status_reason=None)
        run.stage("download", _dl)
    v = db.get_video(conn, tiktok_id) or v
    if v["status"] == "downloaded":
        def _tf():
            with db.tx(conn):
                transform.process(conn, v, cfg.get("limits", {}))
        run.stage("transform", _tf)
    v = db.get_video(conn, tiktok_id) or v
    if v["status"] == "ready" and platform in rewrite.enabled_platforms(conn) and v.get("rewrite_status") is None:
        def _rw():
            with db.tx(conn):
                res = rewrite.rewrite_video(conn, v, cfg, platforms=[platform])
            if res["error"]:
                run.alerts.append(f"rewrite failed for {tiktok_id}, template used: {res['error']}")
        run.stage("rewrite", _rw)
        v = db.get_video(conn, tiktok_id) or v
    if v["status"] != "ready":
        run.alerts.append(f"{tiktok_id} is {v['status']} ({v.get('status_reason') or 'no reason'}); not published")
    elif v[f"{px}_status"] == "skipped":
        run.alerts.append(f"{tiktok_id} is skipped on {platform}: {v.get(f'{px}_error')}")
    else:
        stage_publish(conn, cfg, run, overrides, only=tiktok_id, only_platform=platform, force_manual=True)
    run.stage("notify", stage_notify, cfg, run)
    run.finish()
    print_summary(run)
    return 0 if run.ok else 1


def cmd_import(conn: sqlite3.Connection, cfg: dict[str, Any], log_path: Path, limit: int | None = None) -> int:
    run = Run(conn, "import", log_path)
    if limit:
        print(f"Importing the newest {limit} TikToks from the profile (metadata only).")
    else:
        print("Importing the full TikTok catalogue (metadata only). This can take several minutes.")
    stats = run.stage("import", poller.import_catalogue, conn, cfg, limit=limit, progress=print)
    if stats:
        run.videos_seen = stats["seen"]
        scope = f"newest {limit}" if limit else "full catalogue"
        run.notes.append(f"import ({scope}): {stats['seen']} listed, {stats['inserted']} inserted, {stats['held']} held (sponsored)")
        print(f"Done: {stats['seen']} listed, {stats['inserted']} inserted, {stats['held']} held as sponsored.")
    run.finish()
    return 0 if run.ok else 1


def cmd_check(conn: sqlite3.Connection, cfg: dict[str, Any], log_path: Path) -> int:
    run = Run(conn, "check", log_path)
    res = run.stage("connections", stage_connections, conn, cfg, run) or {}
    for plat, r in res.items():
        print(f"{plat:10s} {'healthy' if r['healthy'] else 'UNHEALTHY'}  {r.get('account_name') or ''}  {r.get('error') or ''}")
    run.finish()
    return 0 if run.ok else 1


def cmd_status(conn: sqlite3.Connection, limit: int = 20) -> int:
    tz = db.timezone(conn)
    rows = db.rows(conn, "SELECT * FROM videos ORDER BY COALESCE(published_at, first_seen) DESC LIMIT ?", (limit,))
    if not rows:
        print("no videos yet; run a cycle or --import-catalogue")
        return 0
    print(f"{'tiktok_id':20s} {'posted':16s} {'origin':8s} {'status':10s} {'youtube':10s} {'yt slot':16s} {'instagram':10s} {'ig slot':16s} caption")
    for r in rows:
        print(f"{r['tiktok_id']:20s} {fmt_local(r['published_at'], tz):16s} {r['origin']:8s} {r['status']:10s} "
              f"{r['yt_status']:10s} {fmt_local(r['yt_scheduled_for'], tz):16s} "
              f"{r['ig_status']:10s} {fmt_local(r['ig_scheduled_for'], tz):16s} {(r['tiktok_caption'] or '')[:40]}")
        for label, err in (("yt", r["yt_error"]), ("ig", r["ig_error"]), ("video", r["status_reason"])):
            if err:
                print(f"{'':20s}   {label}: {err.splitlines()[0][:110]}")
    return 0


def print_summary(run: Run) -> None:
    text, _ = notify.build_summary(run.as_dict())
    print(text)
    for n in run.notes:
        print(f"  note: {n}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--import-catalogue", action="store_true")
    ap.add_argument("--limit", type=int, metavar="N", help="with --import-catalogue: only the newest N videos")
    ap.add_argument("--check-connections", action="store_true")
    ap.add_argument("--test-notify", action="store_true")
    ap.add_argument("--digest", action="store_true")
    ap.add_argument("--republish", metavar="TIKTOK_ID")
    ap.add_argument("--platform", choices=sorted(PLATFORM_ALIASES))
    ap.add_argument("--job", type=int, metavar="JOB_ID")
    args = ap.parse_args(argv)

    config.ensure_dirs()
    log_path = logsetup.setup("worker")
    conn = db.open_db()
    try:
        cfg = config.load_config()
    except config.ConfigError as exc:
        log.error("%s", exc)
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    db.seed_from_config(conn, cfg)

    if args.status:
        return cmd_status(conn)
    if args.digest:
        text = digest.build_digest(conn, cfg)
        try:
            notify.send(cfg, text)
        except notify.NotifyError as exc:
            print(text)
            print(f"notify failed: {exc}", file=sys.stderr)
            return 1
        print("digest sent")
        return 0
    if args.test_notify:
        try:
            notify.send(cfg, "Repurposer test: alerts are working. Every run with a publish or a failure will arrive here.")
        except notify.NotifyError as exc:
            print(f"notify failed: {exc}", file=sys.stderr)
            return 1
        print("sent")
        return 0
    if args.check_connections:
        return cmd_check(conn, cfg, log_path)
    if args.import_catalogue:
        if args.limit is not None and args.limit < 1:
            ap.error("--limit must be at least 1")
        return cmd_import(conn, cfg, log_path, limit=args.limit)
    if args.republish:
        if not args.platform:
            ap.error("--republish needs --platform")
        return cmd_single(conn, cfg, log_path, args.republish, args.platform)
    if args.job:
        return cmd_job(conn, cfg, log_path, args.job)
    return cmd_cycle(conn, cfg, log_path, dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
