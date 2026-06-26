# tests/cloud/test_media_mcp.py — STUDIO media-generation in-process MCP server.
#
# Created: 2026-06-10 (feat/studio-code-migration).
# 2026-06-26 (MCG-6 + MCG-7): media generation now routes through the LiteLLM
# proxy's OpenAI-compatible endpoints, so the tests mock the PROXY HTTP calls
# (httpx.MockTransport injected via media._PROXY_TRANSPORT — the same seam the
# catalog client exposes) instead of mocking Google genai / Replicate. They
# assert each modality POSTs to the correct proxy path with the catalog-selected
# model AND the Bearer proxy key, and that the OpenAI `user` field carries the
# tenant (the proxy keys spend off it). Coverage:
#   * MEDIA_TOOL_IDS — the four namespaced tool ids the surface allow-list keys on.
#   * image_generate — happy path (POST /v1/images/generations, decodes b64_json,
#     saves a PNG, lands in a trusted-create gallery), the backward-compat path
#     (no `model` arg → settings.image_model), and identity / proxy-error guards.
#   * audio_generate (TTS) — happy path (POST /v1/audio/speech, raw bytes saved,
#     lands in the gallery), default model when none passed.
#   * audio_transcribe (STT) — happy path (POST /v1/audio/transcriptions multipart,
#     returns text, does NOT touch the gallery), missing-file guard.
#   * video_generate — happy path (POST /videos terminal job, lands the URL in the
#     gallery) and the async poll path (pending → GET /videos/{id} → completed).
#   * gallery spec still uses only existing widget types (now incl. audio tiles).
#
# All tests mock the per-stream ContextVar identity (``_identity``) and the
# session-bind/SSE side effects (``_bind_session_and_emit``) so they run without
# a live SSE chat stream.

from __future__ import annotations

import base64
import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pocketpaw_ee.agent.mcp_servers.media as media
import pytest

# pytest-asyncio runs in auto mode (see pyproject [tool.pytest] asyncio_mode),
# so async tests are detected automatically — no module-level mark needed (a
# module mark would wrongly tag the sync util tests below).

_PROXY_BASE = "https://proxy.test:4000"
_PROXY_KEY = "sk-proxy-abc"


@pytest.fixture
def proxy_env(monkeypatch):
    """Point the catalog proxy config (which media reuses) at a fake proxy with a
    key, so the Bearer header is exercised end-to-end through catalog.config."""
    monkeypatch.setenv("POCKETPAW_LITELLM_API_BASE", _PROXY_BASE)
    monkeypatch.setenv("POCKETPAW_LITELLM_API_KEY", _PROXY_KEY)


def _install_transport(monkeypatch, handler) -> dict:
    """Install an httpx.MockTransport on media._PROXY_TRANSPORT and capture every
    request the handler sees. Returns a dict the test inspects (``requests``)."""
    captured: dict = {"requests": []}

    def _wrapped(request: httpx.Request) -> httpx.Response:
        captured["requests"].append(request)
        return handler(request)

    monkeypatch.setattr(media, "_PROXY_TRANSPORT", httpx.MockTransport(_wrapped))
    return captured


def test_media_tool_ids_are_namespaced() -> None:
    """The tool ids use the ``mcp__<server>__<tool>`` form the Claude Code
    allowlist machinery matches — now four tools (image / audio TTS / audio STT /
    video)."""
    assert media.SERVER_NAME == "pocketpaw_media"
    assert media.IMAGE_GENERATE_TOOL_ID == "mcp__pocketpaw_media__image_generate"
    assert media.AUDIO_GENERATE_TOOL_ID == "mcp__pocketpaw_media__audio_generate"
    assert media.AUDIO_TRANSCRIBE_TOOL_ID == "mcp__pocketpaw_media__audio_transcribe"
    assert media.VIDEO_GENERATE_TOOL_ID == "mcp__pocketpaw_media__video_generate"
    assert media.MEDIA_TOOL_IDS == (
        media.IMAGE_GENERATE_TOOL_ID,
        media.AUDIO_GENERATE_TOOL_ID,
        media.AUDIO_TRANSCRIBE_TOOL_ID,
        media.VIDEO_GENERATE_TOOL_ID,
    )


# --- image_generate ---


async def test_image_generate_error_when_no_identity() -> None:
    """No workspace/user context → a clear error (called outside a chat stream)."""
    with patch.object(media, "_identity", return_value=(None, None)):
        result = await media._image_generate_handler({"prompt": "a red bicycle"})

    assert result.get("is_error") is True
    assert "workspace and user context" in result["content"][0]["text"]


async def test_image_generate_routes_to_proxy_with_model_and_bearer(
    tmp_path, monkeypatch, proxy_env
) -> None:
    """A successful image generation POSTs the catalog model id to the proxy's
    /v1/images/generations with the Bearer key + the tenant in `user`, decodes
    b64_json, saves a PNG, and lands it in a trusted-create gallery."""
    monkeypatch.setattr(media, "_GALLERY_STATE", {})
    b64 = base64.b64encode(b"png-bytes").decode()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/images/generations"
        body = json.loads(request.content)
        assert body["model"] == "openai/gpt-image-1"
        assert body["prompt"] == "a red bicycle"
        assert body["user"] == "ws-1"  # tenant tag (workspace id)
        return httpx.Response(200, json={"data": [{"b64_json": b64}]})

    captured = _install_transport(monkeypatch, handler)
    fake_view = {"name": "Studio gallery", "type": "studio", "pattern": "gallery"}
    agent_create = AsyncMock(return_value=(fake_view, "pkt-gallery-1", None))

    with (
        patch.object(media, "_identity", return_value=("ws-1", "u-1")),
        patch.object(media, "_generated_dir", return_value=tmp_path),
        patch(
            "pocketpaw_ee.cloud.chat.agent_service.current_session_mongo_id",
            return_value="sess-1",
        ),
        patch("pocketpaw_ee.cloud.pockets.service.agent_create", agent_create),
        patch.object(media, "_bind_session_and_emit", AsyncMock()),
    ):
        result = await media._image_generate_handler(
            {"prompt": "a red bicycle", "model": "openai/gpt-image-1", "size": "1024x1024"}
        )

    assert result.get("is_error") is not True
    body = json.loads(result["content"][0]["text"])
    assert body["ok"] is True
    assert body["kind"] == "image"
    assert body["model"] == "openai/gpt-image-1"
    assert body["pocket_id"] == "pkt-gallery-1"
    assert body["gallery_count"] == 1
    # The Bearer proxy key rode on the request.
    assert captured["requests"][0].headers.get("authorization") == f"Bearer {_PROXY_KEY}"
    # The PNG was written with the decoded image bytes.
    pngs = list(tmp_path.glob("*.png"))
    assert len(pngs) == 1
    assert pngs[0].read_bytes() == b"png-bytes"
    # agent_create was called with the trusted-create STUDIO gallery contract.
    _, kwargs = agent_create.call_args
    assert kwargs["type_"] == "studio"
    assert kwargs["pattern"] == "gallery"
    assert kwargs["trusted"] is True
    assert kwargs["ripple_spec"] is not None


async def test_image_generate_backward_compat_default_model(
    tmp_path, monkeypatch, proxy_env
) -> None:
    """No `model` arg (the existing studio skill/preamble call shape) →
    settings.image_model is used as the catalog model id."""
    monkeypatch.setattr(media, "_GALLERY_STATE", {})
    b64 = base64.b64encode(b"img").decode()
    seen_model: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen_model["model"] = json.loads(request.content)["model"]
        return httpx.Response(200, json={"data": [{"b64_json": b64}]})

    _install_transport(monkeypatch, handler)
    agent_create = AsyncMock(return_value=({"x": 1}, "pkt-1", None))

    with (
        patch.object(media, "_identity", return_value=("ws-1", "u-1")),
        patch.object(media, "_generated_dir", return_value=tmp_path),
        patch(
            "pocketpaw.config.get_settings",
            return_value=MagicMock(image_model="google/imagen-4"),
        ),
        patch(
            "pocketpaw_ee.cloud.chat.agent_service.current_session_mongo_id",
            return_value="sess-1",
        ),
        patch("pocketpaw_ee.cloud.pockets.service.agent_create", agent_create),
        patch.object(media, "_bind_session_and_emit", AsyncMock()),
    ):
        result = await media._image_generate_handler({"prompt": "a cat"})

    assert result.get("is_error") is not True
    assert seen_model["model"] == "google/imagen-4"


async def test_image_generate_relays_proxy_error(tmp_path, monkeypatch, proxy_env) -> None:
    """A proxy 4xx (e.g. unknown model) is relayed plainly, no asset, no gallery
    write."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": {"message": "model not found"}})

    _install_transport(monkeypatch, handler)
    agent_create = AsyncMock()

    with (
        patch.object(media, "_identity", return_value=("ws-1", "u-1")),
        patch.object(media, "_generated_dir", return_value=tmp_path),
        patch("pocketpaw_ee.cloud.pockets.service.agent_create", agent_create),
    ):
        result = await media._image_generate_handler(
            {"prompt": "x", "model": "openai/does-not-exist"}
        )

    assert result.get("is_error") is True
    text = result["content"][0]["text"]
    assert "400" in text
    assert "model not found" in text
    agent_create.assert_not_called()


# --- audio_generate (TTS) ---


async def test_audio_generate_routes_to_speech_endpoint(tmp_path, monkeypatch, proxy_env) -> None:
    """A successful TTS POSTs to /v1/audio/speech with the model + Bearer + tenant,
    saves the raw audio bytes, and lands an audio tile in the gallery."""
    monkeypatch.setattr(media, "_GALLERY_STATE", {})

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/audio/speech"
        body = json.loads(request.content)
        assert body["model"] == "openai/tts-1-hd"
        assert body["input"] == "hello world"
        assert body["voice"] == "nova"
        assert body["user"] == "ws-1"
        return httpx.Response(200, content=b"mp3-bytes")

    captured = _install_transport(monkeypatch, handler)
    agent_create = AsyncMock(return_value=({"x": 1}, "pkt-audio-1", None))

    with (
        patch.object(media, "_identity", return_value=("ws-1", "u-1")),
        patch.object(media, "_generated_dir", return_value=tmp_path),
        patch(
            "pocketpaw_ee.cloud.chat.agent_service.current_session_mongo_id",
            return_value="sess-1",
        ),
        patch("pocketpaw_ee.cloud.pockets.service.agent_create", agent_create),
        patch.object(media, "_bind_session_and_emit", AsyncMock()),
    ):
        result = await media._audio_generate_handler(
            {"text": "hello world", "model": "openai/tts-1-hd", "voice": "nova"}
        )

    assert result.get("is_error") is not True
    body = json.loads(result["content"][0]["text"])
    assert body["ok"] is True
    assert body["kind"] == "audio"
    assert body["model"] == "openai/tts-1-hd"
    assert body["pocket_id"] == "pkt-audio-1"
    assert captured["requests"][0].headers.get("authorization") == f"Bearer {_PROXY_KEY}"
    mp3s = list(tmp_path.glob("*.mp3"))
    assert len(mp3s) == 1
    assert mp3s[0].read_bytes() == b"mp3-bytes"
    _, kwargs = agent_create.call_args
    assert kwargs["trusted"] is True


async def test_audio_generate_default_model(tmp_path, monkeypatch, proxy_env) -> None:
    """No `model` → the built-in TTS default (tts-1)."""
    monkeypatch.setattr(media, "_GALLERY_STATE", {})
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["model"] = json.loads(request.content)["model"]
        return httpx.Response(200, content=b"a")

    _install_transport(monkeypatch, handler)

    with (
        patch.object(media, "_identity", return_value=("ws-1", "u-1")),
        patch.object(media, "_generated_dir", return_value=tmp_path),
        patch(
            "pocketpaw_ee.cloud.chat.agent_service.current_session_mongo_id",
            return_value="sess-1",
        ),
        patch(
            "pocketpaw_ee.cloud.pockets.service.agent_create",
            AsyncMock(return_value=({"x": 1}, "p", None)),
        ),
        patch.object(media, "_bind_session_and_emit", AsyncMock()),
    ):
        result = await media._audio_generate_handler({"text": "hi"})

    assert result.get("is_error") is not True
    assert seen["model"] == media._DEFAULT_AUDIO_TTS_MODEL


async def test_audio_generate_error_when_no_identity() -> None:
    with patch.object(media, "_identity", return_value=(None, None)):
        result = await media._audio_generate_handler({"text": "hi"})
    assert result.get("is_error") is True
    assert "workspace and user context" in result["content"][0]["text"]


# --- audio_transcribe (STT) ---


async def test_audio_transcribe_routes_to_transcriptions_endpoint(
    tmp_path, monkeypatch, proxy_env
) -> None:
    """A successful STT POSTs a multipart upload to /v1/audio/transcriptions with
    the model + Bearer, returns the text, and does NOT touch the gallery."""
    audio_file = tmp_path / "clip.mp3"
    audio_file.write_bytes(b"fake-audio")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/audio/transcriptions"
        # multipart upload — the boundary content-type is set by httpx, not the
        # JSON content-type, and the model + file ride in the body.
        ctype = request.headers.get("content-type", "")
        assert ctype.startswith("multipart/form-data")
        raw = request.content
        assert b"whisper-1" in raw
        assert b"fake-audio" in raw
        return httpx.Response(200, json={"text": "transcribed words"})

    captured = _install_transport(monkeypatch, handler)
    agent_create = AsyncMock()

    with (
        patch.object(media, "_identity", return_value=("ws-1", "u-1")),
        # If transcription wrongly tried to write the gallery, this would be hit.
        patch("pocketpaw_ee.cloud.pockets.service.agent_create", agent_create),
    ):
        result = await media._audio_transcribe_handler({"path": str(audio_file)})

    assert result.get("is_error") is not True
    body = json.loads(result["content"][0]["text"])
    assert body["ok"] is True
    assert body["kind"] == "transcription"
    assert body["text"] == "transcribed words"
    assert body["model"] == "whisper-1"
    assert captured["requests"][0].headers.get("authorization") == f"Bearer {_PROXY_KEY}"
    # STT is an input op — it must NOT create a gallery pocket.
    agent_create.assert_not_called()


async def test_audio_transcribe_missing_file() -> None:
    with patch.object(media, "_identity", return_value=("ws-1", "u-1")):
        result = await media._audio_transcribe_handler({"path": "/no/such/file.mp3"})
    assert result.get("is_error") is True
    assert "not found" in result["content"][0]["text"]


# --- video_generate ---


async def test_video_generate_terminal_job_lands_in_gallery(monkeypatch, proxy_env) -> None:
    """A /videos POST that returns an already-terminal job lands the output URL in
    the gallery with the catalog model id + Bearer + tenant."""
    monkeypatch.setattr(media, "_GALLERY_STATE", {})

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/videos"
        body = json.loads(request.content)
        assert body["model"] == "openai/sora-2"
        assert body["prompt"] == "a drone shot of a city"
        assert body["seconds"] == 8
        assert body["user"] == "ws-1"
        return httpx.Response(
            200,
            json={"id": "vid-1", "status": "completed", "url": "https://cdn/v.mp4"},
        )

    captured = _install_transport(monkeypatch, handler)
    agent_create = AsyncMock(return_value=({"x": 1}, "pkt-video-1", None))

    with (
        patch.object(media, "_identity", return_value=("ws-1", "u-1")),
        patch(
            "pocketpaw_ee.cloud.chat.agent_service.current_session_mongo_id",
            return_value="sess-1",
        ),
        patch("pocketpaw_ee.cloud.pockets.service.agent_create", agent_create),
        patch.object(media, "_bind_session_and_emit", AsyncMock()),
    ):
        result = await media._video_generate_handler(
            {"prompt": "a drone shot of a city", "model": "openai/sora-2", "duration": 8}
        )

    assert result.get("is_error") is not True
    body = json.loads(result["content"][0]["text"])
    assert body["ok"] is True
    assert body["kind"] == "video"
    assert body["model"] == "openai/sora-2"
    assert body["url"] == "https://cdn/v.mp4"
    assert body["pocket_id"] == "pkt-video-1"
    assert captured["requests"][0].headers.get("authorization") == f"Bearer {_PROXY_KEY}"


async def test_video_generate_polls_pending_job(monkeypatch, proxy_env) -> None:
    """A pending /videos job is GET-polled at /videos/{id} until completed, then
    the output URL is landed in the gallery. asyncio.sleep is patched out so the
    poll loop runs instantly."""
    monkeypatch.setattr(media, "_GALLERY_STATE", {})

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/videos":
            return httpx.Response(200, json={"id": "vid-9", "status": "queued"})
        if request.method == "GET" and request.url.path == "/videos/vid-9":
            return httpx.Response(
                200,
                json={
                    "id": "vid-9",
                    "status": "completed",
                    "data": [{"url": "https://cdn/poll.mp4"}],
                },
            )
        return httpx.Response(404)

    _install_transport(monkeypatch, handler)
    agent_create = AsyncMock(return_value=({"x": 1}, "pkt-v9", None))

    with (
        patch.object(media, "_identity", return_value=("ws-1", "u-1")),
        patch("pocketpaw_ee.agent.mcp_servers.media.asyncio.sleep", AsyncMock()),
        patch(
            "pocketpaw_ee.cloud.chat.agent_service.current_session_mongo_id",
            return_value="sess-1",
        ),
        patch("pocketpaw_ee.cloud.pockets.service.agent_create", agent_create),
        patch.object(media, "_bind_session_and_emit", AsyncMock()),
    ):
        result = await media._video_generate_handler({"prompt": "x", "model": "openai/sora-2"})

    assert result.get("is_error") is not True
    body = json.loads(result["content"][0]["text"])
    assert body["url"] == "https://cdn/poll.mp4"


async def test_video_generate_failed_job_relays_error(monkeypatch, proxy_env) -> None:
    """A terminal job with status=failed relays the error; no gallery write."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": "v", "status": "failed", "error": "content policy"})

    _install_transport(monkeypatch, handler)
    agent_create = AsyncMock()

    with (
        patch.object(media, "_identity", return_value=("ws-1", "u-1")),
        patch("pocketpaw_ee.cloud.pockets.service.agent_create", agent_create),
    ):
        result = await media._video_generate_handler({"prompt": "x", "model": "openai/sora-2"})

    assert result.get("is_error") is True
    assert "content policy" in result["content"][0]["text"]
    agent_create.assert_not_called()


async def test_video_generate_error_when_no_identity() -> None:
    with patch.object(media, "_identity", return_value=(None, None)):
        result = await media._video_generate_handler({"prompt": "x"})
    assert result.get("is_error") is True
    assert "workspace and user context" in result["content"][0]["text"]


# --- gallery spec assembly (existing widget types only) ---


def test_gallery_spec_uses_only_existing_widget_types() -> None:
    """The assembled gallery spec uses ONLY container / grid / card / image /
    video-player / text so it renders today (audio degrades to a text tile), and
    carries version + a non-dashboard intent so toRippleEnvelope passes it through
    untouched and Ripple renders it in node mode."""
    spec = media._build_gallery_spec(
        [
            {"kind": "image", "src": "/tmp/a.png", "prompt": "a"},
            {"kind": "video", "src": "https://x/v.mp4", "prompt": "b"},
            {"kind": "audio", "src": "/tmp/c.mp3", "prompt": "c"},
        ]
    )
    assert spec["version"] == "2.0"
    assert spec["intent"] != "dashboard"
    allowed = {"container", "grid", "card", "image", "video-player", "text"}

    def _walk(nodes):
        for n in nodes:
            assert n["type"] in allowed, f"unexpected widget type {n['type']!r}"
            _walk(n.get("children", []))

    # `ui` is a single root node — NodeRenderer takes one node, not a list.
    root = spec["ui"]
    assert isinstance(root, dict)
    _walk([root])
    # The grid holds one tile per media item.
    grid = next(n for n in root["children"] if n["type"] == "grid")
    assert len(grid["children"]) == 3
