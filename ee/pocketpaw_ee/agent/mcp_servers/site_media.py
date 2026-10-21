# site_media.py — in-process MCP server letting the SITE-AUTHORING agent generate
# imagery and video straight onto the public asset rail.
#
# Thin MCP wrappers over ``pocketpaw_ee.sites.media_generation``, which owns the
# generation, the ownership guard, the public sink and the history record (and is
# shared with the builder UI's ``/assets/generate`` route). This module owns only
# what is agent-specific: argument coercion, the early-access refusal, and error
# text written for the agent to act on (fall back to stock, never invent a URL).
#
# WHY THIS IS NOT JUST ``media.py`` ON THE /sites ALLOW-LIST. Studio's media
# server writes the PRIVATE adapter and returns a backend-relative
# ``/api/v1/media/<name>``, which 404s on a published site's domain; and it
# refreshes a ripple gallery pocket on the chat canvas every call, which is noise
# mid-build. Generations made here still land in the /studio history
# (``source="sites"``), so they show up there beside the user's own work.
#
# TENANCY. ``pocket_id`` comes from the args because the agent chooses which site
# it is building; ``workspace_id`` comes from the per-stream ContextVars and NEVER
# from the args. The /sites allow-list does NOT confine these tools:
# ``allow_mcp_tool_ids`` is None (= unrestricted) on every surface spec that sets
# no profile, so an ambient server is reachable from /chat too. The pocket
# ownership check in ``media_generation`` is the only real boundary.
#
# VIDEO ENGINES: ``generate_site_video`` takes ``engine`` — ``motion`` (animated
# type/shapes/brand graphics rendered from HTML by HyperFrames, no fal needed),
# ``generative`` (fal footage) or ``auto`` (fal when the server has a key).
#
# COST: unmetered (real metering is SM-2). ``_MAX_IMAGES_PER_CALL`` bounds one
# call; it is not a meter. An unpaid early-access workspace is refused before
# any generation (``media._early_access_refusal``).

from __future__ import annotations

import json
import logging
from typing import Any

from pocketpaw_ee.agent.mcp_servers.media import _early_access_refusal
from pocketpaw_ee.sites.media_generation import (  # noqa: F401 — re-exported for tests
    _MAX_IMAGES_PER_CALL,
    _VIDEO_DURATIONS,
)

logger = logging.getLogger(__name__)

SERVER_NAME = "pocketpaw_site_media"

GENERATE_SITE_IMAGE_TOOL_ID = f"mcp__{SERVER_NAME}__generate_site_image"
GENERATE_SITE_VIDEO_TOOL_ID = f"mcp__{SERVER_NAME}__generate_site_video"

#: Ids the surface allow-list must carry. An id absent from ``sites_allow`` in
#: ``cloud/surface/surface_registry.py`` is SILENTLY filtered out and the tool is
#: unreachable — registration alone proves nothing.
SITE_MEDIA_TOOL_IDS = (GENERATE_SITE_IMAGE_TOOL_ID, GENERATE_SITE_VIDEO_TOOL_ID)


def _error_response(message: str) -> dict[str, Any]:
    """MCP error shape. The agent reads ``text`` and surfaces the reason."""
    return {"content": [{"type": "text", "text": f"Error: {message}"}], "is_error": True}


def _success_response(body: dict[str, Any]) -> dict[str, Any]:
    """MCP success shape carrying ``body`` as JSON."""
    return {
        "content": [{"type": "text", "text": json.dumps(body, separators=(",", ":"), default=str)}]
    }


def _identity() -> tuple[str | None, str | None]:
    """Workspace + user from the per-stream ContextVars. Never from tool args."""
    try:
        from pocketpaw_ee.cloud.chat.agent_service import current_user_id, current_workspace_id

        return current_workspace_id(), current_user_id()
    except Exception:  # noqa: BLE001
        return None, None


async def _generate_site_image_handler(args: dict) -> dict:
    """Generate one or more images and land them on the site's public rail."""
    from pocketpaw_ee.sites.media_generation import SiteMediaError, generate_site_image_assets

    prompt = str(args.get("prompt") or "").strip()
    if not prompt:
        return _error_response("prompt is required — describe the image to generate.")

    pocket_id = str(args.get("pocket_id") or "").strip()
    if not pocket_id:
        return _error_response("pocket_id is required — say which site this image belongs to.")

    workspace_id, _user_id = _identity()
    if not workspace_id:
        return _error_response("No active workspace — cannot generate site media.")
    if (refusal := await _early_access_refusal(workspace_id)) is not None:
        return refusal

    # MCP input schemas are ADVISORY for in-process SDK tools, so a model that
    # emits {"count": "two"} would otherwise raise ValueError straight out of the
    # handler instead of the structured error every other path here returns.
    try:
        requested = int(args.get("count") or 1)
    except (TypeError, ValueError):
        requested = 1

    try:
        result = await generate_site_image_assets(
            workspace_id=workspace_id,
            pocket_id=pocket_id,
            prompt=prompt,
            count=requested,
            aspect_ratio=str(args.get("aspect_ratio") or "16:9"),
        )
    except SiteMediaError as exc:
        if exc.reason == "no_store":
            # A deployment fact, not a failure of this call. Say so plainly and
            # name the fallback so the agent stops trying rather than retrying.
            return _error_response(
                "Public asset storage is not configured on this deployment, so a generated "
                "image would have no address a visitor could load. Use "
                "`mcp__pocketpaw_stock__search_stock_images` instead."
            )
        if exc.reason == "failed":
            return _error_response(
                "Image generation failed: "
                + exc.message
                + ". Do NOT invent an image URL — fall back to "
                "`mcp__pocketpaw_stock__search_stock_images`, or to a tasteful gradient."
            )
        return _error_response(exc.message)

    assets: list[dict[str, Any]] = []
    for asset in result.assets:
        assets.append({"url": asset.url, "mime": asset.mime, "size": asset.size})
    errors = result.errors

    return _success_response(
        {
            "ok": True,
            "count": len(assets),
            "assets": assets,
            "message": (
                f"{len(assets)} image(s) generated and published for this site. Each `url` "
                "is absolute, public and permanent — put it in `src` VERBATIM, do not "
                "rewrite it and do not copy the file into the source map."
                + (f" {len(errors)} attempt(s) failed: {'; '.join(errors)}" if errors else "")
            ),
        }
    )


async def _generate_site_video_handler(args: dict) -> dict:
    """Generate a short video for the site and land it on the public rail, with
    its poster frame (a scroll-scrubbed ``<video>`` paints nothing until it has
    buffered, so without a poster the hero is blank on first load)."""
    from pocketpaw_ee.sites.media_generation import (
        DEFAULT_VIDEO_ENGINE,
        SiteMediaError,
        generate_site_video_asset,
    )

    prompt = str(args.get("prompt") or "").strip()
    if not prompt:
        return _error_response("prompt is required — describe the shot and the camera move.")

    pocket_id = str(args.get("pocket_id") or "").strip()
    if not pocket_id:
        return _error_response("pocket_id is required — say which site this video belongs to.")

    workspace_id, _user_id = _identity()
    if not workspace_id:
        return _error_response("No active workspace — cannot generate site media.")
    if (refusal := await _early_access_refusal(workspace_id)) is not None:
        return refusal

    try:
        duration_sec = int(args.get("duration_sec") or 5)
    except (TypeError, ValueError):
        duration_sec = 5

    raw_palette = args.get("palette")
    palette = (
        [str(c).strip() for c in raw_palette if str(c).strip()][:6]
        if isinstance(raw_palette, list)
        else None
    )

    try:
        result = await generate_site_video_asset(
            workspace_id=workspace_id,
            pocket_id=pocket_id,
            prompt=prompt,
            image_url=str(args.get("image_url") or "").strip() or None,
            aspect_ratio=str(args.get("aspect_ratio") or "16:9"),
            duration_sec=duration_sec,
            video_engine=str(args.get("engine") or DEFAULT_VIDEO_ENGINE),
            palette=palette,
        )
    except SiteMediaError as exc:
        if exc.reason == "no_store":
            return _error_response(
                "Public asset storage is not configured on this deployment, so a generated "
                "video would have no address a visitor could load."
            )
        if exc.reason == "busy":
            return _error_response(
                f"{exc.message} Build the page with a still hero now; offer the user to "
                "add the video later rather than retrying in a loop."
            )
        if exc.reason == "unavailable":
            return _error_response(
                f"{exc.message} Do NOT retry this engine in this build. Use the other "
                "engine, or fall back to a still hero via generate_site_image."
            )
        if exc.reason == "failed":
            return _error_response(
                f"Video generation failed: {exc.message}. Do NOT invent a video URL — fall "
                "back to a still hero via generate_site_image, or to stock photography."
            )
        return _error_response(exc.message)

    video = result.assets[0]
    if result.video_engine == "motion":
        usage = (
            "This is a seamless loop made for a background: render it as a muted, "
            "autoplay, loop, playsinline video element WITH the poster set, layered "
            "under the hero's own headline and buttons (object-fit: cover). Respect "
            "prefers-reduced-motion by showing the poster instead."
        )
    else:
        usage = (
            "For a scroll-scrubbed hero, render a muted playsinline video element "
            "WITH the poster set, and drive its currentTime from scroll position. "
            "Seeks land on KEYFRAMES, so scrubbing is chunky by nature: map a long "
            "scroll distance onto the clip rather than a short one, and never rely "
            "on frame-exact stops."
        )
    return _success_response(
        {
            "ok": True,
            "url": video.url,
            "poster_url": result.poster.url if result.poster else None,
            "mime": video.mime,
            "size": video.size,
            "duration_sec": result.duration_sec,
            "engine": result.video_engine,
            "message": (
                "Video published for this site. Both URLs are absolute, public and "
                "permanent — use them VERBATIM. " + usage
            ),
        }
    )


def build_site_media_server() -> tuple[str, Any] | None:
    """Build the in-process SDK MCP server, or ``None`` if the SDK isn't installed.

    Returns the ``(name, server)`` shape ``build_sites_manager_server`` returns so
    the backend's registration loop treats it identically.
    """
    try:
        from claude_agent_sdk import create_sdk_mcp_server, tool
    except ImportError:
        logger.debug("claude_agent_sdk not installed; pocketpaw_site_media MCP disabled")
        return None

    @tool(
        "generate_site_image",
        (
            "Generate original imagery for the site you are building and publish it "
            "to the site's own asset storage, returning a PERMANENT PUBLIC URL you "
            "can put straight into markup. Use this when the page needs a visual "
            "that stock photography cannot supply — a bespoke hero, a product or "
            "concept image, an abstract brand texture. For ordinary photography "
            "(an office, a meal, a person at work) prefer "
            "`mcp__pocketpaw_stock__search_stock_images`, which is free and instant; "
            "this costs money per image. Args: `pocket_id` (required — the site), "
            "`prompt` (required — describe the image in concrete visual terms: "
            "subject, composition, lighting, mood, style), optional `aspect_ratio` "
            "('16:9' default, '1:1', '9:16', '4:3', '3:2'), `count` (1-4, default 1) "
            "Returns {ok, count, assets:[{url, mime, size}], message}. "
            "On failure it returns an error — relay it and fall back to stock or a "
            "gradient. NEVER invent an image URL."
        ),
        {
            "type": "object",
            "properties": {
                "pocket_id": {
                    "type": "string",
                    "description": "The site's pocket id — the image is stored against this site.",
                },
                "prompt": {
                    "type": "string",
                    "minLength": 1,
                    "description": (
                        "Concrete visual description: subject, composition, lighting, "
                        "mood, style. Vague prompts produce generic images."
                    ),
                },
                "aspect_ratio": {
                    "type": "string",
                    "enum": ["1:1", "16:9", "9:16", "4:3", "3:4", "3:2", "2:3"],
                    "description": "Shape of the image. Default '16:9'.",
                },
                "count": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": _MAX_IMAGES_PER_CALL,
                    "description": "How many images to generate (1-4). Each one costs money.",
                },
            },
            "required": ["pocket_id", "prompt"],
            "additionalProperties": False,
        },
    )
    async def generate_site_image(args):  # type: ignore[no-untyped-def]
        return await _generate_site_image_handler(args)

    @tool(
        "generate_site_video",
        (
            "Generate a SHORT VIDEO for the site you are building and publish it, "
            "returning permanent public URLs for the clip and its poster frame. "
            "Two engines (`engine`): `motion` renders animated typography, shapes "
            "and brand graphics from HTML as a seamless loop, which is great for a "
            "hero BACKGROUND and works on every server (pass the site's brand "
            "colours as `palette`); `generative` makes photoreal footage with a "
            "video model and needs fal on the server. `auto` (default) uses "
            "generative when it is available, else motion. Pass `image_url` to "
            "ANIMATE AN EXISTING IMAGE (the owner's uploaded photo, or one you just "
            "generated, by its site asset URL): generative moves their actual "
            "subject; motion places it "
            "with a slow Ken Burns / parallax move under graphic layers. For "
            "generative, describe the CAMERA MOVE in the prompt ('slow dolly in', "
            "'orbit left'); for motion, describe the ONE visual idea, mood and "
            "pace ('slow aurora of brand colours with drifting rings'). This is the "
            "most expensive call available to you and it takes a minute or more — "
            "use it for one hero moment, never decoratively, and tell the user it "
            "is rendering before you wait on it. Args: `pocket_id` and `prompt` "
            "(required), optional `engine`, `palette`, `image_url`, `duration_sec` "
            "(5 or 10) and `aspect_ratio`. Returns {ok, url, poster_url, mime, "
            "size, duration_sec, engine, message}. On failure, relay the reason "
            "and fall back to a still hero — NEVER invent a video URL."
        ),
        {
            "type": "object",
            "properties": {
                "pocket_id": {
                    "type": "string",
                    "description": "The site's pocket id — the video is stored against this site.",
                },
                "prompt": {
                    "type": "string",
                    "minLength": 1,
                    "description": (
                        "The shot AND the camera move, concretely: subject, motion, "
                        "pace, mood. The camera move is prose, not a parameter."
                    ),
                },
                "image_url": {
                    "type": "string",
                    "description": (
                        "Image to animate (image-to-video): one of THIS site's asset "
                        "URLs (the owner's upload from `list_site_assets`, or one "
                        "`generate_site_image` returned). Any other URL is refused."
                    ),
                },
                "duration_sec": {
                    "type": "integer",
                    "enum": [5, 10],
                    "description": "Clip length. Default 5.",
                },
                "engine": {
                    "type": "string",
                    "enum": ["auto", "motion", "generative"],
                    "description": (
                        "motion = animated typography/shapes/brand graphics rendered "
                        "from HTML, a seamless loop, great for hero backgrounds, no "
                        "fal needed. generative = photoreal footage from a video "
                        "model, needs fal. auto (default) = generative when the "
                        "server has fal, else motion."
                    ),
                },
                "palette": {
                    "type": "array",
                    "items": {"type": "string"},
                    "maxItems": 6,
                    "description": (
                        "Brand colours as CSS colours ('#0f172a'), dominant first. "
                        "Steers the motion engine; ignored by generative."
                    ),
                },
                "aspect_ratio": {
                    "type": "string",
                    "enum": ["16:9", "9:16", "1:1"],
                    "description": "Shape of the clip. Default 16:9.",
                },
            },
            "required": ["pocket_id", "prompt"],
            "additionalProperties": False,
        },
    )
    async def generate_site_video(args):  # type: ignore[no-untyped-def]
        return await _generate_site_video_handler(args)

    server = create_sdk_mcp_server(
        name=SERVER_NAME, tools=[generate_site_image, generate_site_video]
    )
    return SERVER_NAME, server


__all__ = [
    "GENERATE_SITE_IMAGE_TOOL_ID",
    "GENERATE_SITE_VIDEO_TOOL_ID",
    "SERVER_NAME",
    "SITE_MEDIA_TOOL_IDS",
    "build_site_media_server",
]
