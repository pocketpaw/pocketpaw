# tests/cloud/test_media_mcp.py — STUDIO media-generation in-process MCP server.
#
# Created: 2026-06-10 (feat/studio-code-migration) — Guards the media MCP server
# the cloud chat (claude_agent_sdk) backend uses on the /studio surface:
#   * MEDIA_TOOL_IDS — the two namespaced tool ids the surface allow-list + the
#     SDK allowlist machinery key on.
#   * image_generate — happy path (mock the google genai client + agent_create →
#     asserts the asset lands in a trusted-create gallery and the success body
#     carries the pocket_id) and the error path when no google key is set.
#     2026-06-10: happy path mocks generate_content (the gemini-2.5-flash-image
#     default route in generate_image_file); the imagen/generate_images route is
#     covered in tests/test_image_gen.py.
#   * video_generate — the error path when no Replicate token is set (mock the
#     settings; httpx must not even be reached). The happy path needs a live
#     Replicate poll, so it is covered structurally by the no-token guard + the
#     output-URL parsing being exercised in the handler.
#
# All tests mock the per-stream ContextVar identity (``_identity``) and the
# session-bind/SSE side effects (``_bind_session_and_emit``) so they run without
# a live SSE chat stream.

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pocketpaw_ee.agent.mcp_servers.media as media

# pytest-asyncio runs in auto mode (see pyproject [tool.pytest] asyncio_mode),
# so async tests are detected automatically — no module-level mark needed (a
# module mark would wrongly tag the sync util tests below).


def test_media_tool_ids_are_namespaced() -> None:
    """The tool ids use the ``mcp__<server>__<tool>`` form the Claude Code
    allowlist machinery matches."""
    assert media.SERVER_NAME == "pocketpaw_media"
    assert media.IMAGE_GENERATE_TOOL_ID == "mcp__pocketpaw_media__image_generate"
    assert media.VIDEO_GENERATE_TOOL_ID == "mcp__pocketpaw_media__video_generate"
    assert media.MEDIA_TOOL_IDS == (
        media.IMAGE_GENERATE_TOOL_ID,
        media.VIDEO_GENERATE_TOOL_ID,
    )


# --- image_generate ---


async def test_image_generate_error_when_no_google_key() -> None:
    """No google key → a clear error response, no asset, no gallery write."""
    with (
        patch.object(media, "_identity", return_value=("ws-1", "u-1")),
        patch("pocketpaw.config.get_settings", return_value=MagicMock(google_api_key=None)),
    ):
        result = await media._image_generate_handler({"prompt": "a red bicycle"})

    assert result.get("is_error") is True
    assert "POCKETPAW_GOOGLE_API_KEY" in result["content"][0]["text"]


async def test_image_generate_error_when_no_identity() -> None:
    """No workspace/user context → a clear error (called outside a chat stream)."""
    with patch.object(media, "_identity", return_value=(None, None)):
        result = await media._image_generate_handler({"prompt": "a red bicycle"})

    assert result.get("is_error") is True
    assert "workspace and user context" in result["content"][0]["text"]


async def test_image_generate_happy_path_lands_in_gallery(tmp_path, monkeypatch) -> None:
    """A successful Gemini generation saves a PNG and lands it in a trusted-create
    STUDIO gallery; the success body carries the gallery pocket_id."""
    import sys

    # Fresh per-session state so the gallery count is deterministic.
    monkeypatch.setattr(media, "_GALLERY_STATE", {})

    # Mock the google genai module (`from google import genai`). The default
    # image model is now gemini-2.5-flash-image, which routes through
    # generate_content (free-tier path) — see generate_image_file().
    mock_part = MagicMock()
    mock_part.inline_data = MagicMock(mime_type="image/png", data=b"png-bytes")
    mock_candidate = MagicMock()
    mock_candidate.content.parts = [mock_part]
    mock_response = MagicMock(candidates=[mock_candidate])
    mock_client = MagicMock()
    mock_client.models.generate_content.return_value = mock_response
    mock_genai = MagicMock()
    mock_genai.Client.return_value = mock_client

    # agent_create returns (view, pocket_id, err).
    fake_view = {"name": "Studio gallery", "type": "studio", "pattern": "gallery"}
    agent_create = AsyncMock(return_value=(fake_view, "pkt-gallery-1", None))

    with (
        patch.object(media, "_identity", return_value=("ws-1", "u-1")),
        patch(
            "pocketpaw.config.get_settings",
            return_value=MagicMock(google_api_key="k", image_model="gemini-2.5-flash-image"),
        ),
        patch.object(media, "_generated_dir", return_value=tmp_path),
        patch.dict(
            sys.modules, {"google": MagicMock(genai=mock_genai), "google.genai": mock_genai}
        ),
        patch(
            "pocketpaw_ee.cloud.chat.agent_service.current_session_mongo_id",
            return_value="sess-1",
        ),
        patch("pocketpaw_ee.cloud.pockets.service.agent_create", agent_create),
        patch.object(media, "_bind_session_and_emit", AsyncMock()),
    ):
        result = await media._image_generate_handler(
            {"prompt": "a red bicycle", "aspect_ratio": "16:9"}
        )

    # No is_error — a success body with the gallery pocket id.
    assert result.get("is_error") is not True
    import json

    body = json.loads(result["content"][0]["text"])
    assert body["ok"] is True
    assert body["kind"] == "image"
    assert body["pocket_id"] == "pkt-gallery-1"
    assert body["gallery_count"] == 1
    # The PNG was written under the generated dir with the inline image bytes.
    pngs = list(tmp_path.glob("*.png"))
    assert len(pngs) == 1
    assert pngs[0].read_bytes() == b"png-bytes"
    # agent_create was called with the trusted-create STUDIO gallery contract.
    _, kwargs = agent_create.call_args
    assert kwargs["type_"] == "studio"
    assert kwargs["pattern"] == "gallery"
    assert kwargs["trusted"] is True
    assert kwargs["ripple_spec"] is not None


# --- video_generate ---


async def test_video_generate_error_when_no_replicate_token() -> None:
    """No Replicate token → a clear error response; httpx is never reached."""
    with (
        patch.object(media, "_identity", return_value=("ws-1", "u-1")),
        patch(
            "pocketpaw.config.get_settings",
            return_value=MagicMock(replicate_api_token=None, video_model="kwaivgi/kling-v2.0"),
        ),
    ):
        result = await media._video_generate_handler({"prompt": "a drone shot of a city"})

    assert result.get("is_error") is True
    assert "POCKETPAW_REPLICATE_API_TOKEN" in result["content"][0]["text"]


async def test_video_generate_error_when_no_identity() -> None:
    """No workspace/user context → a clear error."""
    with patch.object(media, "_identity", return_value=(None, None)):
        result = await media._video_generate_handler({"prompt": "a drone shot"})

    assert result.get("is_error") is True
    assert "workspace and user context" in result["content"][0]["text"]


# --- gallery spec assembly (existing widget types only) ---


def test_gallery_spec_uses_only_existing_widget_types() -> None:
    """The assembled gallery spec uses ONLY grid / card / image / video-player /
    text so it renders today."""
    spec = media._build_gallery_spec(
        [
            {"kind": "image", "src": "/tmp/a.png", "prompt": "a"},
            {"kind": "video", "src": "https://x/v.mp4", "prompt": "b"},
        ]
    )
    allowed = {"grid", "card", "image", "video-player", "text"}

    def _walk(nodes):
        for n in nodes:
            assert n["type"] in allowed, f"unexpected widget type {n['type']!r}"
            _walk(n.get("children", []))

    _walk(spec["ui"])
    # The grid holds one tile per media item.
    grid = next(n for n in spec["ui"] if n["type"] == "grid")
    assert len(grid["children"]) == 2
