"""ffprobe / ffmpeg normalisation and the hard format rules.

Re-encode only when the file is not already H.264 + AAC in MP4. Reject (status 'skipped') anything
over the duration cap or not vertical. Between the Instagram cap and the overall cap, only
Instagram is skipped; YouTube still goes ahead.
"""
from __future__ import annotations

import json
import logging
import shutil
import sqlite3
import subprocess
from pathlib import Path
from typing import Any

from . import actions, db
from .config import PREFIX

log = logging.getLogger("repurposer.transform")

ASPECT_MIN, ASPECT_MAX = 0.50, 0.60  # 9:16 = 0.5625


class ProbeError(RuntimeError):
    pass


def probe(path: Path | str) -> dict[str, Any]:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise ProbeError("ffprobe not found on PATH; install ffmpeg")
    cmd = [ffprobe, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise ProbeError(f"ffprobe failed ({proc.returncode}): {proc.stderr.strip()[:500]}")
    data = json.loads(proc.stdout or "{}")
    video = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), None)
    audio = next((s for s in data.get("streams", []) if s.get("codec_type") == "audio"), None)
    if video is None:
        raise ProbeError("no video stream found")
    fmt = data.get("format", {})
    duration = float(fmt.get("duration") or video.get("duration") or 0)
    width, height = int(video.get("width") or 0), int(video.get("height") or 0)
    # Respect rotation metadata so a sideways-stored vertical is still treated as vertical.
    rotation = 0
    for sd in video.get("side_data_list", []) or []:
        if "rotation" in sd:
            rotation = int(abs(float(sd["rotation"])))
    tags = video.get("tags") or {}
    if "rotate" in tags:
        rotation = int(abs(float(tags["rotate"])))
    if rotation in (90, 270):
        width, height = height, width
    return {
        "duration": duration,
        "width": width,
        "height": height,
        "vcodec": video.get("codec_name"),
        "acodec": audio.get("codec_name") if audio else None,
        "container": fmt.get("format_name", ""),
    }


def needs_reencode(info: dict[str, Any]) -> bool:
    if info.get("vcodec") != "h264":
        return True
    if info.get("acodec") not in (None, "aac"):
        return True
    return "mp4" not in (info.get("container") or "")


def reencode(src: Path, dst: Path) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise ProbeError("ffmpeg not found on PATH")
    cmd = [ffmpeg, "-y", "-i", str(src), "-c:v", "libx264", "-preset", "medium", "-crf", "18",
           "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(dst)]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise ProbeError(f"ffmpeg re-encode failed ({proc.returncode}): {proc.stderr.strip()[-800:]}")


def thumbnail(src: Path) -> Path | None:
    """Grab a frame at one second for the UI. Failure is logged, never fatal."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return None
    dst = src.with_suffix(".jpg")
    if dst.exists():
        return dst
    proc = subprocess.run([ffmpeg, "-y", "-ss", "1", "-i", str(src), "-frames:v", "1", "-vf", "scale=240:-2", "-q:v", "4", str(dst)],
                          capture_output=True, text=True)
    if proc.returncode != 0:
        log.warning("thumbnail failed for %s: %s", src.name, proc.stderr.strip()[-300:])
        return None
    return dst


def decide(info: dict[str, Any], limits: dict[str, Any]) -> dict[str, Any]:
    """Pure rule check. Returns {'reject': reason|None, 'ig_skip': reason|None}."""
    max_s = float(limits.get("max_duration_s", 180))
    ig_max = float(limits.get("ig_max_duration_s", 90))
    duration = float(info.get("duration") or 0)
    width, height = info.get("width") or 0, info.get("height") or 0
    if duration <= 0:
        return {"reject": "could not read duration", "ig_skip": None}
    if duration > max_s:
        return {"reject": f"duration {duration:.0f}s exceeds {max_s:.0f}s cap", "ig_skip": None}
    if not height or not (ASPECT_MIN <= width / height <= ASPECT_MAX):
        return {"reject": f"not vertical 9:16 ({width}x{height})", "ig_skip": None}
    ig_skip = f"duration {duration:.0f}s exceeds Instagram API cap of {ig_max:.0f}s" if duration > ig_max else None
    return {"reject": None, "ig_skip": ig_skip}


def process(conn: sqlite3.Connection, video: dict[str, Any], limits: dict[str, Any]) -> str:
    """Take a 'downloaded' row to 'ready' or 'skipped'. Returns the resulting status."""
    tiktok_id = video["tiktok_id"]
    path = Path(video["local_path"])
    if not path.exists():
        db.update_video(conn, tiktok_id, status="new", local_path=None, status_reason="file missing, will re-download")
        return "new"
    try:
        info = probe(path)
    except ProbeError as exc:
        if "no video stream" in str(exc):
            actions.skip(conn, tiktok_id, "photo post, no video stream")
            return "skipped"
        raise
    if needs_reencode(info):
        tmp = path.with_suffix(".norm.mp4")
        log.info("re-encoding %s (%s/%s in %s)", tiktok_id, info["vcodec"], info["acodec"], info["container"])
        reencode(path, tmp)
        tmp.replace(path)
        info = probe(path)
    verdict = decide(info, limits)
    thumbnail(path)
    db.update_video(conn, tiktok_id, duration_s=info["duration"], width=info["width"], height=info["height"])
    if verdict["reject"]:
        actions.skip(conn, tiktok_id, verdict["reject"])
        return "skipped"
    if verdict["ig_skip"] and (video.get("ig_status") not in {"uploaded", "skipped", "cancelled"}):
        db.update_video(conn, tiktok_id, **{f"{PREFIX['instagram']}_status": "skipped",
                                            f"{PREFIX['instagram']}_scheduled_for": None,
                                            f"{PREFIX['instagram']}_error": verdict["ig_skip"]})
    db.update_video(conn, tiktok_id, status="ready", status_reason=None)
    return "ready"
