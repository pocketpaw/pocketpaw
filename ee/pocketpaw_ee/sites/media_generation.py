# ee/pocketpaw_ee/sites/media_generation.py — Studio media onto a site's public rail.
#
# The one implementation behind both callers that put generated media on a site:
# the site-authoring agent's MCP tools (``agent/mcp_servers/site_media.py``) and
# the builder UI's REST routes (``sites/router.py``, ``/assets/generate`` and
# ``/assets/from-studio``). Every function takes ``workspace_id`` explicitly; the
# caller resolves it (ContextVars for the agent, the request context for HTTP)
# and NEVER from model- or client-supplied input.
#
# INVARIANTS A READER MUST NOT BREAK:
#   * OWNERSHIP IS CHECKED HERE, BEFORE ANY SPEND. The pocket must belong to the
#     workspace. The /sites MCP allow-list does not confine the agent tools, and
#     an HTTP gate on fabric.write does not prove the pocket is the caller's.
#   * THE SINK IS ``PublicAssetStore``. A private ``/api/v1/media/<name>`` URL
#     resolves against a published site's own domain and 404s.
#   * EVERY GENERATION LANDS IN THE STUDIO HISTORY with ``source="sites"`` and
#     the pocket id, so /studio shows it beside the user's own work. Video writes
#     its row ``running`` before the render and moves that same row afterwards.
#   * COPYING FROM STUDIO READS ONLY THE GENERATION'S OWN STORED MEDIA — the
#     private media adapter, or this workspace's prefix on the public rail. It
#     never fetches a URL, so a crafted record cannot turn it into an SSRF.
#
# COST: Studio generation is unmetered (``StudioModel.credits`` is decorative).
# ``_MAX_IMAGES_PER_CALL`` bounds one call; it is not a meter. Real metering is
# SM-2. Callers apply the early-access refusal before calling in here.

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pocketpaw_ee.sites.public_assets import PublicAsset, PublicAssetStore

logger = logging.getLogger(__name__)

#: A page needs a hero and a few section images, not a contact sheet.
_MAX_IMAGES_PER_CALL = 4

#: Kling's standard tiers. A scroll-scrubbed clip gains nothing from length.
_VIDEO_DURATIONS = (5, 10)

_MEDIA_URL_PREFIX = "/api/v1/media/"


class SiteMediaError(Exception):
    """A refusal or failure. ``reason`` is a closed vocabulary callers map:

    ``not_found`` (pocket not in this workspace), ``no_store`` (no public bucket),
    ``invalid`` (bad input), ``bad_source`` (an unreadable or foreign source image),
    ``generation_not_found``, ``unsupported`` (not an image/video generation),
    ``not_ready`` (no finished media), ``missing`` (media gone from storage),
    ``rejected`` (the rail refused the bytes), ``failed`` (the provider or the
    store failed). ``message`` is safe to show a user.
    """

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message


@dataclass(frozen=True)
class SiteMediaResult:
    """What landed on the rail. ``errors`` lists attempts that failed while
    others succeeded (images only); ``poster`` is a video's poster frame."""

    generation_id: str
    assets: list[PublicAsset]
    poster: PublicAsset | None = None
    errors: list[str] = field(default_factory=list)
    duration_sec: int | None = None


def filename_for(prompt: str, index: int) -> str:
    """A readable, safe filename. The rail content-addresses the key, so this only
    has to be legible in a listing; collisions are impossible."""
    words = [w for w in "".join(c if c.isalnum() or c.isspace() else " " for c in prompt).split()]
    stem = "-".join(words[:5]).lower() or "generated"
    return f"{stem}-{index + 1}.png"


async def _require_site(workspace_id: str, pocket_id: str) -> None:
    from pocketpaw_ee.cloud.pockets import service as pockets_service

    owner = await pockets_service.get_pocket_workspace(pocket_id)
    if owner != workspace_id:
        raise SiteMediaError(
            "not_found", f"No site {pocket_id!r} in this workspace — nothing was generated."
        )


def _store_or_raise() -> PublicAssetStore:
    from pocketpaw_ee.sites.public_assets import public_asset_store

    store = public_asset_store()
    if store is None:
        raise SiteMediaError(
            "no_store", "Public asset storage is not configured on this deployment."
        )
    return store


async def generate_site_image_assets(
    *,
    workspace_id: str,
    pocket_id: str,
    prompt: str,
    count: int = 1,
    aspect_ratio: str = "16:9",
) -> SiteMediaResult:
    """Generate ``count`` images (clamped to 1-4) and publish them for the site."""
    from pocketpaw_ee.cloud.studio import schemas, service
    from pocketpaw_ee.sites.public_assets import PublicAssetError

    prompt = prompt.strip()
    if not prompt:
        raise SiteMediaError("invalid", "Describe the image to generate.")

    await _require_site(workspace_id, pocket_id)
    store = _store_or_raise()

    count = max(1, min(count, _MAX_IMAGES_PER_CALL))
    size = service._SIZE_MAP.get(aspect_ratio)

    from pocketpaw_ee.agent.mcp_servers.media import _default_image_model

    # NO caller-supplied model. ``service.generate`` routes curated ids to fal's
    # own endpoint while this dispatches through the LiteLLM proxy, so a catalog
    # id that works on /studio would 400 here.
    model = _default_image_model()
    auth_key = await service._resolve_auth_key(workspace_id)

    assets: list[PublicAsset] = []
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
                filename=filename_for(prompt, index),
                workspace_id=workspace_id,
                pocket_id=pocket_id,
            )
        except PublicAssetError as exc:
            errors.append(str(exc))
            continue
        except Exception as exc:  # noqa: BLE001
            # A StorageFailure or an S3 client error is not a PublicAssetError. If
            # one escapes here it takes the whole call with it, so the images
            # ALREADY generated, paid for and stored in this loop are never
            # returned and no history row is written for them.
            logger.warning("site_media: could not store a generated image", exc_info=True)
            errors.append(f"could not store the image: {exc}")
            continue

        assets.append(asset)

    # uuid, not hash(): PYTHONHASHSEED randomises str hashing per process, so a
    # hash-derived id would collide or drift between the web container and worker.
    generation_id = f"site-{uuid.uuid4().hex[:16]}"

    # Best-effort: a history failure must not lose an image the caller already
    # has a working URL for.
    try:
        await service.record_generation(
            workspace_id,
            schemas.Generation(
                id=generation_id,
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
                    schemas.GeneratedAsset(id=str(i), url=a.url, mime=a.mime)
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
        raise SiteMediaError("failed", "; ".join(errors) or "unknown error")

    return SiteMediaResult(generation_id=generation_id, assets=assets, errors=errors)


async def generate_site_video_asset(
    *,
    workspace_id: str,
    pocket_id: str,
    prompt: str,
    image_url: str | None = None,
    aspect_ratio: str = "16:9",
    duration_sec: int = 5,
) -> SiteMediaResult:
    """Generate a short video (image-to-video when ``image_url`` is given) and
    publish it with its poster frame.

    The history row is written ``running`` BEFORE the render (minutes on fal) so
    /studio shows the job in flight, and the same row moves to succeeded or
    failed afterwards. ``image_url`` is narrowed by ``resolve_site_video_source``
    for EVERY caller (agent and HTTP alike) before anything reads it, so neither
    can make the server fetch a URL or read another workspace's media.
    """
    from pocketpaw_ee.cloud.studio import fal_video, schemas, service
    from pocketpaw_ee.sites.public_assets import PublicAssetError

    prompt = prompt.strip()
    if not prompt:
        raise SiteMediaError("invalid", "Describe the shot and the camera move.")

    await _require_site(workspace_id, pocket_id)
    store = _store_or_raise()

    image_url = (image_url or "").strip()
    if duration_sec not in _VIDEO_DURATIONS:
        duration_sec = 5

    # An image makes this image-to-video: the clip MOVES THEIR IMAGE rather than
    # inventing a new scene that merely resembles it.
    model = fal_video.DEFAULT_IMAGE_TO_VIDEO_MODEL if image_url else fal_video.DEFAULT_VIDEO_MODEL

    input_urls: list[str] | None = None
    if image_url:
        source = await resolve_site_video_source(
            image_url, workspace_id=workspace_id, pocket_id=pocket_id, store=store
        )
        try:
            # A site asset is already inlined as data:; a media name is read from
            # the private adapter by studio's resolver (never fetched).
            data_url = (
                source
                if source.startswith("data:")
                else (await service._resolve_source_data_url(source))[0]
            )
        except Exception as exc:  # noqa: BLE001
            raise SiteMediaError(
                "bad_source", f"Could not read the source image {image_url!r}: {exc}"
            ) from exc
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

    async def _fail(reason: str) -> SiteMediaError:
        await service.record_generation_best_effort(
            workspace_id, _record("failed", [], reason), source="sites", pocket_id=pocket_id
        )
        return SiteMediaError("failed", reason)

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
        raise await _fail(str(exc)) from exc

    if not video_bytes:
        raise await _fail("the model returned no video")

    stem = filename_for(prompt, 0).rsplit(".", 1)[0]
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
        raise await _fail(str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 — StorageFailure / S3 client errors
        logger.warning("site_media: could not store a generated video", exc_info=True)
        raise await _fail(str(exc)) from exc

    poster = None
    if poster_bytes:
        poster_ext = "png" if "png" in (poster_mime or "") else "jpg"
        try:
            poster = await store.put(
                poster_bytes,
                filename=f"{stem}-poster.{poster_ext}",
                workspace_id=workspace_id,
                pocket_id=pocket_id,
            )
        except Exception:  # noqa: BLE001 — a missing poster is a worse page, not a failure
            logger.warning("site_media: could not store the poster frame", exc_info=True)

    assets = [schemas.GeneratedAsset(id="0", url=video.url, mime=video.mime)]
    if poster is not None:
        assets.append(schemas.GeneratedAsset(id="1", url=poster.url, mime="image/jpeg"))
    await service.record_generation_best_effort(
        workspace_id, _record("succeeded", assets), source="sites", pocket_id=pocket_id
    )

    return SiteMediaResult(
        generation_id=generation_id, assets=[video], poster=poster, duration_sec=duration_sec
    )


# ── Narrowing references to media this workspace owns ───────────────────


def _media_name(url: str) -> str | None:
    """The filename of a private ``/api/v1/media/<name>`` URL, or None."""
    if not url.startswith(_MEDIA_URL_PREFIX):
        return None
    name = url[len(_MEDIA_URL_PREFIX) :]
    if not name or ".." in name or "/" in name or "\\" in name or name != Path(name).name:
        return None
    return name


def _foreign_media_name(name: str, workspace_id: str) -> bool:
    """True when ``name`` carries ANOTHER workspace's owner token (a /browser
    capture or a stamped gallery file). An untagged legacy name is not foreign."""
    from pocketpaw_ee.cloud.media import storage as media_storage

    token = media_storage.capture_owner_token(workspace_id)
    capture = media_storage.capture_owner_of(name)
    owner = media_storage.owner_of(name)
    return (capture is not None and capture != token) or (owner is not None and owner != token)


def _owned_media_name(name: str, workspace_id: str) -> bool:
    """True only when ``name`` is stamped with THIS workspace's token. Checked
    directly rather than through ``storage.visible_to``, whose legacy-name policy
    is a listing rule and has changed under us before."""
    from pocketpaw_ee.cloud.media import storage as media_storage

    token = media_storage.capture_owner_token(workspace_id)
    return media_storage.owner_of(name) == token or media_storage.capture_owner_of(name) == token


async def _read_private_media(name: str, *, max_bytes: int) -> bytes:
    from pocketpaw.uploads.errors import NotFound
    from pocketpaw_ee.cloud.media import storage as media_storage

    adapter = media_storage.get_adapter()
    key = media_storage.media_key(name)
    if not await adapter.exists(key):
        raise SiteMediaError("missing", "That file is no longer in storage.")
    buf = bytearray()
    try:
        async for chunk in adapter.open(key):
            buf.extend(chunk)
            if len(buf) > max_bytes:
                raise SiteMediaError(
                    "rejected",
                    f"That file is over the {max_bytes // (1024 * 1024)} MB a site can publish.",
                )
    except NotFound as exc:
        raise SiteMediaError("missing", "That file is no longer in storage.") from exc
    return bytes(buf)


async def resolve_site_video_source(
    image_url: str,
    *,
    workspace_id: str,
    pocket_id: str,
    store: PublicAssetStore | None = None,
) -> str:
    """Narrow a caller-supplied image-to-video source to media this site owns.

    Accepts ONLY a public asset URL under this pocket's own prefix (read back
    through the rail and inlined as a ``data:`` URL) or a private
    ``/api/v1/media/<name>`` stamped with this workspace's owner token (studio's
    resolver reads it from the media adapter). Anything else — any other http(s)
    URL, a ``data:`` URL, another tenant's capture — is refused, so neither the
    agent nor a client can make the server fetch a URL.

    A LEGACY UNTAGGED Studio image (written before every save stamped an owner)
    cannot be animated by URL: nothing proves which workspace it belongs to.
    Copy it onto the site first (``copy_studio_generation`` trusts it, because
    there the name comes from this workspace's own history record), then pass
    the resulting site asset URL.
    """
    from pocketpaw_ee.sites.public_assets import (
        MAX_IMAGE_BYTES,
        PUBLIC_IMAGE_MIMES,
        PublicAssetError,
        sniff_media_mime,
    )

    url = image_url.strip()
    refusal = SiteMediaError(
        "bad_source",
        "image_url must be an image from this site's assets or from Studio in this workspace.",
    )

    name = _media_name(url)
    if name is not None:
        if not _owned_media_name(name, workspace_id):
            raise refusal
        return url

    store = store or _store_or_raise()
    key = store.key_for_url(url, workspace_id=workspace_id, pocket_id=pocket_id)
    if key is None:
        raise refusal
    try:
        data = await store.read(key, max_bytes=MAX_IMAGE_BYTES)
    except PublicAssetError as exc:
        raise SiteMediaError("bad_source", str(exc)) from exc
    mime = sniff_media_mime(data[:512])
    if mime not in PUBLIC_IMAGE_MIMES:
        raise SiteMediaError("bad_source", "image_url must point at an image, not a video.")

    from pocketpaw_ee.cloud.studio import fal_edit

    return fal_edit.encode_bytes(data, mime)


async def _read_generation_asset(url: str, *, workspace_id: str, store: PublicAssetStore) -> bytes:
    """The bytes of one asset a generation recorded, from storage only.

    The URL comes from this workspace's own history record (``get_generation``
    is workspace-scoped), so an untagged legacy name is trusted here; only a name
    stamped with ANOTHER workspace's token is refused.
    """
    from pocketpaw_ee.sites.public_assets import MAX_ASSET_BYTES, PublicAssetError

    name = _media_name(url)
    if name is not None:
        if _foreign_media_name(name, workspace_id):
            raise SiteMediaError("missing", "That generation's media is not readable here.")
        return await _read_private_media(name, max_bytes=MAX_ASSET_BYTES)

    # A sites-sourced generation already lives on the public rail; it may belong
    # to another of this workspace's sites. Never another workspace's prefix.
    key = store.key_for_url(url, workspace_id=workspace_id)
    if key is None:
        raise SiteMediaError(
            "missing", "That generation's media is not stored where it can be copied."
        )
    try:
        return await store.read(key)
    except PublicAssetError as exc:
        raise SiteMediaError("missing", str(exc)) from exc


def _ext_for(url: str, default: str) -> str:
    suffix = Path(url.split("?", 1)[0]).suffix.lower().lstrip(".")
    return suffix or default


async def copy_studio_generation(
    *, workspace_id: str, pocket_id: str, generation_id: str
) -> SiteMediaResult:
    """Publish an existing Studio generation's media onto this site's rail.

    Images: every image asset. Video: the video asset, plus its poster frame
    (``posterUrl`` on a /studio video, or the image asset a sites video records)
    as ``poster``. Puts are content-addressed with a prompt-derived filename, so
    copying the same generation twice returns the same keys.
    """
    from pocketpaw_ee.cloud.studio import service as studio_service
    from pocketpaw_ee.sites.public_assets import PublicAssetError

    await _require_site(workspace_id, pocket_id)
    store = _store_or_raise()

    gen = await studio_service.get_generation(workspace_id, generation_id)
    if gen is None:
        raise SiteMediaError(
            "generation_not_found", "No Studio generation with that id in this workspace."
        )
    if gen.kind not in ("image", "video"):
        raise SiteMediaError("unsupported", "Only images and videos can be added to a site.")
    if gen.status != "succeeded" or not gen.assets:
        raise SiteMediaError(
            "not_ready", "That generation has no finished media yet. Wait for it to finish."
        )

    stem = filename_for(gen.prompt, 0).rsplit("-", 1)[0]
    poster_src: str | None = None
    if gen.kind == "video":
        media = [a for a in gen.assets if a.mime.startswith("video/")]
        stills = [a for a in gen.assets if a.mime.startswith("image/")]
        poster_src = next((a.posterUrl for a in media if a.posterUrl), None) or (
            stills[0].url if stills else None
        )
    else:
        media = [a for a in gen.assets if a.mime.startswith("image/")]
    if not media:
        raise SiteMediaError("not_ready", "That generation has no media a site can publish.")

    assets: list[PublicAsset] = []
    for index, item in enumerate(media):
        data = await _read_generation_asset(item.url, workspace_id=workspace_id, store=store)
        default_ext = "mp4" if gen.kind == "video" else "png"
        try:
            assets.append(
                await store.put(
                    data,
                    filename=f"{stem}-{index + 1}.{_ext_for(item.url, default_ext)}",
                    workspace_id=workspace_id,
                    pocket_id=pocket_id,
                )
            )
        except PublicAssetError as exc:
            raise SiteMediaError("rejected", str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 — StorageFailure / S3 client errors
            logger.warning("site_media: could not copy a studio asset", exc_info=True)
            raise SiteMediaError("failed", f"Could not store the file: {exc}") from exc

    poster = None
    if poster_src:
        try:
            data = await _read_generation_asset(poster_src, workspace_id=workspace_id, store=store)
            poster = await store.put(
                data,
                filename=f"{stem}-poster.{_ext_for(poster_src, 'jpg')}",
                workspace_id=workspace_id,
                pocket_id=pocket_id,
            )
        except Exception:  # noqa: BLE001 — a missing poster is a worse page, not a failure
            logger.warning("site_media: could not copy the poster frame", exc_info=True)

    return SiteMediaResult(generation_id=gen.id, assets=assets, poster=poster)


__all__ = [
    "SiteMediaError",
    "SiteMediaResult",
    "copy_studio_generation",
    "filename_for",
    "generate_site_image_assets",
    "generate_site_video_asset",
    "resolve_site_video_source",
]
