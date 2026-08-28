# ee/pocketpaw_ee/cloud/studio/fal_image.py — CURATED image model registry +
# direct fal.ai image generation (text-to-image + reference-based edit).
#
# The /studio composer already generates images through the LiteLLM gateway
# (proxy ``/v1/images/generations``), but the gateway only serves whatever an
# operator fronted — there is no curated, documented image-model registry with
# per-model metadata, no reference-based generation (character/location/element
# consistency), and no per-model reference-image caps. This module fills that
# gap with a SMALL curated registry (the endpoints the movie-maker actually
# needs) and direct fal dispatch for the two paths the proxy can't cover:
#   * reference-based generation — the model's EDIT endpoint, fed ``image_urls``
#     (reference images) + a prompt, so a shot can be conditioned on consistent
#     characters/locations/elements.
#   * curated text-to-image — the same endpoints run DIRECTLY against fal so the
#     curated models are usable regardless of proxy catalog config.
#
# Same credential path (``FAL_AI_API_KEY``), SDK (``fal_client.AsyncClient``),
# and persistence (bytes → media storage) as the other fal_* modules. Argument
# building + result extraction are pure; ``_run_fal`` / ``_download`` are the
# seams tests monkeypatch.
#
# Created 2026-08-25 (studio-curated-image-models): curated registry + direct
# fal image dispatch.

from __future__ import annotations

import logging
from typing import Any

import httpx

from . import fal_edit

logger = logging.getLogger(__name__)

# ── The curated image model registry (text-to-image) ────────────────────────
# The reduced set the movie-maker composer offers. ``image_size`` handling
# differs per model:
#   * gpt_image_2 — OpenAI's native pixel sizes (``"1024x1024"`` etc.), no
#     preset-name scheme.
#   * everything else — fal's ``image_size`` preset names (``square_hd`` etc.).

IMAGE_MODELS: dict[str, dict[str, Any]] = {
    "nano_banana": {
        "id": "fal-ai/nano-banana-2",
        "name": "Nano Banana 2",
        "vendor": "Google",
        "description": "Fast image generation and editing",
    },
    "gpt_image_2": {
        "id": "openai/gpt-image-2",
        "name": "GPT Image 2",
        "vendor": "OpenAI",
        "description": "Near-perfect text rendering, native image sizing",
    },
    "seedream_2k": {
        "id": "bytedance/seedream/v5/pro/text-to-image",
        "name": "Seedream 2K",
        "vendor": "ByteDance",
        "description": "Native 2K generation and editing",
    },
    "recraft_v3": {
        "id": "fal-ai/recraft/v3/text-to-image",
        "name": "Recraft V3",
        "vendor": "Recraft",
        "description": "Design-grade illustrations and vector-style art",
    },
    "grok_imagine": {
        "id": "xai/grok-imagine-image/v2.0/text-to-image",
        "name": "Grok Imagine 2.0",
        "vendor": "xAI",
        "description": "Imagine image model — 1K/2K, edit up to 3 refs",
    },
}

DEFAULT_IMAGE_MODEL = "nano_banana"

# Human labels for a future /studio image-model picker.
IMAGE_MODEL_LABELS: dict[str, str] = {m["id"]: m["name"] for m in IMAGE_MODELS.values()}
IMAGE_MODEL_IDS: frozenset[str] = frozenset(m["id"] for m in IMAGE_MODELS.values())

# ── Edit endpoints (the "/edits" variants) ───────────────────────────────────
# Map a curated model key → its fal EDIT endpoint. nano-banana and gpt-image-2
# are reference-based edits (``image_urls`` + prompt); seedream's variant is the
# native edit endpoint (single ``image_url`` + prompt — same shape as the
# sketch-to-image op in fal_edit). The builder below handles both shapes.

EDIT_ENDPOINTS: dict[str, str] = {
    "nano_banana": "fal-ai/nano-banana-2/edit",
    "gpt_image_2": "openai/gpt-image-2/edit",
    "seedream_2k": "bytedance/seedream/v5/pro/edit",
}

# Per-model ceiling on ``image_urls`` for the reference-based edit endpoints.
# fal enforces these server-side (it REJECTS over the limit, it does not
# truncate). Absent = no known cap.
EDIT_REFERENCE_LIMITS: dict[str, int] = {
    "grok_imagine": 3,
}

# ── Aspect ratio → image_size ────────────────────────────────────────────────
# The studio composer speaks ratios; fal image models take an ``image_size``.
# fal-ai preset names are multiples of 16; gpt-image-2 takes OpenAI pixel dims.

IMAGE_SIZE_MAP: dict[str, str] = {
    "1:1": "square_hd",
    "16:9": "landscape_16_9",
    "9:16": "portrait_16_9",
    "4:3": "landscape_4_3",
    "3:4": "portrait_4_3",
    "3:2": "landscape_3_2",
    "2:3": "portrait_3_2",
}

GPT_IMAGE_SIZE_MAP: dict[str, str] = {
    "1:1": "1024x1024",
    "16:9": "1536x1024",
    "9:16": "1024x1536",
    "4:3": "1024x1024",
    "3:4": "1024x1024",
    "3:2": "1536x1024",
    "2:3": "1024x1536",
}


def resolve_image_size(aspect_ratio: str | None, model_key: str | None = None) -> str:
    """Map a composer aspect ratio onto the model's ``image_size`` value."""
    ratio = (aspect_ratio or "1:1").strip()
    if model_key == "gpt_image_2":
        return GPT_IMAGE_SIZE_MAP.get(ratio, "1024x1024")
    return IMAGE_SIZE_MAP.get(ratio, "square_hd")


# ── Helpers ──────────────────────────────────────────────────────────────────


def model_key_for_id(model_id: str | None) -> str | None:
    """Return the curated registry key for a model id, else None."""
    m = (model_id or "").strip()
    for key, cfg in IMAGE_MODELS.items():
        if cfg["id"] == m:
            return key
    return None


def cap_reference_images(model_key: str | None, references: list[str]) -> list[str]:
    """Trim reference images to what ``model``'s edit endpoint accepts. Order is
    preserved (caller orders by identity priority), so truncation drops the least
    identity-critical references last."""
    if not model_key or model_key not in EDIT_REFERENCE_LIMITS:
        return references
    return references[: EDIT_REFERENCE_LIMITS[model_key]]


# ── Argument building ────────────────────────────────────────────────────────


def build_text_to_image_arguments(
    *,
    prompt: str,
    aspect_ratio: str | None = None,
    count: int = 1,
    seed: int | None = None,
    model_key: str | None = None,
) -> dict[str, Any]:
    """Build the fal ``arguments`` dict for a curated text-to-image call.

    Pure + side-effect free. The generic shape covers the fal-hosted seedream /
    recraft / nano-banana family; gpt-image-2 uses its own native sizing and
    fewer knobs (OpenAI rejects unknown fields, so it gets only prompt + size +
    count)."""
    if model_key == "gpt_image_2":
        args: dict[str, Any] = {
            "prompt": prompt,
            "image_size": resolve_image_size(aspect_ratio, model_key),
            "num_images": max(1, min(int(count), 4)),
        }
        if seed is not None:
            args["seed"] = int(seed)
        return args

    args = {
        "prompt": prompt,
        "image_size": resolve_image_size(aspect_ratio, model_key),
        "num_images": max(1, min(int(count), 4)),
        "output_format": "jpeg",
        "sync_mode": False,
    }
    if seed is not None:
        args["seed"] = int(seed)
    return args


def build_edit_arguments(
    *,
    prompt: str,
    image_urls: list[str],
    model_key: str | None = None,
    aspect_ratio: str | None = None,
    count: int = 1,
    seed: int | None = None,
) -> dict[str, Any]:
    """Build the fal ``arguments`` dict for a reference-based edit call.

    nano-banana / gpt-image-2 take the references as ``image_urls`` (capped per
    model) + prompt; seedream's edit endpoint is a SINGLE-image op, so a single
    reference is sent as ``image_url`` (same shape as fal_edit's sketch-to-image
    op). Pure + side-effect free; raises ValueError when no references were
    supplied.
    """
    refs = [u for u in (image_urls or []) if u and u.strip()]
    if not refs:
        raise ValueError("at least one reference image is required for image edit")
    refs = cap_reference_images(model_key, refs)

    if model_key == "seedream_2k":
        args = {
            "prompt": prompt,
            "image_url": refs[0],
        }
        return args

    args = {
        "prompt": prompt,
        "image_urls": refs,
        "num_images": max(1, min(int(count), 4)),
        "output_format": "jpeg",
        "sync_mode": False,
    }
    if seed is not None:
        args["seed"] = int(seed)
    return args


# ── Result handling ──────────────────────────────────────────────────────────


def _extract_image_urls(result: dict[str, Any]) -> list[str]:
    """Pull every output image URL from a fal image result (``images`` list or
    single ``image`` object)."""
    urls: list[str] = []
    images = result.get("images")
    if isinstance(images, list):
        for im in images:
            if isinstance(im, dict) and im.get("url"):
                urls.append(im["url"])
    elif isinstance(images, dict) and images.get("url"):
        urls.append(images["url"])
    single = result.get("image")
    if isinstance(single, dict) and single.get("url"):
        urls.append(single["url"])
    return urls


async def _download(url: str) -> tuple[bytes, str]:
    """Download one fal-hosted result image and return ``(bytes, mime)``."""
    async with httpx.AsyncClient(timeout=180.0) as client:
        resp = await client.get(url)
        resp.raise_for_status()
    mime = resp.headers.get("content-type", "image/jpeg").split(";")[0].strip() or "image/jpeg"
    return resp.content, mime


# ── The fal SDK seam (tests inject _run_fal) ────────────────────────────────


async def _run_fal(
    endpoint: str,
    arguments: dict[str, Any],
    *,
    key: str,
) -> dict[str, Any]:
    """Run a fal endpoint via the official SDK. Lazy-imports fal_client."""
    try:
        import fal_client  # noqa: PLC0415 — lazy: SDK is an optional runtime dep
    except ImportError as exc:  # pragma: no cover — env with the dep installed
        raise FalImageError(
            "fal-client is not installed — run `pip install fal-client` (pocketpaw-ee dep)"
        ) from exc
    try:
        client = fal_client.AsyncClient(key=key, default_timeout=300.0)
        result = await client.run(
            endpoint,
            arguments=arguments,
            timeout=300.0,
            start_timeout=180.0,
        )
    except Exception as exc:  # noqa: BLE001 — surface the upstream reason to the user
        logger.warning("studio: fal image '%s' failed", endpoint, exc_info=True)
        raise FalImageError(f"fal image '{endpoint}' failed: {exc}") from exc
    if not isinstance(result, dict):
        raise FalImageError(f"fal image '{endpoint}' returned an unexpected result")
    return result


class FalImageError(Exception):
    """A fal.ai image call failed (missing SDK, upstream error, malformed
    result, or no image data). The studio service maps this to a 502."""


# ── Public entry points ──────────────────────────────────────────────────────


async def run_fal_image(
    *,
    prompt: str,
    model: str | None = None,
    aspect_ratio: str | None = None,
    count: int = 1,
    seed: int | None = None,
    key: str | None = None,
) -> list[tuple[bytes, str]]:
    """Run a curated text-to-image generation and return ``[(bytes, mime), …]``."""
    text = (prompt or "").strip()
    if not text:
        raise ValueError("prompt is required for image generation")

    model_key = model_key_for_id(model)
    endpoint = IMAGE_MODELS[model_key]["id"] if model_key else (model or "").strip()
    if not endpoint:
        raise ValueError("model is required for image generation")

    arguments = build_text_to_image_arguments(
        prompt=text,
        aspect_ratio=aspect_ratio,
        count=count,
        seed=seed,
        model_key=model_key,
    )

    api_key = key if key is not None else fal_edit.fal_api_key()
    if not api_key:
        raise FalImageError("fal.ai API key is not configured (set FAL_AI_API_KEY)")

    result = await _run_fal(endpoint, arguments, key=api_key)
    urls = _extract_image_urls(result)
    if not urls:
        raise FalImageError(f"fal image '{endpoint}' returned no image data")
    return [await _download(url) for url in urls]


async def run_fal_image_edit(
    *,
    prompt: str,
    image_urls: list[str],
    model: str | None = None,
    aspect_ratio: str | None = None,
    count: int = 1,
    seed: int | None = None,
    key: str | None = None,
) -> list[tuple[bytes, str]]:
    """Run a reference-based image edit and return ``[(bytes, mime), …]``.

    ``image_urls`` are the reference images (character/location/element) the edit
    endpoint conditions on; the model is resolved to its edit variant via
    ``EDIT_ENDPOINTS`` (seedream resolves to its single-image edit endpoint).
    Raises ValueError (no references / no edit endpoint for the model) and
    FalImageError (upstream / no output).
    """
    text = (prompt or "").strip()
    if not text:
        raise ValueError("prompt is required for image edit")
    refs = [u for u in (image_urls or []) if u and u.strip()]
    if not refs:
        raise ValueError("at least one reference image is required for image edit")

    model_key = model_key_for_id(model)
    if model_key not in EDIT_ENDPOINTS:
        raise ValueError(f"model '{model or ''}' has no edit variant")
    endpoint = EDIT_ENDPOINTS[model_key]

    arguments = build_edit_arguments(
        prompt=text,
        image_urls=refs,
        model_key=model_key,
        aspect_ratio=aspect_ratio,
        count=count,
        seed=seed,
    )

    api_key = key if key is not None else fal_edit.fal_api_key()
    if not api_key:
        raise FalImageError("fal.ai API key is not configured (set FAL_AI_API_KEY)")

    result = await _run_fal(endpoint, arguments, key=api_key)
    urls = _extract_image_urls(result)
    if not urls:
        raise FalImageError(f"fal image edit '{endpoint}' returned no image data")
    return [await _download(url) for url in urls]


__all__ = [
    "IMAGE_MODELS",
    "DEFAULT_IMAGE_MODEL",
    "IMAGE_MODEL_IDS",
    "IMAGE_MODEL_LABELS",
    "EDIT_ENDPOINTS",
    "EDIT_REFERENCE_LIMITS",
    "IMAGE_SIZE_MAP",
    "GPT_IMAGE_SIZE_MAP",
    "FalImageError",
    "model_key_for_id",
    "cap_reference_images",
    "resolve_image_size",
    "build_text_to_image_arguments",
    "build_edit_arguments",
    "run_fal_image",
    "run_fal_image_edit",
]
