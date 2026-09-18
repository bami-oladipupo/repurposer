"""Instagram publish flow with the Graph API and staging faked out."""
import contextlib
from pathlib import Path

from repurposer import db
from repurposer.publishers import PublishResult, instagram, staging
from conftest import add_video


class FakeResp:
    def __init__(self, payload, status=200):
        self._p, self.status_code, self.text = payload, status, str(payload)

    def json(self):
        return self._p


def _wire(monkeypatch, tmp_path, *, container_status="FINISHED"):
    video = tmp_path / "v.mp4"; video.write_bytes(b"x")
    calls = []

    def post(url, data=None, timeout=None, **kw):
        calls.append(("POST", url, dict(data or {})))
        if url.endswith("/media"):
            return FakeResp({"id": "c1"})
        if url.endswith("/media_publish"):
            return FakeResp({"id": "m1"})
        raise AssertionError(url)

    def get(url, params=None, timeout=None, **kw):
        calls.append(("GET", url, dict(params or {})))
        if url.endswith("/c1"):
            return FakeResp({"status_code": container_status, "status": "ok"})
        if url.endswith("/m1"):
            return FakeResp({"permalink": "https://www.instagram.com/reel/abc/"})
        raise AssertionError(url)

    monkeypatch.setattr(instagram.requests, "post", post)
    monkeypatch.setattr(instagram.requests, "get", get)
    monkeypatch.setattr(instagram, "load_token", lambda: {"access_token": "t", "user_id": "u"})
    monkeypatch.setattr(instagram.time, "sleep", lambda s: None)
    staged = {"open": 0, "closed": 0}

    @contextlib.contextmanager
    def fake_stage(path, **kw):
        staged["open"] += 1
        try:
            yield "https://pub-x.r2.dev/ig/random.mp4"
        finally:
            staged["closed"] += 1

    monkeypatch.setattr(staging, "stage", fake_stage)
    return video, calls, staged


def test_publish_uses_public_url_and_cleans_up(tmp_db, cfg, tmp_path, monkeypatch):
    video, calls, staged = _wire(monkeypatch, tmp_path)
    add_video(tmp_db, "v", status="ready", local_path=str(video), ig_status="scheduled", caption="hello")
    wf = db.get_workflow(tmp_db, "instagram")
    res = instagram.publish(tmp_db, db.get_video(tmp_db, "v"), wf, {}, cfg)
    assert res.ok and res.url == "https://www.instagram.com/reel/abc/"
    create = next(c for c in calls if c[0] == "POST" and c[1].endswith("/media"))
    assert create[2]["media_type"] == "REELS" and create[2]["video_url"] == "https://pub-x.r2.dev/ig/random.mp4"
    assert "upload_type" not in create[2]
    assert staged == {"open": 1, "closed": 1}  # staged file removed once the container finished
    row = db.get_video(tmp_db, "v")
    assert row["ig_status"] == "uploaded" and row["ig_media_id"] == "m1" and row["ig_container_id"] == "c1"


def test_publish_reports_staging_failure_without_creating_container(tmp_db, cfg, tmp_path, monkeypatch):
    video, calls, staged = _wire(monkeypatch, tmp_path)

    @contextlib.contextmanager
    def broken(path, **kw):
        raise staging.StagingError("R2 staging not configured")
        yield  # noqa: unreachable, keeps it a generator

    monkeypatch.setattr(staging, "stage", broken)
    add_video(tmp_db, "v", status="ready", local_path=str(video), ig_status="scheduled")
    res = instagram.publish(tmp_db, db.get_video(tmp_db, "v"), db.get_workflow(tmp_db, "instagram"), {}, cfg)
    assert not res.ok and "R2 staging not configured" in res.message
    assert not any(c[1].endswith("/media") for c in calls)
