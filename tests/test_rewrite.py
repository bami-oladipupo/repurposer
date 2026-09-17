"""Caption rewriting with a fake Anthropic client. Never touches the network."""
from types import SimpleNamespace

import pytest

from repurposer import db, rewrite


class FakeMessages:
    def __init__(self, outputs, stop_reason="end_turn"):
        self.outputs = list(outputs)
        self.stop_reason = stop_reason
        self.calls = []

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        schema = kwargs["output_format"]
        payload = self.outputs.pop(0)
        return SimpleNamespace(stop_reason=self.stop_reason, parsed_output=schema(**payload) if payload else None,
                               stop_details=SimpleNamespace(category="other"))


class FakeClient:
    def __init__(self, outputs, stop_reason="end_turn"):
        self.messages = FakeMessages(outputs, stop_reason)


@pytest.fixture
def rewrite_on(tmp_db, cfg):
    conn = tmp_db
    for plat in ("youtube", "instagram"):
        wf = db.get_workflow(conn, plat)
        extra = dict(wf["extra"]); extra["rewrite"] = True
        with db.tx(conn):
            db.save_workflow(conn, plat, enabled=1, extra=extra)
    return conn, cfg


def test_rewrite_youtube_appends_hashtags_and_strips_noise(rewrite_on, make_video, monkeypatch):
    conn, cfg = rewrite_on
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    v = make_video(conn, "v1", caption="What does a BA actually do #ba")
    fake = FakeClient([{"title": "What a business analyst actually does — daily", "description": "Short answer.\n#nope"}])
    res = rewrite.rewrite_video(conn, v, cfg, platforms=["youtube"], cl=fake)
    assert res["error"] is None and res["done"] == ["youtube"]
    row = db.get_video(conn, "v1")
    assert row["yt_title"] == "What a business analyst actually does , daily"
    assert row["yt_description"].startswith("Short answer.") and "#nope" not in row["yt_description"]
    assert "#Shorts" in row["yt_description"]  # workflow hashtags appended
    assert row["rewrite_status"] == "done" and row["rewritten_at"]
    call = fake.messages.calls[0]
    assert call["model"] == "claude-opus-5" and call["output_config"] == {"effort": "medium"}
    assert "voice guide" in call["system"][0]["text"].lower()


def test_rewrite_skips_platforms_with_text_unless_forced(rewrite_on, make_video, monkeypatch):
    conn, cfg = rewrite_on
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    v = make_video(conn, "v2", caption="x")
    with db.tx(conn):
        db.update_video(conn, "v2", yt_title="Mine", yt_description="Mine too")
    v = db.get_video(conn, "v2")
    fake = FakeClient([{"caption": "IG text"}])
    res = rewrite.rewrite_video(conn, v, cfg, cl=fake)
    assert res["done"] == ["instagram"]
    assert db.get_video(conn, "v2")["yt_title"] == "Mine"
    fake2 = FakeClient([{"title": "New", "description": "New d"}, {"caption": "IG again"}])
    res = rewrite.rewrite_video(conn, db.get_video(conn, "v2"), cfg, force=True, cl=fake2)
    assert set(res["done"]) == {"youtube", "instagram"}
    assert db.get_video(conn, "v2")["yt_title"] == "New"


def test_refusal_and_exception_are_recorded_not_raised(rewrite_on, make_video, monkeypatch):
    conn, cfg = rewrite_on
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    v = make_video(conn, "v3", caption="x")
    res = rewrite.rewrite_video(conn, v, cfg, platforms=["youtube"], cl=FakeClient([{"title": "t", "description": "d"}], stop_reason="refusal"))
    assert res["error"] and "declined" in res["error"]
    row = db.get_video(conn, "v3")
    assert row["rewrite_status"] == "failed" and row["yt_title"] is None

    class Boom:
        class messages:
            @staticmethod
            def parse(**kw):
                raise ValueError("kaboom")
    res = rewrite.rewrite_video(conn, db.get_video(conn, "v3"), cfg, platforms=["youtube"], cl=Boom())
    assert "ValueError: kaboom" in res["error"]


def test_run_without_api_key_alerts_once_and_calls_nothing(rewrite_on, make_video, monkeypatch):
    conn, cfg = rewrite_on
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    make_video(conn, "v4", caption="x")
    stats = rewrite.run(conn, cfg)
    assert "ANTHROPIC_API_KEY" in stats["alert"] and stats["rewritten"] == 0
    assert db.get_video(conn, "v4")["rewrite_status"] is None  # nothing marked, retried once the key exists


def test_run_rewrites_pending_only_and_skips_held(rewrite_on, make_video, monkeypatch):
    conn, cfg = rewrite_on
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    make_video(conn, "a", caption="x")
    make_video(conn, "b", caption="y", status="held")
    make_video(conn, "c", caption="z")
    with db.tx(conn):
        db.update_video(conn, "c", rewrite_status="done", yt_title="C", yt_description="c", ig_caption="c ig")
    outputs = [{"title": "A", "description": "a"}, {"caption": "a ig"}]
    fake = FakeClient(outputs)
    monkeypatch.setattr(rewrite, "client", lambda: fake)
    stats = rewrite.run(conn, cfg)
    assert stats["rewritten"] == 1 and stats["failed"] == [] and stats["alert"] is None
    assert db.get_video(conn, "a")["rewrite_status"] == "done"
    assert db.get_video(conn, "b")["rewrite_status"] is None
    assert len(fake.messages.calls) == 2


def test_run_is_noop_when_no_workflow_has_rewrite(tmp_db, make_video, monkeypatch, cfg):
    conn = tmp_db
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    make_video(conn, "a", caption="x")
    monkeypatch.setattr(rewrite, "client", lambda: (_ for _ in ()).throw(AssertionError("must not be called")))
    assert rewrite.run(conn, cfg)["platforms"] == []


def test_run_picks_up_platform_enabled_after_first_rewrite(rewrite_on, make_video, monkeypatch):
    """A video rewritten while only YouTube had rewriting on gets its Instagram caption once Instagram is enabled."""
    conn, cfg = rewrite_on
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    make_video(conn, "a", caption="x")
    with db.tx(conn):
        db.update_video(conn, "a", rewrite_status="done", yt_title="A", yt_description="a")
    fake = FakeClient([{"caption": "a ig"}])
    monkeypatch.setattr(rewrite, "client", lambda: fake)
    stats = rewrite.run(conn, cfg)
    assert stats["rewritten"] == 1 and stats["failed"] == []
    row = db.get_video(conn, "a")
    assert row["yt_title"] == "A" and row["ig_caption"].startswith("a ig")
    assert len(fake.messages.calls) == 1  # YouTube text untouched, only Instagram called
    # Second run: nothing left to do.
    assert rewrite.run(conn, cfg)["rewritten"] == 0
