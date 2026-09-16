from datetime import timedelta

from repurposer import db, poller
from repurposer.timeutil import iso, utcnow


def _count(conn):
    return conn.execute("SELECT COUNT(*) FROM videos").fetchone()[0]


def test_poll_inserts_unseen_only_and_never_reinserts(tmp_db, cfg, fake_listing, monkeypatch):
    monkeypatch.setattr(poller, "list_videos", fake_listing({"tiktok_id": "a"}, {"tiktok_id": "b"}))
    first = poller.poll(tmp_db, cfg)
    assert first == {"seen": 2, "inserted": 2, "held": 0}
    assert _count(tmp_db) == 2
    for _ in range(4):
        assert poller.poll(tmp_db, cfg) == {"seen": 2, "inserted": 0, "held": 0}
    assert _count(tmp_db) == 2
    row = db.get_video(tmp_db, "a")
    assert row["origin"] == "new" and row["status"] == "new" and row["yt_status"] == "queued"


def test_poll_picks_up_a_new_video_and_keeps_local_state(tmp_db, cfg, fake_listing, monkeypatch):
    monkeypatch.setattr(poller, "list_videos", fake_listing({"tiktok_id": "a"}))
    poller.poll(tmp_db, cfg)
    db.update_video(tmp_db, "a", status="done", yt_status="uploaded", tiktok_caption="edited locally")
    monkeypatch.setattr(poller, "list_videos", fake_listing({"tiktok_id": "b", "tiktok_caption": "newer"}, {"tiktok_id": "a"}))
    assert poller.poll(tmp_db, cfg)["inserted"] == 1
    row = db.get_video(tmp_db, "a")
    assert row["status"] == "done" and row["tiktok_caption"] == "edited locally"
    assert db.get_video(tmp_db, "b")["tiktok_caption"] == "newer"


def test_poll_uses_lookback_then_overlap_after_newest(tmp_db, cfg, monkeypatch):
    seen_after = []

    def _list(url, *, after=None, progress=None, **_kw):
        seen_after.append(after)
        yield {"tiktok_id": "x", "tiktok_url": None, "tiktok_caption": "c", "thumbnail_url": None,
               "published_at": iso(utcnow() - timedelta(days=1))}

    monkeypatch.setattr(poller, "utcnow", lambda: utcnow().replace(second=0))
    monkeypatch.setattr(poller, "list_videos", _list)
    poller.poll(tmp_db, cfg)
    poller.poll(tmp_db, cfg)
    lookback = cfg["source"]["lookback_days"]
    assert abs((utcnow() - seen_after[0]) - timedelta(days=lookback)) < timedelta(minutes=2)
    newest = db.get_video(tmp_db, "x")["published_at"]
    assert iso(seen_after[1]) == iso(utcnow() - timedelta(days=1) - timedelta(hours=1)) or \
        abs((seen_after[1] + timedelta(hours=1)) - (utcnow() - timedelta(days=1))) < timedelta(minutes=2)
    assert newest is not None


def test_sponsored_captions_held_with_reason_naming_keyword(tmp_db, cfg, fake_listing, monkeypatch):
    monkeypatch.setattr(poller, "list_videos", fake_listing(
        {"tiktok_id": "ad", "tiktok_caption": "New shoes! #AD"},
        {"tiktok_id": "paid", "tiktok_caption": "Paid Partnership with Brand"},
        {"tiktok_id": "kaplan", "tiktok_caption": "my kaplan course"},
        {"tiktok_id": "clean", "tiktok_caption": "normal video"},
    ))
    stats = poller.poll(tmp_db, cfg)
    assert stats == {"seen": 4, "inserted": 4, "held": 3}
    assert db.get_video(tmp_db, "ad")["status"] == "held"
    assert db.get_video(tmp_db, "ad")["status_reason"] == "caption contains '#ad'"
    assert db.get_video(tmp_db, "paid")["status_reason"] == "caption contains 'paid partnership'"
    assert db.get_video(tmp_db, "kaplan")["status_reason"] == "caption contains 'Kaplan'"
    assert db.get_video(tmp_db, "clean")["status"] == "new"


def test_exclusions_come_from_workflows(tmp_db, cfg, fake_listing, monkeypatch):
    db.save_workflow(tmp_db, "youtube", exclude_keywords=["secret"])
    assert poller._exclusions(tmp_db)[0] == "secret"
    monkeypatch.setattr(poller, "list_videos", fake_listing({"tiktok_id": "s", "tiktok_caption": "a SECRET"}))
    assert poller.poll(tmp_db, cfg)["held"] == 1


def test_import_catalogue_idempotent_and_holds_sponsored(tmp_db, cfg, fake_listing, monkeypatch):
    """Criterion 10: every video inserted as existing, sponsored held, second run adds zero rows."""
    entries = [{"tiktok_id": str(i), "tiktok_caption": ("#sponsored " if i % 10 == 0 else "") + f"video {i}",
                "published_at": iso(utcnow() - timedelta(days=i + 1))} for i in range(1, 41)]
    monkeypatch.setattr(poller, "list_videos", fake_listing(*entries))
    progress = []
    stats = poller.import_catalogue(tmp_db, cfg, progress=progress.append)
    assert stats == {"seen": 40, "inserted": 40, "held": 4}
    assert _count(tmp_db) == 40
    assert progress and progress[0].startswith("  listed 1 ") and any("listed 40 " in p for p in progress)
    assert db.rows(tmp_db, "SELECT COUNT(*) AS n FROM videos WHERE origin = 'existing'")[0]["n"] == 40
    held = db.rows(tmp_db, "SELECT tiktok_id, status_reason FROM videos WHERE status = 'held' ORDER BY tiktok_id")
    assert [h["tiktok_id"] for h in held] == ["10", "20", "30", "40"]
    assert all(h["status_reason"] == "caption contains '#sponsored'" for h in held)
    again = poller.import_catalogue(tmp_db, cfg)
    assert again == {"seen": 40, "inserted": 0, "held": 0}
    assert _count(tmp_db) == 40


def test_import_does_not_overwrite_existing_new_rows(tmp_db, cfg, fake_listing, monkeypatch):
    monkeypatch.setattr(poller, "list_videos", fake_listing({"tiktok_id": "a"}))
    poller.poll(tmp_db, cfg)
    assert poller.import_catalogue(tmp_db, cfg)["inserted"] == 0
    assert db.get_video(tmp_db, "a")["origin"] == "new"


def test_entry_to_video_mapping():
    v = poller._entry_to_video({"id": 123, "timestamp": 1_756_000_000, "description": "desc", "webpage_url": "u", "thumbnail": "t"})
    assert v["tiktok_id"] == "123" and v["tiktok_url"] == "u" and v["tiktok_caption"] == "desc"
    from datetime import datetime, timezone
    assert v["published_at"] == iso(datetime.fromtimestamp(1_756_000_000, tz=timezone.utc)) and v["thumbnail_url"] == "t"
    assert poller._entry_to_video({"id": "9", "upload_date": "20260102", "title": "t"})["published_at"] == "2026-01-02T00:00:00+00:00"
    assert poller._entry_to_video({}) is None


def test_import_catalogue_limit_caps_the_listing(tmp_db, cfg, monkeypatch):
    """A limit is passed to the listing as max_items and only that many rows are inserted."""
    calls = []

    def _list(profile_url, *, after=None, max_items=None, progress=None, **_kw):
        calls.append(max_items)
        for i in range(1, (max_items or 40) + 1):
            yield {"tiktok_id": str(i), "tiktok_url": f"https://www.tiktok.com/@x/video/{i}", "tiktok_caption": f"video {i}",
                   "thumbnail_url": None, "published_at": iso(utcnow() - timedelta(days=i))}

    monkeypatch.setattr(poller, "list_videos", _list)
    stats = poller.import_catalogue(tmp_db, cfg, limit=5)
    assert calls == [5]
    assert stats == {"seen": 5, "inserted": 5, "held": 0}
    assert _count(tmp_db) == 5
    stats = poller.import_catalogue(tmp_db, cfg)
    assert calls[-1] is None
    assert stats["seen"] == 40 and stats["inserted"] == 35
