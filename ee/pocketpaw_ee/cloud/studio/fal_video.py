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
from typing import Any, NamedTuple

import httpx

from . import fal_edit, fal_errors

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

# ByteDance Seedance 2.5 image-to-video — the movie-maker's single-still path.
# Unlike Kling's 2-frame contract (image_url + optional end_image_url per call,
# 3+ images chained as pairs), Seedance animates ONE still into a native
# 30-second clip at up to 720p with NO stitching, so its contract is its own:
# ``image_url`` (start, required) + optional ``end_image_url`` + ``resolution``
# ("480p"/"720p") + ``duration`` (a STRING enum "auto"/"4".."30") + a string
# ``aspect_ratio`` enum (incl. "auto") + ``generate_audio`` (sync audio).
SEEDANCE_I2V_MODEL = "bytedance/seedance-2.5/image-to-video"

# ByteDance Seedance 2.5 REFERENCE-to-video — the movie pipeline's terminal node.
#
# The only endpoint family that accepts AUDIO as an input, which is what makes
# "character image + generated music + prompt -> one clip" a single call instead
# of a generate-then-mux dance. Its contract is three flat arrays of URL strings
# (``image_urls`` / ``video_urls`` / ``audio_urls``) plus a prompt that CITES them
# by 1-indexed token — "@Image1", "@Audio1", "@Video1". A reference nobody cites
# is dead weight the model may ignore, so the citation is part of the contract,
# not decoration.
#
# 2.5 is the default. A 2.0 endpoint exists with the SAME argument shape but much
# tighter limits (15s vs 30s, 12 files vs 50), so it stays recognised — a saved
# project naming it still routes here rather than falling through to Kling — and
# the caps are looked up per endpoint. Getting that backwards is a 422, not a
# graceful degrade, which is why the limits live in a table instead of as one set
# of constants.
#
# ``seed`` is NOT an input on 2.5 (it is output-only), so the reference path never
# sends one.
SEEDANCE_REF_TO_VIDEO_MODEL = "bytedance/seedance-2.5/reference-to-video"
SEEDANCE_REF_TO_VIDEO_LEGACY_MODEL = "bytedance/seedance-2.0/reference-to-video"
SEEDANCE_REF_TO_VIDEO_FAST_MODEL = "bytedance/seedance-2.0/fast/reference-to-video"


class _RefLimits(NamedTuple):
    images: int
    videos: int
    audio: int
    files: int
    duration_min: int
    duration_max: int


_REF_LIMITS_25 = _RefLimits(
    images=30, videos=10, audio=10, files=50, duration_min=4, duration_max=30
)
_REF_LIMITS_20 = _RefLimits(images=9, videos=3, audio=3, files=12, duration_min=4, duration_max=15)


def reference_limits(endpoint: str | None) -> _RefLimits:
    """fal's documented caps for a reference endpoint.

    2.0 and 2.5 take identical arguments and enforce very different limits, so
    the caps are resolved from the endpoint rather than assumed."""
    return _REF_LIMITS_20 if "seedance-2.0" in (endpoint or "") else _REF_LIMITS_25


# Back-compat aliases for callers that read the 2.5 caps directly.
REF_MAX_IMAGES = _REF_LIMITS_25.images
REF_MAX_VIDEOS = _REF_LIMITS_25.videos
REF_MAX_AUDIO = _REF_LIMITS_25.audio
REF_MAX_FILES = _REF_LIMITS_25.files
REF_DURATION_MIN = _REF_LIMITS_25.duration_min
REF_DURATION_MAX = _REF_LIMITS_25.duration_max

# Seedance 2.5 TEXT-to-video. Same family, same argument shape minus the frames.
#
# This is the PUBLIC route. The catalog used to advertise
# ``bytedance/seedance-2.5/enterprise/text-to-video``, which is not a routable
# fal path — every generation with it came back
# ``404 … Path /enterprise/text-to-video not found`` (→ our 502). The enterprise
# variant exists but sits behind a request-access gate, so it cannot be the
# default a picker offers. The dead id is aliased below rather than deleted, so
# projects that already saved it keep working.
SEEDANCE_T2V_MODEL = "bytedance/seedance-2.5/text-to-video"
SEEDANCE_T2V_ENTERPRISE_MODEL = "bytedance/seedance-2.5/enterprise/text-to-video"
SEEDANCE_RESOLUTIONS: tuple[str, ...] = ("480p", "720p")
# The durations the composer offers for this endpoint (Seedance accepts "4".."30",
# so 5s / 10s / 30s all map to valid string enums).
SEEDANCE_DURATIONS: tuple[int, ...] = (5, 10, 30)

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
    # The access-gated id the catalog used to ship → the public route. Kept as an
    # alias so a saved project or an in-flight client that still names it keeps
    # generating instead of 404-ing.
    SEEDANCE_T2V_ENTERPRISE_MODEL: SEEDANCE_T2V_MODEL,
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
        "id": SEEDANCE_T2V_MODEL,  # bytedance/seedance-2.5/text-to-video
        "name": "Seedance 2.5",
        "vendor": "ByteDance",
        "kind": "text-to-video",
        "aspect_ratios": ("16:9", "9:16", "1:1"),
        "durations": (5, 10),
        # fal declares Seedance's `duration` as an ENUM whose members are strings
        # ("auto", "4".."30"), not an integer field — the same encoding its
        # image-to-video sibling already used. This said False, which was the
        # next failure waiting behind the 404.
        "duration_as_string": True,
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
    "seedance_2_5_ref": {
        "id": SEEDANCE_REF_TO_VIDEO_MODEL,  # bytedance/seedance-2.5/reference-to-video
        "name": "Seedance 2.5 (reference)",
        "vendor": "ByteDance",
        "kind": "reference-to-video",
        # The full enum this endpoint accepts — wider than its siblings, which
        # offer no 21:9 or 4:3.
        "aspect_ratios": ("16:9", "9:16", "1:1", "21:9", "4:3", "3:4"),
        # 4..30; the composer offers the three that matter. This is the only
        # video model that takes an AUDIO input, so it is what a flow lands on
        # when a Music node is wired into its Video node.
        "durations": (5, 10, 30),
        "duration_as_string": True,
    },
    "seedance_2_5_i2v": {
        "id": SEEDANCE_I2V_MODEL,  # bytedance/seedance-2.5/image-to-video
        "name": "Seedance 2.5 (image)",
        "vendor": "ByteDance",
        "kind": "image-to-video",
        "aspect_ratios": ("16:9", "9:16", "1:1", "4:3", "3:4"),
        "durations": SEEDANCE_DURATIONS,
        "duration_as_string": True,
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
    """Encode ``duration`` the way the endpoint expects.

    Kling and Seedance both take a string enum ("5" / "10"); Gemini and anything
    else take an integer of seconds.

    Seedance was on the integer branch, which was wrong: fal types its
    ``duration`` as an enum of string members ("auto", "4".."30") and its
    documented payload sends ``"duration": "auto"``. The image-to-video builder
    had this right already (`str(int(duration_sec))`) — the text path did not,
    and nobody noticed because the endpoint it pointed at 404'd before fal ever
    validated an argument.
    """
    if "kling-video" in endpoint or "seedance" in endpoint:
        return str(int(duration_sec))
    return int(duration_sec)


# Client + server-side deadlines for the fal call. Video jobs queue and render
# for seconds to minutes, so these are generous but bounded so a hung upstream
# fails fast instead of pinning a worker forever.
_CLIENT_TIMEOUT = 600.0
_START_TIMEOUT = 180.0
_DOWNLOAD_TIMEOUT = 300.0


class FalVideoRejected(Exception):
    """fal REFUSED the request — a content-policy hit, a file too long, a value
    out of range. The user can fix these by changing an input, so they must not
    be reported as an upstream outage: the router maps this to a 4xx and shows
    the message, while ``FalVideoError`` stays a 502."""

    def __init__(self, message: str, *, code: str | None = None, field: str | None = None):
        super().__init__(message)
        self.code = code
        self.field = field


class FalVideoError(Exception):
    """A fal.ai video-generation call failed (missing SDK, upstream error,
    malformed result, or no output video). The studio service maps this to 502."""


# ── Endpoint + argument building ─────────────────────────────────────────────


def resolve_endpoint(model_id: str | None) -> str:
    """Map a requested model id onto a real fal endpoint.

    Known aliases resolve first; otherwise endpoint-looking ids (``fal-ai/…``,
    ``bytedance/…``, ``google/…``, …) pass straight through, and anything
    unknown falls back to ``DEFAULT_VIDEO_MODEL``.

    Aliases are checked BEFORE the namespace passthrough so a dead endpoint id
    can be redirected. That ordering is the whole reason the Seedance enterprise
    entry works: it starts with ``bytedance/``, so under a passthrough-first rule
    it would sail past the alias table and 404 at fal. Every pre-existing alias
    maps an endpoint-looking id to itself, so nothing else changes meaning.
    """
    m = (model_id or "").strip()
    aliased = VIDEO_MODEL_ALIASES.get(m)
    if aliased:
        return aliased
    if m.startswith(_ENDPOINT_NAMESPACES):
        return m
    return DEFAULT_VIDEO_MODEL


def supports_generate_audio(endpoint: str | None) -> bool:
    """True when ``endpoint`` accepts a ``generate_audio`` flag.

    Seedance generates picture and sound together in one pass — ``generate_audio``
    is a documented boolean on both its text-to-video and image-to-video
    contracts, default ``true``, and fal charges the same either way. Kling has
    no such field at all.

    This is a CAPABILITY check rather than an image/text one on purpose. The flag
    used to ride along only on the image-to-video path, which meant a Seedance
    text-to-video run silently lost the user's choice: audio came back on, since
    that is fal's default, whatever the switch said. Gating on the model instead
    is also what keeps the UI honest — a toggle offered for Kling would be a
    control that cannot do anything, and an argument fal does not know is exactly
    the kind of thing a provider drops without complaining.
    """
    return "seedance" in (endpoint or "").strip().lower()


def build_arguments(
    *,
    prompt: str,
    duration_sec: int | None,
    aspect_ratio: str | None,
    endpoint: str | None = None,
    generate_audio: bool | None = None,
) -> dict[str, Any]:
    """Build the fal ``arguments`` dict for a text-to-video call.

    Pure + side-effect free so it is unit-testable in isolation. Kling standard
    takes ``duration`` as a string ("5" / "10"); Seedance takes an integer of
    seconds — ``_duration_value`` encodes per-endpoint. ``aspect_ratio`` is one
    of "16:9" / "9:16" / "1:1". Unsupported values pass through and the
    endpoint's own validation reports them (clearer than silently clamping).

    ``generate_audio`` is forwarded only to endpoints that document it (see
    ``supports_generate_audio``) and only when the caller actually expressed a
    preference — ``None`` leaves it out so fal applies its own default.
    """
    args: dict[str, Any] = {"prompt": prompt}
    if duration_sec and duration_sec > 0:
        args["duration"] = _duration_value(endpoint or DEFAULT_VIDEO_MODEL, duration_sec)
    if aspect_ratio:
        args["aspect_ratio"] = aspect_ratio
    if generate_audio is not None and supports_generate_audio(endpoint or DEFAULT_VIDEO_MODEL):
        args["generate_audio"] = bool(generate_audio)
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
        args["duration"] = _duration_value(endpoint or DEFAULT_IMAGE_TO_VIDEO_MODEL, duration_sec)
    if aspect_ratio:
        args["aspect_ratio"] = aspect_ratio
    return args


# ── Seedance 2.5 image-to-video (single still → 30s clip, no stitching) ──────


def is_seedance_i2v_endpoint(endpoint: str | None) -> bool:
    """True when ``endpoint`` is the Seedance 2.5 image-to-video model.

    Seedance's i2v contract is nothing like Kling's (one still, string enums for
    duration/aspect, resolution, sync audio), so the dispatcher must short-circuit
    the Kling pair-chaining path the moment it sees this model."""
    e = (endpoint or "").strip()
    return "seedance-2.5" in e and "image-to-video" in e


def build_seedance_i2v_arguments(
    *,
    image_url: str,
    prompt: str | None = None,
    end_image_url: str | None = None,
    resolution: str | None = None,
    duration_sec: int | None = None,
    aspect_ratio: str | None = None,
    generate_audio: bool | None = None,
) -> dict[str, Any]:
    """Build the fal ``arguments`` dict for ONE Seedance 2.5 i2v call.

    Pure + side-effect free so it unit-tests in isolation. Mirrors the fal schema
    for ``bytedance/seedance-2.5/image-to-video``:
      * ``image_url``    — required start frame.
      * ``end_image_url``— optional end frame.
      * ``resolution``   — "480p" | "720p".
      * ``duration``     — a STRING enum ("auto"/"4".."30"); we send str(seconds)
        for the composer's 5s/10s/30s choices.
      * ``aspect_ratio`` — a string enum (incl. "auto"); the composer sends a
        concrete ratio like "16:9".
      * ``generate_audio`` — boolean (sync audio alongside the video).

    Blank/None values are omitted so fal fills its own defaults ("auto" duration,
    "auto" aspect, "720p" resolution, audio on).
    """
    args: dict[str, Any] = {"image_url": image_url}
    if prompt and prompt.strip():
        args["prompt"] = prompt.strip()
    if end_image_url and end_image_url.strip():
        args["end_image_url"] = end_image_url.strip()
    if resolution:
        args["resolution"] = resolution
    if duration_sec and duration_sec > 0:
        args["duration"] = str(int(duration_sec))
    if aspect_ratio:
        args["aspect_ratio"] = aspect_ratio
    if generate_audio is not None:
        args["generate_audio"] = bool(generate_audio)
    return args


def is_seedance_reference_endpoint(endpoint: str | None) -> bool:
    """True when ``endpoint`` is a Seedance 2.0 reference-to-video model.

    Both the standard and fast tiers share one schema, so one predicate covers
    the dispatcher for both."""
    e = (endpoint or "").strip()
    return "reference-to-video" in e and ("seedance-2.5" in e or "seedance-2.0" in e)


def reference_token(kind: str, index: int) -> str:
    """The token that cites a reference inside the prompt: ``@Image1``.

    1-indexed, matching the position in the corresponding array. Centralised
    because an off-by-one here silently points the model at the wrong asset —
    the call still succeeds and the video is simply built from something else.
    """
    return f"@{kind}{index + 1}"


def annotate_reference_prompt(
    prompt: str,
    *,
    image_count: int = 0,
    video_count: int = 0,
    audio_count: int = 0,
) -> str:
    """Append citations for any reference the prompt does not already mention.

    fal's contract is that an asset is used by being CITED. A caller who attaches
    a character still and a music bed but writes a prompt that never says
    "@Image1" has attached them for nothing — the call succeeds and the model
    quietly ignores the references. Rather than let that fail silently, the
    uncited ones are appended in a short trailing clause.
    """
    text = (prompt or "").strip()
    missing: list[str] = []
    for kind, count in (("Image", image_count), ("Video", video_count), ("Audio", audio_count)):
        for i in range(max(0, count)):
            token = reference_token(kind, i)
            if token not in text:
                missing.append(token)
    if not missing:
        return text
    clause = f"Use {', '.join(missing)} as reference."
    if not text:
        return clause
    # Close the prompt off first, or the clause runs on: "a detective walks in
    # Use @Image1 as reference."
    stem = text if text.endswith((".", "!", "?", ",", ";", ":")) else f"{text}."
    return f"{stem} {clause}"


def build_reference_arguments(
    *,
    prompt: str,
    endpoint: str | None = None,
    image_urls: list[str] | None = None,
    video_urls: list[str] | None = None,
    audio_urls: list[str] | None = None,
    resolution: str | None = None,
    duration_sec: int | None = None,
    aspect_ratio: str | None = None,
    generate_audio: bool | None = None,
    bitrate_mode: str | None = None,
) -> dict[str, Any]:
    """Build the fal ``arguments`` for ONE Seedance 2.0 reference-to-video call.

    Pure + side-effect free so it unit-tests in isolation. Enforces fal's caps
    HERE rather than letting the endpoint 422: over-long arrays are truncated to
    their documented maximum and duration is clamped into the endpoint's range
    (4..30 on 2.5, 4..15 on the tighter 2.0).

    Raises ValueError for the one case that cannot be salvaged by truncating —
    audio with no image or video to anchor it, which fal rejects outright.
    """
    limits = reference_limits(endpoint or SEEDANCE_REF_TO_VIDEO_MODEL)
    images = [u.strip() for u in (image_urls or []) if u and u.strip()][: limits.images]
    videos = [u.strip() for u in (video_urls or []) if u and u.strip()][: limits.videos]
    audio = [u.strip() for u in (audio_urls or []) if u and u.strip()][: limits.audio]

    if not images and not videos:
        # fal: "At least one reference image or video is required." Now that this
        # model is selectable in the composer, a user can reach it with nothing
        # wired — so name the missing piece here rather than return a 422 whose
        # message says only that the request was invalid.
        raise ValueError(
            "Seedance reference-to-video needs at least one reference image or video"
            + (" — audio alone is not enough" if audio else "")
        )

    # The overall file cap. Trimmed from the least specific end — extra images
    # are the most replaceable, and dropping the audio or the only video would
    # change what the shot IS.
    while len(images) + len(videos) + len(audio) > limits.files and images:
        images.pop()

    args: dict[str, Any] = {
        "prompt": annotate_reference_prompt(
            prompt,
            image_count=len(images),
            video_count=len(videos),
            audio_count=len(audio),
        )
    }
    if images:
        args["image_urls"] = images
    if videos:
        args["video_urls"] = videos
    if audio:
        args["audio_urls"] = audio
    if resolution:
        args["resolution"] = resolution
    if duration_sec and duration_sec > 0:
        clamped = max(limits.duration_min, min(limits.duration_max, int(duration_sec)))
        args["duration"] = str(clamped)
    if aspect_ratio:
        args["aspect_ratio"] = aspect_ratio
    if generate_audio is not None:
        args["generate_audio"] = bool(generate_audio)
    if bitrate_mode:
        args["bitrate_mode"] = bitrate_mode
    # No ``seed``: it is an OUTPUT field on 2.5, not an input. Sending it would
    # be an unrecognised argument on the endpoint this path defaults to.
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


async def _run_seedance_reference(
    *,
    prompt: str,
    endpoint: str,
    image_urls: list[str] | None = None,
    video_urls: list[str] | None = None,
    audio_urls: list[str] | None = None,
    resolution: str | None = None,
    duration_sec: int | None = None,
    aspect_ratio: str | None = None,
    generate_audio: bool | None = None,
    key: str,
) -> tuple[bytes, str, bytes | None, str | None]:
    """Run ONE Seedance 2.0 reference-to-video call and return the result bytes.

    One call, however many references: the endpoint takes the arrays whole, so
    unlike Kling's pair-chaining there is nothing to stitch afterwards.
    """
    arguments = build_reference_arguments(
        prompt=prompt,
        endpoint=endpoint,
        image_urls=image_urls,
        video_urls=video_urls,
        audio_urls=audio_urls,
        resolution=resolution,
        duration_sec=duration_sec,
        aspect_ratio=aspect_ratio,
        generate_audio=generate_audio,
    )
    result = await _run_fal(endpoint, arguments, key=key)
    video_url = _extract_video_url(result)
    if not video_url:
        raise FalVideoError(f"fal Seedance reference '{endpoint}' returned no video data")
    video_bytes, video_mime = await _download(video_url)

    poster_bytes: bytes | None = None
    poster_mime: str | None = None
    poster_url = _extract_poster_url(result)
    if poster_url:
        try:
            poster_bytes, poster_mime = await _download(poster_url)
        except Exception:  # noqa: BLE001 — a poster is cosmetic; never fail the video
            logger.warning(
                "studio: fal Seedance reference poster download failed (non-fatal)",
                exc_info=True,
            )
    return video_bytes, video_mime, poster_bytes, poster_mime


async def _run_seedance_i2v(
    *,
    image_url: str,
    prompt: str,
    end_image_url: str | None = None,
    resolution: str | None = None,
    duration_sec: int | None = None,
    aspect_ratio: str | None = None,
    generate_audio: bool | None = None,
    key: str,
) -> tuple[bytes, str, bytes | None, str | None]:
    """Run ONE Seedance 2.5 image-to-video call and return the result bytes.

    The Seedance i2v contract is a single still (no Kling pair chaining), so this
    is a plain one-call dispatch: build the Seedance-shaped arguments, run the
    endpoint via ``_run_fal``, download the output video (+ optional poster).
    """
    arguments = build_seedance_i2v_arguments(
        image_url=image_url,
        prompt=prompt,
        end_image_url=end_image_url,
        resolution=resolution,
        duration_sec=duration_sec,
        aspect_ratio=aspect_ratio,
        generate_audio=generate_audio,
    )
    result = await _run_fal(SEEDANCE_I2V_MODEL, arguments, key=key)
    video_url = _extract_video_url(result)
    if not video_url:
        raise FalVideoError(f"fal Seedance i2v '{SEEDANCE_I2V_MODEL}' returned no video data")
    video_bytes, video_mime = await _download(video_url)

    poster_bytes: bytes | None = None
    poster_mime: str | None = None
    poster_url = _extract_poster_url(result)
    if poster_url:
        try:
            poster_bytes, poster_mime = await _download(poster_url)
        except Exception:  # noqa: BLE001 — a poster is cosmetic; never fail the video
            logger.warning(
                "studio: fal Seedance i2v poster download failed (non-fatal)", exc_info=True
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
        failure = fal_errors.parse_fal_error(exc, action="video generation")
        if failure.client_fault:
            # A request fal refused. One log line, NO traceback and NO payload:
            # the rejected body carries the base64 images and audio, and dumping
            # it taught nobody anything while making the logs unreadable. The
            # reason travels to the user instead, which is where it is useful.
            logger.info("studio: fal video '%s' rejected — %s", endpoint, failure.log_line)
            raise FalVideoRejected(failure.message, code=failure.code, field=failure.field) from exc
        # A genuine failure — keep the traceback, that one is ours to debug.
        logger.warning("studio: fal video '%s' failed", endpoint, exc_info=True)
        raise FalVideoError(failure.message) from exc
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
    resolution: str | None = None,
    generate_audio: bool | None = None,
    audio_urls: list[str] | None = None,
    video_urls: list[str] | None = None,
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

    # Seedance 2.0 reference-to-video is the only endpoint that accepts audio, so
    # it claims the call whenever audio is attached — even if the caller named a
    # different model, since every other endpoint would silently drop the track
    # and hand back a video with none of the music that was asked for.
    audio = [u for u in (audio_urls or []) if u and u.strip()]
    videos = [u for u in (video_urls or []) if u and u.strip()]
    if audio or videos or is_seedance_reference_endpoint(model):
        endpoint = model if is_seedance_reference_endpoint(model) else SEEDANCE_REF_TO_VIDEO_MODEL
        return await _run_seedance_reference(
            prompt=text or DEFAULT_I2V_PROMPT,
            endpoint=endpoint,
            image_urls=images,
            video_urls=videos,
            audio_urls=audio,
            resolution=resolution,
            duration_sec=duration_sec,
            aspect_ratio=aspect_ratio,
            generate_audio=generate_audio,
            key=api_key,
        )

    if images:
        # Seedance 2.5 i2v is its own single-still contract (no Kling pair
        # chaining) — short-circuit before the Kling path so its string-enum
        # duration/aspect + resolution + sync-audio arguments are built correctly.
        if is_seedance_i2v_endpoint(model):
            return await _run_seedance_i2v(
                image_url=images[0],
                prompt=text or DEFAULT_I2V_PROMPT,
                end_image_url=images[1] if len(images) > 1 else None,
                resolution=resolution,
                duration_sec=duration_sec,
                aspect_ratio=aspect_ratio,
                generate_audio=generate_audio,
                key=api_key,
            )
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
        prompt=text,
        duration_sec=duration_sec,
        aspect_ratio=aspect_ratio,
        endpoint=endpoint,
        generate_audio=generate_audio,
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
    "SEEDANCE_I2V_MODEL",
    "SEEDANCE_REF_TO_VIDEO_MODEL",
    "SEEDANCE_REF_TO_VIDEO_LEGACY_MODEL",
    "SEEDANCE_REF_TO_VIDEO_FAST_MODEL",
    "reference_limits",
    "REF_MAX_IMAGES",
    "REF_MAX_VIDEOS",
    "REF_MAX_AUDIO",
    "REF_MAX_FILES",
    "SEEDANCE_T2V_MODEL",
    "SEEDANCE_T2V_ENTERPRISE_MODEL",
    "SEEDANCE_RESOLUTIONS",
    "SEEDANCE_DURATIONS",
    "VIDEO_MODEL_ALIASES",
    "IMAGE_TO_VIDEO_MODEL_ALIASES",
    "SUPPORTED_DURATIONS",
    "CURATED_VIDEO_MODELS",
    "FalVideoError",
    "FalVideoRejected",
    "resolve_endpoint",
    "resolve_image_to_video_endpoint",
    "build_arguments",
    "build_image_to_video_arguments",
    "build_seedance_i2v_arguments",
    "is_seedance_i2v_endpoint",
    "is_seedance_reference_endpoint",
    "build_reference_arguments",
    "annotate_reference_prompt",
    "reference_token",
    "supports_generate_audio",
    "run_fal_video",
]
