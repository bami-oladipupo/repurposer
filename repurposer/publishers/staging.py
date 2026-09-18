"""Stage a local video at a public URL for the minutes Meta needs to fetch it, then remove it.

Instagram's API (Instagram Login flow) does not accept uploaded bytes; it fetches the video from a
public URL. The Mac has no public address, so the file goes to a Cloudflare R2 bucket with public
read under a random key and is deleted as soon as the publish finishes, success or failure.
"""
from __future__ import annotations

import contextlib
import logging
import secrets
from pathlib import Path
from typing import Any, Iterator

from .. import config

log = logging.getLogger("repurposer.staging")

REQUIRED = ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET", "R2_PUBLIC_BASE_URL")


class StagingError(RuntimeError):
    pass


def settings() -> dict[str, str]:
    values = {k: config.env(k) for k in REQUIRED}
    missing = [k for k, v in values.items() if not v]
    if missing:
        raise StagingError(f"R2 staging not configured: {', '.join(missing)} missing from .env")
    return {k: str(v) for k, v in values.items()}


def client(st: dict[str, str]) -> Any:
    import boto3  # imported here so the rest of the app never needs it
    from botocore.config import Config

    return boto3.client(
        "s3",
        endpoint_url=f"https://{st['R2_ACCOUNT_ID']}.r2.cloudflarestorage.com",
        aws_access_key_id=st["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=st["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
        config=Config(signature_version="s3v4", retries={"max_attempts": 3}),
    )


@contextlib.contextmanager
def stage(path: Path, *, prefix: str = "ig") -> Iterator[str]:
    """Upload `path` under a random key and yield its public URL. The object is deleted on exit."""
    st = settings()
    key = f"{prefix}/{secrets.token_urlsafe(24)}{path.suffix}"
    s3 = client(st)
    try:
        with path.open("rb") as fh:
            s3.upload_fileobj(fh, st["R2_BUCKET"], key, ExtraArgs={"ContentType": "video/mp4"})
    except Exception as exc:  # noqa: BLE001
        raise StagingError(f"upload to R2 failed: {type(exc).__name__}: {exc}") from exc
    url = f"{st['R2_PUBLIC_BASE_URL'].rstrip('/')}/{key}"
    log.info("staged %s at %s", path.name, url)
    try:
        yield url
    finally:
        try:
            s3.delete_object(Bucket=st["R2_BUCKET"], Key=key)
            log.info("removed staged object %s", key)
        except Exception as exc:  # noqa: BLE001 - surfaced, never hidden
            log.error("could not delete staged object %s: %s", key, exc)


def check() -> tuple[bool, str]:
    """Used by the Connections page: can we reach the bucket with these credentials?"""
    try:
        st = settings()
        client(st).head_bucket(Bucket=st["R2_BUCKET"])
        return True, f"R2 bucket {st['R2_BUCKET']} reachable"
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"
