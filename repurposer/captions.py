"""Caption and title rendering from templates, plus the sponsored-content keyword check.

Templates support: {caption} {caption_first_line} {caption_no_hashtags} {yt_hashtags} {ig_hashtags}
{hashtags} {tiktok_url} {date}. Unknown placeholders are left untouched rather than crashing.
"""
from __future__ import annotations

import re
from typing import Any

YT_TITLE_MAX = 100
YT_DESCRIPTION_MAX = 5000
IG_CAPTION_MAX = 2200
_HASHTAG_RE = re.compile(r"(?<!\w)#[\wÀ-￿]+")


def first_line(caption: str | None) -> str:
    text = (caption or "").strip()
    if not text:
        return ""
    line = text.splitlines()[0].strip()
    return line


def strip_hashtags(caption: str | None) -> str:
    text = _HASHTAG_RE.sub("", caption or "")
    return re.sub(r"[ \t]{2,}", " ", text).strip()


def _fmt(template: str, values: dict[str, str]) -> str:
    def repl(m: re.Match[str]) -> str:
        key = m.group(1)
        return values.get(key, m.group(0))
    return re.sub(r"\{(\w+)\}", repl, template or "")


def template_values(video: dict[str, Any], yt_hashtags: list[str], ig_hashtags: list[str]) -> dict[str, str]:
    caption = (video.get("tiktok_caption") or "").strip()
    return {
        "caption": caption,
        "caption_first_line": first_line(caption),
        "caption_no_hashtags": strip_hashtags(caption),
        "yt_hashtags": " ".join(yt_hashtags),
        "ig_hashtags": " ".join(ig_hashtags),
        "hashtags": " ".join(yt_hashtags or ig_hashtags),
        "tiktok_url": video.get("tiktok_url") or "",
        "date": (video.get("published_at") or "")[:10],
    }


def _ensure_shorts_tag(title: str, description: str, hashtags: list[str]) -> tuple[str, str]:
    """#Shorts must appear in the title or description to land on the Shorts shelf."""
    if any(h.lower() == "#shorts" for h in hashtags):
        if "#shorts" not in title.lower() and "#shorts" not in description.lower():
            description = (description.rstrip() + "\n\n#Shorts").strip()
    return title, description


def youtube_snippet(video: dict[str, Any], workflow: dict[str, Any], override: dict[str, Any] | None = None) -> tuple[str, str]:
    """Returns (title, description). Per-video values win over the template; overrides.yaml wins over both."""
    override = override or {}
    hashtags = list(workflow.get("hashtags") or [])
    values = template_values(video, hashtags, [])
    title = override.get("yt_title") or video.get("yt_title") or _fmt(workflow.get("title_template") or "{caption_first_line}", values)
    description = override.get("yt_description") or video.get("yt_description") or _fmt(
        workflow.get("caption_template") or "{caption}\n\n{yt_hashtags}", values)
    title = re.sub(r"\s+", " ", title).strip() or f"TikTok {video.get('tiktok_id', '')}".strip()
    # YouTube rejects < and > in titles.
    title = title.replace("<", "").replace(">", "")
    if len(title) > YT_TITLE_MAX:
        title = title[: YT_TITLE_MAX - 1].rstrip() + "…"
    title, description = _ensure_shorts_tag(title, description, hashtags)
    if len(description) > YT_DESCRIPTION_MAX:
        description = description[:YT_DESCRIPTION_MAX]
    return title, description


def instagram_caption(video: dict[str, Any], workflow: dict[str, Any], override: dict[str, Any] | None = None) -> str:
    override = override or {}
    hashtags = list(workflow.get("hashtags") or [])
    values = template_values(video, [], hashtags)
    caption = override.get("ig_caption") or video.get("ig_caption") or _fmt(
        workflow.get("caption_template") or "{caption}\n.\n.\n{ig_hashtags}", values)
    caption = caption.strip()
    if len(caption) > IG_CAPTION_MAX:
        caption = caption[:IG_CAPTION_MAX]
    return caption


def matches_exclusion(caption: str | None, keywords: list[str]) -> str | None:
    """Return the first keyword found in the caption (case-insensitive), or None."""
    text = (caption or "").lower()
    for kw in keywords or []:
        k = str(kw).strip().lower()
        if k and k in text:
            return str(kw)
    return None
