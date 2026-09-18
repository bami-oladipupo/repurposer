"""R2 staging with a fake S3 client. Never touches the network."""
from pathlib import Path

import pytest

from repurposer.publishers import staging


class FakeS3:
    def __init__(self, fail_upload=False):
        self.objects = {}
        self.deleted = []
        self.fail_upload = fail_upload

    def upload_fileobj(self, fh, bucket, key, ExtraArgs=None):
        if self.fail_upload:
            raise RuntimeError("boom")
        self.objects[(bucket, key)] = fh.read()

    def delete_object(self, Bucket, Key):
        self.deleted.append((Bucket, Key))
        self.objects.pop((Bucket, Key), None)

    def head_bucket(self, Bucket):
        return {}


@pytest.fixture
def r2_env(monkeypatch):
    for k, v in {"R2_ACCOUNT_ID": "acc", "R2_ACCESS_KEY_ID": "id", "R2_SECRET_ACCESS_KEY": "secret",
                 "R2_BUCKET": "bucket", "R2_PUBLIC_BASE_URL": "https://pub-x.r2.dev/"}.items():
        monkeypatch.setenv(k, v)


def test_stage_uploads_under_random_key_and_deletes_after(tmp_path: Path, r2_env, monkeypatch):
    f = tmp_path / "v.mp4"; f.write_bytes(b"video")
    s3 = FakeS3(); monkeypatch.setattr(staging, "client", lambda st: s3)
    with staging.stage(f) as url:
        assert url.startswith("https://pub-x.r2.dev/ig/") and url.endswith(".mp4")
        key = url.split("https://pub-x.r2.dev/", 1)[1]
        assert s3.objects[("bucket", key)] == b"video"
    assert s3.deleted == [("bucket", key)] and s3.objects == {}


def test_stage_deletes_even_when_body_raises(tmp_path: Path, r2_env, monkeypatch):
    f = tmp_path / "v.mp4"; f.write_bytes(b"video")
    s3 = FakeS3(); monkeypatch.setattr(staging, "client", lambda st: s3)
    with pytest.raises(ValueError):
        with staging.stage(f):
            raise ValueError("meta said no")
    assert len(s3.deleted) == 1 and s3.objects == {}


def test_stage_missing_config_is_a_clear_error(tmp_path: Path, monkeypatch):
    for k in staging.REQUIRED:
        monkeypatch.delenv(k, raising=False)
    f = tmp_path / "v.mp4"; f.write_bytes(b"video")
    with pytest.raises(staging.StagingError, match="R2_ACCOUNT_ID"):
        with staging.stage(f):
            pass


def test_upload_failure_is_a_staging_error(tmp_path: Path, r2_env, monkeypatch):
    f = tmp_path / "v.mp4"; f.write_bytes(b"video")
    monkeypatch.setattr(staging, "client", lambda st: FakeS3(fail_upload=True))
    with pytest.raises(staging.StagingError, match="upload to R2 failed"):
        with staging.stage(f):
            pass
