from datetime import timedelta

from repurposer import db, downloader
from repurposer.timeutil import iso, utcnow
from conftest import add_video


def _ids(rows):
    return sorted(r["tiktok_id"] for r in rows)


def test_select_pending_respects_min_age_for_new_origin(tmp_db, cfg):
    add_video(tmp_db, "fresh", status="new", published_at=utcnow() - timedelta(minutes=10))
    add_video(tmp_db, "old", status="new", published_at=utcnow() - timedelta(minutes=90))
    add_video(tmp_db, "nodate", status="new", published_at=None, first_seen=iso(utcnow() - timedelta(minutes=5)))
    add_video(tmp_db, "ready", status="ready", published_at=utcnow() - timedelta(minutes=90))
    assert _ids(downloader.select_pending(tmp_db, cfg)) == ["old"]
    db.set_setting(tmp_db, "min_age_minutes", 5)
    assert _ids(downloader.select_pending(tmp_db, cfg)) == ["fresh", "nodate", "old"]


def test_backfill_selected_only_when_slot_is_near(tmp_db, cfg):
    old = utcnow() - timedelta(days=200)
    add_video(tmp_db, "soon", origin="existing", status="new", published_at=old,
              yt_status="scheduled", yt_scheduled_for=iso(utcnow() + timedelta(hours=2)))
    add_video(tmp_db, "later", origin="existing", status="new", published_at=old,
              yt_status="scheduled", yt_scheduled_for=iso(utcnow() + timedelta(hours=48)))
    add_video(tmp_db, "ig_soon", origin="existing", status="new", published_at=old,
              ig_status="scheduled", ig_scheduled_for=iso(utcnow() + timedelta(hours=1)))
    add_video(tmp_db, "queued", origin="existing", status="new", published_at=old)
    assert _ids(downloader.select_pending(tmp_db, cfg)) == ["ig_soon", "soon"]
    cfg["limits"]["backfill_download_hours"] = 72
    assert _ids(downloader.select_pending(tmp_db, cfg)) == ["ig_soon", "later", "soon"]


def test_run_downloads_pending_and_marks_downloaded(tmp_db, cfg, fake_download):
    add_video(tmp_db, "v", status="new", published_at=utcnow() - timedelta(hours=2))
    stats = downloader.run(tmp_db, cfg)
    assert stats["downloaded"] == 1 and stats["gone"] == 0 and stats["failed"] == [] and stats["disk_alert"] is None
    row = db.get_video(tmp_db, "v")
    assert row["status"] == "downloaded" and row["local_path"].endswith("v.mp4")


def test_video_gone_marks_skipped_with_reason(tmp_db, cfg, monkeypatch):
    add_video(tmp_db, "gone", status="new", published_at=utcnow() - timedelta(hours=2),
              yt_status="scheduled", yt_scheduled_for=iso(utcnow()))
    monkeypatch.setattr(downloader, "free_gb", lambda path: 100.0)

    def _raise(video, media_dir=None):
        raise downloader.VideoGone(f"TikTok {video['tiktok_id']} is no longer available")

    monkeypatch.setattr(downloader, "download", _raise)
    stats = downloader.run(tmp_db, cfg)
    assert stats["gone"] == 1 and stats["downloaded"] == 0 and stats["failed"] == []
    row = db.get_video(tmp_db, "gone")
    assert row["status"] == "skipped" and row["status_reason"] == "removed from TikTok before repost"
    assert row["yt_status"] == "skipped" and row["yt_scheduled_for"] is None
    assert row["ig_status"] == "skipped"
    # Not selected again.
    assert downloader.select_pending(tmp_db, cfg) == []


def test_download_error_recorded_and_retried(tmp_db, cfg, monkeypatch):
    add_video(tmp_db, "bad", status="new", published_at=utcnow() - timedelta(hours=2))
    monkeypatch.setattr(downloader, "free_gb", lambda path: 100.0)

    def _raise(video, media_dir=None):
        raise downloader.DownloadError("yt-dlp failed for bad: network")

    monkeypatch.setattr(downloader, "download", _raise)
    stats = downloader.run(tmp_db, cfg)
    assert stats["failed"] == [("bad", "yt-dlp failed for bad: network")]
    row = db.get_video(tmp_db, "bad")
    assert row["status"] == "new" and row["attempts"] == 1 and "network" in row["status_reason"]
    assert _ids(downloader.select_pending(tmp_db, cfg)) == ["bad"]


def test_low_disk_pauses_downloads(tmp_db, cfg, fake_download, monkeypatch):
    add_video(tmp_db, "v", status="new", published_at=utcnow() - timedelta(hours=2))
    monkeypatch.setattr(downloader, "free_gb", lambda path: 0.5)
    stats = downloader.run(tmp_db, cfg)
    assert stats["downloaded"] == 0 and "GB free" in stats["disk_alert"]
    assert db.get_video(tmp_db, "v")["status"] == "new"


def test_download_returns_existing_file_without_network(tmp_path, monkeypatch):
    """A non-empty file already in media_dir short-circuits before yt-dlp is asked to fetch anything."""
    import yt_dlp

    class _NoNetwork:
        def __init__(self, *a, **k):
            raise AssertionError("YoutubeDL must not be constructed when the file already exists")

    monkeypatch.setattr(yt_dlp, "YoutubeDL", _NoNetwork)
    target = tmp_path / "abc.mp4"
    target.write_bytes(b"data")
    assert downloader.download({"tiktok_id": "abc"}, tmp_path) == target
