from datetime import datetime, timedelta, timezone

import pytest

from repurposer import actions, db, scheduler
from repurposer.timeutil import iso, parse, to_local
from conftest import add_video

UTC = timezone.utc
TZ = "Europe/London"
# Thursday 10 Sep 2026, 08:00 UTC (09:00 BST). Slots seeded from config: 10:00 and 18:00 local.
NOW = datetime(2026, 9, 10, 8, 0, tzinfo=UTC)
SLOT1 = datetime(2026, 9, 10, 9, 0, tzinfo=UTC)
SLOT2 = datetime(2026, 9, 10, 17, 0, tzinfo=UTC)
SLOT3 = datetime(2026, 9, 11, 9, 0, tzinfo=UTC)
SLOT4 = datetime(2026, 9, 11, 17, 0, tzinfo=UTC)


def slot_of(conn, tiktok_id, px="yt"):
    return db.get_video(conn, tiktok_id)[f"{px}_scheduled_for"]


# ---------- validate_slots ----------

def test_validate_slots_limits():
    assert scheduler.validate_slots(["10:00", "18:00"]) is None
    assert scheduler.validate_slots([]) is None
    assert scheduler.validate_slots(["00:00", "02:00", "04:00", "06:00", "08:00"]) is None
    assert "at most 5" in scheduler.validate_slots(["00:00", "02:00", "04:00", "06:00", "08:00", "10:00"])
    assert "two hours" in scheduler.validate_slots(["10:00", "11:59"])
    assert "two hours" in scheduler.validate_slots(["11:59", "10:00"])  # order independent
    assert scheduler.validate_slots(["10:00", "12:00"]) is None
    assert "HH:MM" in scheduler.validate_slots(["ten"])
    assert "valid time" in scheduler.validate_slots(["25:00"])


# ---------- future_slots ----------

def test_future_slots_in_timezone(tmp_db):
    slots = scheduler.future_slots(tmp_db, "youtube", TZ, NOW)
    assert slots[0] == SLOT1 and slots[1] == SLOT2 and slots[2] == SLOT3
    assert slots == sorted(slots) and all(s >= NOW for s in slots)
    assert {to_local(s, TZ).strftime("%H:%M") for s in slots} == {"10:00", "18:00"}
    assert len(slots) == 2 * (scheduler.HORIZON_DAYS + 1)
    # Starting after the first slot of the day drops it.
    later = scheduler.future_slots(tmp_db, "youtube", TZ, datetime(2026, 9, 10, 9, 30, tzinfo=UTC))
    assert later[0] == SLOT2


def test_future_slots_respects_per_weekday_table(tmp_db):
    db.replace_slots(tmp_db, "youtube", [(0, "12:00")])  # Mondays only
    slots = scheduler.future_slots(tmp_db, "youtube", TZ, NOW)
    assert slots and all(to_local(s, TZ).weekday() == 0 for s in slots)
    assert slots[0] == datetime(2026, 9, 14, 11, 0, tzinfo=UTC)


# ---------- assign, schedule mode ----------

def test_new_takes_earliest_slot_ahead_of_backlog(tmp_db):
    """Criterion 11: new content claims the next free slot; existing fills what remains in order."""
    add_video(tmp_db, "e_old", origin="existing", status="new", published_at=datetime(2026, 1, 1, tzinfo=UTC))
    add_video(tmp_db, "e_new", origin="existing", status="new", published_at=datetime(2026, 3, 1, tzinfo=UTC))
    add_video(tmp_db, "fresh", origin="new", status="new", published_at=NOW - timedelta(hours=3))
    assigned = scheduler.assign(tmp_db, "youtube", now=NOW)
    assert dict(assigned) == {"fresh": iso(SLOT1), "e_new": iso(SLOT2), "e_old": iso(SLOT3)}
    for vid in ("fresh", "e_new", "e_old"):
        assert db.get_video(tmp_db, vid)["yt_status"] == "scheduled"


def test_new_arriving_after_backlog_gets_next_free_slot(tmp_db):
    add_video(tmp_db, "e1", origin="existing", status="new", published_at=datetime(2026, 1, 1, tzinfo=UTC))
    scheduler.assign(tmp_db, "youtube", now=NOW)
    assert slot_of(tmp_db, "e1") == iso(SLOT1)
    add_video(tmp_db, "fresh", origin="new", status="new", published_at=NOW - timedelta(hours=3))
    assigned = scheduler.assign(tmp_db, "youtube", now=NOW)
    assert assigned == [("fresh", iso(SLOT2))]
    assert slot_of(tmp_db, "e1") == iso(SLOT1)  # backlog is not displaced


def test_new_too_fresh_for_first_slot_waits(tmp_db):
    # min_age 60: published 07:30 UTC is allowed from 08:30; first slot 09:00 is fine.
    add_video(tmp_db, "ok", origin="new", status="new", published_at=NOW - timedelta(minutes=30))
    # published 08:30 UTC is allowed from 09:30, so it must skip the 09:00 slot.
    add_video(tmp_db, "late", origin="new", status="new", published_at=NOW + timedelta(minutes=30))
    assigned = dict(scheduler.assign(tmp_db, "youtube", now=NOW))
    assert assigned == {"ok": iso(SLOT1), "late": iso(SLOT2)}


def test_no_video_gets_two_slots_and_no_slot_is_shared(tmp_db):
    for i in range(6):
        add_video(tmp_db, f"e{i}", origin="existing", status="new", published_at=datetime(2026, 1, 1 + i, tzinfo=UTC))
    add_video(tmp_db, "n1", origin="new", status="new", published_at=NOW - timedelta(hours=3))
    scheduler.assign(tmp_db, "youtube", now=NOW)
    scheduler.assign(tmp_db, "youtube", now=NOW)
    rows = db.rows(tmp_db, "SELECT tiktok_id, yt_scheduled_for FROM videos WHERE yt_status = 'scheduled'")
    ids = [r["tiktok_id"] for r in rows]
    times = [r["yt_scheduled_for"] for r in rows]
    assert len(ids) == len(set(ids)) == 7
    assert len(times) == len(set(times)) == 7


def test_existing_only_from_start_from_onwards(tmp_db):
    # existing_start_from is 2026-09-08 in config; run on Sat 5 Sep.
    now = datetime(2026, 9, 5, 8, 0, tzinfo=UTC)
    add_video(tmp_db, "e1", origin="existing", status="new", published_at=datetime(2026, 1, 1, tzinfo=UTC))
    add_video(tmp_db, "n1", origin="new", status="new", published_at=now - timedelta(hours=3))
    assigned = dict(scheduler.assign(tmp_db, "youtube", now=now))
    assert assigned["n1"] == iso(datetime(2026, 9, 5, 9, 0, tzinfo=UTC))
    assert assigned["e1"] == iso(datetime(2026, 9, 8, 9, 0, tzinfo=UTC))
    assert parse(assigned["e1"]) >= datetime(2026, 9, 7, 23, 0, tzinfo=UTC)


def test_existing_include_before_filters(tmp_db):
    db.save_workflow(tmp_db, "youtube", existing_include_before="2026-02-01")
    add_video(tmp_db, "before", origin="existing", status="new", published_at=datetime(2026, 1, 15, tzinfo=UTC))
    add_video(tmp_db, "after", origin="existing", status="new", published_at=datetime(2026, 2, 15, tzinfo=UTC))
    assert dict(scheduler.assign(tmp_db, "youtube", now=NOW)) == {"before": iso(SLOT1)}


def test_existing_scope_new_only_ignores_backlog(tmp_db):
    db.save_workflow(tmp_db, "youtube", content_scope="new")
    add_video(tmp_db, "e1", origin="existing", status="new", published_at=datetime(2026, 1, 1, tzinfo=UTC))
    assert scheduler.assign(tmp_db, "youtube", now=NOW) == []


@pytest.mark.parametrize("order,expected", [("newest_first", ["e_new", "e_mid", "e_old"]),
                                            ("oldest_first", ["e_old", "e_mid", "e_new"])])
def test_existing_order(tmp_db, order, expected):
    db.save_workflow(tmp_db, "youtube", existing_order=order)
    add_video(tmp_db, "e_mid", origin="existing", status="new", published_at=datetime(2026, 2, 1, tzinfo=UTC))
    add_video(tmp_db, "e_old", origin="existing", status="new", published_at=datetime(2026, 1, 1, tzinfo=UTC))
    add_video(tmp_db, "e_new", origin="existing", status="new", published_at=datetime(2026, 3, 1, tzinfo=UTC))
    assigned = scheduler.assign(tmp_db, "youtube", now=NOW)
    assert [t for t, _ in assigned] == expected
    assert [w for _, w in assigned] == [iso(SLOT1), iso(SLOT2), iso(SLOT3)]


def test_assign_twice_does_not_reassign(tmp_db):
    add_video(tmp_db, "n1", origin="new", status="new", published_at=NOW - timedelta(hours=3))
    first = scheduler.assign(tmp_db, "youtube", now=NOW)
    assert first == [("n1", iso(SLOT1))]
    assert scheduler.assign(tmp_db, "youtube", now=NOW + timedelta(hours=1)) == []
    assert slot_of(tmp_db, "n1") == iso(SLOT1)
    assert db.get_video(tmp_db, "n1")["yt_status"] == "scheduled"


def test_disabled_workflow_assigns_nothing(tmp_db):
    add_video(tmp_db, "n1", origin="new", status="new", published_at=NOW - timedelta(hours=3))
    assert scheduler.assign(tmp_db, "instagram", now=NOW) == []  # disabled in config
    db.save_workflow(tmp_db, "youtube", enabled=0)
    assert scheduler.assign(tmp_db, "youtube", now=NOW) == []
    assert db.get_video(tmp_db, "n1")["yt_status"] == "queued"


def test_held_skipped_done_not_candidates(tmp_db):
    for status in ("held", "skipped", "done", "failed"):
        add_video(tmp_db, status, origin="new", status=status, published_at=NOW - timedelta(hours=3))
    assert scheduler.assign(tmp_db, "youtube", now=NOW) == []


def test_platforms_assigned_independently(tmp_db):
    db.save_workflow(tmp_db, "instagram", enabled=1)
    add_video(tmp_db, "n1", origin="new", status="new", published_at=NOW - timedelta(hours=3))
    out = scheduler.assign_all(tmp_db, now=NOW)
    assert out["youtube"] == [("n1", iso(SLOT1))]
    assert out["instagram"] == [("n1", iso(datetime(2026, 9, 10, 10, 0, tzinfo=UTC)))]  # 11:00 BST


# ---------- assign, asap mode ----------

def test_asap_schedules_new_at_max_now_or_age(tmp_db):
    db.save_workflow(tmp_db, "youtube", mode="asap")
    add_video(tmp_db, "old", origin="new", status="new", published_at=NOW - timedelta(hours=3))
    add_video(tmp_db, "young", origin="new", status="new", published_at=NOW - timedelta(minutes=20))
    assigned = dict(scheduler.assign(tmp_db, "youtube", now=NOW))
    assert assigned["old"] == iso(NOW)
    assert assigned["young"] == iso(NOW + timedelta(minutes=40))  # published + 60 min


def test_asap_adds_delay_and_existing_still_uses_slots(tmp_db):
    db.save_workflow(tmp_db, "youtube", mode="asap", delay_minutes=30)
    add_video(tmp_db, "young", origin="new", status="new", published_at=NOW - timedelta(minutes=20))
    add_video(tmp_db, "e1", origin="existing", status="new", published_at=datetime(2026, 1, 1, tzinfo=UTC))
    assigned = dict(scheduler.assign(tmp_db, "youtube", now=NOW))
    assert assigned["young"] == iso(NOW + timedelta(minutes=70))
    assert assigned["e1"] == iso(SLOT1)


# ---------- due ----------

def test_due_returns_only_arrived_ready_rows(tmp_db):
    past, future = iso(NOW - timedelta(minutes=5)), iso(NOW + timedelta(minutes=5))
    add_video(tmp_db, "due", status="ready", yt_status="scheduled", yt_scheduled_for=past)
    add_video(tmp_db, "later", status="ready", yt_status="scheduled", yt_scheduled_for=future)
    add_video(tmp_db, "notready", status="new", yt_status="scheduled", yt_scheduled_for=past)
    add_video(tmp_db, "held", status="held", yt_status="scheduled", yt_scheduled_for=past)
    add_video(tmp_db, "queued", status="ready", yt_status="queued", yt_scheduled_for=past)
    add_video(tmp_db, "retry", status="ready", yt_status="failed", yt_scheduled_for=past, yt_attempts=3)
    add_video(tmp_db, "exhausted", status="ready", yt_status="failed", yt_scheduled_for=past, yt_attempts=4)
    add_video(tmp_db, "uploaded", status="ready", yt_status="uploaded", yt_scheduled_for=past)
    ids = [r["tiktok_id"] for r in scheduler.due(tmp_db, "youtube", now=NOW, retry_runs=3)]
    assert ids == ["due", "retry"]
    # Instagram column is independent.
    assert scheduler.due(tmp_db, "instagram", now=NOW) == []


def test_due_ordered_by_slot(tmp_db):
    add_video(tmp_db, "b", status="ready", yt_status="scheduled", yt_scheduled_for=iso(NOW - timedelta(minutes=1)))
    add_video(tmp_db, "a", status="ready", yt_status="scheduled", yt_scheduled_for=iso(NOW - timedelta(hours=1)))
    assert [r["tiktok_id"] for r in scheduler.due(tmp_db, "youtube", now=NOW)] == ["a", "b"]


def test_next_scheduled(tmp_db, monkeypatch):
    monkeypatch.setattr(scheduler, "utcnow", lambda: NOW)
    assert scheduler.next_scheduled(tmp_db, "youtube") is None
    add_video(tmp_db, "n1", status="ready", yt_status="scheduled", yt_scheduled_for=iso(SLOT2))
    add_video(tmp_db, "n2", status="ready", yt_status="scheduled", yt_scheduled_for=iso(SLOT1))
    add_video(tmp_db, "past", status="ready", yt_status="scheduled", yt_scheduled_for=iso(NOW - timedelta(days=1)))
    assert scheduler.next_scheduled(tmp_db, "youtube") == iso(SLOT1)
