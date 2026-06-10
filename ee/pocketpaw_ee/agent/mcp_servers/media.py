# media.py — in-process MCP server exposing the STUDIO media-generation actions
# (image + video) to the claude_agent_sdk cloud chat backend. Created: 2026-06-10
# (feat/studio-code-migration).
#
# What this file does: clones the sites.py / sites_create.py shape — a single
# ``create_sdk_mcp_server`` with an SDK import-guard, ``SERVER_NAME`` /
# ``*_TOOL_ID`` allowlist constants, ContextVar-sourced identity (the same
# ``current_workspace_id`` / ``current_user_id`` accessors in
# ``ee.cloud.chat.agent_service`` the sites / pocket-specialist servers read),
# and the ``_error_response`` / ``_success_response`` helpers. Tool ids namespace
# as ``mcp__pocketpaw_media__image_generate`` / ``__video_generate`` so the
# Claude Code allowlist machinery matches them.
#
# Two SDK @tool defs:
#   * image_generate — delegates to generate_image_file() in
#     src/pocketpaw/tools/builtin/image_gen.py (2026-06-10: shared helper that
#     routes gemini-*-image models via generateContent and imagen-* via the
#     paid-tier predict endpoint), saves to get_config_dir()/generated/<uuid>.png.
#     Missing key → clear error.
#   * video_generate — calls the Replicate HTTP API with httpx (an EXISTING
#     dep — the replicate package is NOT added). Reads settings.replicate_api_token
#     + settings.video_model, POSTs a prediction, polls the GET until terminal
#     (~120s cap, 3s sleep), returns the output URL. Missing token → clear error.
#
# Result contract: every successful generation lands the asset in a STUDIO
# gallery pocket via the TRUSTED-CREATE path (``agent_create(... type_="studio",
# pattern="gallery", engine="ripple", ripple_spec=<gallery spec>, trusted=True)``)
# so the strict catalog gate is bypassed (image / video-player widgets lag the
# published manifest), then binds the session + emits ``pocket_created`` exactly
# like sites_create.py (``_bind_session_and_emit``). The gallery spec uses ONLY
# existing widget types (grid / card / image / video-player / text) so it renders
# today — a responsive grid of media tiles. Media accumulates across the session
# (per-session state holds the gallery's media list); each generation re-creates
# the gallery with the full list and emits the refreshed pocket, so the canvas
# always shows every asset made this session.
#
# EE→OSS boundary: this module lives in pocketpaw_ee; the surface service loads
# MEDIA_TOOL_IDS as a plain frozenset[str] inside a try/except (never importing a
# pocketpaw_ee symbol into src/pocketpaw).
"""Agent-side MCP surface for STUDIO image + video generation."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

SERVER_NAME = "pocketpaw_media"
# Claude Code namespaces in-process MCP tools as ``mcp__<server>__<tool>``.
# Allowlist entries must use this exact form.
IMAGE_GENERATE_TOOL_ID = f"mcp__{SERVER_NAME}__image_generate"
VIDEO_GENERATE_TOOL_ID = f"mcp__{SERVER_NAME}__video_generate"

MEDIA_TOOL_IDS = (IMAGE_GENERATE_TOOL_ID, VIDEO_GENERATE_TOOL_ID)

# Replicate HTTP API polling bounds (video generation is async — POST a
# prediction, then GET-poll until terminal). httpx is the existing transport;
# the replicate package is intentionally NOT a dependency.
_REPLICATE_BASE = "https://api.replicate.com/v1"
_POLL_INTERVAL_SECONDS = 3.0
_POLL_MAX_SECONDS = 120.0


def _error_response(message: str) -> dict[str, Any]:
    """Build an MCP error response in the shape Claude's SDK expects. The agent
    reads ``text`` and surfaces the reason."""
    return {
        "content": [{"type": "text", "text": f"Error: {message}"}],
        "is_error": True,
    }


def _success_response(body: dict[str, Any]) -> dict[str, Any]:
    """Build an MCP success response carrying ``body`` as JSON."""
    return {
        "content": [
            {
                "type": "text",
                "text": json.dumps(body, separators=(",", ":"), default=str),
            }
        ]
    }


def _identity() -> tuple[str | None, str | None]:
    """Resolve the active workspace + user id from the per-stream ContextVars set
    by the cloud chat agent runtime. Returns ``(workspace_id, user_id)``."""
    try:
        from pocketpaw_ee.cloud.chat.agent_service import current_user_id, current_workspace_id

        return current_workspace_id(), current_user_id()
    except Exception:  # noqa: BLE001
        return None, None


# ── Per-session gallery state ───────────────────────────────────────────────
# Keyed by session mongo id, holds the running list of media items produced this
# session so subsequent generations append to the same gallery rather than
# starting empty. ``pocket_id`` is the most-recently-created gallery pocket for
# the session. Best-effort, in-process — a fresh process just starts a new
# gallery, which is acceptable (the asset files persist regardless).
_GALLERY_STATE: dict[str, dict[str, Any]] = {}


def _generated_dir() -> Path:
    """Get (and create) the directory for generated media assets — same location
    the OSS ImageGenerateTool uses (``get_config_dir()/generated``)."""
    from pocketpaw.config import get_config_dir

    d = get_config_dir() / "generated"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _build_gallery_spec(media: list[dict[str, Any]]) -> dict[str, Any]:
    """Assemble a STUDIO gallery rippleSpec from the accumulated media list.

    Uses ONLY existing widget types (``grid`` / ``card`` / ``image`` /
    ``video-player`` / ``text``) so it renders today. Each media item becomes a
    card tile in a responsive grid; an ``image`` item carries a local file path,
    a ``video`` item carries the provider output URL. A leading ``text`` node
    titles the gallery. ``media`` items: ``{kind: "image"|"video", src, prompt}``.
    """
    tiles: list[dict[str, Any]] = []
    for i, item in enumerate(media):
        kind = item.get("kind")
        src = item.get("src", "")
        prompt = item.get("prompt", "")
        if kind == "video":
            inner = {"type": "video-player", "props": {"src": src, "controls": True}}
        else:
            inner = {"type": "image", "props": {"src": src, "alt": prompt}}
        tiles.append(
            {
                "id": f"tile-{i}",
                "type": "card",
                "children": [
                    inner,
                    {"type": "text", "props": {"text": prompt}},
                ],
            }
        )
    return {
        "ui": [
            {
                "id": "studio-title",
                "type": "text",
                "props": {"text": "Generated media"},
            },
            {
                "id": "studio-grid",
                "type": "grid",
                "props": {"columns": 3, "gap": "1rem"},
                "children": tiles,
            },
        ]
    }


async def _bind_session_and_emit(pocket_id: str, view: dict[str, Any], user_id: str) -> None:
    """Bind the active chat session to the new gallery pocket and push the
    ``pocket_created`` SSE event so the canvas auto-opens — the same atomic
    post-create side effects ``sites_create.py`` runs. Best-effort: a bind / SSE
    failure must never undo a successful create (the pocket already exists in
    Mongo, which is the primary contract)."""
    try:
        from pocketpaw_ee.cloud.chat.agent_service import (
            current_session_mongo_id,
            push_sse_event,
        )
        from pocketpaw_ee.cloud.sessions import service as sessions_service

        session_mongo_id = current_session_mongo_id()
        if session_mongo_id:
            await sessions_service.attach_pocket_to_session_doc(
                session_mongo_id, user_id, pocket_id
            )
        push_sse_event(
            "pocket_created",
            {"pocket_id": pocket_id, "pocket": view, "session_id": session_mongo_id},
        )
    except Exception:  # noqa: BLE001
        logger.warning(
            "media: post-create side effects failed (non-fatal)",
            exc_info=True,
        )


async def _land_in_gallery(
    *,
    workspace_id: str,
    user_id: str,
    kind: str,
    src: str,
    prompt: str,
) -> tuple[dict[str, Any] | None, str | None]:
    """Append a freshly generated asset to the session's STUDIO gallery and
    persist it via the TRUSTED-CREATE path.

    Tracks the running media list per session, rebuilds the full gallery spec,
    and calls ``agent_create(... type_="studio", pattern="gallery",
    ripple_spec=<spec>, trusted=True)`` so the strict catalog gate is bypassed
    (the image / video-player widgets lag the published manifest, same reason
    sites_create.py uses ``trusted=True``). Then binds the session + emits
    ``pocket_created``. Returns ``({pocket_id, count}, None)`` on success or
    ``(None, error)`` on failure.
    """
    from pocketpaw_ee.cloud.chat.agent_service import current_session_mongo_id
    from pocketpaw_ee.cloud.pockets.service import agent_create

    session_key = current_session_mongo_id() or f"{workspace_id}:{user_id}"
    state = _GALLERY_STATE.setdefault(session_key, {"media": [], "pocket_id": None})
    state["media"].append({"kind": kind, "src": src, "prompt": prompt})

    ripple_spec = _build_gallery_spec(state["media"])

    try:
        view, new_pocket_id, err = await agent_create(
            workspace_id=workspace_id,
            owner_id=user_id,
            name="Studio gallery",
            description="Media generated in /studio",
            type_="studio",
            pattern="gallery",
            engine="ripple",
            ripple_spec=ripple_spec,
            trusted=True,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("media: gallery persist raised", exc_info=True)
        return None, f"gallery persist failed: {exc}"

    if err is not None or view is None or new_pocket_id is None:
        return None, f"gallery persist failed: {err or 'create returned no view'}"

    state["pocket_id"] = new_pocket_id
    await _bind_session_and_emit(new_pocket_id, view, user_id)
    return {"pocket_id": new_pocket_id, "count": len(state["media"])}, None


async def _image_generate_handler(args: dict) -> dict:
    """MCP handler for ``media__image_generate``.

    Reuses the Gemini image logic from the OSS ImageGenerateTool: settings'
    google_api_key + image_model, saves the PNG under get_config_dir()/generated,
    then lands it in the STUDIO gallery. Sets ``is_error`` when identity or the
    google key is missing, the genai package is absent, or generation fails.
    """
    workspace_id, user_id = _identity()
    if not workspace_id or not user_id:
        return _error_response(
            "image_generate requires workspace and user context (call from a cloud chat session)."
        )

    prompt = args.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        return _error_response("image_generate requires a non-empty `prompt`.")
    aspect_ratio = args.get("aspect_ratio") if isinstance(args.get("aspect_ratio"), str) else "1:1"

    from pocketpaw.config import get_settings

    settings = get_settings()
    if not settings.google_api_key:
        return _error_response(
            "Set POCKETPAW_GOOGLE_API_KEY to enable image generation (Google Gemini)."
        )

    try:
        from google import genai
    except ImportError:
        return _error_response(
            "google-genai package not installed. Install with: pip install 'pocketpaw[image]'."
        )

    from pocketpaw.tools.builtin.image_gen import generate_image_file

    try:
        client = genai.Client(api_key=settings.google_api_key)
        out_path = _generated_dir() / f"{uuid.uuid4()}.png"
        err = generate_image_file(client, settings.image_model, prompt, aspect_ratio, out_path)
        if err:
            return _error_response(err)
        logger.info("media: generated image %s", out_path)
    except Exception as exc:  # noqa: BLE001
        logger.warning("media: image generation failed", exc_info=True)
        return _error_response(f"image generation failed: {exc}")

    result, err = await _land_in_gallery(
        workspace_id=workspace_id,
        user_id=user_id,
        kind="image",
        src=str(out_path),
        prompt=prompt,
    )
    if err is not None or result is None:
        return _error_response(err or "could not add the image to the gallery")

    return _success_response(
        {
            "ok": True,
            "kind": "image",
            "path": str(out_path),
            "pocket_id": result["pocket_id"],
            "gallery_count": result["count"],
        }
    )


async def _poll_replicate(client: Any, token: str, get_url: str) -> dict[str, Any]:
    """Poll a Replicate prediction GET until it reaches a terminal status,
    bounded by ``_POLL_MAX_SECONDS``. Returns the final prediction dict (caller
    inspects ``status`` / ``output`` / ``error``)."""
    headers = {"Authorization": f"Bearer {token}"}
    waited = 0.0
    prediction: dict[str, Any] = {}
    while waited < _POLL_MAX_SECONDS:
        resp = await client.get(get_url, headers=headers, timeout=30.0)
        resp.raise_for_status()
        prediction = resp.json()
        status = prediction.get("status")
        if status in ("succeeded", "failed", "canceled"):
            return prediction
        await asyncio.sleep(_POLL_INTERVAL_SECONDS)
        waited += _POLL_INTERVAL_SECONDS
    prediction["status"] = prediction.get("status") or "timed_out"
    return prediction


async def _video_generate_handler(args: dict) -> dict:
    """MCP handler for ``media__video_generate``.

    Calls the Replicate HTTP API with httpx (no replicate package): POSTs a
    prediction for ``settings.video_model`` with the prompt + duration +
    aspect_ratio, polls the prediction GET until terminal (~120s cap, 3s sleep),
    then lands the resulting output URL in the STUDIO gallery. Sets ``is_error``
    when identity or the token is missing, the prediction fails, or it times out.
    """
    workspace_id, user_id = _identity()
    if not workspace_id or not user_id:
        return _error_response(
            "video_generate requires workspace and user context (call from a cloud chat session)."
        )

    prompt = args.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        return _error_response("video_generate requires a non-empty `prompt`.")
    duration = args.get("duration") if isinstance(args.get("duration"), int) else 5
    aspect_ratio = args.get("aspect_ratio") if isinstance(args.get("aspect_ratio"), str) else "16:9"

    from pocketpaw.config import get_settings

    settings = get_settings()
    if not settings.replicate_api_token:
        return _error_response("Set POCKETPAW_REPLICATE_API_TOKEN to enable video generation.")

    import httpx

    token = settings.replicate_api_token
    model = settings.video_model
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    # Replicate runs a model by owner/name slug via the model-scoped predictions
    # endpoint; the input shape is model-specific but prompt/duration/aspect_ratio
    # are the common kling-style fields.
    create_url = f"{_REPLICATE_BASE}/models/{model}/predictions"
    payload = {
        "input": {
            "prompt": prompt,
            "duration": duration,
            "aspect_ratio": aspect_ratio,
        }
    }

    try:
        async with httpx.AsyncClient() as client:
            create_resp = await client.post(create_url, headers=headers, json=payload, timeout=30.0)
            create_resp.raise_for_status()
            prediction = create_resp.json()
            get_url = (prediction.get("urls") or {}).get("get")
            if not get_url:
                return _error_response("Replicate did not return a prediction poll URL.")
            prediction = await _poll_replicate(client, token, get_url)
    except httpx.HTTPStatusError as exc:
        logger.warning("media: replicate HTTP error", exc_info=True)
        return _error_response(f"video generation request failed: {exc.response.status_code}")
    except Exception as exc:  # noqa: BLE001
        logger.warning("media: video generation failed", exc_info=True)
        return _error_response(f"video generation failed: {exc}")

    status = prediction.get("status")
    if status != "succeeded":
        detail = prediction.get("error") or status
        return _error_response(f"video generation did not succeed: {detail}")

    output = prediction.get("output")
    # Replicate output is a URL or a list of URLs — take the first URL.
    if isinstance(output, list):
        output_url = next((u for u in output if isinstance(u, str)), None)
    elif isinstance(output, str):
        output_url = output
    else:
        output_url = None
    if not output_url:
        return _error_response("video generation succeeded but returned no output URL.")

    result, err = await _land_in_gallery(
        workspace_id=workspace_id,
        user_id=user_id,
        kind="video",
        src=output_url,
        prompt=prompt,
    )
    if err is not None or result is None:
        return _error_response(err or "could not add the video to the gallery")

    return _success_response(
        {
            "ok": True,
            "kind": "video",
            "url": output_url,
            "pocket_id": result["pocket_id"],
            "gallery_count": result["count"],
        }
    )


def build_media_server() -> tuple[str, Any] | None:
    """Build the in-process SDK MCP server for STUDIO media generation, or return
    ``None`` if the Claude Agent SDK isn't installed.

    Matches the shape returned by ``build_sites_manager_server`` (``(name,
    server)`` or ``None``) so the backend's MCP registration loop treats it
    identically.
    """
    try:
        from claude_agent_sdk import create_sdk_mcp_server, tool
    except ImportError:
        logger.debug("claude_agent_sdk not installed; pocketpaw_media MCP disabled")
        return None

    @tool(
        "image_generate",
        (
            "Generate an IMAGE from a text prompt (Google Gemini) and add it to "
            "the user's /studio gallery. Use this on the STUDIO surface when the "
            "user describes an image / poster / illustration to create. Args: "
            "`prompt` (required — describe the image), optional `aspect_ratio` "
            "(e.g. '1:1', '16:9', '9:16') and `size`. Returns {ok, kind:'image', "
            "path, pocket_id, gallery_count}; the image is laid out as a tile in "
            "the gallery pocket and the canvas opens automatically. ok=false with "
            "an error means relay the reason (e.g. the Google API key is not set) "
            "— do NOT claim a phantom image."
        ),
        {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Text description of the image to generate.",
                },
                "aspect_ratio": {
                    "type": "string",
                    "description": "Aspect ratio (default '1:1'). e.g. '1:1', '16:9', '9:16'.",
                },
                "size": {
                    "type": "string",
                    "description": "Output resolution hint (default '1K').",
                },
            },
            "required": ["prompt"],
            "additionalProperties": False,
        },
    )
    async def image_generate(args):  # type: ignore[no-untyped-def]
        return await _image_generate_handler(args)

    @tool(
        "video_generate",
        (
            "Generate a VIDEO from a text prompt (Replicate) and add it to the "
            "user's /studio gallery. Use this on the STUDIO surface when the user "
            "describes a short video / clip / animation to create. Args: `prompt` "
            "(required — describe the video), optional `duration` (seconds, "
            "default 5) and `aspect_ratio` (e.g. '16:9', '9:16'). Generation is "
            "async and may take up to ~2 minutes. Returns {ok, kind:'video', url, "
            "pocket_id, gallery_count}; the video is laid out as a tile in the "
            "gallery pocket and the canvas opens automatically. ok=false with an "
            "error means relay the reason (e.g. POCKETPAW_REPLICATE_API_TOKEN is "
            "not set) — do NOT claim a phantom video."
        ),
        {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Text description of the video to generate.",
                },
                "duration": {
                    "type": "integer",
                    "description": "Clip length in seconds (default 5).",
                },
                "aspect_ratio": {
                    "type": "string",
                    "description": "Aspect ratio (default '16:9'). e.g. '16:9', '9:16'.",
                },
            },
            "required": ["prompt"],
            "additionalProperties": False,
        },
    )
    async def video_generate(args):  # type: ignore[no-untyped-def]
        return await _video_generate_handler(args)

    server = create_sdk_mcp_server(
        name=SERVER_NAME,
        version="1.0.0",
        tools=[image_generate, video_generate],
    )
    return SERVER_NAME, server


__all__ = [
    "IMAGE_GENERATE_TOOL_ID",
    "MEDIA_TOOL_IDS",
    "SERVER_NAME",
    "VIDEO_GENERATE_TOOL_ID",
    "build_media_server",
]
