from datetime import timedelta

from repurposer import db, scheduler
from repurposer.timeutil import iso, utcnow


def test_existing_limit_keeps_only_newest_n(tmp_db, make_video):
    conn = tmp_db
    now = utcnow()
    for i in range(6):
        make_video(conn, f"e{i}", origin="existing", status="new", published_at=iso(now - timedelta(days=30 + i)))
    with db.tx(conn):
        db.save_workflow(conn, "youtube", content_scope="new_and_existing", existing_start_from=None, existing_limit=3,
                         existing_order="newest_first")
    assigned = scheduler.assign(conn, "youtube", now=now)
    ids = [a[0] for a in assigned]
    assert set(ids) == {"e0", "e1", "e2"}          # newest three only
    assert ids == ["e0", "e1", "e2"]               # newest first
    for old in ("e3", "e4", "e5"):
        assert db.get_video(conn, old)["yt_status"] == "queued" and db.get_video(conn, old)["yt_scheduled_for"] is None
    # a second pass must not creep further down the catalogue
    assert scheduler.assign(conn, "youtube", now=now) == []
    # once one of the newest is uploaded it still counts towards the cap
    with db.tx(conn):
        db.update_video(conn, "e0", yt_status="uploaded", yt_scheduled_for=None)
    assert scheduler.assign(conn, "youtube", now=now) == []


def test_existing_limit_ignores_held_and_skipped(tmp_db, make_video):
    conn = tmp_db
    now = utcnow()
    make_video(conn, "held", origin="existing", status="held", published_at=iso(now - timedelta(days=1)))
    make_video(conn, "photo", origin="existing", status="skipped", published_at=iso(now - timedelta(days=2)))
    make_video(conn, "ok1", origin="existing", status="new", published_at=iso(now - timedelta(days=3)))
    make_video(conn, "ok2", origin="existing", status="new", published_at=iso(now - timedelta(days=4)))
    with db.tx(conn):
        db.save_workflow(conn, "youtube", content_scope="new_and_existing", existing_start_from=None, existing_limit=2)
    assert [a[0] for a in scheduler.assign(conn, "youtube", now=now)] == ["ok1", "ok2"]


def test_existing_limit_respects_oldest_first_order(tmp_db, make_video):
    conn = tmp_db
    now = utcnow()
    for i in range(4):
        make_video(conn, f"e{i}", origin="existing", status="new", published_at=iso(now - timedelta(days=30 + i)))
    with db.tx(conn):
        db.save_workflow(conn, "youtube", content_scope="new_and_existing", existing_start_from=None, existing_limit=2,
                         existing_order="oldest_first")
    assigned = scheduler.assign(conn, "youtube", now=now)
    assert [a[0] for a in assigned] == ["e1", "e0"]  # the newest two, oldest of those first
