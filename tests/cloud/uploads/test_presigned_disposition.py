# test_presigned_disposition.py — ART-4 security follow-up.
#
# Locks EEUploadService.presigned_get's Content-Disposition policy: non-inline
# mimes (the HTML/SVG/JS the deliver_artifact path can now store) are served as
# `attachment` so a presigned S3 GET downloads them instead of rendering active
# content on the storage origin; inline-safe mimes (images, pdf, plain text)
# keep the byte-identical None (adapter/object default → inline). A spy adapter
# records the disposition the service forwards, so the policy is observable
# without a real S3 bucket.
"""Tests for the presigned-download Content-Disposition gate."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from pocketpaw_ee.cloud.uploads.service import EEUploadService

pytestmark = pytest.mark.asyncio


class _SpyAdapter:
    """Captures the response_content_disposition the service forwards and
    returns a fake URL (so the policy is observable without S3)."""

    def __init__(self) -> None:
        self.dispositions: list[str | None] = []

    async def presigned_get(self, key, ttl_seconds, response_content_disposition=None):
        self.dispositions.append(response_content_disposition)
        return f"https://signed.example/{key}"


class _FakeMeta:
    def __init__(self, rec) -> None:
        self._rec = rec

    async def get_scoped(self, file_id, workspace):
        return self._rec


async def _disposition_for(mime: str, filename: str = "file.bin") -> str | None:
    rec = SimpleNamespace(
        id="f1",
        mime=mime,
        filename=filename,
        owner_id="u1",
        storage_key="k1",
        chat_id=None,
    )
    spy = _SpyAdapter()
    svc = EEUploadService.__new__(EEUploadService)
    svc._adapter = spy
    svc._meta = _FakeMeta(rec)
    svc._is_chat_member = None
    svc._is_workspace_admin = None

    got_rec, url = await svc.presigned_get("f1", "u1", "w1", 300)
    assert url == "https://signed.example/k1"
    assert got_rec is rec
    return spy.dispositions[-1]


@pytest.mark.parametrize(
    "mime",
    [
        "text/html",
        "image/svg+xml",
        "text/javascript",
        "application/zip",
        "application/octet-stream",
    ],
)
async def test_noninline_mimes_forced_attachment(mime: str) -> None:
    disp = await _disposition_for(mime, filename="evil.bin")
    assert disp is not None
    assert disp.startswith("attachment;")


@pytest.mark.parametrize(
    "mime",
    [
        "image/png",
        "image/jpeg",
        "image/gif",
        "image/webp",
        "application/pdf",
        "text/plain",
        "text/markdown",
        "text/csv",
    ],
)
async def test_inline_mimes_unchanged(mime: str) -> None:
    # None ⇒ adapter/object default ⇒ inline, byte-identical to before the fix.
    assert await _disposition_for(mime) is None


async def test_disposition_filename_has_no_header_breakers() -> None:
    disp = await _disposition_for("text/html", filename='a"b\r\n; drop.html')
    assert disp is not None
    assert "\r" not in disp and "\n" not in disp
    # the quote that would close the filename token early is stripped
    assert disp.count('"') == 2
