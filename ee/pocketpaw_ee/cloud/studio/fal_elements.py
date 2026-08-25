# ee/pocketpaw_ee/cloud/studio/fal_elements.py — direct fal.ai Kling ELEMENTS client.
#
# The /studio "Edit video" panel drives Kling's Elements model
# (``fal-ai/kling-video/v1.6/standard/elements``) DIRECTLY against fal here, the
# same decision as the canvas edit ops (``fal_edit``) and text/image-to-video
# (``fal_video``): the LiteLLM gateway serves generation models only and has no
# route for Kling's element-based video editing. One credential path
# (``FAL_AI_API_KEY``), one SDK, and the output persists through media storage.
#
# The Elements model takes a prompt plus a set of reference images
# (``input_image_urls`` — the "elements" the edit is conditioned on) and,
# optionally, a source video (``video_url``) to edit in place. The studio
# service resolves local media paths / http URLs / data URLs to ``data:`` URLs
# before calling here (mirroring ``fal_video``'s image-to-video path), so fal
# never needs a route back into the deployment's private media storage.
#
# Argument building + result extraction are pure so they unit-test without the
# SDK; ``_run_fal`` / ``_download`` are the seams tests monkeypatch (exactly like
# ``fal_edit`` / ``fal_video``).
#
# Created 2026-08-24 (studio-video-elements): Kling Elements dispatch.

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# The Kling Elements endpoint. Its contract is ``{prompt, input_image_urls?,
# video_url?, duration?, aspect_ratio?}`` — prompt + element images compose the
# scene, and an optional ``video_url`` switches it to edit-an-existing-video.
DEFAULT_ELEMENTS_MODEL = "fal-ai/kling-video/v1.6/standard/elements"

# The studio "Edit video" panel caps reference/element images at 20 (the user's
# spec). fal's own per-request cap may be lower for a given model version, but we
# enforce the product limit here and let fal surface anything stricter.
MAX_ELEMENT_IMAGES = 20

# Durations the composer offers (mirrors the rail's 2s / 5s / 10s picker). fal
# endpoints enforce their own accepted set; a duration the chosen model doesn't
# accept surfaces as a clear upstream validation error rather than a silent clamp.
SUPPORTED_DURATIONS: tuple[int, ...] = (2, 5, 10)

# Client + server-side deadlines for the fal call. Video jobs queue and render
# for seconds to minutes, so these are generous but bounded so a hung upstream
# fails fast instead of pinning a worker forever.
_CLIENT_TIMEOUT = 600.0
_START_TIMEOUT = 180.0
_DOWNLOAD_TIMEOUT = 300.0


class FalElementsError(Exception):
    """A fal.ai Kling Elements call failed (missing SDK, upstream error,
    malformed result, or no output video). The studio service maps this to 502."""


# ── Endpoint + argument building ─────────────────────────────────────────────


def resolve_endpoint(model_id: str | None) -> str:
    """Map a requested model id onto a real fal endpoint.

    ``fal-ai/...`` ids pass straight through (the caller already picked an
    endpoint); anything else — including None — falls back to the Kling Elements
    default.
    """
    m = (model_id or "").strip()
    if m.startswith("fal-ai/"):
        return m
    return DEFAULT_ELEMENTS_MODEL


def build_arguments(
    *,
    prompt: str,
    input_image_urls: list[str] | None = None,
    video_url: str | None = None,
    duration_sec: int | None = None,
    aspect_ratio: str | None = None,
) -> dict[str, Any]:
    """Build the fal ``arguments`` dict for one Kling Elements call.

    Pure + side-effect free so it is unit-testable in isolation. Kling standard
    takes ``duration`` as a string ("5" / "10") and ``aspect_ratio`` as one of
    "16:9" / "9:16" / "1:1". ``input_image_urls`` is the list of element/reference
    images; ``video_url`` switches the call to edit-an-existing-video. Both are
    omitted when absent so a bare prompt-only call is still valid.
    """
    args: dict[str, Any] = {"prompt": prompt}
    urls = [u for u in (input_image_urls or []) if u and u.strip()]
    if urls:
        args["input_image_urls"] = urls
    if video_url and video_url.strip():
        args["video_url"] = video_url.strip()
    if duration_sec and duration_sec > 0:
        args["duration"] = str(int(duration_sec))
    if aspect_ratio:
        args["aspect_ratio"] = aspect_ratio
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
        raise FalElementsError(
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
    except Exception as exc:  # noqa: BLE001 — surface the upstream reason to the user
        logger.warning("studio: fal elements '%s' failed", endpoint, exc_info=True)
        raise FalElementsError(f"fal elements '{endpoint}' failed: {exc}") from exc
    if not isinstance(result, dict):
        raise FalElementsError(f"fal elements '{endpoint}' returned an unexpected result")
    return result


# ── Public entry point ───────────────────────────────────────────────────────


async def run_fal_elements(
    *,
    prompt: str,
    input_image_urls: list[str] | None = None,
    video_url: str | None = None,
    duration_sec: int | None = None,
    aspect_ratio: str | None = None,
    model: str | None = None,
    key: str | None = None,
) -> tuple[bytes, str, bytes | None, str | None]:
    """Run one Kling Elements call against fal and return the result.

    ``input_image_urls`` (one or more ``data:`` image URLs) are the element/
    reference images; ``video_url`` (a ``data:`` video URL) switches the call to
    edit-an-existing-video. At least one of them OR a non-empty prompt is
    required — a bare prompt-only call is a valid text-to-video through Elements.

    Returns ``(video_bytes, video_mime, poster_bytes, poster_mime)`` — the
    poster pair is ``(None, None)`` when the endpoint didn't return one. Raises
    ValueError (missing key / empty prompt + no inputs) and FalElementsError
    (upstream failure / no output).
    """
    from . import fal_edit

    text = (prompt or "").strip()
    urls = [u for u in (input_image_urls or []) if u and u.strip()]
    if not text and not urls and not (video_url or "").strip():
        raise ValueError("prompt or element images are required for video editing")

    endpoint = resolve_endpoint(model)
    arguments = build_arguments(
        prompt=text or "edit the video with these elements",
        input_image_urls=urls or None,
        video_url=video_url,
        duration_sec=duration_sec,
        aspect_ratio=aspect_ratio,
    )

    api_key = key if key is not None else fal_edit.fal_api_key()
    if not api_key:
        raise FalElementsError("fal.ai API key is not configured (set FAL_AI_API_KEY)")

    result = await _run_fal(endpoint, arguments, key=api_key)
    video_url_out = _extract_video_url(result)
    if not video_url_out:
        raise FalElementsError(f"fal elements '{endpoint}' returned no video data")

    video_bytes, video_mime = await _download(video_url_out)

    poster_bytes: bytes | None = None
    poster_mime: str | None = None
    poster_url = _extract_poster_url(result)
    if poster_url:
        try:
            poster_bytes, poster_mime = await _download(poster_url)
        except Exception:  # noqa: BLE001 — a poster is cosmetic; never fail the video
            logger.warning("studio: fal elements poster download failed (non-fatal)", exc_info=True)
            poster_bytes, poster_mime = None, None
    return video_bytes, video_mime, poster_bytes, poster_mime


__all__ = [
    "DEFAULT_ELEMENTS_MODEL",
    "MAX_ELEMENT_IMAGES",
    "SUPPORTED_DURATIONS",
    "FalElementsError",
    "resolve_endpoint",
    "build_arguments",
    "run_fal_elements",
]
