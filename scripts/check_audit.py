"""Check whether the YouTube API compliance audit is live for this project.

Google forces every videos.insert from an unaudited project to private, whatever privacyStatus
you send. The only reliable test is to upload something and read back the status Google kept.

    .venv/bin/python scripts/check_audit.py

Uploads a 2-second black clip as UNLISTED (title "Repurposer audit check"), waits, then reads
status.privacyStatus back:
  unlisted  -> audit is live; the app's "public" setting will now be honoured
  private   -> not live yet; try again later
Delete the test clip in YouTube Studio afterwards (the token has no delete scope).
Costs about 1,600 of the 10,000 daily quota units.
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from repurposer.publishers import youtube as yt  # noqa: E402

SCRATCH = Path(__file__).resolve().parent.parent / "media" / "_audit_check.mp4"


def make_clip() -> Path:
    if SCRATCH.exists():
        return SCRATCH
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-f", "lavfi", "-i", "color=c=black:s=1080x1920:d=2",
        "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
        "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(SCRATCH),
    ]
    subprocess.run(cmd, check=True)
    return SCRATCH


def main() -> int:
    creds = yt.load_credentials()
    if creds is None:
        sys.exit("YouTube is not connected; use Reconnect in the web UI first")
    svc = yt.service(creds)
    chan = svc.channels().list(part="snippet", mine=True).execute()["items"][0]
    print(f"Connected channel: {chan['snippet']['title']} ({chan['id']})")

    clip = make_clip()
    video_id = yt.upload(
        clip, "Repurposer audit check", "Temporary test upload. Safe to delete.",
        {"privacy": "unlisted", "category_id": "27", "made_for_kids": False}, [],
    )
    print(f"Uploaded test clip as unlisted: https://youtu.be/{video_id}")

    status = None
    for _ in range(6):
        time.sleep(10)
        resp = svc.videos().list(part="status", id=video_id).execute()
        status = resp["items"][0]["status"]
        if status.get("uploadStatus") == "processed":
            break
    assert status is not None
    kept = status.get("privacyStatus")
    print(f"uploadStatus={status.get('uploadStatus')} privacyStatus={kept}")
    if kept == "unlisted":
        print("RESULT: audit is LIVE. Uploads are no longer forced to private.")
        rc = 0
    elif kept == "private":
        print("RESULT: audit NOT live yet. Google still forced the upload to private. Retry later.")
        rc = 1
    else:
        print(f"RESULT: unexpected privacyStatus {kept!r}; inspect in YouTube Studio.")
        rc = 2
    print(f"Delete the test clip in YouTube Studio: https://studio.youtube.com/video/{video_id}/edit")
    return rc


if __name__ == "__main__":
    sys.exit(main())
