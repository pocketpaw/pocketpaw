"""Tests for the upload adapter factory."""

from __future__ import annotations

from pathlib import Path

import pytest

from pocketpaw.uploads.factory import build_adapter
from pocketpaw.uploads.local import LocalStorageAdapter


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    """Stub out the factory's defensive ``load_dotenv`` so tests don't see
    S3 config leaking in from the dev machine's ``.env``. Also clear the
    upload-related env vars so each test starts from a clean slate.
    """
    import pocketpaw.uploads.factory as factory_module

    # load_dotenv is imported lazily inside build_adapter; patch the symbol
    # in dotenv itself so the lazy import inside the factory is a no-op.
    try:
        import dotenv

        monkeypatch.setattr(dotenv, "load_dotenv", lambda *a, **kw: False)
    except ImportError:
        pass
    _ = factory_module  # keep the import live so monkeypatch has scope
    for var in (
        "POCKETPAW_UPLOAD_ADAPTER",
        "S3_PRIVATE_BUCKET",
        "S3_BUCKET",
        "S3_REGION",
        "S3_ENDPOINT",
        "S3_ACCESS_KEY_ID",
        "S3_SECRET_ACCESS_KEY",
    ):
        monkeypatch.delenv(var, raising=False)


def test_defaults_to_local(tmp_path: Path):
    adapter = build_adapter(tmp_path)
    assert isinstance(adapter, LocalStorageAdapter)


def test_explicit_local(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("POCKETPAW_UPLOAD_ADAPTER", "local")
    adapter = build_adapter(tmp_path)
    assert isinstance(adapter, LocalStorageAdapter)


def test_s3_requires_bucket(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("POCKETPAW_UPLOAD_ADAPTER", "s3")
    with pytest.raises(RuntimeError, match="S3_PRIVATE_BUCKET"):
        build_adapter(tmp_path)


def test_s3_mode_builds_s3_adapter(tmp_path: Path, monkeypatch):
    # Skip if boto3 isn't available (OSS installs without enterprise extra).
    pytest.importorskip("boto3")
    from pocketpaw.uploads.s3 import S3StorageAdapter

    monkeypatch.setenv("POCKETPAW_UPLOAD_ADAPTER", "s3")
    monkeypatch.setenv("S3_PRIVATE_BUCKET", "test-bucket")
    monkeypatch.setenv("S3_REGION", "us-east-1")
    monkeypatch.setenv("S3_ACCESS_KEY_ID", "AKIA_TEST")
    monkeypatch.setenv("S3_SECRET_ACCESS_KEY", "test-secret")
    adapter = build_adapter(tmp_path)
    assert isinstance(adapter, S3StorageAdapter)
