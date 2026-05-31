from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

PNG = b"\x89PNG\r\n\x1a\n" + b"body"


@pytest.fixture()
def client(tmp_path: Path, monkeypatch):
    """Build an app with the uploads router pointed at a tmp dir."""
    # Patch module-level globals BEFORE importing the router
    import pocketpaw.api.v1.uploads as uploads_module

    root = tmp_path / "u"
    root.mkdir()

    # Rebuild module-level service against tmp dirs
    from pocketpaw.uploads.config import UploadSettings
    from pocketpaw.uploads.file_store import JSONLFileStore
    from pocketpaw.uploads.local import LocalStorageAdapter
    from pocketpaw.uploads.service import UploadService

    test_cfg = UploadSettings(local_root=root)
    test_adapter = LocalStorageAdapter(root=root)
    test_meta = JSONLFileStore(path=root / "_idx.jsonl")
    test_svc = UploadService(adapter=test_adapter, meta=test_meta, cfg=test_cfg)

    monkeypatch.setattr(uploads_module, "_SVC", test_svc)

    app = FastAPI()
    app.include_router(uploads_module.router, prefix="/api/v1")
    return TestClient(app)


def test_upload_single_roundtrip(client: TestClient):
    r = client.post(
        "/api/v1/uploads",
        files=[("files", ("cat.png", PNG, "image/png"))],
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert len(data["uploaded"]) == 1
    assert data["uploaded"][0]["filename"] == "cat.png"
    assert data["uploaded"][0]["mime"] == "image/png"
    fid = data["uploaded"][0]["id"]

    r2 = client.get(f"/api/v1/uploads/{fid}")
    assert r2.status_code == 200
    assert r2.content == PNG
    assert r2.headers["content-type"].startswith("image/png")
    assert "inline" in r2.headers["content-disposition"]


def test_bulk_upload_partial_success(client: TestClient):
    r = client.post(
        "/api/v1/uploads",
        files=[
            ("files", ("good.png", PNG, "image/png")),
            ("files", ("bad.svg", b"<svg/>", "image/svg+xml")),
        ],
    )
    assert r.status_code == 200
    data = r.json()
    assert len(data["uploaded"]) == 1
    assert len(data["failed"]) == 1
    assert data["failed"][0]["code"] == "unsupported_mime"


def test_delete_then_get_not_found(client: TestClient):
    r = client.post(
        "/api/v1/uploads",
        files=[("files", ("cat.png", PNG, "image/png"))],
    )
    fid = r.json()["uploaded"][0]["id"]

    r2 = client.delete(f"/api/v1/uploads/{fid}")
    assert r2.status_code == 204

    r3 = client.get(f"/api/v1/uploads/{fid}")
    assert r3.status_code == 404


def test_get_missing_id_is_404(client: TestClient):
    r = client.get("/api/v1/uploads/nope")
    assert r.status_code == 404


def test_grant_returns_signed_url_that_fetches_bytes(client: TestClient, monkeypatch):
    # Pin the master token so the grant's HMAC key is deterministic for the
    # duration of the test. The dashboard middleware reads ``get_access_token``
    # from the same module, so stubbing it in both call sites keeps the
    # token-signing key and the verifier aligned.
    from pocketpaw import dashboard_auth
    from pocketpaw.api.v1 import uploads as uploads_module

    monkeypatch.setattr(uploads_module, "get_access_token", lambda: "test-secret")
    monkeypatch.setattr(dashboard_auth, "get_access_token", lambda: "test-secret")

    r = client.post(
        "/api/v1/uploads",
        files=[("files", ("cat.png", PNG, "image/png"))],
    )
    fid = r.json()["uploaded"][0]["id"]

    g = client.get(f"/api/v1/uploads/{fid}/grant")
    assert g.status_code == 200
    body = g.json()
    assert body["url"].startswith(f"/api/v1/uploads/{fid}?t=")
    assert body["expires_at"] > 0

    # The signed URL fetches bytes. The isolated TestClient app doesn't mount
    # AuthMiddleware, so middleware-level signature verification is exercised
    # separately in test_signing.py — here we just confirm the handler honors
    # the token query param without erroring.
    r2 = client.get(body["url"])
    assert r2.status_code == 200
    assert r2.content == PNG


def test_grant_missing_file_is_404(client: TestClient):
    r = client.get("/api/v1/uploads/does-not-exist/grant")
    assert r.status_code == 404


def test_grant_returns_adapter_presigned_url_when_available(client: TestClient, monkeypatch):
    """When the storage adapter returns a presigned URL (S3/GCS), the grant
    endpoint proxies it verbatim — no HMAC fallback, no signature query param.
    """
    from pocketpaw.api.v1 import uploads as uploads_module

    r = client.post(
        "/api/v1/uploads",
        files=[("files", ("cat.png", PNG, "image/png"))],
    )
    fid = r.json()["uploaded"][0]["id"]

    async def fake_presigned(self, file_id, requester_id, ttl_seconds):
        rec = self._meta.get(file_id)
        return rec, f"https://s3.example.com/bucket/{rec.storage_key}?X-Amz-Signature=abc"

    monkeypatch.setattr(type(uploads_module._SVC), "presigned_get", fake_presigned)

    g = client.get(f"/api/v1/uploads/{fid}/grant")
    assert g.status_code == 200
    body = g.json()
    assert body["url"].startswith("https://s3.example.com/bucket/")
    assert "X-Amz-Signature" in body["url"]
    assert body["expires_at"] > 0


def test_docx_gets_attachment_disposition(client: TestClient):
    docx = b"PK\x03\x04" + b"rest"
    r = client.post(
        "/api/v1/uploads",
        files=[
            (
                "files",
                (
                    "doc.docx",
                    docx,
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                ),
            )
        ],
    )
    fid = r.json()["uploaded"][0]["id"]
    r2 = client.get(f"/api/v1/uploads/{fid}")
    assert r2.status_code == 200
    assert "attachment" in r2.headers["content-disposition"]
