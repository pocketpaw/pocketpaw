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

import asyncio
import logging
import shutil
import tempfile
from pathlib import Path
from typing import Any

import httpx

from . import fal_edit

logger = logging.getLogger(__name__)

# The default fal text-to-video endpoint. Kling standard is long-stable and its
# argument contract (``{prompt, duration, aspect_ratio, resolution}``) is
# dependable, so it's the safe default the catalog ids alias to.
DEFAULT_VIDEO_MODEL = "fal-ai/kling-video/v1/standard/text-to-video"

# Image-to-video endpoints (the flow wires Image nodes into a Video node and the
# service passes their result URLs in as ``inputImageUrls``). fal's Kling
# image-to-video contract carries at most TWO frames per call — ``image_url``
# (start, required) + ``end_image_url`` (end, optional) — so:
#   * 1 image  — one call: ``image_url`` (v1 standard).
#   * 2 images — one call: ``image_url`` + ``end_image_url`` (v1.6, first→last).
#   * 3+ images — animate EVERY adjacent pair (image1→image2, image2→image3, …)
#     as a separate 2-frame call on the SAME v1.6 endpoint, then stitch the clips
#     into ONE video on the backend with ffmpeg (``_concat_videos``). The model
#     does all the animation; ffmpeg only joins the already-generated clips, so
#     EVERY image is guaranteed in the output in order. (Both the fal ``v1.5``
#     endpoint and a ``keyframes`` array were tried: v1.5 does NOT exist on fal
#     (404) and v1.6 has no ``keyframes`` field (silently stripped → only the
#     first image showed up). The documented 2-frame contract on v1.6 is the
#     reliable path.)
DEFAULT_IMAGE_TO_VIDEO_MODEL = "fal-ai/kling-video/v1/standard/image-to-video"
IMAGE_TO_VIDEO_PAIR_MODEL = "fal-ai/kling-video/v1.6/standard/image-to-video"

# When the user wires images but types no prompt, this drives the motion the
# Kling call animates (fal image-to-video needs SOMETHING describing the move).
DEFAULT_I2V_PROMPT = "animate this scene with smooth, natural motion"

# Catalog video model ids → the image-to-video endpoint to dispatch when the flow
# conditions a video node on input images (mirrors VIDEO_MODEL_ALIASES). A
# ``fal-ai/...`` id passes straight through for a single image; multi-image runs
# always use the pair contract (2 images in one call, 3+ chained as pairs and
# stitched). The service still echoes the REQUESTED model in the Generation record.
IMAGE_TO_VIDEO_MODEL_ALIASES: dict[str, str] = {
    "fal_ai/fal-ai/kling/v2": DEFAULT_IMAGE_TO_VIDEO_MODEL,
    "kling-v2": DEFAULT_IMAGE_TO_VIDEO_MODEL,
    "kwaivgi/kling-v2.0": DEFAULT_IMAGE_TO_VIDEO_MODEL,
    DEFAULT_VIDEO_MODEL: DEFAULT_IMAGE_TO_VIDEO_MODEL,
}

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

# ── Curated video catalog (movie-maker) ─────────────────────────────────────
# The reduced set of video endpoints the composer offers. Endpoint ids are the
# fal model paths (verify against fal's /llms.txt when updating). ``kind`` groups
# them for the model picker. ``duration_as_string`` marks endpoints that take
# ``duration`` as a string enum ("5"/"10" — Kling) rather than an integer of
# seconds (Seedance / Gemini).

_ENDPOINT_NAMESPACES: tuple[str, ...] = (
    "fal-ai/",
    "bytedance/",
    "google/",
    "openai/",
    "xai/",
    "recraft/",
)

CURATED_VIDEO_MODELS: dict[str, dict[str, Any]] = {
    "seedance_2_5": {
        "id": "bytedance/seedance-2.5/enterprise/text-to-video",
        "name": "Seedance 2.5",
        "vendor": "ByteDance",
        "kind": "text-to-video",
        "aspect_ratios": ("16:9", "9:16", "1:1"),
        "durations": (5, 10),
        "duration_as_string": False,
    },
    "kling": {
        "id": DEFAULT_VIDEO_MODEL,  # fal-ai/kling-video/v1/standard/text-to-video
        "name": "Kling Video",
        "vendor": "Kling",
        "kind": "text-to-video",
        "aspect_ratios": ("16:9", "9:16", "1:1"),
        "durations": (5, 10),
        "duration_as_string": True,
    },
    "seedance_2_5_i2v": {
        "id": "bytedance/seedance-2.5/enterprise/image-to-video",
        "name": "Seedance 2.5 (image)",
        "vendor": "ByteDance",
        "kind": "image-to-video",
        "aspect_ratios": ("16:9", "9:16", "1:1"),
        "durations": (5, 10),
        "duration_as_string": False,
    },
    "kling_i2v": {
        "id": IMAGE_TO_VIDEO_PAIR_MODEL,  # fal-ai/kling-video/v1.6/standard/image-to-video
        "name": "Kling Video (image)",
        "vendor": "Kling",
        "kind": "image-to-video",
        "aspect_ratios": ("16:9", "9:16", "1:1"),
        "durations": (5, 10),
        "duration_as_string": True,
    },
}


def _duration_value(endpoint: str, duration_sec: int) -> int | str:
    """Encode ``duration`` the way the endpoint expects: Kling takes a string
    enum ("5"/"10"); Seedance / Gemini take an integer of seconds."""
    if "kling-video" in endpoint:
        return str(int(duration_sec))
    return int(duration_sec)


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

    Endpoint-looking ids (``fal-ai/…``, ``bytedance/…``, ``google/…``, …) pass
    straight through; known catalog aliases resolve to their endpoint; anything
    unknown falls back to ``DEFAULT_VIDEO_MODEL``.
    """
    m = (model_id or "").strip()
    if m.startswith(_ENDPOINT_NAMESPACES):
        return m
    return VIDEO_MODEL_ALIASES.get(m, DEFAULT_VIDEO_MODEL)


def build_arguments(
    *,
    prompt: str,
    duration_sec: int | None,
    aspect_ratio: str | None,
    endpoint: str | None = None,
) -> dict[str, Any]:
    """Build the fal ``arguments`` dict for a text-to-video call.

    Pure + side-effect free so it is unit-testable in isolation. Kling standard
    takes ``duration`` as a string ("5" / "10"); Seedance takes an integer of
    seconds — ``_duration_value`` encodes per-endpoint. ``aspect_ratio`` is one
    of "16:9" / "9:16" / "1:1". Unsupported values pass through and the
    endpoint's own validation reports them (clearer than silently clamping).
    """
    args: dict[str, Any] = {"prompt": prompt}
    if duration_sec and duration_sec > 0:
        args["duration"] = _duration_value(endpoint or DEFAULT_VIDEO_MODEL, duration_sec)
    if aspect_ratio:
        args["aspect_ratio"] = aspect_ratio
    return args


# ── Image-to-video (the flow conditions a video node on Image nodes) ──────────


def resolve_image_to_video_endpoint(model_id: str | None, image_count: int) -> str:
    """Map a requested model id onto the fal image-to-video endpoint to dispatch.

    Two+ frames go through the pair endpoint (v1.6, ``image_url`` +
    ``end_image_url`` — the documented multi-frame contract); 3+ images chain
    pairs and stitch, but each per-pair call still uses this endpoint. A
    single-image call may use whatever ``fal-ai/...`` endpoint the caller named,
    else the catalog alias (defaulting to ``DEFAULT_IMAGE_TO_VIDEO_MODEL``).
    """
    if image_count >= 2:
        return IMAGE_TO_VIDEO_PAIR_MODEL
    m = (model_id or "").strip()
    if m.startswith(_ENDPOINT_NAMESPACES):
        return m
    return IMAGE_TO_VIDEO_MODEL_ALIASES.get(m, DEFAULT_IMAGE_TO_VIDEO_MODEL)


def build_image_to_video_arguments(
    *,
    prompt: str,
    image_urls: list[str],
    duration_sec: int | None,
    aspect_ratio: str | None,
    endpoint: str | None = None,
) -> dict[str, Any]:
    """Build the fal ``arguments`` dict for ONE image-to-video clip of 1–2 frames.

    Pure + side-effect free so it is unit-testable in isolation:
      * 1 image  → ``image_url`` (start frame).
      * 2 images → ``image_url`` + ``end_image_url`` (first-frame → last-frame).

    This is the DOCUMENTED Kling contract — fal reliably animates at most two
    frames, so 3+ images are handled by the caller chaining adjacent pairs (each
    pair a 2-frame clip here) and stitching the clips (``_concat_videos``);
    passing more than 2 frames here is a ValueError instead of a silent bug.
    ``prompt``/``duration``/``aspect_ratio`` are optional (Kling animates the
    frames even with a bare image input). ``duration`` is encoded per-endpoint
    via ``_duration_value`` (Kling string enum, Seedance integer seconds).
    """
    urls = [u for u in (image_urls or []) if u and u.strip()]
    if not urls:
        raise ValueError("at least one image URL is required for image-to-video")
    if len(urls) > 2:
        raise ValueError(
            "fal image-to-video carries at most 2 frames per call — 3+ images must be "
            "chained as adjacent pairs and stitched (_run_image_to_video does this)"
        )
    args: dict[str, Any] = {"image_url": urls[0]}
    if len(urls) == 2:
        args["end_image_url"] = urls[1]
    if prompt:
        args["prompt"] = prompt
    if duration_sec and duration_sec > 0:
        args["duration"] = _duration_value(
            endpoint or DEFAULT_IMAGE_TO_VIDEO_MODEL, duration_sec
        )
    if aspect_ratio:
        args["aspect_ratio"] = aspect_ratio
    return args


# ── Clip stitching (3+ images: chain adjacent 2-frame pairs, then join) ──────
#
# fal's Kling image-to-video carries at most two frames per call, so N≥3 images
# become (N-1) 2-frame clips (image1→image2, image2→image3, …) and one backend
# join. Every clip comes from the SAME endpoint / duration / aspect_ratio, so the
# MP4s share codec params and the ffmpeg concat demuxer joins them LOSSESSLY with
# ``-c copy`` (no re-encode, no quality loss). ffmpeg is only a joiner here — the
# model still does all the animation.


def _concat_ffmpeg_args(concat_file: str, output_path: str) -> list[str]:
    """ffmpeg CLI for losslessly joining same-format clips via the concat demuxer.

    Pure + side-effect free so it is unit-testable in isolation. ``-safe 0`` lets
    the demuxer read absolute file paths (the list file is written with absolute
    paths); ``-c copy`` copies streams instead of re-encoding, which is lossless
    for clips with identical codec params (guaranteed here — same endpoint).
    """
    return [
        "-y",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        concat_file,
        "-c",
        "copy",
        output_path,
    ]


async def _run_ffmpeg_concat(args: list[str]) -> None:
    """Run one ffmpeg command, fail with a clear FalVideoError on a non-zero exit.

    This is the seam tests monkeypatch. ffmpeg must be installed on the runtime
    (``shutil.which``); a missing binary is a hard error — a silent "joined"
    output would otherwise be a truncated/incomplete video with no diagnosis.
    """
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise FalVideoError(
            "ffmpeg is not installed — the studio backend needs it to stitch the "
            "per-pair clips of a 3+ image video into one file (e.g. `apt install ffmpeg`)"
        )
    proc = await asyncio.create_subprocess_exec(
        ffmpeg,
        *args,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        detail = stderr.decode("utf-8", errors="replace").strip()
        raise FalVideoError(
            f"ffmpeg failed to stitch the image-to-video clips (exit {proc.returncode}): "
            f"{detail[-500:]}"
        )


async def _concat_videos(clips: list[tuple[bytes, str]]) -> tuple[bytes, str]:
    """Losslessly join same-format video clips into one ``(bytes, mime)``.

    A single clip passes through untouched; N clips are written to temp files,
    listed for the concat demuxer, and joined with ``-c copy``. All inputs come
    from the same fal endpoint + duration + ratio, so the copy path is lossless.
    """
    if not clips:
        raise ValueError("at least one clip is required to concatenate")
    if len(clips) == 1:
        return clips[0][0], clips[0][1]
    mime = clips[0][1] or "video/mp4"
    with tempfile.TemporaryDirectory(prefix="pocketpaw-video-") as tmp:
        tmp_path = Path(tmp)
        entries: list[str] = []
        for i, (clip_bytes, _) in enumerate(clips):
            clip_path = tmp_path / f"clip-{i}.mp4"
            clip_path.write_bytes(clip_bytes)
            entries.append(f"file '{clip_path}'")  # absolute path → safe with -safe 0
        concat_file = tmp_path / "concat.txt"
        concat_file.write_text("\n".join(entries) + "\n", encoding="utf-8")
        output_path = tmp_path / "combined.mp4"
        await _run_ffmpeg_concat(_concat_ffmpeg_args(str(concat_file), str(output_path)))
        combined = output_path.read_bytes()
    return combined, mime


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


# ── Image-to-video dispatch ──────────────────────────────────────────────────


async def _run_image_to_video(
    *,
    prompt: str,
    image_urls: list[str],
    duration_sec: int | None,
    aspect_ratio: str | None,
    model: str | None,
    key: str,
) -> tuple[bytes, str, bytes | None, str | None]:
    """Turn one or more input images into ONE fal video, every image included.

    * 1 image  → one call: ``image_url`` (v1 standard, or the caller's endpoint).
    * 2 images → one call: first-frame → last-frame (v1.6 ``image_url`` +
      ``end_image_url``).
    * 3+ images → fal can only carry TWO frames per call, so every ADJACENT pair
      is animated as its own 2-frame clip (image1→image2, image2→image3, …) on
      the same v1.6 endpoint, then the clips are stitched into ONE video with
      ffmpeg (``_concat_videos``). The model does all the animation; ffmpeg only
      joins clips. This guarantees EVERY image appears, in order — a single-call
      ``keyframes`` attempt was ignored by fal (only the first image showed up).

    An empty image list is a ValueError; an empty prompt falls back to
    ``DEFAULT_I2V_PROMPT``. Returns ``(video_bytes, video_mime, poster_bytes,
    poster_mime)`` — the poster pair is ``(None, None)`` when the endpoint didn't
    return one (for chained runs the first clip's poster, i.e. the first frame)."""
    urls = [u for u in (image_urls or []) if u and u.strip()]
    if not urls:
        raise ValueError("at least one image URL is required for image-to-video")
    effective_prompt = (prompt or "").strip() or DEFAULT_I2V_PROMPT

    async def _one_clip(frames: list[str]) -> tuple[bytes, str, dict[str, Any]]:
        """Run ONE 1–2 frame image-to-video clip and return its bytes + result."""
        endpoint = resolve_image_to_video_endpoint(model, len(frames))
        arguments = build_image_to_video_arguments(
            prompt=effective_prompt,
            image_urls=frames,
            duration_sec=duration_sec,
            aspect_ratio=aspect_ratio,
            endpoint=endpoint,
        )
        result = await _run_fal(endpoint, arguments, key=key)
        video_url = _extract_video_url(result)
        if not video_url:
            raise FalVideoError(f"fal image-to-video '{endpoint}' returned no video data")
        video_bytes, video_mime = await _download(video_url)
        return video_bytes, video_mime, result

    if len(urls) <= 2:
        clips = [await _one_clip(urls)]
        video_bytes, video_mime = clips[0][0], clips[0][1]
        result: dict[str, Any] = clips[0][2]
    else:
        clips: list[tuple[bytes, str]] = []
        result = {}
        for i in range(len(urls) - 1):
            clip_bytes, clip_mime, pair_result = await _one_clip([urls[i], urls[i + 1]])
            clips.append((clip_bytes, clip_mime))
            if not result:
                result = pair_result  # first clip's poster = the final video's first frame
        video_bytes, video_mime = await _concat_videos(clips)

    poster_bytes: bytes | None = None
    poster_mime: str | None = None
    poster_url = _extract_poster_url(result)
    if poster_url:
        try:
            poster_bytes, poster_mime = await _download(poster_url)
        except Exception:  # noqa: BLE001 — a poster is cosmetic; never fail the video
            logger.warning(
                "studio: fal image-to-video poster download failed (non-fatal)", exc_info=True
            )
            poster_bytes, poster_mime = None, None
    return video_bytes, video_mime, poster_bytes, poster_mime


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
    image_urls: list[str] | None = None,
) -> tuple[bytes, str, bytes | None, str | None]:
    """Run a fal video generation and return the result.

    ``image_urls`` (one or more ``data:`` image URLs) switches to the
    image-to-video path — 1–2 images go in a single fal call, 3+ images chain
    every adjacent pair as a 2-frame clip and stitch them into ONE video on the
    backend (see ``_run_image_to_video``). Without them this is the plain
    text-to-video path.

    Returns ``(video_bytes, video_mime, poster_bytes, poster_mime)`` — the
    poster pair is ``(None, None)`` when the endpoint didn't return one. Raises
    ValueError (empty prompt on the text path / empty image list) and
    FalVideoError (missing key / upstream failure / no output).
    """
    # An explicit image list (even one that's all blanks) means the caller wants
    # image-to-video — fail fast BEFORE any fal call rather than falling through
    # to a text-to-video run with zero input frames.
    images: list[str] = []
    if image_urls is not None:
        images = [u for u in image_urls if u and u.strip()]
        if not images:
            raise ValueError("at least one image URL is required for image-to-video")
    text = (prompt or "").strip()

    api_key = key if key is not None else fal_edit.fal_api_key()
    if not api_key:
        raise FalVideoError("fal.ai API key is not configured (set FAL_AI_API_KEY)")

    if images:
        return await _run_image_to_video(
            prompt=text,
            image_urls=images,
            duration_sec=duration_sec,
            aspect_ratio=aspect_ratio,
            model=model,
            key=api_key,
        )

    if not text:
        raise ValueError("prompt is required for video generation")

    endpoint = resolve_endpoint(model)
    arguments = build_arguments(
        prompt=text, duration_sec=duration_sec, aspect_ratio=aspect_ratio, endpoint=endpoint
    )

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
    "DEFAULT_IMAGE_TO_VIDEO_MODEL",
    "IMAGE_TO_VIDEO_PAIR_MODEL",
    "DEFAULT_I2V_PROMPT",
    "VIDEO_MODEL_ALIASES",
    "IMAGE_TO_VIDEO_MODEL_ALIASES",
    "SUPPORTED_DURATIONS",
    "CURATED_VIDEO_MODELS",
    "FalVideoError",
    "resolve_endpoint",
    "resolve_image_to_video_endpoint",
    "build_arguments",
    "build_image_to_video_arguments",
    "run_fal_video",
]
