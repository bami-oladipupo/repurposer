import pytest

from repurposer import db, transform
from conftest import add_video, probe_info

LIMITS = {"max_duration_s": 180, "ig_max_duration_s": 90}


def test_decide_rejects_over_max_duration():
    v = transform.decide(probe_info(duration=181), LIMITS)
    assert v["reject"] and "181s" in v["reject"] and "180s" in v["reject"]
    assert v["ig_skip"] is None


def test_decide_rejects_non_vertical():
    v = transform.decide(probe_info(width=1920, height=1080), LIMITS)
    assert v["reject"] and "not vertical" in v["reject"]
    assert transform.decide(probe_info(width=1080, height=0), LIMITS)["reject"]


def test_decide_rejects_unreadable_duration():
    assert transform.decide(probe_info(duration=0), LIMITS)["reject"] == "could not read duration"


@pytest.mark.parametrize("duration", [91, 120, 180])
def test_decide_between_caps_skips_instagram_only(duration):
    v = transform.decide(probe_info(duration=duration), LIMITS)
    assert v["reject"] is None
    assert v["ig_skip"] and "Instagram" in v["ig_skip"] and "90s" in v["ig_skip"]


@pytest.mark.parametrize("duration", [1, 30, 90])
def test_decide_accepts_short_vertical(duration):
    assert transform.decide(probe_info(duration=duration), LIMITS) == {"reject": None, "ig_skip": None}


def test_decide_respects_rotated_vertical_and_aspect_bounds():
    assert transform.decide(probe_info(width=720, height=1280), LIMITS)["reject"] is None  # 0.5625
    assert transform.decide(probe_info(width=1000, height=1000), LIMITS)["reject"]  # square


@pytest.mark.parametrize("info,expected", [
    (probe_info(), False),
    (probe_info(acodec=None), False),
    (probe_info(vcodec="hevc"), True),
    (probe_info(acodec="opus"), True),
    (probe_info(container="matroska,webm"), True),
    (probe_info(vcodec=None), True),
])
def test_needs_reencode(info, expected):
    assert transform.needs_reencode(info) is expected


def _downloaded(conn, tmp_path, tiktok_id):
    f = tmp_path / f"{tiktok_id}.mp4"
    f.write_bytes(b"\x00" * 16)
    return add_video(conn, tiktok_id, status="downloaded", local_path=f, duration_s=None)


def test_process_95s_ready_with_instagram_skipped(tmp_db, tmp_path, fake_probe):
    """Acceptance criterion 4: over 90s goes to YouTube; Instagram is skipped with a reason."""
    fake_probe.info = probe_info(duration=95)
    v = _downloaded(tmp_db, tmp_path, "v95")
    assert transform.process(tmp_db, v, LIMITS) == "ready"
    row = db.get_video(tmp_db, "v95")
    assert row["status"] == "ready"
    assert row["duration_s"] == 95 and row["width"] == 1080 and row["height"] == 1920
    assert row["yt_status"] == "queued"
    assert row["ig_status"] == "skipped" and row["ig_scheduled_for"] is None
    assert "Instagram" in row["ig_error"] and "95s" in row["ig_error"]


def test_process_200s_skipped_on_both(tmp_db, tmp_path, fake_probe):
    fake_probe.info = probe_info(duration=200)
    v = _downloaded(tmp_db, tmp_path, "v200")
    assert transform.process(tmp_db, v, LIMITS) == "skipped"
    row = db.get_video(tmp_db, "v200")
    assert row["status"] == "skipped" and "200s" in row["status_reason"]
    assert row["yt_status"] == "skipped" and row["ig_status"] == "skipped"
    assert row["yt_error"] == row["status_reason"]


def test_process_short_video_ready_on_both(tmp_db, tmp_path, fake_probe):
    v = _downloaded(tmp_db, tmp_path, "v30")
    assert transform.process(tmp_db, v, LIMITS) == "ready"
    row = db.get_video(tmp_db, "v30")
    assert (row["status"], row["yt_status"], row["ig_status"]) == ("ready", "queued", "queued")


def test_process_missing_file_goes_back_to_new(tmp_db, tmp_path, fake_probe):
    v = add_video(tmp_db, "gone", status="downloaded", local_path=tmp_path / "nope.mp4", duration_s=None)
    assert transform.process(tmp_db, v, LIMITS) == "new"
    row = db.get_video(tmp_db, "gone")
    assert row["status"] == "new" and row["local_path"] is None


def test_process_reencodes_when_needed(tmp_db, tmp_path, fake_probe, monkeypatch):
    calls = []
    fake_probe.info = probe_info(vcodec="hevc")
    monkeypatch.setattr(transform, "reencode", lambda src, dst: (calls.append((src, dst)), dst.write_bytes(b"\x01")))
    v = _downloaded(tmp_db, tmp_path, "hevc")
    assert transform.process(tmp_db, v, LIMITS) == "ready"
    assert len(calls) == 1 and calls[0][1].name == "hevc.norm.mp4"
    assert (tmp_path / "hevc.mp4").read_bytes() == b"\x01"
