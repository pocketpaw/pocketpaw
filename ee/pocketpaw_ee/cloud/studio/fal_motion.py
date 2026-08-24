# ee/pocketpaw_ee/cloud/studio/fal_motion.py — direct fal.ai Kling MOTION CONTROL client.
#
# The /studio "Motion control" panel drives Kling's Motion Control model
# (``fal-ai/kling-video/v2.6/standard/motion-control``) DIRECTLY against fal,
# the same decision as the canvas edit ops (``fal_edit``), text/image-to-video
# (``fal_video``) and the "Edit video" panel (``fal_elements``): the LiteLLM
# gateway serves generation models only and has no route for Kling's motion
# control. One credential path (``FAL_AI_API_KEY``), one SDK, and the output
# persists through media storage.
#
# Motion control animates a character image (``image_url`` — a subject whose
# face and body are visible) to follow a reference motion clip (``video_url`` —
# a "motion preset" whose camera/body motion is transferred). The
# ``character_orientation`` flag controls whether the character keeps the motion
# video's orientation ("video") or its own source orientation ("image"). The
# studio service resolves local media paths / http URLs / data URLs to ``data:``
# URLs before calling here (mirroring ``fal_elements``), so fal never needs a
# route back into the deployment's private media storage.
#
# Argument building + result extraction are pure so they unit-test without the
# SDK; ``_run_fal`` / ``_download`` are the seams tests monkeypatch (exactly like
# ``fal_edit`` / ``fal_video`` / ``fal_elements``).
#
# Created 2026-08-24 (studio-motion-control): Kling Motion Control dispatch.

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# The Kling Motion Control endpoint. Its contract is ``{image_url, video_url,
# character_orientation?}`` — a character image is animated to follow the
# reference motion video.
DEFAULT_MOTION_MODEL = "fal-ai/kling-video/v2.6/standard/motion-control"

# ``character_orientation``: "video" keeps the character's orientation in sync
# with the motion clip; "image" keeps the character's original orientation.
CHARACTER_ORIENTATIONS: tuple[str, ...] = ("video", "image")
DEFAULT_CHARACTER_ORIENTATION = "video"

# Client + server-side deadlines for the fal call. Video jobs queue and render
# for seconds to minutes, so these are generous but bounded so a hung upstream
# fails fast instead of pinning a worker forever.
_CLIENT_TIMEOUT = 600.0
_START_TIMEOUT = 180.0
_DOWNLOAD_TIMEOUT = 300.0


class FalMotionError(Exception):
    """A fal.ai Kling Motion Control call failed (missing SDK, upstream error,
    malformed result, or no output video). The studio service maps this to 502."""


class FalMotionValidationError(FalMotionError):
    """fal rejected the request as invalid input (a 4xx from the endpoint —
    e.g. image too small / bad character_orientation / video too long). The
    studio service maps this to a 400 so the user sees the exact reason."""


# ── Endpoint + argument building ─────────────────────────────────────────────


def resolve_endpoint(model_id: str | None) -> str:
    """Map a requested model id onto a real fal endpoint.

    ``fal-ai/...`` ids pass straight through (the caller already picked an
    endpoint); anything else — including None — falls back to the Kling Motion
    Control default.
    """
    m = (model_id or "").strip()
    if m.startswith("fal-ai/"):
        return m
    return DEFAULT_MOTION_MODEL


def build_arguments(
    *,
    image_url: str,
    video_url: str,
    character_orientation: str | None = None,
) -> dict[str, Any]:
    """Build the fal ``arguments`` dict for one Kling Motion Control call.

    Pure + side-effect free so it is unit-testable in isolation. The character
    image (``image_url``) is animated to follow the reference motion video
    (``video_url``); ``character_orientation`` defaults to "video" (follow the
    motion clip's orientation).
    """
    args: dict[str, Any] = {
        "image_url": image_url.strip(),
        "video_url": video_url.strip(),
    }
    orientation = (character_orientation or "").strip() or DEFAULT_CHARACTER_ORIENTATION
    args["character_orientation"] = orientation
    return args


# ── Result handling ──────────────────────────────────────────────────────────


def _extract_video_url(result: dict[str, Any]) -> str | None:
    """Pull the output video URL from a fal video result.

    Handles the common shapes: ``video: {url, …}`` (kling / wan / minimax /
    veo), ``videos: [{url, …}]`` (rare multi-output), or a bare top-level
    ``url``.
    """
    video = result.get("video")
    if isinstance(video, dict) and isinstance(video.get("url"), str):
        return video["url"]
    videos = result.get("videos")
    if isinstance(videos, list):
        for v in videos:
            if isinstance(v, dict) and isinstance(v.get("url"), str):
                return v["url"]
    if isinstance(result.get("url"), str):
        return result["url"]
    return None


def _extract_poster_url(result: dict[str, Any]) -> str | None:
    """Pull a poster/thumbnail URL when the endpoint returns one (some models
    return ``poster: {url}`` or ``thumbnails: [{url}]``); None when absent."""
    poster = result.get("poster")
    if isinstance(poster, dict) and isinstance(poster.get("url"), str):
        return poster["url"]
    thumbs = result.get("thumbnails")
    if isinstance(thumbs, list):
        for t in thumbs:
            if isinstance(t, dict) and isinstance(t.get("url"), str):
                return t["url"]
    return None


async def _download(url: str) -> tuple[bytes, str]:
    """Download one fal-hosted result file and return ``(bytes, content_type)``.

    fal media URLs are publicly accessible (no auth header needed) but expire per
    the account's media-expiration setting, so the service persists the bytes
    into media storage before the URL goes stale.
    """
    async with httpx.AsyncClient(timeout=_DOWNLOAD_TIMEOUT) as client:
        resp = await client.get(url)
        resp.raise_for_status()
    mime = resp.headers.get("content-type", "video/mp4").split(";")[0].strip() or "video/mp4"
    return resp.content, mime


# ── The fal SDK seam (tests inject _run_fal) ────────────────────────────────


async def _run_fal(
    endpoint: str,
    arguments: dict[str, Any],
    *,
    key: str,
    client_timeout: float = _CLIENT_TIMEOUT,
    start_timeout: float = _START_TIMEOUT,
) -> dict[str, Any]:
    """Run a fal endpoint via the official SDK. Lazy-imports fal_client so the
    module imports even before the dep is installed (EE lazy-import pattern)."""
    try:
        import fal_client  # noqa: PLC0415 — lazy: SDK is an optional runtime dep
    except ImportError as exc:  # pragma: no cover — env with the dep installed
        raise FalMotionError(
            "fal-client is not installed — run `pip install fal-client` (pocketpaw-ee dep)"
        ) from exc
    try:
        client = fal_client.AsyncClient(key=key, default_timeout=client_timeout)
        result = await client.run(
            endpoint,
            arguments=arguments,
            timeout=client_timeout,
            start_timeout=start_timeout,
        )
    except fal_client.FalClientHTTPError as exc:
        # A 4xx means fal REJECTED the inputs (image/video/character_orientation
        # validation) — surface it as a client error (→ 400), not a 502.
        if exc.status_code is not None and 400 <= exc.status_code < 500:
            raise FalMotionValidationError(
                f"fal motion-control rejected the request: {exc}"
            ) from exc
        raise FalMotionError(f"fal motion-control '{endpoint}' failed: {exc}") from exc
    except Exception as exc:  # noqa: BLE001 — surface the upstream reason to the user
        logger.warning("studio: fal motion-control '%s' failed", endpoint, exc_info=True)
        raise FalMotionError(f"fal motion-control '{endpoint}' failed: {exc}") from exc
    if not isinstance(result, dict):
        raise FalMotionError(f"fal motion-control '{endpoint}' returned an unexpected result")
    return result


# ── Public entry point ───────────────────────────────────────────────────────


async def run_fal_motion(
    *,
    image_url: str,
    video_url: str,
    character_orientation: str | None = None,
    model: str | None = None,
    key: str | None = None,
) -> tuple[bytes, str, bytes | None, str | None]:
    """Run one Kling Motion Control call against fal and return the result.

    ``image_url`` (a ``data:`` image URL) is the character to animate (visible
    face and body); ``video_url`` (a ``data:`` video URL) is the reference motion
    preset. Both are required. Returns
    ``(video_bytes, video_mime, poster_bytes, poster_mime)`` — the poster pair is
    ``(None, None)`` when the endpoint didn't return one. Raises ValueError
    (missing image/video/key) and FalMotionError (upstream failure / no output).
    """
    from . import fal_edit

    img = (image_url or "").strip()
    vid = (video_url or "").strip()
    if not img:
        raise ValueError("a character image is required for motion control")
    if not vid:
        raise ValueError("a motion reference video is required for motion control")

    orientation = (character_orientation or "").strip() or DEFAULT_CHARACTER_ORIENTATION
    if orientation not in CHARACTER_ORIENTATIONS:
        raise ValueError(
            f"character_orientation must be one of {', '.join(CHARACTER_ORIENTATIONS)}"
        )

    endpoint = resolve_endpoint(model)
    arguments = build_arguments(
        image_url=img,
        video_url=vid,
        character_orientation=orientation,
    )

    api_key = key if key is not None else fal_edit.fal_api_key()
    if not api_key:
        raise FalMotionError("fal.ai API key is not configured (set FAL_AI_API_KEY)")

    result = await _run_fal(endpoint, arguments, key=api_key)
    video_url_out = _extract_video_url(result)
    if not video_url_out:
        raise FalMotionError(f"fal motion-control '{endpoint}' returned no video data")

    video_bytes, video_mime = await _download(video_url_out)

    poster_bytes: bytes | None = None
    poster_mime: str | None = None
    poster_url = _extract_poster_url(result)
    if poster_url:
        try:
            poster_bytes, poster_mime = await _download(poster_url)
        except Exception:  # noqa: BLE001 — a poster is cosmetic; never fail the video
            logger.warning(
                "studio: fal motion-control poster download failed (non-fatal)", exc_info=True
            )
            poster_bytes, poster_mime = None, None
    return video_bytes, video_mime, poster_bytes, poster_mime


__all__ = [
    "DEFAULT_MOTION_MODEL",
    "CHARACTER_ORIENTATIONS",
    "DEFAULT_CHARACTER_ORIENTATION",
    "FalMotionError",
    "FalMotionValidationError",
    "resolve_endpoint",
    "build_arguments",
    "run_fal_motion",
]
