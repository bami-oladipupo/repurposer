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


def test_quota_exhausted_moves_due_slots_to_next_free_slots(tmp_db, cfg, fake):
    """Quota exhaustion no longer stacks every due video onto the same time tomorrow: each takes
    the next free slot, so tomorrow's slots still hold one video each."""
    fake.quota = (False, "daily quota used")
    when = utcnow() - timedelta(minutes=30)
    due_video(tmp_db, "a", yt_scheduled_for=iso(when))
    due_video(tmp_db, "b", yt_scheduled_for=iso(when - timedelta(hours=1)))
    add_video(tmp_db, "future", status="ready", yt_status="scheduled", yt_scheduled_for=iso(when + timedelta(days=3)))
    out = publish_due(tmp_db, cfg, "youtube", {})
    assert sorted(out["rolled"]) == ["a", "b"] and out["published"] == [] and fake.calls == []
    assert "daily quota used" in out["note"] and "2 slot(s)" in out["note"]
    slots = {v: db.get_video(tmp_db, v)["yt_scheduled_for"] for v in ("a", "b")}
    assert all(parse(t) > utcnow() for t in slots.values())
    assert len(set(slots.values())) == 2
    assert all(db.get_video(tmp_db, v)["yt_status"] == "scheduled" for v in ("a", "b"))
    assert db.get_video(tmp_db, "future")["yt_scheduled_for"] == iso(when + timedelta(days=3))
    assert {d[0] for d in out["deferred"]} == {"a", "b"} and all(d[1] == "quota" for d in out["deferred"])


# ---------- burst guards (five Shorts went out in one run on 2026-09-15 after two days asleep) ----------

def future_and_distinct(conn, ids):
    times = [db.get_video(conn, v)["yt_scheduled_for"] for v in ids]
    assert all(t and parse(t) > utcnow() for t in times), times
    assert len(set(times)) == len(times), times
    assert all(db.get_video(conn, v)["yt_status"] == "scheduled" for v in ids)
    return times


def test_missed_slots_move_forward_instead_of_publishing_in_a_burst(tmp_db, cfg, fake):
    """The 2026-09-15 incident: the worker had not run for two days. Four slots had passed long ago
    and one had just passed. Exactly one video goes out; the other four take the next free slots."""
    now = utcnow()
    stale = ["d3a", "d3b", "d2a", "d2b"]
    due_video(tmp_db, "d3a", yt_scheduled_for=iso(now - timedelta(days=3, hours=2)))
    due_video(tmp_db, "d3b", yt_scheduled_for=iso(now - timedelta(days=2, hours=18)))
    due_video(tmp_db, "d2a", yt_scheduled_for=iso(now - timedelta(days=2, hours=2)))
    due_video(tmp_db, "d2b", yt_scheduled_for=iso(now - timedelta(days=1, hours=18)))
    due_video(tmp_db, "fresh", yt_scheduled_for=iso(now - timedelta(minutes=20)))
    out = publish_due(tmp_db, cfg, "youtube", {})
    assert [t for t, _ in out["published"]] == ["fresh"] and fake.calls == ["fresh"]
    assert sorted(d[0] for d in out["deferred"]) == sorted(stale)
    assert all("missed" in d[1] for d in out["deferred"])
    future_and_distinct(tmp_db, stale)
    assert "4 missed slot(s)" in out["note"]
    # Nothing else goes out on the next run either: the moved slots are in the future.
    assert publish_due(tmp_db, cfg, "youtube", {})["published"] == [] and fake.calls == ["fresh"]


def test_missed_slot_within_grace_still_publishes(tmp_db, cfg, fake):
    cfg["limits"]["slot_grace_minutes"] = 90
    due_video(tmp_db, "v", yt_scheduled_for=iso(utcnow() - timedelta(minutes=89)))
    out = publish_due(tmp_db, cfg, "youtube", {})
    assert [t for t, _ in out["published"]] == ["v"] and out["deferred"] == []


def test_stale_slot_is_moved_even_when_video_is_not_ready_yet(tmp_db, cfg, fake):
    """A backfill video whose download never happened while the Mac slept must not keep a dead slot."""
    add_video(tmp_db, "nd", status="new", yt_status="scheduled", yt_scheduled_for=iso(utcnow() - timedelta(days=1)))
    out = publish_due(tmp_db, cfg, "youtube", {})
    assert out["published"] == [] and [d[0] for d in out["deferred"]] == ["nd"]
    future_and_distinct(tmp_db, ["nd"])
    assert db.get_video(tmp_db, "nd")["status"] == "new"


def test_only_one_upload_per_run_by_default(tmp_db, cfg, fake):
    now = utcnow()
    due_video(tmp_db, "first", yt_scheduled_for=iso(now - timedelta(minutes=40)))
    due_video(tmp_db, "second", yt_scheduled_for=iso(now - timedelta(minutes=10)))
    out = publish_due(tmp_db, cfg, "youtube", {})
    assert [t for t, _ in out["published"]] == ["first"] and fake.calls == ["first"]
    assert [d[0] for d in out["deferred"]] == ["second"] and "per run" in out["deferred"][0][1]
    future_and_distinct(tmp_db, ["second"])


def test_max_publish_per_run_is_configurable(tmp_db, cfg, fake):
    cfg["limits"]["max_publish_per_run"] = 2
    now = utcnow()
    due_video(tmp_db, "a", yt_scheduled_for=iso(now - timedelta(minutes=40)))
    due_video(tmp_db, "b", yt_scheduled_for=iso(now - timedelta(minutes=10)))
    out = publish_due(tmp_db, cfg, "youtube", {})
    assert sorted(t for t, _ in out["published"]) == ["a", "b"] and out["deferred"] == []


def test_daily_limit_defaults_to_slot_count_and_defers_the_rest(tmp_db, cfg, fake):
    """Config seeds two slots a day, so the third upload of a local day waits for tomorrow."""
    now = utcnow()
    add_video(tmp_db, "u1", status="done", yt_status="uploaded", yt_published_at=iso(now - timedelta(minutes=5)))
    add_video(tmp_db, "u2", status="done", yt_status="uploaded", yt_published_at=iso(now - timedelta(minutes=3)))
    due_video(tmp_db, "third", yt_scheduled_for=iso(now - timedelta(minutes=10)))
    out = publish_due(tmp_db, cfg, "youtube", {})
    assert out["published"] == [] and fake.calls == []
    assert [d[0] for d in out["deferred"]] == ["third"] and "daily limit" in out["deferred"][0][1]
    assert "daily limit reached (2/2" in out["note"]
    future_and_distinct(tmp_db, ["third"])


def test_daily_limit_from_config(tmp_db, cfg, fake):
    cfg["limits"]["max_publish_per_day"] = 1
    add_video(tmp_db, "u1", status="done", yt_status="uploaded", yt_published_at=iso(utcnow() - timedelta(minutes=5)))
    due_video(tmp_db, "v", yt_scheduled_for=iso(utcnow() - timedelta(minutes=10)))
    assert publish_due(tmp_db, cfg, "youtube", {})["published"] == [] and fake.calls == []
    cfg["limits"]["max_publish_per_day"] = 3
    due_video(tmp_db, "w", yt_scheduled_for=iso(utcnow() - timedelta(minutes=10)))  # 'v' now holds a future slot
    assert [t for t, _ in publish_due(tmp_db, cfg, "youtube", {})["published"]] == ["w"]


def test_uploads_before_local_midnight_do_not_count_today(tmp_db, cfg, fake):
    add_video(tmp_db, "y1", status="done", yt_status="uploaded", yt_published_at=iso(utcnow() - timedelta(days=1, minutes=1)))
    add_video(tmp_db, "y2", status="done", yt_status="uploaded", yt_published_at=iso(utcnow() - timedelta(days=1, minutes=2)))
    due_video(tmp_db, "v", yt_scheduled_for=iso(utcnow() - timedelta(minutes=10)))
    assert [t for t, _ in publish_due(tmp_db, cfg, "youtube", {})["published"]] == ["v"]


def test_publish_now_bypasses_the_burst_guards(tmp_db, cfg, fake):
    now = utcnow()
    add_video(tmp_db, "u1", status="done", yt_status="uploaded", yt_published_at=iso(now - timedelta(minutes=5)))
    add_video(tmp_db, "u2", status="done", yt_status="uploaded", yt_published_at=iso(now - timedelta(minutes=3)))
    due_video(tmp_db, "old", yt_scheduled_for=iso(now - timedelta(days=2)))
    due_video(tmp_db, "other", yt_scheduled_for=iso(now - timedelta(days=2)))
    out = publish_due(tmp_db, cfg, "youtube", {}, only="old")
    assert [t for t, _ in out["published"]] == ["old"] and fake.calls == ["old"]
    # The other stale row is untouched by a Publish Now run; the next automatic run moves it.
    assert db.get_video(tmp_db, "other")["yt_scheduled_for"] == iso(now - timedelta(days=2))
    assert out["deferred"] == []


def test_dry_run_reports_missed_slots_without_moving_them(tmp_db, cfg, fake):
    when = iso(utcnow() - timedelta(days=1))
    due_video(tmp_db, "v", yt_scheduled_for=when)
    out = publish_due(tmp_db, cfg, "youtube", {}, dry_run=True)
    assert out["published"] == [] and out["deferred"] == [] and fake.calls == []
    assert "1 missed slot(s) would move" in out["note"]
    assert db.get_video(tmp_db, "v")["yt_scheduled_for"] == when


def test_failed_retry_is_not_moved_by_the_stale_guard(tmp_db, cfg, fake):
    """Retries keep their slot in the past on purpose (that is what makes them due again)."""
    when = iso(utcnow() - timedelta(hours=5))
    add_video(tmp_db, "r", status="ready", local_path="f.mp4", yt_status="failed", yt_attempts=1, yt_scheduled_for=when)
    out = publish_due(tmp_db, cfg, "youtube", {})
    assert [t for t, _ in out["published"]] == ["r"] and out["deferred"] == []


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
