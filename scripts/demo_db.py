"""Build a throwaway database with realistic rows so the UI can be reviewed without real data.

    REPURPOSER_DB=/tmp/demo.db python scripts/demo_db.py
"""
from __future__ import annotations

import os
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from repurposer import config, db, scheduler  # noqa: E402
from repurposer.timeutil import iso, utcnow  # noqa: E402

if "REPURPOSER_DB" not in os.environ:
    sys.exit("refusing to seed the real database; set REPURPOSER_DB to a scratch path")

conn = db.open_db()
cfg = config.load_config()
db.seed_from_config(conn, cfg)
now = utcnow()
caps = [
    "What does a Business Analyst actually do all day\n#businessanalyst #careeradvice #ukjobs",
    "Three questions to ask in a BA interview that nobody prepares for #interviewtips #businessanalyst",
    "Day rates for contract BAs in London insurance, honest numbers #contracting #ir35",
    "Requirements workshop gone wrong. What I would do differently #businessanalysis",
    "Paid partnership with Kaplan: the certification route I would pick in 2026 #ad #kaplan",
    "Fit check before the client site, then a word on stakeholder mapping",
    "How I read a process map in under a minute #processmapping #lucid",
    "Jira for BAs: the three boards I keep #jira #agile",
]
rows = []
with db.tx(conn):
    for i, cap in enumerate(caps):
        tid = f"74{i:017d}"
        origin = "new" if i < 4 else "existing"
        published = now - timedelta(days=i * 3 + 1, hours=i)
        db.insert_video(conn, tiktok_id=tid, tiktok_url=f"https://www.tiktok.com/@itsbami_/video/{tid}", tiktok_caption=cap,
                        published_at=iso(published), origin=origin, status="held" if "#ad" in cap else "ready",
                        status_reason="caption contains '#ad'" if "#ad" in cap else None,
                        local_path=None, duration_s=[38, 52, 95, 61, 44, 30, 71, 120][i], width=1080, height=1920)
    # one uploaded, one failed, one ig skipped for length
    db.update_video(conn, rows and rows[0] or "7400000000000000000", yt_status="uploaded", yt_video_id="dQw4w9WgXcQ",
                    yt_published_at=iso(now - timedelta(hours=5)), status="done")
    db.update_video(conn, "7400000000000000001", yt_status="failed", yt_attempts=2, yt_scheduled_for=iso(now - timedelta(hours=1)),
                    yt_error='HttpError 403: {"error": {"errors": [{"domain": "youtube.quota", "reason": "quotaExceeded", "message": "The request cannot be completed because you have exceeded your quota."}]}}')
    db.update_video(conn, "7400000000000000002", ig_status="skipped", ig_error="duration 95s exceeds Instagram API cap of 90s")
    db.save_workflow(conn, "instagram", enabled=1)
    db.upsert_connection(conn, "youtube", account_name="Bami", account_id="UCxxxxxxxx", healthy=1,
                         token_expires_at=iso(now + timedelta(hours=1)))
    db.upsert_connection(conn, "instagram", healthy=0, last_error="InstagramError: not connected: no token. Use Reconnect on the Connections page")
    conn.execute("INSERT INTO runs(started, finished, kind, videos_seen, videos_published, failures, ok, summary, log_path) VALUES (?,?,?,?,?,?,?,?,?)",
                 (iso(now - timedelta(minutes=20)), iso(now - timedelta(minutes=19)), "cycle", 3, 1, 1, 0,
                  '{"published": [["youtube", "7400000000000000000", "https://youtube.com/shorts/dQw4w9WgXcQ"]], "failed": [["youtube", "7400000000000000001", "HttpError 403 quotaExceeded"]], "stage_errors": [], "alerts": ["instagram connection unhealthy: not connected"], "needs_attention": [], "notes": ["poll: 3 seen, 1 new, 0 held"]}',
                  "logs/worker.log"))
scheduler.assign_all(conn)
print("demo rows:", conn.execute("select count(*) from videos").fetchone()[0])
for r in db.rows(conn, "select tiktok_id, status, yt_status, yt_scheduled_for, ig_status, ig_scheduled_for from videos"):
    print(r)
