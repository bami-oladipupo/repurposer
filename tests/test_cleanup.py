from pathlib import Path

from repurposer import db, downloader


def test_cleanup_removes_files_for_finished_videos_only(tmp_db, cfg, make_video, tmp_path):
    conn = tmp_db
    done_file = tmp_path / "done.mp4"; done_file.write_bytes(b"x" * 10)
    live_file = tmp_path / "live.mp4"; live_file.write_bytes(b"x" * 10)
    make_video(conn, "done", status="done", local_path=str(done_file))
    make_video(conn, "live", status="ready", local_path=str(live_file))
    make_video(conn, "gone", status="skipped", local_path=str(tmp_path / "missing.mp4"))
    stats = downloader.cleanup(conn, cfg)
    assert stats["removed"] == 2 and stats["failed"] == []
    assert not done_file.exists() and live_file.exists()
    assert db.get_video(conn, "done")["local_path"] is None
    assert db.get_video(conn, "live")["local_path"] == str(live_file)
    assert db.get_video(conn, "gone")["local_path"] is None
