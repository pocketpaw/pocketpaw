# tests/cloud/studio/test_router_transcribe.py — POST /studio/transcribe.
#
# The router is a thin adapter, so these assert the HTTP CONTRACT rather than
# Deepgram's behaviour (test_deepgram_stt covers that): the endpoint accepts a
# multipart ``file`` upload, relays the provider error message as a 502 so the
# editor can show a real reason instead of a spinner that never stops, maps an
# empty upload to 400, and returns camelCase millisecond word timings the
# frontend can feed straight into its caption path.
#
# Created 2026-09-02 (studio-transcribe): transcribe route tests.

from __future__ import annotations

import pocketpaw_ee.cloud.studio.service as studio_service
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pocketpaw_ee.cloud._core.deps import current_workspace_id
from pocketpaw_ee.cloud.license import require_license
from pocketpaw_ee.cloud.studio import schemas
from pocketpaw_ee.cloud.studio.router import router as studio_router


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.include_router(studio_router, prefix="/api/v1")
    app.dependency_overrides[require_license] = lambda: None
    app.dependency_overrides[current_workspace_id] = lambda: "ws-1"
    return TestClient(app, raise_server_exceptions=False)


def _response(**overrides) -> schemas.TranscriptResponse:
    payload = {
        "text": "hello there",
        "words": [{"text": "hello", "startMs": 0, "endMs": 500, "confidence": 0.98}],
        "model": "nova-3",
    }
    payload.update(overrides)
    return schemas.TranscriptResponse(**payload)


def test_transcribe_accepts_multipart_upload(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict = {}

    async def fake(audio_bytes, **kwargs):
        seen["bytes"] = audio_bytes
        seen.update(kwargs)
        return _response()

    monkeypatch.setattr(studio_service, "transcribe", fake)
    resp = client.post(
        "/api/v1/studio/transcribe",
        files={"file": ("clip.wav", b"fake-wav", "audio/wav")},
    )

    assert resp.status_code == 200, resp.text
    assert seen["bytes"] == b"fake-wav"
    # The browser's content type reaches the provider unchanged.
    assert seen["content_type"] == "audio/wav"
    body = resp.json()
    assert body["text"] == "hello there"
    # camelCase milliseconds on the wire — the shape paw-enterprise's CaptionWord
    # consumes without conversion.
    assert body["words"][0] == {"text": "hello", "startMs": 0, "endMs": 500, "confidence": 0.98}


def test_transcribe_forwards_model_and_language(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict = {}

    async def fake(audio_bytes, **kwargs):
        seen.update(kwargs)
        return _response()

    monkeypatch.setattr(studio_service, "transcribe", fake)
    resp = client.post(
        "/api/v1/studio/transcribe",
        files={"file": ("clip.wav", b"x", "audio/wav")},
        data={"model": "nova-2", "language": "en-US"},
    )

    assert resp.status_code == 200, resp.text
    assert seen["model"] == "nova-2"
    assert seen["language"] == "en-US"


def test_transcribe_rejects_missing_file(client: TestClient) -> None:
    """No file at all is a 4xx, not a crash inside UploadFile.read()."""
    resp = client.post("/api/v1/studio/transcribe", data={"model": "nova-3"})
    assert resp.status_code in (400, 422)


def test_transcribe_empty_upload_is_400(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake(audio_bytes, **kwargs):
        raise ValueError("audio file is empty")

    monkeypatch.setattr(studio_service, "transcribe", fake)
    resp = client.post(
        "/api/v1/studio/transcribe", files={"file": ("clip.wav", b"", "audio/wav")}
    )
    assert resp.status_code == 400
    assert "empty" in resp.json()["detail"]


def test_transcribe_upstream_failure_relays_reason(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The provider's message must survive into the 502 detail.

    A bare "transcription failed" would leave a user with no idea whether it
    was a missing API key or a bad file — the two things they can actually fix.
    """

    async def fake(audio_bytes, **kwargs):
        raise studio_service.StudioUpstreamError(
            "Deepgram is not configured — set POCKETPAW_DEEPGRAM_API_KEY"
        )

    monkeypatch.setattr(studio_service, "transcribe", fake)
    resp = client.post(
        "/api/v1/studio/transcribe", files={"file": ("clip.wav", b"x", "audio/wav")}
    )
    assert resp.status_code == 502
    assert "POCKETPAW_DEEPGRAM_API_KEY" in resp.json()["detail"]
