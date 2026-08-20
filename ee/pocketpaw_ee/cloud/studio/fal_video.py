# ee/pocketpaw_ee/cloud/studio/fal_video.py — direct fal.ai video-GENERATION client.
#
# The /studio direct surface generates IMAGES through the LiteLLM gateway
# (proxy ``/v1/images/generations``), but there is no gateway route for video
# that the /studio service can rely on today. Video generation therefore runs
# DIRECTLY against fal here — the same decision as the canvas edit ops
# (``fal_edit``): one credential path (``FAL_AI_API_KEY``), one SDK, and the
# output persists through media storage so the gallery + flow grow like any
# other generation.
#
# This module is the video twin of ``fal_edit``: resolve the model id → fal
# endpoint, build the arguments (prompt / duration / aspect_ratio), run the
# endpoint through the OFFICIAL fal-client SDK (``fal_client.AsyncClient``),
# download the result video (+ optional poster frame), and return the bytes for
# the studio service to persist. Argument building + result extraction are pure
# so they unit-test without the SDK, and ``_run_fal`` / ``_download`` are the
# seams tests monkeypatch (exactly like ``fal_edit``).
#
# Created 2026-08-18 (studio-video-generation): direct fal video dispatch.

from __future__ import annotations

import logging
from typing import Any

import httpx

from . import fal_edit

logger = logging.getLogger(__name__)

# The default fal text-to-video endpoint. Kling standard is long-stable and its
# argument contract (``{prompt, duration, aspect_ratio, resolution}``) is
# dependable, so it's the safe default the catalog ids alias to.
DEFAULT_VIDEO_MODEL = "fal-ai/kling-video/v1/standard/text-to-video"

# Catalog video model ids (as the /studio models picker serves them from the
# LiteLLM catalog — e.g. ``fal_ai/fal-ai/kling/v2``) → the real fal endpoint to
# dispatch. A model id that already looks like a fal endpoint (starts with
# ``fal-ai/``) passes straight through; anything unknown falls back to
# ``DEFAULT_VIDEO_MODEL``. The service still echoes the REQUESTED model in the
# Generation record, so the user always sees what they asked for.
VIDEO_MODEL_ALIASES: dict[str, str] = {
    "fal_ai/fal-ai/kling/v2": "fal-ai/kling-video/v1/standard/text-to-video",
    "kling-v2": "fal-ai/kling-video/v1/standard/text-to-video",
    "kwaivgi/kling-v2.0": "fal-ai/kling-video/v1/standard/text-to-video",
    "fal-ai/kling-video/v1/standard/text-to-video": "fal-ai/kling-video/v1/standard/text-to-video",
}

# Durations the composer offers (mirrors the frontend's catalog ``durationsSec``
# and the rail's 2s / 5s / 10s picker). fal endpoints enforce their own accepted
# set per model; a duration the chosen model doesn't accept surfaces as a clear
# upstream validation error (the service maps it to a 502), so we never silently
# clamp a user's choice.
SUPPORTED_DURATIONS: tuple[int, ...] = (2, 5, 10)

# Endpoint → accepted durations (best-effort doc for the default; a model that
# rejects a requested duration reports it, which is more honest than guessing).
_ENDPOINT_DURATIONS: dict[str, tuple[int, ...]] = {
    "fal-ai/kling-video/v1/standard/text-to-video": (5, 10),
}

# Client + server-side deadlines for the fal call. Video jobs queue and render
# for seconds to minutes, so these are generous but bounded so a hung upstream
# fails fast instead of pinning a worker forever.
_CLIENT_TIMEOUT = 600.0
_START_TIMEOUT = 180.0
_DOWNLOAD_TIMEOUT = 300.0


class FalVideoError(Exception):
    """A fal.ai video-generation call failed (missing SDK, upstream error,
    malformed result, or no output video). The studio service maps this to 502."""


# ── Endpoint + argument building ─────────────────────────────────────────────


def resolve_endpoint(model_id: str | None) -> str:
    """Map a requested model id onto a real fal endpoint.

    ``fal-ai/...`` ids pass straight through (the caller already picked an
    endpoint); known catalog aliases resolve to their endpoint; anything unknown
    falls back to ``DEFAULT_VIDEO_MODEL``.
    """
    m = (model_id or "").strip()
    if m.startswith("fal-ai/"):
        return m
    return VIDEO_MODEL_ALIASES.get(m, DEFAULT_VIDEO_MODEL)


def build_arguments(
    *, prompt: str, duration_sec: int | None, aspect_ratio: str | None
) -> dict[str, Any]:
    """Build the fal ``arguments`` dict for a text-to-video call.

    Pure + side-effect free so it is unit-testable in isolation. Kling standard
    takes ``duration`` as a string ("5" / "10") and ``aspect_ratio`` as one of
    "16:9" / "9:16" / "1:1". Unsupported values pass through and the endpoint's
    own validation reports them (clearer than silently clamping).
    """
    args: dict[str, Any] = {"prompt": prompt}
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
    return ``poster: {url}`` or ``thumbnails: [{url}]``); None when absent —
    the frontend renders the video's first frame via ``<video preload="metadata">``."""
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
    into media storage before the URL goes stale. Video files are larger than
    images, so the timeout is generous.
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
        raise FalVideoError(
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
        logger.warning("studio: fal video '%s' failed", endpoint, exc_info=True)
        raise FalVideoError(f"fal video '{endpoint}' failed: {exc}") from exc
    if not isinstance(result, dict):
        raise FalVideoError(f"fal video '{endpoint}' returned an unexpected result")
    return result


# ── Public entry point ───────────────────────────────────────────────────────


async def run_fal_video(
    *,
    prompt: str,
    duration_sec: int | None = None,
    aspect_ratio: str | None = None,
    model: str | None = None,
    key: str | None = None,
) -> tuple[bytes, str, bytes | None, str | None]:
    """Run a text-to-video generation against fal and return the result.

    Returns ``(video_bytes, video_mime, poster_bytes, poster_mime)`` — the
    poster pair is ``(None, None)`` when the endpoint didn't return one. Raises
    ValueError (empty prompt) and FalVideoError (missing key / upstream failure
    / no output).
    """
    text = (prompt or "").strip()
    if not text:
        raise ValueError("prompt is required for video generation")

    endpoint = resolve_endpoint(model)
    arguments = build_arguments(prompt=text, duration_sec=duration_sec, aspect_ratio=aspect_ratio)

    api_key = key if key is not None else fal_edit.fal_api_key()
    if not api_key:
        raise FalVideoError("fal.ai API key is not configured (set FAL_AI_API_KEY)")

    result = await _run_fal(endpoint, arguments, key=api_key)
    video_url = _extract_video_url(result)
    if not video_url:
        raise FalVideoError(f"fal video '{endpoint}' returned no video data")

    video_bytes, video_mime = await _download(video_url)

    poster_bytes: bytes | None = None
    poster_mime: str | None = None
    poster_url = _extract_poster_url(result)
    if poster_url:
        try:
            poster_bytes, poster_mime = await _download(poster_url)
        except Exception:  # noqa: BLE001 — a poster is cosmetic; never fail the video
            logger.warning("studio: fal video poster download failed (non-fatal)", exc_info=True)
            poster_bytes, poster_mime = None, None
    return video_bytes, video_mime, poster_bytes, poster_mime


__all__ = [
    "DEFAULT_VIDEO_MODEL",
    "VIDEO_MODEL_ALIASES",
    "SUPPORTED_DURATIONS",
    "FalVideoError",
    "resolve_endpoint",
    "build_arguments",
    "run_fal_video",
]
