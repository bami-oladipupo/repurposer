from datetime import timedelta

from repurposer import db, digest
from repurposer.timeutil import iso, utcnow


def test_digest_lists_published_scheduled_failed_held_and_connections(tmp_db, make_video, cfg):
    conn = tmp_db
    now = utcnow()
    make_video(conn, "pub", caption="Published one")
    make_video(conn, "sched", caption="Scheduled one")
    make_video(conn, "fail", caption="Failed one")
    make_video(conn, "held", caption="Sponsored #ad", status="held")
    with db.tx(conn):
        db.update_video(conn, "pub", yt_status="uploaded", yt_video_id="abc", yt_published_at=iso(now - timedelta(days=1)))
        db.update_video(conn, "sched", yt_status="scheduled", yt_scheduled_for=iso(now + timedelta(days=2)))
        db.update_video(conn, "fail", yt_status="failed", yt_error="HttpError 403 quota", yt_attempts=5)
        db.update_video(conn, "held", status_reason="caption contains '#ad'")
        db.update_video(conn, "sched", rewrite_status="failed", rewrite_error="rate limit")
        db.upsert_connection(conn, "youtube", account_name="Bami", healthy=1, token_expires_at=iso(now + timedelta(days=30)))
    text = digest.build_digest(conn, cfg)
    assert "Published last 7 days: 1" in text and "youtube.com/shorts/abc" in text
    assert "Scheduled next 7 days: 1" in text and "Scheduled one" in text
    assert "needs manual attention" in text and "HttpError 403 quota" in text
    assert "Held (1)" in text and "#ad" in text
    assert "Caption rewrite failed (1)" in text
    assert "YouTube Shorts: healthy" in text and "days left" in text
    assert "Instagram" not in text.split("Connections")[1]  # disabled workflow not listed


def test_digest_marks_manual_mode_and_unhealthy(tmp_db, cfg):
    conn = tmp_db
    with db.tx(conn):
        db.save_workflow(conn, "youtube", auto_publish=0)
    text = digest.build_digest(conn, cfg)
    assert "Manual (paused)" in text and "UNHEALTHY" in text
