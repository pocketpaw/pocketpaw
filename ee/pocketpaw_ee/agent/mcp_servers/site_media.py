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
#      follows, so a prompt-injected pocket id cannot reach another tenant.
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

#: Ids the surface allow-list must carry. An id absent from ``sites_allow`` in
#: ``cloud/surface/surface_registry.py`` is SILENTLY filtered out and the tool is
#: unreachable — registration alone proves nothing.
SITE_MEDIA_TOOL_IDS = (GENERATE_SITE_IMAGE_TOOL_ID,)

#: A page needs a hero and a few section images, not a contact sheet. This bounds
#: one call; it is NOT a spend control (see the cost note above).
_MAX_IMAGES_PER_CALL = 4


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

    store = public_asset_store()
    if store is None:
        # A deployment fact, not a failure of this call. Say so plainly and name
        # the fallback so the agent stops trying rather than retrying.
        return _error_response(
            "Public asset storage is not configured on this deployment, so a generated "
            "image would have no address a visitor could load. Use "
            "`mcp__pocketpaw_stock__search_stock_images` instead."
        )

    count = max(1, min(int(args.get("count") or 1), _MAX_IMAGES_PER_CALL))
    aspect_ratio = str(args.get("aspect_ratio") or "16:9")
    size = service._SIZE_MAP.get(aspect_ratio)

    from pocketpaw_ee.agent.mcp_servers.media import _default_image_model

    model = str(args.get("model") or "").strip() or _default_image_model()
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
            "and `model`. Returns {ok, count, assets:[{url, mime, size}], message}. "
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
                "model": {
                    "type": "string",
                    "description": "Catalog image-model id (optional; default = deployment model).",
                },
            },
            "required": ["pocket_id", "prompt"],
            "additionalProperties": False,
        },
    )
    async def generate_site_image(args):  # type: ignore[no-untyped-def]
        return await _generate_site_image_handler(args)

    server = create_sdk_mcp_server(name=SERVER_NAME, tools=[generate_site_image])
    return SERVER_NAME, server


__all__ = [
    "GENERATE_SITE_IMAGE_TOOL_ID",
    "SERVER_NAME",
    "SITE_MEDIA_TOOL_IDS",
    "build_site_media_server",
]
