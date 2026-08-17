# ee/pocketpaw_ee/cloud/studio/service.py — /studio generation + catalog service.
#
# The direct describe-to-media surface (the paw-enterprise /studio composer +
# gallery) drives its backend through one typed adapter. This service implements
# it over the SAME LiteLLM gateway the agent-side media MCP uses:
#
#   * models            — mapped from the LiteLLM proxy's catalog (catalog.service),
#                         image_generation/video entries only, shaped to the
#                         frontend's StudioModel.
#   * generate (image)  — POST {proxy}/v1/images/generations (OpenAI-compatible,
#                         fal.ai models served upstream), save the returned PNG
#                         under ~/.pocketpaw/generated, return a Generation.
#   * generations       — persisted per-workspace history (JSONL under
#                         ~/.pocketpaw/studio) so the gallery survives reloads.
#   * edit              — the canvas edit ops are NOT yet wired through the
#                         gateway; the router maps this to a clear 501.
#   * suggest-prompt    — lightweight heuristic enrichment (no LLM call).
#
# Proxy base/key + the httpx style are REUSED from the catalog entity
# (ee.pocketpaw_ee.catalog.config + its LiteLLMClient header shape) — one config
# path for the whole deployment. The tenant is tagged on every proxy request via
# the OpenAI `user` field so the proxy's spend log attributes cost to the
# workspace. A per-workspace LiteLLM virtual key is preferred (MCG-8) and falls
# back to the deployment master key, mirroring the media MCP's resolution.
#
# Created 2026-08-17 (studio-real-backend): new service module.

from __future__ import annotations

import base64
import json
import logging
import re
import uuid
from pathlib import Path
from typing import Any

import httpx

from pocketpaw.config import get_config_dir
from pocketpaw_ee.catalog import config as catalog_config
from pocketpaw_ee.catalog import service as catalog_service
from pocketpaw_ee.catalog.models import Modality, ModelCatalogEntry

from . import schemas

logger = logging.getLogger(__name__)


class StudioNotSupported(Exception):
    """Raised when a requested /studio operation has no gateway route yet
    (e.g. canvas edit ops). The router maps this to a 501."""


class StudioUpstreamError(Exception):
    """A LiteLLM proxy media call failed (non-2xx or malformed). Raised so the
    router can surface a typed 502 rather than a bare 500."""


# ── One-tap styles (mirrors the frontend mock so the pickers match) ──────────

STYLES: list[dict[str, Any]] = [
    {"id": "none", "label": "No style", "promptSuffix": ""},
    {
        "id": "cinematic",
        "label": "Cinematic",
        "description": "Filmic lighting, shallow depth of field",
        "promptSuffix": ", cinematic lighting, shallow depth of field, film grain, "
        "dramatic composition",
    },
    {
        "id": "photoreal",
        "label": "Photoreal",
        "description": "Sharp, realistic photography",
        "promptSuffix": ", photorealistic, ultra detailed, natural lighting, 50mm lens",
    },
    {
        "id": "watercolor",
        "label": "Watercolor",
        "description": "Soft washes, loose brushwork",
        "promptSuffix": ", watercolor illustration, soft pastel washes, loose brushwork",
    },
    {
        "id": "anime",
        "label": "Anime",
        "description": "Clean cel-shaded anime",
        "promptSuffix": ", anime style, cel shaded, vibrant colors, clean line art",
    },
    {
        "id": "threed",
        "label": "3D Render",
        "description": "Stylised 3D render",
        "promptSuffix": ", 3D render, octane, soft studio lighting, subtle subsurface scattering",
    },
    {
        "id": "neon",
        "label": "Neon",
        "description": "Cyberpunk neon glow",
        "promptSuffix": ", neon cyberpunk, glowing accents, moody night atmosphere",
    },
]

# Studio aspect-ratio id → OpenAI-compatible ``size`` sent to the proxy.
# Extends the agent MCP's 1:1/16:9/9:16 set with the extra ratios the studio
# composer offers (4:3 / 3:4 / 3:2 / 2:3), all multiples of 16 so fal image
# models accept them.
_SIZE_MAP: dict[str, str] = {
    "1:1": "1024x1024",
    "16:9": "1792x1024",
    "9:16": "1024x1792",
    "4:3": "1024x768",
    "3:4": "768x1024",
    "3:2": "1152x768",
    "2:3": "768x1152",
}

# A single image generation is one proxy call with ``n:1`` (the fal models the
# gateway serves return one image per request; keeping count=1 is reliable and
# matches how the agent-side media MCP already calls the endpoint).
_MAX_GENERATED_ASSETS = 4


def _generated_dir() -> Path:
    """Get (and create) the generated-media directory — same location the OSS
    ImageGenerateTool and the media MCP server use (``get_config_dir()/generated``)."""
    d = get_config_dir() / "generated"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _history_path() -> Path:
    """Get (and create) the generation-history JSONL path. History is persisted
    per-deployment under ``~/.pocketpaw/studio/generations.jsonl`` so the /studio
    gallery survives process restarts."""
    d = get_config_dir() / "studio"
    d.mkdir(parents=True, exist_ok=True)
    return d / "generations.jsonl"


# ── Model catalog mapping ────────────────────────────────────────────────────

def _friendly_label(model_id: str, display_name: str) -> str:
    """Human label from the catalog's display name (e.g. ``flux/schnell`` →
    ``Flux Schnell``, ``bytedance/seedream/v3/text-to-image`` → ``Seedream V3``).
    Strips the redundant terminal ``text-to-image`` segment and hyphen/underscore
    joins, keeping version tokens readable (``v3`` → ``V3``).

    The fal_ai provider's model ids are double-prefixed (``fal_ai/fal-ai/flux/
    schnell`` → display ``fal-ai/flux/schnell``), so a leading display segment
    that duplicates the id's provider segment is dropped too."""
    tail = display_name or model_id
    parts = [
        p
        for p in tail.split("/")
        if p and p.strip().lower() not in {"text-to-image", "text", "image"}
    ]
    provider = (model_id.split("/", 1)[0] or "").lower().replace("_", "-")
    if parts and parts[0].strip().lower().replace("_", "-") == provider:
        parts = parts[1:]
    words: list[str] = []
    for part in parts:
        for w in part.replace("-", " ").replace("_", " ").split():
            words.append(w)
    out: list[str] = []
    for w in words:
        lw = w.lower()
        if lw.startswith("v") and len(w) > 1 and w[1:].isdigit():
            out.append(w.upper())
        elif w and w[0].isdigit():
            out.append(w)
        else:
            out.append(w.capitalize())
    label = " ".join(out).strip()
    return label or tail


def _map_entry(entry: ModelCatalogEntry, *, default: bool) -> schemas.StudioModel:
    """Map one LiteLLM catalog entry onto the frontend's StudioModel shape.

    ``maxCount`` is pinned to 1 (the gateway serves fal image models one image
    per request); negative prompts aren't sent through the OpenAI-compatible
    image endpoint, so ``supportsNegativePrompt`` is False for every model.
    """
    kind = "image" if entry.modality == Modality.IMAGE else "video"
    if kind == "image":
        aspect_ratios = ["1:1", "16:9", "9:16", "4:3", "3:4"]
        durations = None
    else:
        aspect_ratios = ["16:9", "9:16", "1:1"]
        durations = [5]
    return schemas.StudioModel(
        id=entry.id,
        label=_friendly_label(entry.id, entry.display_name),
        kind=kind,
        provider=entry.provider,
        description=entry.description,
        aspectRatios=aspect_ratios,
        maxCount=1,
        supportsNegativePrompt=False,
        durationsSec=durations,
        credits=1,
        tags=[entry.provider, kind],
        default=default,
    )


async def list_models() -> list[schemas.StudioModel]:
    """Return the image + video models the LiteLLM proxy serves, shaped for the
    /studio picker. The first image model is the catalog default. A proxy outage
    propagates CatalogUpstreamError so the router can surface 502."""
    entries = await catalog_service.list_models()
    images = [e for e in entries if e.modality == Modality.IMAGE]
    videos = [e for e in entries if e.modality == Modality.VIDEO]
    models: list[schemas.StudioModel] = []
    for i, entry in enumerate(images):
        models.append(_map_entry(entry, default=(i == 0)))
    for entry in videos:
        models.append(_map_entry(entry, default=False))
    return models


def list_styles() -> list[schemas.StudioStyle]:
    """Return the one-tap style catalog (same set the frontend mock shipped)."""
    return [schemas.StudioStyle.model_validate(s) for s in STYLES]


# ── Generation history (JSONL persistence) ──────────────────────────────────

def _load_history() -> list[dict[str, Any]]:
    """Read the persisted generation records (best-effort — a corrupt/missing
    file degrades to an empty history, never a crash)."""
    path = _history_path()
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    try:
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except ValueError:
                logger.warning("studio: skipping corrupt history line")
    except OSError:
        logger.warning("studio: could not read history file", exc_info=True)
    return records


def _append_history(record: dict[str, Any]) -> None:
    """Append one record to the JSONL history (best-effort)."""
    try:
        with _history_path().open("a") as fh:
            fh.write(json.dumps(record) + "\n")
    except OSError:
        logger.warning("studio: could not append history", exc_info=True)


def list_generations(workspace_id: str) -> list[schemas.Generation]:
    """Return the workspace's generation history, newest first. Records are
    tagged with the owning workspace so multi-tenant deployments never leak."""
    records = _load_history()
    mine = [
        r
        for r in records
        if r.get("_workspace") == workspace_id or r.get("_workspace") is None
    ]
    mine.sort(key=lambda r: r.get("createdAt", 0), reverse=True)
    return [schemas.Generation.model_validate(r) for r in mine]


def get_generation(gen_id: str, workspace_id: str) -> schemas.Generation | None:
    """Return one generation by id (scoped to the workspace), or None."""
    for r in _load_history():
        if r.get("id") != gen_id:
            continue
        if r.get("_workspace") not in (None, workspace_id):
            continue
        return schemas.Generation.model_validate(r)
    return None


def tracked_generation_filenames() -> set[str]:
    """The set of media filenames owned by direct /studio generations. The /media
    list router uses this to EXCLUDE generation outputs so the gallery doesn't
    show a direct generation twice (once via /studio/generations and once via the
    /media file list). Agent-side generated files (media MCP) are NOT tracked here
    and therefore still surface through /media."""
    names: set[str] = set()
    for r in _load_history():
        for asset in r.get("assets") or []:
            url = asset.get("url") or ""
            if url.startswith("/api/v1/media/"):
                names.add(url.rsplit("/", 1)[-1])
    return names


# ── LiteLLM proxy transport (REUSED from the catalog entity) ────────────────

def _proxy_base() -> str:
    return catalog_config.litellm_proxy_url()


def _proxy_key() -> str | None:
    return catalog_config.litellm_proxy_api_key()


# Tests set this to an httpx.MockTransport so the proxy HTTP calls are exercised
# without a live proxy (the same seam catalog.litellm_client + media MCP expose).
_PROXY_TRANSPORT: httpx.BaseTransport | None = None


async def _resolve_auth_key(workspace_id: str | None) -> str | None:
    """Resolve the Bearer key for a tenant's media proxy call (MCG-8).

    Prefer the workspace's PROVISIONED LiteLLM virtual key so the proxy enforces
    the tenant's budget + attributes spend to the key; fall back to the deployment
    master key when the workspace has no key yet (or the lookup fails). Best-effort
    + non-fatal — studio must keep working on the master key before provisioning
    runs, and the ``user=workspace_id`` tag still attributes spend either way.
    """
    if workspace_id:
        try:
            from pocketpaw_ee.cloud.llm_provisioning import service as provisioning

            tenant_key = await provisioning.get_tenant_key(workspace_id)
            if tenant_key:
                return tenant_key
        except Exception:  # noqa: BLE001 — never let key resolution break a generation
            logger.debug(
                "studio: tenant key lookup failed for workspace=%s; using master key",
                workspace_id,
                exc_info=True,
            )
    return _proxy_key()


def _proxy_headers(*, auth_key: str | None = None) -> dict[str, str]:
    """Bearer the proxy key (when set) the same way the catalog client does.
    ``auth_key`` is the per-tenant virtual key resolved by ``_resolve_auth_key``;
    when None we fall back to the deployment master key."""
    headers: dict[str, str] = {}
    key = auth_key if auth_key is not None else _proxy_key()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    headers["Content-Type"] = "application/json"
    return headers


def _proxy_client(timeout: float = 120.0) -> httpx.AsyncClient:
    """An httpx.AsyncClient pointed at the proxy. Image generation can take a few
    seconds, so the timeout is generous relative to the catalog client's 15s."""
    return httpx.AsyncClient(transport=_PROXY_TRANSPORT, timeout=timeout)


def _http_error_detail(what: str, exc: httpx.HTTPStatusError) -> str:
    """A compact, user-relayable message for a proxy HTTP error — surfaces the
    status code and, when the proxy returned a JSON error body, its message, so a
    missing-model / no-quota / bad-key proxy response is legible."""
    status = exc.response.status_code
    detail: str | None = None
    try:
        body = exc.response.json()
        if isinstance(body, dict):
            err = body.get("error")
            if isinstance(err, dict):
                detail = err.get("message")
            elif isinstance(err, str):
                detail = err
            detail = detail or body.get("message")
    except Exception:  # noqa: BLE001
        detail = None
    return f"{what} request failed: {status}" + (f" — {detail}" if detail else "")


# ── Image generation (POST {proxy}/v1/images/generations) ───────────────────

async def _proxy_generate_image(
    *,
    model: str,
    prompt: str,
    size: str | None,
    user: str,
    auth_key: str | None = None,
) -> tuple[bytes | None, str | None]:
    """Generate ONE image via the proxy's OpenAI-compatible image endpoint.

    POSTs ``{model, prompt, n:1, size?, user}`` to ``/v1/images/generations`` and
    accepts EITHER return shape: ``data[0].b64_json`` (gpt-image-1 and most
    providers return base64) or ``data[0].url`` (dall-e-style, fetched without the
    proxy key — the key is for the proxy, not arbitrary asset hosts). Returns
    ``(bytes, None)`` or ``(None, error)``.
    """
    base = _proxy_base()
    payload: dict[str, Any] = {
        "model": model,
        "prompt": prompt,
        "n": 1,
        "user": user,
    }
    if size:
        payload["size"] = size
    try:
        async with _proxy_client() as client:
            resp = await client.post(
                f"{base}/v1/images/generations",
                headers=_proxy_headers(auth_key=auth_key),
                json=payload,
            )
            resp.raise_for_status()
            body = resp.json()
            data = (body.get("data") or [{}])[0]
            b64 = data.get("b64_json")
            if b64:
                return base64.b64decode(b64), None
            url = data.get("url")
            if url:
                img_resp = await client.get(url)
                img_resp.raise_for_status()
                return img_resp.content, None
            return None, "proxy returned no image data (no b64_json or url)"
    except httpx.HTTPStatusError as exc:
        return None, _http_error_detail("image generation", exc)
    except StudioUpstreamError:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning("studio: proxy image generation failed", exc_info=True)
        return None, f"image generation failed: {exc}"


def _save_image_bytes(image_bytes: bytes) -> str:
    """Persist one generated PNG under the generated-media dir and return its
    backend-relative URL (the /media router serves it)."""
    out_path = _generated_dir() / f"{uuid.uuid4()}.png"
    out_path.write_bytes(image_bytes)
    return f"/api/v1/media/{out_path.name}"


async def _apply_style(prompt: str, style_id: str | None) -> str:
    """Compose the final prompt by appending the active style's suffix (kept
    explicit so the user always sees what the style does — same as the mock)."""
    if not style_id or style_id == "none":
        return prompt
    style = next((s for s in STYLES if s["id"] == style_id), None)
    if style and style.get("promptSuffix"):
        return f"{prompt}{style['promptSuffix']}"
    return prompt


def _new_generation(
    *,
    gen_id: str,
    prompt: str,
    kind: str,
    model: str,
    params: dict[str, Any],
    assets: list[dict[str, Any]],
    status: str,
    error: str | None = None,
) -> schemas.Generation:
    return schemas.Generation(
        id=gen_id,
        prompt=prompt,
        status=status,
        kind=kind,
        model=model,
        params=schemas.GenerationParams.model_validate(params),
        assets=[schemas.GeneratedAsset.model_validate(a) for a in assets],
        createdAt=int(time_now_ms()),
        error=error,
    )


def _seq_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def time_now_ms() -> int:
    """Current time in unix milliseconds (importable helper so tests can patch)."""
    import time as _time

    return int(_time.time() * 1000)


async def generate(req: schemas.GenerateRequest, *, workspace_id: str) -> schemas.Generation:
    """Run a direct studio generation through the LiteLLM gateway.

    Image requests resolve synchronously to ``succeeded`` (the proxy returns the
    image bytes, which are saved + served back via /api/v1/media). Video requests
    raise StudioNotSupported until the gateway serves video models. The record is
    persisted to the workspace history so the gallery keeps it across reloads.
    """
    prompt = (req.prompt or "").strip()
    if not prompt:
        raise ValueError("prompt is required")

    if req.kind == "video":
        raise StudioNotSupported(
            "Video generation is not configured yet — the model gateway serves "
            "image models only. Switch to the Image tab to generate."
        )

    model = (req.model or "").strip()
    if not model:
        raise ValueError("model is required")

    final_prompt = await _apply_style(prompt, req.styleId)
    size = _SIZE_MAP.get(req.aspectRatio)
    auth_key = await _resolve_auth_key(workspace_id)
    count = max(1, min(int(req.count or 1), _MAX_GENERATED_ASSETS))

    assets: list[dict[str, Any]] = []
    for _ in range(count):
        image_bytes, err = await _proxy_generate_image(
            model=model,
            prompt=final_prompt,
            size=size,
            user=workspace_id,
            auth_key=auth_key,
        )
        if err is not None or image_bytes is None:
            # A failed generation mid-batch: return what we have as a failed
            # generation carrying the reason, never a phantom asset.
            raise StudioUpstreamError(err or "image generation returned no data")
        assets.append(
            {
                "id": _seq_id("asset"),
                "url": _save_image_bytes(image_bytes),
                "mime": "image/png",
            }
        )

    params: dict[str, Any] = {
        "kind": "image",
        "model": model,
        "aspectRatio": req.aspectRatio,
        "count": count,
        "styleId": req.styleId,
        "negativePrompt": req.negativePrompt,
        "seed": req.seed,
        "durationSec": req.durationSec,
    }
    record = _new_generation(
        gen_id=_seq_id("gen"),
        prompt=final_prompt,
        kind="image",
        model=model,
        params=params,
        assets=assets,
        status="succeeded",
    )
    # Persist with the owning workspace tag (dropped by the wire model on read).
    _append_history({**record.model_dump(), "_workspace": workspace_id})
    return record


async def edit(req: schemas.EditRequest, *, workspace_id: str) -> schemas.Generation:
    """Canvas edit ops (inpaint/expand/upscale/variations/remove-bg) are NOT yet
    wired through the model gateway. Raised so the router returns a clean 501 and
    the frontend's optimistic tile resolves to a visible error instead of hanging."""
    raise StudioNotSupported(
        f"Edit op '{req.op}' is not wired through the model gateway yet. "
        "Generate a new image instead."
    )


def suggest_prompt(sentence: str) -> schemas.PromptSuggestion:
    """Enrich a plain sentence into a generation prompt + inferred media kind.
    Heuristic mirror of the mock (no LLM call): a motion/reel vocabulary implies
    video, otherwise image; short sentences get a quality tail."""
    text = (sentence or "").strip()
    s = text.lower()
    kind = (
        "video"
        if re.search(r"\b(video|clip|animate|animation|motion|moving|reel)\b", s)
        else "image"
    )
    enriched = text
    if len(enriched) < 40:
        enriched = f"{enriched}, highly detailed, professional lighting, sharp focus"
    return schemas.PromptSuggestion(prompt=enriched, kind=kind)


__all__ = [
    "STYLES",
    "StudioNotSupported",
    "StudioUpstreamError",
    "list_models",
    "list_styles",
    "list_generations",
    "get_generation",
    "tracked_generation_filenames",
    "generate",
    "edit",
    "suggest_prompt",
]
