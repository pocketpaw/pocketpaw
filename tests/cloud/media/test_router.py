# tests/cloud/media/test_router.py — the /media file list + upload surface.
#
# The studio-real-backend work added POST /media (canvas "save edited image")
# and the generation-tracking EXCLUSION from GET /media — the gallery renders
# direct /studio generations via the generation history, so listing them again
# as bare files would double the tiles. The router's generated-dir is redirected
# to a tmp dir and ``tracked_generation_filenames`` (imported from the studio
# service) is patched to a fixed set so the exclusion is asserted deterministically.
#
# Created 2026-08-17 (studio-real-backend): new media router tests.

from __future__ import annotations

import pocketpaw_ee.cloud.media.router as media_module
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pocketpaw_ee.cloud.media.router import router as media_router


@pytest.fixture
def client(tmp_path, monkeypatch) -> TestClient:
    generated = tmp_path / "generated"
    generated.mkdir(exist_ok=True)
    monkeypatch.setattr(media_module, "_generated_dir", lambda: generated)
    # Tracked = a direct /studio generation output that must NOT re-appear as a
    # bare media file.
    monkeypatch.setattr(
        media_module,
        "tracked_generation_filenames",
        lambda: {"gen-uuid.png"},
    )
    app = FastAPI()
    app.include_router(media_router, prefix="/api/v1")
    return TestClient(app, raise_server_exceptions=False)


def test_list_media_excludes_generation_tracked_files(client, tmp_path) -> None:
    generated = tmp_path / "generated"
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


def test_post_media_upload_saves_and_returns_mediafile(client, tmp_path) -> None:
    generated = tmp_path / "generated"
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


def test_post_media_upload_collision_makes_unique_name(client, tmp_path) -> None:
    generated = tmp_path / "generated"
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


def test_post_media_upload_sanitizes_name(client, tmp_path) -> None:
    generated = tmp_path / "generated"
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
