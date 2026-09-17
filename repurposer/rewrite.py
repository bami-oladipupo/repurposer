"""Rewrite TikTok captions into platform-ready text with Claude.

Runs once per video (status 'done' or 'failed'), writes into the per-video columns that the UI
already treats as overrides, so the editor shows the generated text and Bami can still change it.
Every failure is recorded in rewrite_error and surfaced in the run summary. Templates remain
the fallback, so a failed rewrite never blocks a publish.
"""
from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from . import captions, config, db
from .config import PLATFORMS, PREFIX
from .timeutil import iso, utcnow

log = logging.getLogger("repurposer.rewrite")

VOICE_PATH = config.ROOT / "voice.md"
DEFAULT_MODEL = "claude-opus-5"
DEFAULT_EFFORT = "medium"
MAX_PER_RUN = 20


class RewriteError(RuntimeError):
    pass


class YouTubeText(BaseModel):
    title: str = Field(description="YouTube title, under 100 characters, keyword first, no hashtags")
    description: str = Field(description="Two to four short sentences for the YouTube description, no hashtags")


class InstagramText(BaseModel):
    caption: str = Field(description="Instagram Reel caption, keyword first, no hashtags")


def settings(cfg: dict[str, Any]) -> dict[str, Any]:
    rw = cfg.get("rewrite") or {}
    return {"model": rw.get("model") or DEFAULT_MODEL, "effort": rw.get("effort") or DEFAULT_EFFORT,
            "max_per_run": int(rw.get("max_per_run") or MAX_PER_RUN)}


def load_voice() -> str:
    if not VOICE_PATH.exists():
        raise RewriteError(f"{VOICE_PATH.name} is missing; restore it from the repo")
    return VOICE_PATH.read_text(encoding="utf-8")


def client():
    if not config.env("ANTHROPIC_API_KEY"):
        raise RewriteError("ANTHROPIC_API_KEY missing from .env; rewrite skipped, templates used")
    import anthropic

    return anthropic.Anthropic()


def _system(voice: str, platform: str) -> str:
    target = "YouTube Shorts" if platform == "youtube" else "Instagram Reels"
    return (
        f"You rewrite a creator's TikTok caption into text for the same video reposted to {target}. "
        "Follow the voice guide exactly. Use only facts present in the source caption. "
        "Never add hashtags, emoji, or em dashes. Output only the fields requested.\n\n"
        f"<voice_guide>\n{voice}\n</voice_guide>"
    )


def _user(video: dict[str, Any]) -> str:
    caption = (video.get("tiktok_caption") or "").strip() or "(no caption)"
    duration = video.get("duration_s")
    dur = f"{int(duration)} seconds" if duration else "unknown length"
    return (
        f"Source TikTok caption:\n<caption>\n{caption}\n</caption>\n"
        f"Video length: {dur}. Posted: {(video.get('published_at') or '')[:10]}.\n"
        "Write the fields now."
    )


def _call(cl, model: str, effort: str, system: str, user: str, schema: type[BaseModel]) -> BaseModel:
    import anthropic

    try:
        resp = cl.messages.parse(
            model=model,
            max_tokens=2000,
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user}],
            output_format=schema,
            output_config={"effort": effort},
        )
    except anthropic.AuthenticationError as exc:
        raise RewriteError(f"Anthropic API key rejected: {exc.message}") from exc
    except anthropic.RateLimitError as exc:
        raise RewriteError(f"Anthropic rate limit: {exc.message}") from exc
    except anthropic.APIStatusError as exc:
        raise RewriteError(f"Anthropic API error {exc.status_code}: {exc.message}") from exc
    except anthropic.APIConnectionError as exc:
        raise RewriteError(f"could not reach the Anthropic API: {exc}") from exc
    if resp.stop_reason == "refusal":
        detail = getattr(resp, "stop_details", None)
        raise RewriteError(f"Claude declined this caption ({getattr(detail, 'category', None) or 'unspecified'})")
    if resp.parsed_output is None:
        raise RewriteError(f"no structured output returned (stop_reason {resp.stop_reason})")
    return resp.parsed_output


def _clean(text: str) -> str:
    text = text.replace("—", ", ").replace("–", "-").strip()
    return captions.strip_hashtags(text)


def rewrite_youtube(video: dict[str, Any], workflow: dict[str, Any], cfg: dict[str, Any], *, cl=None) -> tuple[str, str]:
    st = settings(cfg)
    out = _call(cl or client(), st["model"], st["effort"], _system(load_voice(), "youtube"), _user(video), YouTubeText)
    title = _clean(out.title)[: captions.YT_TITLE_MAX]
    hashtags = " ".join(workflow.get("hashtags") or [])
    description = _clean(out.description)
    if hashtags:
        description = f"{description}\n\n{hashtags}"
    return title, description


def rewrite_instagram(video: dict[str, Any], workflow: dict[str, Any], cfg: dict[str, Any], *, cl=None) -> str:
    st = settings(cfg)
    out = _call(cl or client(), st["model"], st["effort"], _system(load_voice(), "instagram"), _user(video), InstagramText)
    caption = _clean(out.caption)
    hashtags = " ".join(workflow.get("hashtags") or [])
    if hashtags:
        caption = f"{caption}\n.\n.\n{hashtags}"
    return caption[: captions.IG_CAPTION_MAX]


def enabled_platforms(conn: sqlite3.Connection) -> list[str]:
    return [p for p, wf in db.all_workflows(conn).items() if wf["enabled"] and (wf.get("extra") or {}).get("rewrite")]


def rewrite_video(conn: sqlite3.Connection, video: dict[str, Any], cfg: dict[str, Any], *, platforms: list[str] | None = None,
                  force: bool = False, cl=None) -> dict[str, Any]:
    """Rewrite for each enabled platform that has no text yet (or all of them when forced).

    Returns {'done': [platforms], 'error': str|None}. Writes rewrite_status/rewrite_error either way.
    """
    platforms = platforms or enabled_platforms(conn)
    wfs = db.all_workflows(conn)
    done: list[str] = []
    fields: dict[str, Any] = {}
    error: str | None = None
    try:
        cl = cl or client()
        for plat in platforms:
            wf = wfs.get(plat)
            if wf is None:
                continue
            px = PREFIX[plat]
            if video.get(f"{px}_status") == "uploaded":
                continue
            if plat == "youtube":
                if not force and video.get("yt_title") and video.get("yt_description"):
                    continue
                title, description = rewrite_youtube(video, wf, cfg, cl=cl)
                fields.update(yt_title=title, yt_description=description)
            else:
                if not force and video.get("ig_caption"):
                    continue
                fields["ig_caption"] = rewrite_instagram(video, wf, cfg, cl=cl)
            done.append(plat)
    except RewriteError as exc:
        error = str(exc)
    except Exception as exc:  # noqa: BLE001 - recorded, never swallowed
        log.exception("rewrite crashed for %s", video["tiktok_id"])
        error = f"{type(exc).__name__}: {exc}"
    fields["rewrite_status"] = "failed" if error else "done"
    fields["rewrite_error"] = error[:2000] if error else None
    fields["rewritten_at"] = iso(utcnow())
    db.update_video(conn, video["tiktok_id"], **fields)
    return {"done": done, "error": error}


_MISSING_TEXT = {
    "youtube": "((yt_title IS NULL OR yt_description IS NULL) AND yt_status NOT IN ('uploaded','skipped','cancelled'))",
    "instagram": "(ig_caption IS NULL AND ig_status NOT IN ('uploaded','skipped','cancelled'))",
}


def pending(conn: sqlite3.Connection, limit: int, platforms: list[str] | None = None) -> list[dict[str, Any]]:
    """Videos that still need a rewrite: active, not held, and either never attempted or already rewritten
    for one platform but missing text for another that has rewriting on (a workflow enabled later).
    Backfill videos without a slot are left alone so a capped catalogue is not rewritten for nothing.
    Failed rewrites are not retried automatically."""
    platforms = platforms if platforms is not None else enabled_platforms(conn)
    missing = " OR ".join(_MISSING_TEXT[p] for p in platforms if p in _MISSING_TEXT) or "0"
    return db.rows(
        conn,
        f"""SELECT * FROM videos WHERE status IN ('new','downloaded','ready')
           AND (origin = 'new' OR yt_scheduled_for IS NOT NULL OR ig_scheduled_for IS NOT NULL)
           AND (rewrite_status IS NULL OR (rewrite_status = 'done' AND ({missing})))
           ORDER BY CASE WHEN origin='new' THEN 0 ELSE 1 END,
                    COALESCE(yt_scheduled_for, ig_scheduled_for, '9999') ASC LIMIT ?""",
        (limit,),
    )


def run(conn: sqlite3.Connection, cfg: dict[str, Any]) -> dict[str, Any]:
    """Worker stage. Returns counts plus per-video errors and a single config-level alert."""
    stats: dict[str, Any] = {"rewritten": 0, "failed": [], "alert": None, "platforms": enabled_platforms(conn)}
    if not stats["platforms"]:
        return stats
    if not config.env("ANTHROPIC_API_KEY"):
        stats["alert"] = "ANTHROPIC_API_KEY missing from .env; rewriting is on but skipped, templates used"
        return stats
    limit = settings(cfg)["max_per_run"]
    cl = None
    for v in pending(conn, limit, stats["platforms"]):
        with db.tx(conn):
            res = rewrite_video(conn, v, cfg, platforms=stats["platforms"], cl=cl)
        if res["error"]:
            stats["failed"].append((v["tiktok_id"], res["error"]))
            if "ANTHROPIC_API_KEY" in res["error"] or "rejected" in res["error"]:
                stats["alert"] = res["error"]
                break
        elif res["done"]:
            stats["rewritten"] += 1
    return stats
