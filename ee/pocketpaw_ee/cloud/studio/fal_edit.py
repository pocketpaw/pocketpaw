# ee/pocketpaw_ee/cloud/studio/fal_edit.py — direct fal.ai image-EDIT client.
#
# LiteLLM proxies the text-to-image (generation) models, but it has NO route for
# the fal.ai image-EDIT endpoints (``fal-ai/nano-banana-2/edit``,
# ``fal-ai/birefnet/v2``, ``fal-ai/esrgan``, …). Those are called DIRECTLY
# against fal here, so the /studio canvas edit ops (inpaint / expand-outpaint /
# upscale / variations / remove-bg / edit / sketch-to-image) actually work.
#
# The studio service resolves which fal endpoint an EditOp maps to, builds the
# arguments (source image as a ``data:`` URL — fal accepts these natively, no
# extra storage upload needed), runs the model through the OFFICIAL fal-client
# SDK (``fal_client.AsyncClient``), downloads the result image(s), and returns
# them as bytes for the service to persist through media storage.
#
# Config:
#   FAL_AI_API_KEY  — fal.ai API key (the value in pocketpaw's .env). Falls back
#                     to ``FAL_KEY``, the env var the fal-client SDK reads
#                     natively, so a dev env that set the canonical name also
#                     works without a second export.
#
# The op → default-model table below is the "edit model catalog" (a curated
# subset of the deployment's fal model registry — the same entries the platform
# exposes via GET /studio/models). Each op also accepts an explicit ``model``
# override from EditRequest; it must be one of EDIT_MODEL_IDS. Endpoint argument
# names follow each model's fal API page (image_url / mask_url / upscale_factor);
# the models chosen are the long-stable ones so the shapes are dependable.
#
# Created 2026-08-18 (studio-fal-edit): direct fal edit dispatch.

from __future__ import annotations

import base64
import logging
import os
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# ── The edit-op catalog (paw-enterprise core/studio/types.ts EditOp) ─────────

SUPPORTED_OPS: frozenset[str] = frozenset(
    {"inpaint", "expand", "upscale", "variations", "remove-bg", "edit", "sketch-to-image"}
)

# Default fal endpoint per op. ``edit``/``inpaint``/``expand``/``variations`` use
# Nano Banana 2's edit endpoint (region-precise, strong instruction adherence);
# ``remove-bg`` uses BiRefNet V2 (high-res segmentation); ``upscale`` uses
# Real-ESRGAN (the long-stable ``{image_url, upscale_factor}`` contract);
# ``sketch-to-image`` uses Seedream 5 Pro (native sketch completion).
DEFAULT_EDIT_MODELS: dict[str, str] = {
    "edit": "fal-ai/nano-banana-2/edit",
    "inpaint": "fal-ai/nano-banana-2/edit",
    "expand": "fal-ai/nano-banana-2/edit",
    "upscale": "fal-ai/esrgan",
    "variations": "fal-ai/nano-banana-2/edit",
    "remove-bg": "fal-ai/birefnet/v2",
    "sketch-to-image": "bytedance/seedream/v5/pro/edit",
}

# The full set of endpoints we will route to (the override allow-list).
EDIT_MODEL_IDS: frozenset[str] = frozenset(DEFAULT_EDIT_MODELS.values())

# Human labels for the default endpoints (a future /studio edit-model picker can
# render these without hitting fal's catalog).
EDIT_MODEL_LABELS: dict[str, str] = {
    "fal-ai/nano-banana-2/edit": "Nano Banana 2 Edit",
    "fal-ai/birefnet/v2": "BiRefNet Background Removal V2",
    "fal-ai/esrgan": "Upscale (Real-ESRGAN)",
    "bytedance/seedream/v5/pro/edit": "Seedream 5.0 Pro Edit",
}

# Default prompts for the ops that don't carry a user instruction.
_DEFAULT_PROMPTS: dict[str, str] = {
    "variations": (
        "Create a creative variation of this image, keeping the subject, "
        "composition, and style recognizable while reimagining the details."
    ),
    "inpaint": (
        "Regenerate the masked region so it blends seamlessly with the "
        "surrounding image, matching its lighting, color, and style."
    ),
    "sketch-to-image": "Turn this sketch into a polished, highly detailed image.",
}

_DIRECTION_TEXT: dict[str, str] = {
    "all": "outward on all sides",
    "up": "upward",
    "down": "downward",
    "left": "to the left",
    "right": "to the right",
}

# Client-side + server-side deadlines for the fal call. Image edits run a few
# seconds to ~a minute on queue-backed models; these are generous but bounded so
# a hung upstream fails fast instead of pinning a worker forever.
_CLIENT_TIMEOUT = 180.0
_START_TIMEOUT = 120.0
_DOWNLOAD_TIMEOUT = 120.0


class FalEditError(Exception):
    """A fal.ai edit call failed (uninstalled SDK, upstream error, malformed
    result, or no image data). The studio service maps this to a 502."""


# ── Config ───────────────────────────────────────────────────────────────────


def fal_api_key() -> str | None:
    """Resolve the fal.ai API key. Prefers ``FAL_AI_API_KEY`` (pocketpaw's env
    name); falls back to ``FAL_KEY`` (the name the fal-client SDK reads on
    import) for dev parity. Returns None when neither is set.

    The ``serve`` process never merges ``.env`` into ``os.environ`` (pydantic
    Settings reads it into the model only, and the ``POCKETPAW_`` prefix excludes
    this key), so we load it explicitly. ``load_dotenv`` will NOT override a key
    that is already exported in the shell, so a real export always wins.
    """
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:  # pragma: no cover - uvicorn[standard] provides it
        pass
    return (os.environ.get("FAL_AI_API_KEY") or os.environ.get("FAL_KEY") or "").strip() or None


# ── Input/encode helpers ─────────────────────────────────────────────────────


def encode_bytes(data: bytes, mime: str = "image/png") -> str:
    """Encode bytes to a ``data:`` URL fal edit models accept as ``image_url``."""
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


def mime_to_ext(mime: str) -> str:
    """Storage extension for a fal result content-type (defaults to png)."""
    mime = (mime or "").split(";")[0].strip().lower()
    return {
        "image/png": "png",
        "image/jpeg": "jpg",
        "image/jpg": "jpg",
        "image/webp": "webp",
        "image/gif": "gif",
    }.get(mime, "png")


# ── Argument building ────────────────────────────────────────────────────────


def _expand_prompt(direction: str | None, factor: float | None) -> str:
    where = _DIRECTION_TEXT.get((direction or "all").strip().lower(), _DIRECTION_TEXT["all"])
    pct = int(round((factor or 1.5) * 100))
    return (
        f"Outpaint / extend the image {where}, seamlessly continuing the scene, "
        f"colors, lighting, and style beyond the original frame by about {pct}%."
    )


def build_arguments(
    *,
    op: str,
    image_data_url: str,
    mask_data_url: str | None = None,
    prompt: str | None = None,
    direction: str | None = None,
    factor: float | None = None,
) -> dict[str, Any]:
    """Build the fal ``arguments`` dict for an edit op.

    Pure + side-effect free so it is unit-testable in isolation. Raises
    ValueError for the ops that need a user prompt when none is supplied (the
    router maps that to a 400).
    """
    if op == "remove-bg":
        return {"image_url": image_data_url}
    if op == "upscale":
        scale = int(factor or 2)
        return {"image_url": image_data_url, "upscale_factor": scale if scale in (2, 4) else 2}
    if op == "expand":
        return {"prompt": _expand_prompt(direction, factor), "image_url": image_data_url}
    if op == "variations":
        return {"prompt": _DEFAULT_PROMPTS["variations"], "image_url": image_data_url}

    # Prompt-driven ops: edit / inpaint / sketch-to-image.
    text = (prompt or "").strip()
    if op == "inpaint" and not text:
        text = _DEFAULT_PROMPTS["inpaint"]
    elif op == "sketch-to-image" and text:
        text = f"{_DEFAULT_PROMPTS['sketch-to-image']} {text}".strip()
    elif op == "sketch-to-image":
        text = _DEFAULT_PROMPTS["sketch-to-image"]
    if not text:
        raise ValueError(f"prompt is required for edit op '{op}'")
    args: dict[str, Any] = {"prompt": text, "image_url": image_data_url}
    if op == "inpaint" and mask_data_url:
        args["mask_url"] = mask_data_url
    return args


# ── Result handling ──────────────────────────────────────────────────────────


def _extract_image_urls(result: dict[str, Any]) -> list[str]:
    """Pull every output image URL from a fal model result.

    Handles both the ``images: [{url, …}]`` shape (nano-banana / seedream /
    flux) and the ``image: {url, …}`` single-asset shape (birefnet / esrgan /
    rembg), so one extractor serves the whole op table.
    """
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
    """Download one fal-hosted result image and return ``(bytes, content_type)``.

    fal media URLs are publicly accessible (no auth header needed) but expire per
    the account's media-expiration setting, so the service persists the bytes
    into media storage before the URL goes stale.
    """
    async with httpx.AsyncClient(timeout=_DOWNLOAD_TIMEOUT) as client:
        resp = await client.get(url)
        resp.raise_for_status()
    mime = resp.headers.get("content-type", "image/png").split(";")[0].strip() or "image/png"
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
        raise FalEditError(
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
        logger.warning("studio: fal edit '%s' failed", endpoint, exc_info=True)
        raise FalEditError(f"fal edit '{endpoint}' failed: {exc}") from exc
    if not isinstance(result, dict):
        raise FalEditError(f"fal edit '{endpoint}' returned an unexpected result")
    return result


# ── Public entry point ───────────────────────────────────────────────────────


async def run_fal_edit(
    *,
    op: str,
    image_data_url: str,
    mask_data_url: str | None = None,
    prompt: str | None = None,
    direction: str | None = None,
    factor: float | None = None,
    model: str | None = None,
    key: str | None = None,
) -> list[tuple[bytes, str]]:
    """Run one /studio edit op against fal and return the result image bytes.

    ``image_data_url`` is the source asset as a ``data:`` URL (the service
    resolves the frontend's ``sourceUrl`` into one). Returns ``[(bytes, mime)]``
    for every output image; raises ValueError (missing prompt / bad model) and
    FalEditError (upstream / no-output).
    """
    op = (op or "").strip()
    if op not in SUPPORTED_OPS:
        raise ValueError(
            f"unknown edit op '{op}' — expected one of {', '.join(sorted(SUPPORTED_OPS))}"
        )
    endpoint = (model or "").strip() or DEFAULT_EDIT_MODELS[op]
    if endpoint not in EDIT_MODEL_IDS:
        raise ValueError(f"unknown edit model '{endpoint}'")
    if not image_data_url:
        raise ValueError("image_data_url is required")

    arguments = build_arguments(
        op=op,
        image_data_url=image_data_url,
        mask_data_url=mask_data_url,
        prompt=prompt,
        direction=direction,
        factor=factor,
    )

    api_key = key if key is not None else fal_api_key()
    if not api_key:
        raise FalEditError("fal.ai API key is not configured (set FAL_AI_API_KEY)")

    result = await _run_fal(endpoint, arguments, key=api_key)
    urls = _extract_image_urls(result)
    if not urls:
        raise FalEditError(f"fal edit '{op}' returned no image data")
    outputs: list[tuple[bytes, str]] = []
    for url in urls:
        outputs.append(await _download(url))
    return outputs


__all__ = [
    "SUPPORTED_OPS",
    "DEFAULT_EDIT_MODELS",
    "EDIT_MODEL_IDS",
    "EDIT_MODEL_LABELS",
    "FalEditError",
    "fal_api_key",
    "encode_bytes",
    "mime_to_ext",
    "build_arguments",
    "run_fal_edit",
]
