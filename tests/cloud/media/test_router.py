# tests/cloud/media/test_router.py — the /media file list + serve + upload surface.
#
# The studio-media-s3 work routed the gallery through the storage swap
# (``media.storage`` → ``pocketpaw.uploads.build_adapter``): local disk in dev,
# S3 in a POCKETPAW_UPLOAD_ADAPTER=s3 deployment. These tests pin BOTH modes:
#   * LOCAL mode — a tmp-backed LocalStorageAdapter; asserts the existing
#     on-disk mtime listing, the generation-tracked EXCLUSION (the gallery
#     renders direct generations via history, so bare files would double the
#     tiles), upload (save edited image), and FileResponse serving.
#   * REMOTE (S3) mode — a fake adapter whose ``local_path`` is None; asserts
#     browse-based listing (with the timestamp-from-name ``modified``), streaming
#     serve, and upload keyed under "generated/".
#
# ``tracked_generation_filenames`` (imported from the studio service) is patched
# to a fixed set so the exclusion is asserted deterministically.
#
# Created 2026-08-17 (studio-real-backend; studio-media-s3): media router tests.

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pocketpaw_ee.cloud.media.router as media_module
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pocketpaw_ee.cloud.media import storage
from pocketpaw_ee.cloud.media.router import router as media_router

from pocketpaw.uploads.adapter import StorageAdapter, StorageItem, StoredObject
from pocketpaw.uploads.local import LocalStorageAdapter


@pytest.fixture
def client(tmp_path, monkeypatch) -> TestClient:
    """LOCAL-mode media router: adapter backed by a tmp dir + a fixed tracked set."""
    media_root = tmp_path / "media-root"
    media_root.mkdir(exist_ok=True)
    monkeypatch.setattr(storage, "_ADAPTER", LocalStorageAdapter(root=media_root))
    monkeypatch.setattr(
        media_module,
        "tracked_generation_filenames",
        lambda: {"gen-uuid.png"},
    )
    app = FastAPI()
    app.include_router(media_router, prefix="/api/v1")
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def generated(client, tmp_path) -> Path:
    """The on-disk generated dir the local adapter writes (media_root/generated)."""
    generated = tmp_path / "media-root" / "generated"
    generated.mkdir(parents=True, exist_ok=True)
    return generated


# ── LOCAL mode ───────────────────────────────────────────────────────────────


def test_list_media_excludes_generation_tracked_files(client, generated) -> None:
    (generated / "gen-uuid.png").write_bytes(b"png-bytes")
    (generated / "agent-uuid.png").write_bytes(b"agent-bytes")
    (generated / "note.txt").write_text("not media")

    resp = client.get("/api/v1/media")
    assert resp.status_code == 200
    media = resp.json()["media"]
    names = [m["name"] for m in media]
    # The tracked generation file is excluded; the agent-side file is not.
    assert names == ["agent-uuid.png"]
    item = media[0]
    assert item["url"] == "/api/v1/media/agent-uuid.png"
    assert item["mime"] == "image/png"


def test_serve_media_local_returns_file(client, generated) -> None:
    (generated / "agent-uuid.png").write_bytes(b"PNG-BYTES")
    resp = client.get("/api/v1/media/agent-uuid.png")
    assert resp.status_code == 200
    assert resp.content == b"PNG-BYTES"
    assert resp.headers["content-type"].startswith("image/png")


def test_serve_media_missing_is_404(client) -> None:
    resp = client.get("/api/v1/media/nope.png")
    assert resp.status_code == 404


def test_serve_media_traversal_is_403(client, generated) -> None:
    (generated / "real.png").write_bytes(b"x")
    resp = client.get("/api/v1/media/..%2F..%2Fetc%2Fpasswd")
    assert resp.status_code in (403, 404)


def test_post_media_upload_saves_and_returns_mediafile(client, generated) -> None:
    resp = client.post(
        "/api/v1/media",
        files={"file": ("edited.png", b"\x89PNG\r\n\x1a\nedit-bytes", "image/png")},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "edited.png"
    assert body["url"] == "/api/v1/media/edited.png"
    assert body["mime"] == "image/png"
    assert body["size"] == len(b"\x89PNG\r\n\x1a\nedit-bytes")
    assert (generated / "edited.png").read_bytes() == b"\x89PNG\r\n\x1a\nedit-bytes"


def test_post_media_upload_collision_makes_unique_name(client, generated) -> None:
    (generated / "edited.png").write_bytes(b"first")
    resp = client.post(
        "/api/v1/media",
        files={"file": ("edited.png", b"second", "image/png")},
    )
    assert resp.status_code == 200
    name = resp.json()["name"]
    assert name != "edited.png"
    assert name.startswith("edited-")
    # Both files exist — the original was not overwritten.
    assert (generated / "edited.png").read_bytes() == b"first"
    assert (generated / name).read_bytes() == b"second"


def test_post_media_upload_unsupported_extension_is_415(client) -> None:
    resp = client.post(
        "/api/v1/media",
        files={"file": ("evil.txt", b"x", "text/plain")},
    )
    assert resp.status_code == 415


def test_post_media_upload_sanitizes_name(client, generated) -> None:
    resp = client.post(
        "/api/v1/media",
        files={"file": ("../../../tmp/evil name!.png", b"x", "image/png")},
    )
    assert resp.status_code == 200
    name = resp.json()["name"]
    # No slashes, no spaces, and it landed inside the generated dir.
    assert "/" not in name
    assert " " not in name
    assert (generated / name).exists()


# ── REMOTE (S3) mode ─────────────────────────────────────────────────────────


class _FakeRemoteAdapter(StorageAdapter):
    """Minimal remote adapter: no local path, an in-memory key→bytes store, and
    browse returning timestamp-prefixed keys to exercise S3-style listing."""

    def __init__(self) -> None:
        self.store: dict[str, bytes] = {
            "generated/1700000000000-aaaa.png": b"gen-bytes",
            "generated/1699999999000-bbbb.png": b"older-gen-bytes",
        }
        self.put_calls: list[tuple[str, bytes]] = []

    def local_path(self, key: str) -> Path | None:
        return None

    async def put(self, key: str, stream: AsyncIterator[bytes], mime: str) -> StoredObject:
        data = b"".join([chunk async for chunk in stream])
        self.store[key] = data
        self.put_calls.append((key, data))
        return StoredObject(key=key, size=len(data), mime=mime)

    async def exists(self, key: str) -> bool:
        return key in self.store

    async def browse(self, prefix: str) -> list[StorageItem]:
        items: list[StorageItem] = []
        for key, data in self.store.items():
            if key.startswith(prefix):
                name = key[len(prefix) :]
                items.append(StorageItem(name=name, is_dir=False, size=len(data)))
        items.sort(key=lambda i: i.name)
        return items

    async def open(self, key: str) -> AsyncIterator[bytes]:  # noqa: D102
        yield self.store[key]


@pytest.fixture
def remote_adapter(monkeypatch) -> _FakeRemoteAdapter:
    """REMOTE-mode media router: the storage adapter's local_path is None, so the
    router takes the S3 browse/stream path."""
    adapter = _FakeRemoteAdapter()
    monkeypatch.setattr(storage, "_ADAPTER", adapter)
    return adapter


@pytest.fixture
def remote_client(remote_adapter, monkeypatch) -> TestClient:
    monkeypatch.setattr(
        media_module,
        "tracked_generation_filenames",
        lambda: {"1699999999000-bbbb.png"},
    )
    app = FastAPI()
    app.include_router(media_router, prefix="/api/v1")
    return TestClient(app, raise_server_exceptions=False)


def test_remote_list_uses_browse_and_timestamp_modified(remote_client, remote_adapter) -> None:
    """S3 listing comes from browse(); the tracked key is excluded; ``modified``
    is parsed from the timestamp in the generated filename, newest first."""
    resp = remote_client.get("/api/v1/media")
    assert resp.status_code == 200
    media = resp.json()["media"]
    # bbbb is tracked (excluded); only aaaa remains.
    assert [m["name"] for m in media] == ["1700000000000-aaaa.png"]
    assert media[0]["modified"] == 1700000000000


def test_remote_serve_streams_bytes(remote_client, remote_adapter) -> None:
    resp = remote_client.get("/api/v1/media/1700000000000-aaaa.png")
    assert resp.status_code == 200
    assert resp.content == b"gen-bytes"
    assert resp.headers["content-type"].startswith("image/png")


def test_remote_upload_keys_under_generated_prefix(remote_client, remote_adapter) -> None:
    resp = remote_client.post(
        "/api/v1/media",
        files={"file": ("edited.png", b"PNG-BYTES", "image/png")},
    )
    assert resp.status_code == 200
    assert resp.json()["name"] == "edited.png"
    assert ("generated/edited.png", b"PNG-BYTES") in remote_adapter.put_calls
