"""Shared fixtures. Every test runs against a throwaway SQLite file under tmp_path and never
touches data/, media/, logs/ or tokens/ in the project. No test hits the network."""
from __future__ import annotations

import copy
import sys
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from repurposer import actions, config, db  # noqa: E402
from repurposer.publishers import PublishResult  # noqa: E402
from repurposer.timeutil import iso, utcnow  # noqa: E402


@pytest.fixture
def cfg() -> dict[str, Any]:
    """A fresh copy of the project config.yaml for each test, with notifications on stdout."""
    c = copy.deepcopy(config.load_config())
    c["notify"]["method"] = "stdout"
    return c


@pytest.fixture
def tmp_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cfg: dict[str, Any]):
    for name in ("DATA_DIR", "MEDIA_DIR", "LOG_DIR", "TOKEN_DIR"):
        d = tmp_path / name.lower()
        d.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(config, name, d)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "data" / "t.db")
    # Overrides resolve relative to config.ROOT; point the file at a location that does not exist.
    cfg["overrides_file"] = str(tmp_path / "overrides.yaml")
    conn = db.open_db(tmp_path / "t.db")
    assert db.seed_from_config(conn, cfg) is True
    yield conn
    conn.close()


_UNSET = object()


def add_video(conn, tiktok_id: str, origin: str = "new", status: str = "ready", published_at=_UNSET,
              local_path=None, duration_s: float | None = 30, caption: str | None = None, **extra: Any) -> dict[str, Any]:
    """Insert a video row through db.insert_video and return the stored row.

    published_at defaults to two hours ago; pass None explicitly to store NULL."""
    if published_at is _UNSET:
        published_at = utcnow() - timedelta(hours=2)
    if published_at is not None and not isinstance(published_at, str):
        published_at = iso(published_at)
    if caption is None:
        caption = f"Caption for {tiktok_id} #test"
    fields: dict[str, Any] = dict(
        tiktok_id=str(tiktok_id), tiktok_url=f"https://www.tiktok.com/@x/video/{tiktok_id}",
        tiktok_caption=caption, published_at=published_at, origin=origin, status=status,
        local_path=str(local_path) if local_path else None, duration_s=duration_s,
    )
    fields.update(extra)
    assert db.insert_video(conn, **fields) is True
    return db.get_video(conn, str(tiktok_id))


@pytest.fixture
def make_video():
    return add_video


class FakePublisher:
    """Stand-in for publishers.youtube / publishers.instagram. Records every publish call."""

    def __init__(self, platform: str = "youtube", *, ok: bool = True, quota: tuple[bool, str] = (True, ""),
                 exc: Exception | None = None, healthy: bool = True) -> None:
        self.platform = platform
        self.ok = ok
        self.quota = quota
        self.exc = exc
        self.healthy = healthy
        self.calls: list[str] = []

    def check_connection(self, conn, cfg):
        return {"healthy": self.healthy, "error": None if self.healthy else "token revoked", "account_name": "fake"}

    def quota_ok(self, conn, cfg):
        return self.quota

    def publish(self, conn, video, workflow, override, cfg):
        self.calls.append(video["tiktok_id"])
        if self.exc is not None:
            raise self.exc
        if not self.ok:
            return PublishResult(False, "boom: upload rejected")
        ids = {"yt_video_id": f"yt-{video['tiktok_id']}"} if self.platform == "youtube" else {"ig_media_id": f"ig-{video['tiktok_id']}"}
        actions.mark_uploaded(conn, video["tiktok_id"], self.platform, **ids)
        return PublishResult(True, "uploaded", url=f"https://example.invalid/{video['tiktok_id']}")


@pytest.fixture
def fake_publisher():
    return FakePublisher


def listing(*entries: dict[str, Any]):
    """Build a fake poller.list_videos: a callable returning a generator over fixed video dicts."""
    def _list(profile_url, *, after=None, progress=None, **_kw):
        n = 0
        for e in entries:
            n += 1
            v = {
                "tiktok_id": str(e["tiktok_id"]),
                "tiktok_url": e.get("tiktok_url") or f"https://www.tiktok.com/@x/video/{e['tiktok_id']}",
                "tiktok_caption": e.get("tiktok_caption", f"video {e['tiktok_id']}"),
                "thumbnail_url": e.get("thumbnail_url"),
                "published_at": e.get("published_at") or iso(utcnow() - timedelta(hours=3)),
            }
            if progress:
                progress(n, v)
            yield v
    return _list


@pytest.fixture
def fake_listing():
    return listing


@pytest.fixture
def fake_download(monkeypatch: pytest.MonkeyPatch):
    """Patch downloader.download to write a small dummy file into the (tmp) media dir."""
    from repurposer import downloader

    def _download(video, media_dir=None):
        media_dir = media_dir or config.MEDIA_DIR
        media_dir.mkdir(parents=True, exist_ok=True)
        target = media_dir / f"{video['tiktok_id']}.mp4"
        target.write_bytes(b"\x00" * 64)
        return target

    monkeypatch.setattr(downloader, "download", _download)
    monkeypatch.setattr(downloader, "free_gb", lambda path: 100.0)
    return _download


def probe_info(duration: float = 30, width: int = 1080, height: int = 1920, vcodec: str = "h264",
               acodec: str | None = "aac", container: str = "mov,mp4,m4a,3gp,3g2,mj2") -> dict[str, Any]:
    return {"duration": duration, "width": width, "height": height, "vcodec": vcodec, "acodec": acodec, "container": container}


@pytest.fixture
def fake_probe(monkeypatch: pytest.MonkeyPatch):
    """Patch transform.probe with a fixed result; returns a holder whose .info can be changed per test."""
    from repurposer import transform

    holder = SimpleNamespace(info=probe_info())
    monkeypatch.setattr(transform, "probe", lambda path: dict(holder.info))
    monkeypatch.setattr(transform, "reencode", lambda src, dst: Path(dst).write_bytes(b"\x00"))
    if hasattr(transform, "thumbnail"):
        monkeypatch.setattr(transform, "thumbnail", lambda src: None)
    return holder
