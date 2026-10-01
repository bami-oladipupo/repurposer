"""The iPhone app's JSON API: the access guard, the read models and the actions it can take."""
from datetime import timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app as webapp
from repurposer import api, db, push
from repurposer.timeutil import iso, utcnow
from conftest import add_video

TOKEN = "test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def client(tmp_db, monkeypatch):
    """TestClient calls arrive from the host 'testclient', i.e. not from this Mac."""
    monkeypatch.setenv("APP_TOKEN", TOKEN)
    spawned: list[tuple[str, ...]] = []
    # Endpoints run on worker threads, so each call opens its own connection to the test database.
    path = tmp_db.execute("PRAGMA database_list").fetchone()["file"]
    fresh = FastAPI()
    fresh.middleware("http")(webapp.guard)
    fresh.add_exception_handler(webapp.StarletteHTTPException, webapp.http_error)
    fresh.include_router(api.build_router(lambda: db.connect(path), lambda *args: spawned.append(args)))
    fresh.get("/workflows")(lambda: "page")
    c = TestClient(fresh)
    c.spawned = spawned
    return c


def test_api_needs_the_token_and_pages_stay_on_the_mac(client, monkeypatch):
    assert client.get("/api/app/status").status_code == 401
    assert client.get("/api/app/status", headers={"Authorization": "Bearer wrong"}).status_code == 401
    r = client.get("/api/app/status", headers=AUTH)
    assert r.status_code == 200 and r.json()["success"] is True
    # The web pages are refused to anything that is not this Mac, token or not.
    assert client.get("/workflows", headers=AUTH).status_code == 403
    monkeypatch.delenv("APP_TOKEN")
    assert client.get("/api/app/status", headers=AUTH).status_code == 503


def test_home_lists_upcoming_published_and_failed(client, tmp_db):
    add_video(tmp_db, "next", yt_status="scheduled", yt_scheduled_for=iso(utcnow() + timedelta(hours=1)))
    add_video(tmp_db, "posted", status="done", yt_status="uploaded", yt_video_id="abc",
              yt_published_at=iso(utcnow()), ig_status="skipped")
    add_video(tmp_db, "broken", yt_status="failed", yt_error="quota", yt_attempts=4)
    d = client.get("/api/app/home", headers=AUTH).json()["data"]
    assert [p["tiktok_id"] for p in d["up_next"]] == ["next"]
    assert [(p["tiktok_id"], p["link"]) for p in d["published_today"]] == [("posted", "https://youtube.com/shorts/abc")]
    assert [(p["tiktok_id"], p["error"]) for p in d["attention"]] == [("broken", "quota")]
    yt = next(p for p in d["platforms"] if p["platform"] == "youtube")
    assert yt["counts"]["scheduled"] == 1 and yt["counts"]["failed"] == 1 and yt["auto_publish"] is True
    assert d["worker"]["stale"] is True  # no cycle has run in this database


def test_posts_window_and_status_filter(client, tmp_db):
    soon = utcnow() + timedelta(hours=2)
    add_video(tmp_db, "in", yt_status="scheduled", yt_scheduled_for=iso(soon))
    add_video(tmp_db, "out", yt_status="scheduled", yt_scheduled_for=iso(soon + timedelta(days=9)))
    add_video(tmp_db, "held", status="held")
    q = {"start": iso(utcnow()), "end": iso(utcnow() + timedelta(days=1))}
    assert [p["tiktok_id"] for p in client.get("/api/app/posts", params=q, headers=AUTH).json()["data"]["posts"]] == ["in"]
    held = client.get("/api/app/posts", params={"status": "held"}, headers=AUTH).json()["data"]["posts"]
    assert {p["tiktok_id"] for p in held} == {"held"} and all(p["can_release"] for p in held)
    assert client.get("/api/app/posts", headers=AUTH).status_code == 400


def test_publish_now_raises_a_job_and_spawns_the_worker(client, tmp_db):
    add_video(tmp_db, "v", yt_status="scheduled", yt_scheduled_for=iso(utcnow() + timedelta(days=2)))
    r = client.post("/api/app/videos/v/youtube/publish-now", headers=AUTH)
    assert r.status_code == 200
    job = db.one(tmp_db, "SELECT * FROM jobs")
    assert job["kind"] == "publish_now" and job["tiktok_id"] == "v"
    assert client.spawned == [("--job", str(job["id"]))]
    # Already uploaded: refused with the reason, nothing spawned.
    add_video(tmp_db, "up", status="done", yt_status="uploaded")
    r = client.post("/api/app/videos/up/youtube/publish-now", headers=AUTH)
    assert r.status_code == 409 and "already uploaded" in r.json()["error"] and len(client.spawned) == 1
    assert client.post("/api/app/videos/nope/youtube/publish-now", headers=AUTH).status_code == 404


def test_reschedule_hold_release_cancel_and_text(client, tmp_db):
    add_video(tmp_db, "v", yt_status="scheduled", yt_scheduled_for=iso(utcnow() + timedelta(days=1)))
    when = iso(utcnow() + timedelta(days=3))
    assert client.post("/api/app/videos/v/youtube/reschedule", json={"when": when}, headers=AUTH).status_code == 200
    assert db.get_video(tmp_db, "v")["yt_scheduled_for"] == when
    past = client.post("/api/app/videos/v/youtube/reschedule", json={"when": iso(utcnow() - timedelta(hours=1))}, headers=AUTH)
    assert past.status_code == 409 and "past" in past.json()["error"]

    client.post("/api/app/videos/v/hold", headers=AUTH)
    row = db.get_video(tmp_db, "v")
    assert row["status"] == "held" and row["yt_status"] == "queued"
    client.post("/api/app/videos/v/release", headers=AUTH)
    assert db.get_video(tmp_db, "v")["status"] == "new"  # no file downloaded yet

    client.post("/api/app/videos/v/youtube/cancel", headers=AUTH)
    assert db.get_video(tmp_db, "v")["yt_status"] == "cancelled"
    client.post("/api/app/videos/v/youtube/requeue", headers=AUTH)
    assert db.get_video(tmp_db, "v")["yt_status"] == "queued"

    client.post("/api/app/videos/v/text", json={"yt_title": "New title", "ig_caption": "New caption"}, headers=AUTH)
    d = client.get("/api/app/videos/v", headers=AUTH).json()["data"]
    assert d["yt_title"].startswith("New title") and d["ig_caption"] == "New caption"


def test_pause_and_resume_a_platform(client, tmp_db):
    client.post("/api/app/workflows/youtube/auto", json={"on": False}, headers=AUTH)
    assert db.get_workflow(tmp_db, "youtube")["auto_publish"] == 0
    client.post("/api/app/workflows/youtube/auto", json={"on": True}, headers=AUTH)
    assert db.get_workflow(tmp_db, "youtube")["auto_publish"] == 1
    assert client.post("/api/app/workflows/myspace/auto", json={"on": True}, headers=AUTH).status_code == 404


def test_run_now_and_runs_feed(client, tmp_db):
    assert client.post("/api/app/run-now", headers=AUTH).status_code == 200
    assert client.spawned and client.spawned[0][0] == "--job"
    with db.tx(tmp_db):
        tmp_db.execute("INSERT INTO runs(started, finished, kind, ok, summary) VALUES (?,?,?,?,?)",
                       (iso(utcnow()), iso(utcnow()), "cycle", 0,
                        '{"published": [["youtube", "v", "https://youtube.com/shorts/x"]], '
                        '"failed": [["download", "v2", "boom"]], "alerts": ["disk low"], "notes": ["poll: 1 seen"]}'))
    d = client.get("/api/app/runs", headers=AUTH).json()["data"]
    run = d["runs"][0]
    assert run["ok"] is False and run["published"][0]["link"].endswith("/x")
    assert run["problems"] == ["download v2: boom", "disk low"]
    assert d["worker"]["stale"] is False


def test_device_registration_and_push_summary(client, tmp_db):
    assert client.post("/api/app/devices", json={"token": "not hex"}, headers=AUTH).status_code == 400
    assert client.post("/api/app/devices", json={"token": "AB12" * 16, "environment": "sandbox"}, headers=AUTH).status_code == 200
    row = db.one(tmp_db, "SELECT * FROM devices")
    assert row["token"] == "ab12" * 16 and row["environment"] == "sandbox" and push.device_count(tmp_db) == 1
    title, body = push.summarise({"published": [["youtube", "v", "u"], ["instagram", "w", "u"]], "failed": []})
    assert title == "Posted" and body == "Posted to YouTube and Instagram"
    title, body = push.summarise({"published": [], "failed": [["instagram", "v", "HTTP 400: nope\nmore"]]})
    assert title == "Needs a look" and body == "Instagram failed: HTTP 400: nope"
