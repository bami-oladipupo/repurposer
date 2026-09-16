import json
from datetime import timedelta

import pytest

import worker
from repurposer import actions, db, poller, publishers, transform
from repurposer.timeutil import iso, utcnow
from conftest import add_video, probe_info


@pytest.fixture
def offline(tmp_db, cfg, tmp_path, monkeypatch, fake_publisher, fake_listing, fake_download, fake_probe):
    """Everything the cycle touches is patched: listing, download, ffprobe, publishers, notify (stdout)."""
    pub = fake_publisher("youtube")
    monkeypatch.setattr(worker, "module_for", lambda platform: pub)
    monkeypatch.setattr(publishers, "module_for", lambda platform: pub)
    monkeypatch.setattr(poller, "list_videos", fake_listing(
        {"tiktok_id": "t1", "tiktok_caption": "first video", "published_at": iso(utcnow() - timedelta(hours=3))},
        {"tiktok_id": "t2", "tiktok_caption": "#ad sponsored", "published_at": iso(utcnow() - timedelta(hours=4))},
    ))
    fake_probe.info = probe_info(duration=30)
    assert cfg["notify"]["method"] == "stdout"
    return {"conn": tmp_db, "cfg": cfg, "pub": pub, "log": tmp_path / "worker.log"}


def runs(conn):
    return db.rows(conn, "SELECT * FROM runs ORDER BY run_id")


def test_clean_cycle_exit_zero_and_runs_row(offline, capsys):
    code = worker.cmd_cycle(offline["conn"], offline["cfg"], offline["log"])
    assert code == 0
    rows = runs(offline["conn"])
    assert len(rows) == 1
    r = rows[0]
    assert r["kind"] == "cycle" and r["ok"] == 1 and r["finished"] is not None and r["failures"] == 0
    assert r["videos_seen"] == 2 and r["log_path"] == str(offline["log"])
    summary = json.loads(r["summary"])
    assert summary["stage_errors"] == [] and summary["failed"] == []
    assert ("t2", "caption contains '#ad'") in [tuple(h) for h in summary["held"]]
    # t1 went new -> scheduled (future slot) -> downloaded -> ready inside one cycle.
    v = db.get_video(offline["conn"], "t1")
    assert v["status"] == "ready" and v["yt_status"] == "scheduled" and v["local_path"].endswith("t1.mp4")
    assert db.get_video(offline["conn"], "t2")["status"] == "held"
    out = capsys.readouterr().out
    assert "Repurposer run #1 (cycle): 2 seen, 0 published" in out


def test_stage_failure_still_writes_runs_row_and_exits_nonzero(offline, monkeypatch, capsys):
    def _boom(conn, cfg):
        raise RuntimeError("tiktok listing exploded")

    monkeypatch.setattr(poller, "poll", _boom)
    code = worker.cmd_cycle(offline["conn"], offline["cfg"], offline["log"])
    assert code == 1
    rows = runs(offline["conn"])
    assert len(rows) == 1 and rows[0]["ok"] == 0 and rows[0]["failures"] == 1 and rows[0]["finished"] is not None
    summary = json.loads(rows[0]["summary"])
    assert summary["stage_errors"][0][0] == "poll"
    assert "RuntimeError: tiktok listing exploded" in summary["stage_errors"][0][1]
    assert "Stage errors" in capsys.readouterr().out


def test_unhealthy_connection_is_an_alert_with_no_side_effects(offline, monkeypatch):
    """Criterion 6: a revoked token is reported as an alert naming the cause, with no other side effects."""
    offline["pub"].healthy = False
    monkeypatch.setattr(poller, "list_videos", lambda url, *, after=None, progress=None, **_kw: iter(()))
    code = worker.cmd_cycle(offline["conn"], offline["cfg"], offline["log"])
    assert code == 1
    summary = json.loads(runs(offline["conn"])[0]["summary"])
    assert summary["alerts"] == ["youtube connection unhealthy: token revoked"]
    assert summary["stage_errors"] == [] and summary["published"] == []
    assert offline["pub"].calls == []


def test_five_cycles_against_unchanged_listing(offline):
    """Criterion 2: five runs against the same profile give zero new posts and zero errors after the first."""
    conn, cfg, pub = offline["conn"], offline["cfg"], offline["pub"]
    db.save_workflow(conn, "youtube", mode="asap")  # so the first cycle actually publishes
    codes = [worker.cmd_cycle(conn, cfg, offline["log"]) for _ in range(5)]
    assert codes == [0, 0, 0, 0, 0]
    rows = runs(conn)
    assert len(rows) == 5
    assert [r["videos_published"] for r in rows] == [1, 0, 0, 0, 0]
    assert all(r["failures"] == 0 and r["ok"] == 1 for r in rows)
    assert pub.calls == ["t1"]
    assert conn.execute("SELECT COUNT(*) FROM videos").fetchone()[0] == 2
    v = db.get_video(conn, "t1")
    assert v["status"] == "done" and v["yt_status"] == "uploaded" and v["yt_video_id"] == "yt-t1"
    assert db.get_video(conn, "t2")["status"] == "held"
    for r in rows[1:]:
        s = json.loads(r["summary"])
        assert s["published"] == [] and s["failed"] == [] and s["stage_errors"] == [] and s["alerts"] == []


def test_dry_run_publishes_nothing(offline):
    conn, cfg = offline["conn"], offline["cfg"]
    db.save_workflow(conn, "youtube", mode="asap")
    assert worker.cmd_cycle(conn, cfg, offline["log"], dry_run=True) == 0
    r = runs(conn)[0]
    assert r["kind"] == "dry-run" and r["videos_published"] == 0 and offline["pub"].calls == []
    assert db.get_video(conn, "t1")["yt_status"] == "scheduled"
    assert any("dry run: 1 due" in n for n in json.loads(r["summary"])["notes"])


def test_overrides_stage_applies_hold_skip_and_publish(offline, tmp_path):
    conn, cfg = offline["conn"], offline["cfg"]
    add_video(conn, "h", status="ready", yt_status="scheduled", yt_scheduled_for=iso(utcnow() + timedelta(hours=1)))
    add_video(conn, "s", status="ready")
    add_video(conn, "p", status="held", status_reason="held by overrides.yaml", local_path="f.mp4")
    add_video(conn, "u", status="held", status_reason="held from UI", local_path="f.mp4")
    f = tmp_path / "ov.yaml"
    f.write_text("h:\n  action: hold\ns:\n  action: skip\np:\n  action: publish\nu:\n  action: publish\n", encoding="utf-8")
    cfg["overrides_file"] = str(f)
    assert worker.cmd_cycle(conn, cfg, offline["log"]) == 0
    assert db.get_video(conn, "h")["status"] == "held" and db.get_video(conn, "h")["yt_scheduled_for"] is None
    assert db.get_video(conn, "s")["status"] == "skipped"
    assert db.get_video(conn, "p")["status"] == "ready"  # release only clears our own hold
    assert db.get_video(conn, "u")["status"] == "held"
    summary = json.loads(runs(conn)[0]["summary"])
    assert ("h", "overrides.yaml") in [tuple(x) for x in summary["held"]]


def test_malformed_overrides_is_a_stage_error_not_a_crash(offline, tmp_path):
    f = tmp_path / "ov.yaml"
    f.write_text("x:\n  action: nuke\n", encoding="utf-8")
    offline["cfg"]["overrides_file"] = str(f)
    assert worker.cmd_cycle(offline["conn"], offline["cfg"], offline["log"]) == 1
    summary = json.loads(runs(offline["conn"])[0]["summary"])
    assert summary["stage_errors"][0][0] == "overrides" and "OverridesError" in summary["stage_errors"][0][1]


def test_transform_marks_long_video_and_summary_says_so(offline, fake_probe):
    conn, cfg = offline["conn"], offline["cfg"]
    fake_probe.info = probe_info(duration=95)
    assert worker.cmd_cycle(conn, cfg, offline["log"]) == 0
    v = db.get_video(conn, "t1")
    assert v["status"] == "ready" and v["ig_status"] == "skipped" and "Instagram" in v["ig_error"]


def test_failed_publish_is_reported_and_retried_until_exhausted(offline):
    conn, cfg, pub = offline["conn"], offline["cfg"], offline["pub"]
    pub.ok = False
    db.save_workflow(conn, "youtube", mode="asap")
    codes = [worker.cmd_cycle(conn, cfg, offline["log"]) for _ in range(5)]
    assert codes == [1, 1, 1, 1, 0]  # four attempts (1 + retry_runs 3), then no longer due
    assert pub.calls == ["t1"] * 4
    v = db.get_video(conn, "t1")
    assert v["yt_status"] == "failed" and v["yt_attempts"] == 4 and v["status"] == "failed"
    last = json.loads(runs(conn)[-1]["summary"])
    assert last["needs_attention"] == [["youtube", "t1", "boom: upload rejected"]]


def test_cmd_status_and_job_publish_now(offline, capsys):
    conn, cfg = offline["conn"], offline["cfg"]
    worker.cmd_cycle(conn, cfg, offline["log"])
    assert worker.cmd_status(conn) == 0
    out = capsys.readouterr().out
    assert "t1" in out and "t2" in out and "caption contains '#ad'" in out
    job_id = actions.publish_now(conn, "t1", "youtube")
    assert worker.cmd_job(conn, cfg, offline["log"], job_id) == 0
    job = db.one(conn, "SELECT * FROM jobs WHERE id = ?", (job_id,))
    assert job["ok"] == 1 and job["error"] is None and job["finished_at"]
    assert db.get_video(conn, "t1")["yt_status"] == "uploaded" and offline["pub"].calls == ["t1"]
    assert worker.cmd_job(conn, cfg, offline["log"], 999) == 2
