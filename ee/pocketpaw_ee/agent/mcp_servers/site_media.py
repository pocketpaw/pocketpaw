# site_media.py — in-process MCP server letting the SITE-AUTHORING agent generate
# imagery straight onto the public asset rail.
#
# Created 2026-09-09 (feat/site-media-tools, SM-1).
#
# WHY THIS IS NOT JUST ``media.py`` ON THE /sites ALLOW-LIST. The studio media
# server exists and generates well, and ``surface_registry`` deliberately keeps it
# off /sites ("site imagery leans on stock first"). Adding its ids there would not
# work anyway, because it differs from what a site needs in three ways:
#
#   1. THE SINK. ``media.py`` writes ``media_storage.save_generated`` — the PRIVATE
#      adapter — and returns a BACKEND-RELATIVE ``/api/v1/media/<name>``. Pasted
#      into a published site that resolves against the SITE's own domain and 404s.
#      A site asset has to land on ``sites.public_assets`` (absolute, unsigned,
#      tenant-scoped, immutable) or it is a broken image the moment it ships.
#   2. THE SIDE EFFECT. ``media.py`` creates and refreshes a ripple GALLERY POCKET
#      on the chat canvas every call. A tile appearing mid-build is noise when the
#      user asked for a website. (The /studio GALLERY — the page reading
#      ``GET /studio/generations`` — is a different thing, and this server DOES
#      write that: see the history record below.)
#   3. TENANCY. ``pocket_id`` comes from the args because the agent chooses which
#      site it is building, but ``workspace_id`` comes from the per-stream
#      ContextVars and NEVER from the args — the same rule ``list_site_assets``
#      follows. The handler then checks the pocket actually belongs to that
#      workspace, because the /sites allow-list does NOT confine this tool:
#      ``allow_mcp_tool_ids`` is None (= unrestricted) on every surface spec that
#      sets no profile, so an ambient server is reachable from /chat too. Listing
#      the id under ``sites_allow`` makes it reachable where it is wanted; it does
#      not make it unreachable anywhere else. Only the ownership check does that.
#
# WHAT IT SHARES WITH STUDIO. The generation itself is ``studio.service``
# verbatim (same proxy call, same per-tenant virtual key, same spend attribution),
# and every generation is recorded in the SAME history with ``source="sites"`` and
# the pocket id, so an asset made while building a site shows up in /studio beside
# the user's own work and can be fed into the Flow canvas. One asset, one history,
# both surfaces.
#
# COST: THERE IS NO METER ON THIS YET. Studio generation is unmetered today —
# ``StudioModel.credits`` is decorative, no route debits a wallet, and
# ``require_license`` checks expiry rather than plan. Text→image at least runs on
# the workspace's own LiteLLM virtual key (so the proxy's budget and the
# after-the-fact spend sweep apply); anything on fal bills the deployment key with
# no check before and no ledger row after. Putting generation on the AGENT's path
# means a retry loop can spend with no human in between. The per-call ceiling here
# (``_MAX_IMAGES_PER_CALL``) is a floor, not a meter — real metering is SM-2 and
# has to land before this is enabled broadly.

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any

logger = logging.getLogger(__name__)

SERVER_NAME = "pocketpaw_site_media"

GENERATE_SITE_IMAGE_TOOL_ID = f"mcp__{SERVER_NAME}__generate_site_image"
GENERATE_SITE_VIDEO_TOOL_ID = f"mcp__{SERVER_NAME}__generate_site_video"

#: Ids the surface allow-list must carry. An id absent from ``sites_allow`` in
#: ``cloud/surface/surface_registry.py`` is SILENTLY filtered out and the tool is
#: unreachable — registration alone proves nothing.
SITE_MEDIA_TOOL_IDS = (GENERATE_SITE_IMAGE_TOOL_ID, GENERATE_SITE_VIDEO_TOOL_ID)

#: A page needs a hero and a few section images, not a contact sheet. This bounds
#: one call; it is NOT a spend control (see the cost note above).
_MAX_IMAGES_PER_CALL = 4

#: A hero backdrop, not a film. Kling's standard tiers take 5 or 10 seconds;
#: longer costs more and a scroll-scrubbed clip gains nothing from length,
#: because the page maps the whole duration onto one scroll distance anyway.
_VIDEO_DURATIONS = (5, 10)


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


def _filename_for(prompt: str, index: int) -> str:
    """A readable, safe filename stem. The rail content-addresses the key, so this
    only has to be human-legible in a listing — collisions are impossible."""
    words = [w for w in "".join(c if c.isalnum() or c.isspace() else " " for c in prompt).split()]
    stem = "-".join(words[:5]).lower() or "generated"
    return f"{stem}-{index + 1}.png"


async def _generate_site_image_handler(args: dict) -> dict:
    """Generate one or more images and land them on the site's public rail."""
    from pocketpaw_ee.cloud.studio import schemas, service
    from pocketpaw_ee.sites.public_assets import PublicAssetError, public_asset_store

    prompt = str(args.get("prompt") or "").strip()
    if not prompt:
        return _error_response("prompt is required — describe the image to generate.")

    pocket_id = str(args.get("pocket_id") or "").strip()
    if not pocket_id:
        return _error_response("pocket_id is required — say which site this image belongs to.")

    workspace_id, _user_id = _identity()
    if not workspace_id:
        return _error_response("No active workspace — cannot generate site media.")

    # THE ACTUAL GUARD, and it is not the surface allow-list. ``allow_mcp_tool_ids``
    # defaults to None = NO restriction, and most surface specs set no profile at
    # all — so an ambient tool is callable from /chat and elsewhere, whatever this
    # module's allow-list entry suggests. Authorisation has to be intrinsic:
    # the pocket must belong to the caller's workspace, so a made-up or another
    # tenant's id cannot be used to spend against someone else's site.
    from pocketpaw_ee.cloud.pockets import service as pockets_service

    owner = await pockets_service.get_pocket_workspace(pocket_id)
    if owner != workspace_id:
        return _error_response(f"No site {pocket_id!r} in this workspace — nothing was generated.")

    store = public_asset_store()
    if store is None:
        # A deployment fact, not a failure of this call. Say so plainly and name
        # the fallback so the agent stops trying rather than retrying.
        return _error_response(
            "Public asset storage is not configured on this deployment, so a generated "
            "image would have no address a visitor could load. Use "
            "`mcp__pocketpaw_stock__search_stock_images` instead."
        )

    # MCP input schemas are ADVISORY for in-process SDK tools, so a model that
    # emits {"count": "two"} would otherwise raise ValueError straight out of the
    # handler instead of the structured error every other path here returns.
    try:
        requested = int(args.get("count") or 1)
    except (TypeError, ValueError):
        requested = 1
    count = max(1, min(requested, _MAX_IMAGES_PER_CALL))
    aspect_ratio = str(args.get("aspect_ratio") or "16:9")
    size = service._SIZE_MAP.get(aspect_ratio)

    from pocketpaw_ee.agent.mcp_servers.media import _default_image_model

    # NO caller-supplied model, deliberately. ``service.generate`` routes a curated
    # id (``fal_image.IMAGE_MODEL_IDS``) to fal's own endpoint, while this handler
    # dispatches through the LiteLLM proxy — so advertising `model` as 'a catalog
    # id' would hand the agent ids that work on /studio and 400 here. Choosing the
    # generator belongs with the generation work, not with this integration.
    model = _default_image_model()
    auth_key = await service._resolve_auth_key(workspace_id)

    assets: list[dict[str, Any]] = []
    errors: list[str] = []

    for index in range(count):
        image_bytes, err = await service._proxy_generate_image(
            model=model,
            prompt=prompt,
            size=size,
            user=workspace_id,
            auth_key=auth_key,
        )
        if err is not None or image_bytes is None:
            errors.append(err or "the model returned no image")
            continue

        try:
            asset = await store.put(
                image_bytes,
                filename=_filename_for(prompt, index),
                workspace_id=workspace_id,
                pocket_id=pocket_id,
            )
        except PublicAssetError as exc:
            errors.append(str(exc))
            continue
        except Exception as exc:  # noqa: BLE001
            # A StorageFailure or an S3 client error is not a PublicAssetError. If
            # one escapes here it takes the whole handler with it, so the images
            # ALREADY generated, paid for and stored in this loop are never
            # returned and no history row is written for them.
            logger.warning("site_media: could not store a generated image", exc_info=True)
            errors.append(f"could not store the image: {exc}")
            continue

        assets.append({"url": asset.url, "mime": asset.mime, "size": asset.size})

    # Record in the SAME history /studio reads, tagged so the gallery can tell a
    # site agent's output from the user's own. Best-effort: a history failure must
    # not lose an image the caller already has a working URL for.
    try:
        await service.record_generation(
            workspace_id,
            schemas.Generation(
                # uuid, not hash(): PYTHONHASHSEED randomises str hashing per
                # process, so a hash-derived id would collide or drift between
                # the web container and the worker.
                id=f"site-{uuid.uuid4().hex[:16]}",
                prompt=prompt,
                status="succeeded" if assets else "failed",
                kind="image",
                model=model,
                params=schemas.GenerationParams(
                    kind="image",
                    model=model,
                    aspectRatio=aspect_ratio,
                    count=len(assets) or count,
                ),
                assets=[
                    schemas.GeneratedAsset(id=str(i), url=a["url"], mime=a["mime"])
                    for i, a in enumerate(assets)
                ],
                createdAt=int(time.time() * 1000),
                error="; ".join(errors) or None,
            ),
            source="sites",
            pocket_id=pocket_id,
        )
    except Exception:  # noqa: BLE001
        logger.warning("site_media: could not record generation history", exc_info=True)

    if not assets:
        return _error_response(
            "Image generation failed: "
            + ("; ".join(errors) or "unknown error")
            + ". Do NOT invent an image URL — fall back to "
            "`mcp__pocketpaw_stock__search_stock_images`, or to a tasteful gradient."
        )

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
    """Generate a short video for the site and land it on the public rail.

    Shares the image tool's contract: public sink, ownership guard, history row.
    Two things differ, and both come from video being slow and expensive.

    IT RECORDS BEFORE IT GENERATES. A fal render takes minutes. The row is written
    ``running`` first, so /studio shows the job while it is in flight and a reload
    does not lose it — the property the JSONL store could not hold, because
    append-on-success was its only write. On the way out the SAME row moves to
    succeeded or failed rather than a second row appearing.

    IT STORES THE POSTER TOO. ``run_fal_video`` returns a poster frame beside the
    video, and a scroll-scrubbed ``<video>`` needs one: the element paints nothing
    until enough data has buffered to decode a frame, so without a poster the hero
    is blank on first load.
    """
    import time
    import uuid

    from pocketpaw_ee.cloud.pockets import service as pockets_service
    from pocketpaw_ee.cloud.studio import fal_video, schemas, service
    from pocketpaw_ee.sites.public_assets import PublicAssetError, public_asset_store

    prompt = str(args.get("prompt") or "").strip()
    if not prompt:
        return _error_response("prompt is required — describe the shot and the camera move.")

    pocket_id = str(args.get("pocket_id") or "").strip()
    if not pocket_id:
        return _error_response("pocket_id is required — say which site this video belongs to.")

    workspace_id, _user_id = _identity()
    if not workspace_id:
        return _error_response("No active workspace — cannot generate site media.")

    owner = await pockets_service.get_pocket_workspace(pocket_id)
    if owner != workspace_id:
        return _error_response(f"No site {pocket_id!r} in this workspace — nothing was generated.")

    store = public_asset_store()
    if store is None:
        return _error_response(
            "Public asset storage is not configured on this deployment, so a generated "
            "video would have no address a visitor could load."
        )

    image_url = str(args.get("image_url") or "").strip()
    aspect_ratio = str(args.get("aspect_ratio") or "16:9")
    try:
        duration_sec = int(args.get("duration_sec") or 5)
    except (TypeError, ValueError):
        duration_sec = 5
    if duration_sec not in _VIDEO_DURATIONS:
        duration_sec = 5

    # An image turns this into image-to-video, which is the whole point when the
    # owner handed us a photo: the clip MOVES THEIR IMAGE rather than inventing a
    # new scene that merely resembles it.
    model = fal_video.DEFAULT_IMAGE_TO_VIDEO_MODEL if image_url else fal_video.DEFAULT_VIDEO_MODEL

    input_urls: list[str] | None = None
    if image_url:
        try:
            data_url, _mime = await service._resolve_source_data_url(image_url)
        except Exception as exc:  # noqa: BLE001
            return _error_response(f"Could not read the source image {image_url!r}: {exc}")
        input_urls = [data_url]

    generation_id = f"site-vid-{uuid.uuid4().hex[:16]}"

    def _record(status: str, assets: list, error: str | None = None):
        return schemas.Generation(
            id=generation_id,
            prompt=prompt,
            status=status,
            kind="video",
            model=model,
            params=schemas.GenerationParams(
                kind="video",
                model=model,
                aspectRatio=aspect_ratio,
                count=1,
                durationSec=duration_sec,
            ),
            assets=assets,
            createdAt=int(time.time() * 1000),
            error=error,
        )

    async def _fail(reason: str) -> dict:
        await service.record_generation_best_effort(
            workspace_id, _record("failed", [], reason), source="sites", pocket_id=pocket_id
        )
        return _error_response(
            f"Video generation failed: {reason}. Do NOT invent a video URL — fall back to a "
            "still hero via generate_site_image, or to stock photography."
        )

    # Running BEFORE the await, so the job is visible while it renders.
    await service.record_generation_best_effort(
        workspace_id, _record("running", []), source="sites", pocket_id=pocket_id
    )

    try:
        video_bytes, video_mime, poster_bytes, poster_mime = await fal_video.run_fal_video(
            prompt=prompt,
            duration_sec=duration_sec,
            aspect_ratio=aspect_ratio,
            model=model,
            image_urls=input_urls,
        )
    except Exception as exc:  # noqa: BLE001 — fal raises its own hierarchy
        return await _fail(str(exc))

    if not video_bytes:
        return await _fail("the model returned no video")

    stem = _filename_for(prompt, 0).rsplit(".", 1)[0]
    video_ext = "webm" if "webm" in (video_mime or "") else "mp4"
    try:
        video = await store.put(
            video_bytes,
            filename=f"{stem}.{video_ext}",
            workspace_id=workspace_id,
            pocket_id=pocket_id,
        )
    except PublicAssetError as exc:
        # The 50 MiB ceiling and the mime allow-list live on the rail, and its
        # message is written to be shown to a user, so pass it through intact.
        return await _fail(str(exc))
    except Exception as exc:  # noqa: BLE001 — StorageFailure / S3 client errors
        logger.warning("site_media: could not store a generated video", exc_info=True)
        return await _fail(str(exc))

    poster_url = None
    if poster_bytes:
        poster_ext = "png" if "png" in (poster_mime or "") else "jpg"
        try:
            poster = await store.put(
                poster_bytes,
                filename=f"{stem}-poster.{poster_ext}",
                workspace_id=workspace_id,
                pocket_id=pocket_id,
            )
            poster_url = poster.url
        except Exception:  # noqa: BLE001 — a missing poster is a worse page, not a failure
            logger.warning("site_media: could not store the poster frame", exc_info=True)

    assets = [schemas.GeneratedAsset(id="0", url=video.url, mime=video.mime)]
    if poster_url:
        assets.append(schemas.GeneratedAsset(id="1", url=poster_url, mime="image/jpeg"))
    await service.record_generation_best_effort(
        workspace_id, _record("succeeded", assets), source="sites", pocket_id=pocket_id
    )

    return _success_response(
        {
            "ok": True,
            "url": video.url,
            "poster_url": poster_url,
            "mime": video.mime,
            "size": video.size,
            "duration_sec": duration_sec,
            "message": (
                "Video published for this site. Both URLs are absolute, public and "
                "permanent — use them VERBATIM. For a scroll-scrubbed hero, render a "
                "muted playsinline video element WITH the poster set, and drive its "
                "currentTime from scroll position. Seeks land on KEYFRAMES, so "
                "scrubbing is chunky by nature: map a long scroll distance onto the "
                "clip rather than a short one, and never rely on frame-exact stops."
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
            "Pass `image_url` to ANIMATE AN EXISTING IMAGE — the owner's uploaded "
            "photo, or one you just generated — which is what you want for a hero "
            "that moves their actual subject rather than inventing a lookalike. "
            "Omit it for a purely generated clip. Describe the CAMERA MOVE in the "
            "prompt ('slow dolly in', 'orbit left around the subject', 'gentle "
            "handheld push'), because the move comes from the words, not from a "
            "parameter. This is the most expensive call available to you and it "
            "takes MINUTES — use it for one hero moment, never decoratively, and "
            "tell the user it is rendering before you wait on it. Args: `pocket_id` "
            "and `prompt` (required), optional `image_url`, `duration_sec` (5 or "
            "10) and `aspect_ratio`. Returns {ok, url, poster_url, mime, size, "
            "duration_sec, message}. On failure, relay the reason and fall back to "
            "a still hero — NEVER invent a video URL."
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
                        "Public URL of an image to animate (image-to-video). Use the "
                        "owner's uploaded photo here when there is one."
                    ),
                },
                "duration_sec": {
                    "type": "integer",
                    "enum": [5, 10],
                    "description": "Clip length. Default 5.",
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
