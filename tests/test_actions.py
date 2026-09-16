from datetime import datetime, timedelta, timezone

import pytest

from repurposer import actions, db, scheduler
from repurposer.timeutil import iso
from conftest import add_video

UTC = timezone.utc
NOW = datetime(2026, 9, 10, 8, 0, tzinfo=UTC)


def test_hold_releases_scheduled_slots_and_blocks(tmp_db):
    add_video(tmp_db, "v", status="ready", yt_status="scheduled", yt_scheduled_for=iso(NOW),
              ig_status="scheduled", ig_scheduled_for=iso(NOW))
    actions.hold(tmp_db, "v", "brand deal")
    row = db.get_video(tmp_db, "v")
    assert row["status"] == "held" and row["status_reason"] == "brand deal"
    assert row["yt_status"] == "queued" and row["yt_scheduled_for"] is None
    assert row["ig_status"] == "queued" and row["ig_scheduled_for"] is None
    assert scheduler.due(tmp_db, "youtube", now=NOW + timedelta(hours=1)) == []
    assert scheduler.assign(tmp_db, "youtube", now=NOW) == []  # held is not a candidate


def test_hold_leaves_uploaded_platform_alone(tmp_db):
    add_video(tmp_db, "v", status="ready", yt_status="uploaded", ig_status="scheduled", ig_scheduled_for=iso(NOW))
    actions.hold(tmp_db, "v")
    row = db.get_video(tmp_db, "v")
    assert row["yt_status"] == "uploaded" and row["ig_status"] == "queued"


@pytest.mark.parametrize("local_path,duration,expected", [
    ("f.mp4", 30, "ready"), ("f.mp4", None, "downloaded"), (None, None, "new"),
])
def test_release_returns_to_stage_status(tmp_db, local_path, duration, expected):
    add_video(tmp_db, "v", status="held", local_path=local_path, duration_s=duration, status_reason="x")
    actions.release(tmp_db, "v")
    row = db.get_video(tmp_db, "v")
    assert row["status"] == expected and row["status_reason"] is None


def test_release_is_noop_when_not_held(tmp_db):
    add_video(tmp_db, "v", status="skipped", status_reason="too long")
    actions.release(tmp_db, "v")
    actions.release(tmp_db, "missing")
    assert db.get_video(tmp_db, "v")["status"] == "skipped"


def test_cancel_then_requeue_back_to_queue_without_slot(tmp_db):
    """Criterion 13: re-adding puts the video back in the queue for the next free slot."""
    add_video(tmp_db, "v", status="ready", yt_status="scheduled", yt_scheduled_for=iso(NOW + timedelta(hours=1)),
              yt_error="old error", yt_attempts=2, published_at=NOW - timedelta(hours=3))
    actions.cancel(tmp_db, "v", "youtube")
    row = db.get_video(tmp_db, "v")
    assert row["yt_status"] == "cancelled" and row["yt_scheduled_for"] is None
    assert row["status"] == "ready"  # cancelled is terminal; nothing uploaded so the video is not 'done'
    actions.requeue(tmp_db, "v", "youtube")
    row = db.get_video(tmp_db, "v")
    assert row["yt_status"] == "queued" and row["yt_scheduled_for"] is None
    assert row["yt_error"] is None and row["yt_attempts"] == 0
    # The scheduler hands it the earliest free slot on the next run.
    add_video(tmp_db, "other", status="ready", yt_status="scheduled", yt_scheduled_for=iso(datetime(2026, 9, 10, 9, 0, tzinfo=UTC)))
    assigned = scheduler.assign(tmp_db, "youtube", now=NOW)
    assert assigned == [("v", iso(datetime(2026, 9, 10, 17, 0, tzinfo=UTC)))]


def test_cancel_and_requeue_ignore_uploaded(tmp_db):
    add_video(tmp_db, "v", status="done", yt_status="uploaded", yt_video_id="abc")
    actions.cancel(tmp_db, "v", "youtube")
    actions.requeue(tmp_db, "v", "youtube")
    assert db.get_video(tmp_db, "v")["yt_status"] == "uploaded"


def test_requeue_from_failed_video_restores_stage(tmp_db):
    add_video(tmp_db, "v", status="failed", status_reason="publish failed after retries", yt_status="failed", yt_attempts=4)
    actions.requeue(tmp_db, "v", "youtube")
    row = db.get_video(tmp_db, "v")
    assert row["status"] == "new" and row["status_reason"] is None and row["yt_status"] == "queued"


def test_publish_now_schedules_now_and_inserts_job(tmp_db, monkeypatch):
    monkeypatch.setattr(actions, "utcnow", lambda: NOW)
    add_video(tmp_db, "v", status="held", status_reason="x", local_path="f.mp4", yt_status="queued", yt_attempts=2, yt_error="e")
    job_id = actions.publish_now(tmp_db, "v", "youtube")
    row = db.get_video(tmp_db, "v")
    assert row["yt_status"] == "scheduled" and row["yt_scheduled_for"] == iso(NOW)
    assert row["yt_attempts"] == 0 and row["yt_error"] is None
    assert row["status"] == "ready" and row["status_reason"] is None  # hold released
    job = db.one(tmp_db, "SELECT * FROM jobs WHERE id = ?", (job_id,))
    assert job["kind"] == "publish_now" and job["tiktok_id"] == "v" and job["platform"] == "youtube"
    assert job["created_at"] == iso(NOW) and job["started_at"] is None


def test_publish_now_rejects_unknown_and_uploaded(tmp_db):
    with pytest.raises(ValueError):
        actions.publish_now(tmp_db, "nope", "youtube")
    add_video(tmp_db, "v", yt_status="uploaded")
    with pytest.raises(ValueError):
        actions.publish_now(tmp_db, "v", "youtube")
    with pytest.raises(ValueError):
        actions.p("tiktok")


def test_mark_failed_increments_and_exhausts_past_retry_runs(tmp_db):
    add_video(tmp_db, "v", status="ready", yt_status="scheduled", yt_scheduled_for=iso(NOW))
    results = [actions.mark_failed(tmp_db, "v", "youtube", f"err {i}", retry_runs=3) for i in range(4)]
    assert results == [False, False, False, True]
    row = db.get_video(tmp_db, "v")
    assert row["yt_status"] == "failed" and row["yt_attempts"] == 4 and row["attempts"] == 4
    assert row["yt_error"] == "err 3" and row["last_attempt"] is not None
    assert row["status"] == "failed" and "after retries" in row["status_reason"]
    # Until exhausted, the video stays 'ready' so due() keeps retrying it.
    add_video(tmp_db, "w", status="ready", yt_status="scheduled", yt_scheduled_for=iso(NOW))
    assert actions.mark_failed(tmp_db, "w", "youtube", "e", retry_runs=3) is False
    assert db.get_video(tmp_db, "w")["status"] == "ready"
    assert [r["tiktok_id"] for r in scheduler.due(tmp_db, "youtube", now=NOW)] == ["w"]


def test_mark_failed_truncates_long_error(tmp_db):
    add_video(tmp_db, "v", status="ready")
    actions.mark_failed(tmp_db, "v", "youtube", "x" * 5000, retry_runs=3)
    assert len(db.get_video(tmp_db, "v")["yt_error"]) == 4000


def test_refresh_done_when_all_enabled_platforms_terminal(tmp_db):
    # Only YouTube is enabled by config.
    add_video(tmp_db, "v", status="ready", local_path="f.mp4", yt_status="uploaded", ig_status="queued")
    actions.refresh_done(tmp_db, "v")
    assert db.get_video(tmp_db, "v")["status"] == "done"
    # Enable Instagram: a queued ig post means not done any more.
    db.save_workflow(tmp_db, "instagram", enabled=1)
    actions.refresh_done(tmp_db, "v")
    assert db.get_video(tmp_db, "v")["status"] == "ready"
    db.update_video(tmp_db, "v", ig_status="skipped")
    actions.refresh_done(tmp_db, "v")
    assert db.get_video(tmp_db, "v")["status"] == "done"


def test_refresh_done_all_skipped_and_no_upload(tmp_db):
    add_video(tmp_db, "v", status="ready", yt_status="skipped")
    actions.refresh_done(tmp_db, "v")
    assert db.get_video(tmp_db, "v")["status"] == "skipped"
    add_video(tmp_db, "c", status="ready", yt_status="cancelled")
    actions.refresh_done(tmp_db, "c")
    assert db.get_video(tmp_db, "c")["status"] == "ready"  # terminal but nothing uploaded, nothing failed


def test_refresh_done_leaves_held_and_skipped_alone(tmp_db):
    add_video(tmp_db, "h", status="held", yt_status="uploaded")
    actions.refresh_done(tmp_db, "h")
    assert db.get_video(tmp_db, "h")["status"] == "held"


def test_mark_uploaded_records_ids(tmp_db, monkeypatch):
    monkeypatch.setattr(actions, "utcnow", lambda: NOW)
    add_video(tmp_db, "v", status="ready", yt_status="scheduled", yt_error="old")
    actions.mark_uploaded(tmp_db, "v", "youtube", yt_video_id="abc123")
    row = db.get_video(tmp_db, "v")
    assert row["yt_status"] == "uploaded" and row["yt_video_id"] == "abc123"
    assert row["yt_error"] is None and row["yt_published_at"] == iso(NOW) and row["status"] == "done"


def test_skip_platform_and_skip(tmp_db):
    add_video(tmp_db, "v", status="ready", yt_status="scheduled", yt_scheduled_for=iso(NOW), ig_status="uploaded")
    actions.skip_platform(tmp_db, "v", "youtube", "too long")
    row = db.get_video(tmp_db, "v")
    # Only enabled workflows count: Instagram is disabled by config, so the video reads as skipped.
    assert row["yt_status"] == "skipped" and row["yt_error"] == "too long" and row["status"] == "skipped"
    db.save_workflow(tmp_db, "instagram", enabled=1)
    db.update_video(tmp_db, "v", status="ready")
    actions.refresh_done(tmp_db, "v")
    assert db.get_video(tmp_db, "v")["status"] == "done"  # the uploaded IG post now counts
    add_video(tmp_db, "w", status="ready", yt_status="scheduled", ig_status="uploaded")
    actions.skip(tmp_db, "w", "gone")
    row = db.get_video(tmp_db, "w")
    assert row["status"] == "skipped" and row["yt_status"] == "skipped" and row["ig_status"] == "uploaded"


def test_reschedule_moves_and_rejects_terminal(tmp_db):
    add_video(tmp_db, "v", status="ready", yt_status="cancelled")
    actions.reschedule(tmp_db, "v", "youtube", iso(NOW))
    assert db.get_video(tmp_db, "v")["yt_status"] == "scheduled"
    add_video(tmp_db, "u", yt_status="uploaded")
    with pytest.raises(ValueError):
        actions.reschedule(tmp_db, "u", "youtube", iso(NOW))


def test_set_text_strips_and_clears(tmp_db):
    add_video(tmp_db, "v")
    actions.set_text(tmp_db, "v", yt_title="  Title  ", yt_description="", ig_caption="cap")
    row = db.get_video(tmp_db, "v")
    assert (row["yt_title"], row["yt_description"], row["ig_caption"]) == ("Title", None, "cap")


def test_set_auto_publish_toggles(tmp_db):
    assert db.get_workflow(tmp_db, "youtube")["auto_publish"] == 1
    actions.set_auto_publish(tmp_db, "youtube", False)
    assert db.get_workflow(tmp_db, "youtube")["auto_publish"] == 0
    actions.set_auto_publish(tmp_db, "youtube", True)
    assert db.get_workflow(tmp_db, "youtube")["auto_publish"] == 1
    assert db.get_workflow(tmp_db, "instagram")["auto_publish"] == 1  # untouched
