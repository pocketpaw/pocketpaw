# test_storage_bootstrap.py — ART-4 cloud upload/blob-storage boot guard.
#
# Locks verify_cloud_storage_backend()'s warn-then-error behavior: no-op off
# cloud and on s3; WARN (no raise) when cloud + a non-s3 adapter; RAISE under
# POCKETPAW_REQUIRE_S3_IN_CLOUD. is_multi_tenant_cloud() is monkeypatched so the
# guard's cloud branch is exercised without standing up a real cloud DB.
"""Tests for the cloud upload-backend boot guard."""

from __future__ import annotations

import pytest
from pocketpaw_ee.cloud.uploads import bootstrap


def _force_cloud(monkeypatch: pytest.MonkeyPatch, *, on: bool) -> None:
    monkeypatch.setattr(bootstrap, "is_multi_tenant_cloud", lambda: on)


def test_noop_off_cloud(monkeypatch: pytest.MonkeyPatch) -> None:
    """OFF cloud, local uploads are correct — the guard must not warn or raise."""
    _force_cloud(monkeypatch, on=False)
    monkeypatch.setenv("POCKETPAW_UPLOAD_ADAPTER", "local")
    monkeypatch.setenv("POCKETPAW_REQUIRE_S3_IN_CLOUD", "1")  # ignored off cloud
    bootstrap.verify_cloud_storage_backend()  # no raise


def test_noop_when_s3(monkeypatch: pytest.MonkeyPatch) -> None:
    _force_cloud(monkeypatch, on=True)
    monkeypatch.setenv("POCKETPAW_UPLOAD_ADAPTER", "s3")
    bootstrap.verify_cloud_storage_backend()  # no raise


def test_warns_when_cloud_and_local(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _force_cloud(monkeypatch, on=True)
    monkeypatch.setenv("POCKETPAW_UPLOAD_ADAPTER", "local")
    monkeypatch.delenv("POCKETPAW_REQUIRE_S3_IN_CLOUD", raising=False)
    with caplog.at_level("WARNING"):
        bootstrap.verify_cloud_storage_backend()  # warns, does NOT raise
    assert "POCKETPAW_UPLOAD_ADAPTER" in caplog.text
    assert "expected 's3'" in caplog.text


def test_warns_when_adapter_unset(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Unset adapter defaults to 'local' — same loud warning."""
    _force_cloud(monkeypatch, on=True)
    monkeypatch.delenv("POCKETPAW_UPLOAD_ADAPTER", raising=False)
    monkeypatch.delenv("POCKETPAW_REQUIRE_S3_IN_CLOUD", raising=False)
    with caplog.at_level("WARNING"):
        bootstrap.verify_cloud_storage_backend()
    assert "POCKETPAW_UPLOAD_ADAPTER" in caplog.text


def test_raises_under_require_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    _force_cloud(monkeypatch, on=True)
    monkeypatch.setenv("POCKETPAW_UPLOAD_ADAPTER", "local")
    monkeypatch.setenv("POCKETPAW_REQUIRE_S3_IN_CLOUD", "1")
    with pytest.raises(RuntimeError, match="POCKETPAW_UPLOAD_ADAPTER"):
        bootstrap.verify_cloud_storage_backend()


def test_require_flag_satisfied_by_s3(monkeypatch: pytest.MonkeyPatch) -> None:
    """The hard-fail flag is moot when the adapter IS s3."""
    _force_cloud(monkeypatch, on=True)
    monkeypatch.setenv("POCKETPAW_UPLOAD_ADAPTER", "s3")
    monkeypatch.setenv("POCKETPAW_REQUIRE_S3_IN_CLOUD", "true")
    bootstrap.verify_cloud_storage_backend()  # no raise
