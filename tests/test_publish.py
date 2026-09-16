from datetime import timedelta

import pytest

from repurposer import actions, db, publishers
from repurposer.publishers import PublishResult, publish_due
from repurposer.timeutil import iso, parse, utcnow
from conftest import add_video


@pytest.fixture
def fake(monkeypatch, fake_publisher):
    pub = fake_publisher("youtube")
    monkeypatch.setattr(publishers, "module_for", lambda platform: pub)
    return pub


def due_video(conn, tiktok_id="v", **extra):
    extra.setdefault("yt_scheduled_for", iso(utcnow() - timedelta(minutes=30)))
    return add_video(conn, tiktok_id, status="ready", local_path="f.mp4", yt_status="scheduled", **extra)


def test_publishes_due_and_marks_uploaded(tmp_db, cfg, fake):
    due_video(tmp_db)
    out = publish_due(tmp_db, cfg, "youtube", {})
    assert out["published"] == [("v", "https://example.invalid/v")]
    assert out["failed"] == [] and out["note"] is None
    row = db.get_video(tmp_db, "v")
    assert row["yt_status"] == "uploaded" and row["yt_video_id"] == "yt-v" and row["status"] == "done"


def test_future_slots_not_published(tmp_db, cfg, fake):
    add_video(tmp_db, "later", status="ready", yt_status="scheduled", yt_scheduled_for=iso(utcnow() + timedelta(hours=1)))
    assert publish_due(tmp_db, cfg, "youtube", {})["published"] == []
    assert fake.calls == []


def test_manual_mode_publishes_nothing_unless_forced(tmp_db, cfg, fake):
    due_video(tmp_db)
    actions.set_auto_publish(tmp_db, "youtube", False)
    out = publish_due(tmp_db, cfg, "youtube", {})
    assert out["published"] == [] and "manual" in out["note"]
    assert fake.calls == []
    row = db.get_video(tmp_db, "v")
    assert row["yt_status"] == "scheduled" and row["yt_scheduled_for"] is not None  # slot kept
    out = publish_due(tmp_db, cfg, "youtube", {}, force_manual=True)
    assert out["published"] and fake.calls == ["v"]


def test_disabled_workflow_publishes_nothing(tmp_db, cfg, fake):
    add_video(tmp_db, "v", status="ready", ig_status="scheduled", ig_scheduled_for=iso(utcnow() - timedelta(minutes=1)))
    out = publish_due(tmp_db, cfg, "instagram", {})
    assert out["note"] == "workflow disabled" and fake.calls == []


def test_quota_exhausted_rolls_due_slots_a_day(tmp_db, cfg, fake):
    fake.quota = (False, "daily quota used")
    when = utcnow() - timedelta(minutes=30)
    due_video(tmp_db, "a", yt_scheduled_for=iso(when))
    due_video(tmp_db, "b", yt_scheduled_for=iso(when - timedelta(hours=1)))
    add_video(tmp_db, "future", status="ready", yt_status="scheduled", yt_scheduled_for=iso(when + timedelta(days=3)))
    out = publish_due(tmp_db, cfg, "youtube", {})
    assert sorted(out["rolled"]) == ["a", "b"] and out["published"] == [] and fake.calls == []
    assert "daily quota used" in out["note"] and "2 slot(s)" in out["note"]
    assert db.get_video(tmp_db, "a")["yt_scheduled_for"] == iso(when + timedelta(days=1))
    assert db.get_video(tmp_db, "b")["yt_scheduled_for"] == iso(when - timedelta(hours=1) + timedelta(days=1))
    assert db.get_video(tmp_db, "a")["yt_status"] == "scheduled"
    assert db.get_video(tmp_db, "future")["yt_scheduled_for"] == iso(when + timedelta(days=3))


def test_override_hold_prevents_publishing(tmp_db, cfg, fake):
    """Criterion 5: hold in overrides.yaml is never published until the entry changes."""
    due_video(tmp_db)
    out = publish_due(tmp_db, cfg, "youtube", {"v": {"action": "hold"}})
    assert out["published"] == [] and out["skipped"] == [("v", "held by overrides.yaml")]
    assert fake.calls == []
    row = db.get_video(tmp_db, "v")
    assert row["status"] == "held" and row["yt_status"] == "queued" and row["yt_scheduled_for"] is None
    # Entry removed: the video is not due any more (its slot was released), so still nothing goes out.
    assert publish_due(tmp_db, cfg, "youtube", {})["published"] == []


def test_override_skip_prevents_publishing(tmp_db, cfg, fake):
    due_video(tmp_db)
    out = publish_due(tmp_db, cfg, "youtube", {"v": {"action": "skip"}})
    assert out["skipped"] == [("v", "skipped by overrides.yaml")] and fake.calls == []
    row = db.get_video(tmp_db, "v")
    assert row["status"] == "skipped" and row["yt_status"] == "skipped" and row["ig_status"] == "skipped"
    assert publish_due(tmp_db, cfg, "youtube", {})["published"] == []


def test_override_captions_passed_through_to_publisher(tmp_db, cfg, fake, monkeypatch):
    seen = {}

    def _publish(conn, video, workflow, override, cfg):
        seen.update(override)
        return PublishResult(True, "ok", url="u")

    monkeypatch.setattr(fake, "publish", _publish)
    due_video(tmp_db)
    publish_due(tmp_db, cfg, "youtube", {"v": {"yt_title": "Custom"}})
    assert seen == {"yt_title": "Custom"}


def test_failed_publish_records_error_and_retry_count(tmp_db, cfg, fake):
    fake.ok = False
    due_video(tmp_db)
    out = publish_due(tmp_db, cfg, "youtube", {})
    assert out["failed"] == [("v", "boom: upload rejected")] and out["exhausted"] == []
    row = db.get_video(tmp_db, "v")
    assert row["yt_status"] == "failed" and row["yt_error"] == "boom: upload rejected"
    assert row["yt_attempts"] == 1 and row["status"] == "ready"
    # Retried on subsequent runs until retry_runs is exceeded, then flagged as exhausted.
    for _ in range(2):
        assert publish_due(tmp_db, cfg, "youtube", {})["exhausted"] == []
    out = publish_due(tmp_db, cfg, "youtube", {})
    assert out["exhausted"] == ["v"] and db.get_video(tmp_db, "v")["yt_attempts"] == 4
    assert db.get_video(tmp_db, "v")["status"] == "failed"
    # Now out of retries: not due any more.
    assert publish_due(tmp_db, cfg, "youtube", {}) ["failed"] == []
    assert len(fake.calls) == 4


def test_already_uploaded_is_not_republished(tmp_db, cfg, fake):
    """Criterion 3: re-running after an upload produces exactly one post."""
    due_video(tmp_db)
    assert publish_due(tmp_db, cfg, "youtube", {})["published"]
    for _ in range(3):
        out = publish_due(tmp_db, cfg, "youtube", {})
        assert out["published"] == [] and out["failed"] == []
    assert fake.calls == ["v"]
    # A row that is 'uploaded' but still carries a past slot is guarded too.
    add_video(tmp_db, "u", status="ready", yt_status="uploaded", yt_scheduled_for=iso(utcnow() - timedelta(hours=1)))
    assert publish_due(tmp_db, cfg, "youtube", {})["published"] == []
    assert fake.calls == ["v"]


def test_uploaded_between_due_query_and_publish_is_skipped(tmp_db, cfg, fake, monkeypatch):
    due_video(tmp_db, "a")
    due_video(tmp_db, "b")
    real = fake.publish

    def _publish(conn, video, workflow, override, cfg):
        # Another process uploads 'b' while 'a' is being published.
        if video["tiktok_id"] == "a":
            actions.mark_uploaded(conn, "b", "youtube", yt_video_id="elsewhere")
        return real(conn, video, workflow, override, cfg)

    monkeypatch.setattr(fake, "publish", _publish)
    out = publish_due(tmp_db, cfg, "youtube", {})
    assert [t for t, _ in out["published"]] == ["a"] and fake.calls == ["a"]
    assert db.get_video(tmp_db, "b")["yt_video_id"] == "elsewhere"


def test_publisher_exception_recorded_as_failed_not_propagated(tmp_db, cfg, fake):
    fake.exc = RuntimeError("kaboom")
    due_video(tmp_db)
    out = publish_due(tmp_db, cfg, "youtube", {})
    assert out["failed"] == [("v", "RuntimeError: kaboom")]
    row = db.get_video(tmp_db, "v")
    assert row["yt_status"] == "failed" and row["yt_error"] == "RuntimeError: kaboom" and row["yt_attempts"] == 1


def test_only_restricts_to_one_video(tmp_db, cfg, fake):
    due_video(tmp_db, "a")
    due_video(tmp_db, "b")
    out = publish_due(tmp_db, cfg, "youtube", {}, only="b")
    assert [t for t, _ in out["published"]] == ["b"] and fake.calls == ["b"]
    assert db.get_video(tmp_db, "a")["yt_status"] == "scheduled"


def test_dry_run_publishes_nothing(tmp_db, cfg, fake):
    due_video(tmp_db)
    out = publish_due(tmp_db, cfg, "youtube", {}, dry_run=True)
    assert out["published"] == [] and "dry run: 1 due" in out["note"] and fake.calls == []
    assert db.get_video(tmp_db, "v")["yt_status"] == "scheduled"


def test_module_for_unknown_platform():
    with pytest.raises(ValueError):
        publishers.module_for("tiktok")
