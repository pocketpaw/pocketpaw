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
#                         fal.ai models served upstream), persist the returned
#                         PNG through the media storage adapter (local disk or
#                         S3), return a Generation.
#   * generate (video)  — run DIRECTLY against fal.ai via cloud.studio.fal_video
#                         (the gateway serves image models for the direct
#                         surface; the fal SDK covers video like the edit ops),
#                         persist the video (+ optional poster) through media
#                         storage, return a Generation.
#   * generations       — persisted per-workspace history (JSONL under
#                         ~/.pocketpaw/studio) so the gallery survives reloads.
#   * edit              — canvas edit ops (inpaint/expand/upscale/variations/
#                         remove-bg/edit/sketch-to-image) run DIRECTLY against
#                         fal.ai via cloud.studio.fal_edit — the LiteLLM gateway
#                         serves generation models only and has no route for
#                         fal's image-edit endpoints. Results persist through
#                         media storage like generations.
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
import io
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
from pocketpaw_ee.cloud.media import storage as media_storage

from . import fal_edit, fal_elements, fal_motion, fal_video, schemas

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
        # The rail's duration picker (2s / 5s / 10s) renders exactly these;
        # fal_video passes the chosen duration through and the endpoint's own
        # validation reports a duration the selected model can't produce.
        durations = list(fal_video.SUPPORTED_DURATIONS)
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


def _fallback_video_model() -> schemas.StudioModel:
    """The pickable fal video model to surface when the proxy catalog serves no
    ``Modality.VIDEO`` entries. Video generation runs DIRECTLY against fal.ai
    (never through the proxy), so the rail must still offer a video model —
    otherwise clicking Video in the context sidebar keeps the stale image model
    selected and the 2s / 5s / 10s duration picker never renders."""
    return schemas.StudioModel(
        id=fal_video.DEFAULT_VIDEO_MODEL,
        label="Kling Video 1.0",
        kind="video",
        provider="fal",
        description=(
            "Text-to-video via fal.ai (Kling v1 standard). 2s / 5s / 10s clips — "
            "runs directly against fal, no proxy dependency."
        ),
        aspectRatios=["16:9", "9:16", "1:1"],
        maxCount=1,
        supportsNegativePrompt=False,
        durationsSec=list(fal_video.SUPPORTED_DURATIONS),
        credits=1,
        tags=["fal", "video"],
        default=True,
    )


async def list_models() -> list[schemas.StudioModel]:
    """Return the image + video models the LiteLLM proxy serves, shaped for the
    /studio picker. The first image model is the catalog default. A proxy outage
    propagates CatalogUpstreamError so the router can surface 502.

    When the catalog serves no video entries, a fallback fal video model is
    appended so the rail's Video kind always has a picker row with the
    2s / 5s / 10s duration set (see _fallback_video_model)."""
    entries = await catalog_service.list_models()
    images = [e for e in entries if e.modality == Modality.IMAGE]
    videos = [e for e in entries if e.modality == Modality.VIDEO]
    models: list[schemas.StudioModel] = []
    for i, entry in enumerate(images):
        models.append(_map_entry(entry, default=(i == 0)))
    for entry in videos:
        models.append(_map_entry(entry, default=False))
    if not videos:
        models.append(_fallback_video_model())
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
        r for r in records if r.get("_workspace") == workspace_id or r.get("_workspace") is None
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


# ── Flow projects (JSONL persistence, workspace-scoped) ─────────────────────
# Same file-based pattern as the generation history, but update-in-place: flow
# projects are mutable (save = upsert, delete = remove), so the whole set is
# rewritten on each write. The set is tiny (a handful of canvases per
# workspace), so a full rewrite is fine and keeps the file trivially debuggable.


def _projects_path() -> Path:
    """Get (and create) the flow-projects JSONL path under ``~/.pocketpaw/studio``
    so flow canvases survive restarts AND round-trip across devices (the /studio
    page pushes saves here via PUT /studio/flow-projects/{id})."""
    d = get_config_dir() / "studio"
    d.mkdir(parents=True, exist_ok=True)
    return d / "flow-projects.jsonl"


def _load_projects() -> list[dict[str, Any]]:
    """Read the persisted flow-project records (best-effort — a corrupt/missing
    file degrades to an empty list, never a crash)."""
    path = _projects_path()
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
                logger.warning("studio: skipping corrupt flow-project line")
    except OSError:
        logger.warning("studio: could not read flow projects", exc_info=True)
    return records


def _rewrite_projects(records: list[dict[str, Any]]) -> None:
    """Full rewrite of the flow-projects JSONL (best-effort)."""
    try:
        with _projects_path().open("w") as fh:
            for record in records:
                fh.write(json.dumps(record) + "\n")
    except OSError:
        logger.warning("studio: could not rewrite flow projects", exc_info=True)


def list_flow_projects(workspace_id: str) -> list[schemas.FlowProject]:
    """Return the workspace's flow projects, most-recently-updated first. Records
    are tagged with the owning workspace so multi-tenant deployments never leak."""
    mine = [r for r in _load_projects() if r.get("_workspace") == workspace_id]
    mine.sort(key=lambda r: r.get("updatedAt", 0), reverse=True)
    return [schemas.FlowProject.model_validate(r) for r in mine]


def get_flow_project(project_id: str, workspace_id: str) -> schemas.FlowProject | None:
    """Return one flow project (scoped to the workspace), or None."""
    for r in _load_projects():
        if r.get("id") == project_id and r.get("_workspace") == workspace_id:
            return schemas.FlowProject.model_validate(r)
    return None


def save_flow_project(
    project_id: str,
    workspace_id: str,
    *,
    name: str | None,
    nodes: list[schemas.FlowNode],
    edges: list[schemas.FlowEdge],
) -> schemas.FlowProject:
    """Create-or-update a flow project (UPSERT). ``name`` falls back to the
    existing name (or 'Flow') when omitted so a canvas-only save never wipes the
    title. Returns the saved project."""
    records = _load_projects()
    now = time_now_ms()
    prior = next(
        (r for r in records if r.get("id") == project_id and r.get("_workspace") == workspace_id),
        None,
    )
    record = {
        "id": project_id,
        "name": (name or (prior or {}).get("name") or "Flow").strip() or "Flow",
        "createdAt": (prior or {}).get("createdAt", now),
        "updatedAt": now,
        "nodes": [n.model_dump() for n in nodes],
        "edges": [e.model_dump() for e in edges],
        "_workspace": workspace_id,
    }
    remaining = [
        r
        for r in records
        if not (r.get("id") == project_id and r.get("_workspace") == workspace_id)
    ]
    remaining.append(record)
    _rewrite_projects(remaining)
    return schemas.FlowProject.model_validate(record)


def delete_flow_project(project_id: str, workspace_id: str) -> bool:
    """Delete one flow project (scoped to the workspace). Returns False if the
    project didn't exist (the router maps that to a 404)."""
    records = _load_projects()
    remaining = [
        r
        for r in records
        if not (r.get("id") == project_id and r.get("_workspace") == workspace_id)
    ]
    if len(remaining) == len(records):
        return False
    _rewrite_projects(remaining)
    return True


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


async def _save_image_bytes(
    image_bytes: bytes, *, mime: str = "image/png", ext: str = "png"
) -> str:
    """Persist one generated image through the media storage adapter and return
    its backend-relative URL (the /media router serves it). Shared with the
    agent-side media MCP so every generated asset lands on the same storage."""
    return await media_storage.save_generated(image_bytes, mime=mime, ext=ext)


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
    """Run a direct studio generation.

    Image requests resolve synchronously to ``succeeded`` (the proxy returns the
    image bytes, which are saved + served back via /api/v1/media). Video requests
    run DIRECTLY against fal.ai via ``fal_video`` (the gateway serves image
    models for the direct surface; the fal SDK covers video, like the edit ops).
    ``inputImageUrls`` (the flow's Video node wiring Image/Picture results in)
    switches video to image-to-video — every image goes to the fal model in one
    call. The record is persisted to the workspace history so the gallery keeps
    it across reloads.
    """
    if req.kind == "video":
        return await _generate_video(req, workspace_id=workspace_id)

    prompt = (req.prompt or "").strip()
    if not prompt:
        raise ValueError("prompt is required")

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
                "url": await _save_image_bytes(image_bytes),
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


# ── Video generation (direct fal.ai dispatch) ────────────────────────────────
# The gateway serves image models for the direct /studio surface; video runs on
# the official fal-client SDK (like the canvas edit ops) so /studio video works
# without waiting on a gateway route. The result video (+ optional poster frame)
# persists through the same media storage as every other generated asset.


def _video_ext(mime: str) -> str:
    """Storage extension for a fal result content-type (defaults to mp4)."""
    mime = (mime or "").split(";")[0].strip().lower()
    return {
        "video/mp4": "mp4",
        "video/webm": "webm",
        "video/quicktime": "mov",
        "video/x-m4v": "m4v",
    }.get(mime, "mp4")


def _video_poster_ext(mime: str) -> str:
    """Storage extension for a fal result poster (defaults to jpg)."""
    mime = (mime or "").split(";")[0].strip().lower()
    return {
        "image/png": "png",
        "image/webp": "webp",
        "image/jpeg": "jpg",
    }.get(mime, "jpg")


async def _generate_video(req: schemas.GenerateRequest, *, workspace_id: str) -> schemas.Generation:
    """Generate a video DIRECTLY against fal.ai via ``fal_video``.

    ``inputImageUrls`` (the flow's Video node wiring Image/Picture results in)
    switches to image-to-video: each URL is resolved to a ``data:`` URL (media
    path / http / data pass through — see ``_resolve_source_data_url``) and all
    of them go to the fal model in ONE call. ``fal_video`` owns the endpoint +
    argument building for 1 / 2 / 3+ images; a text-to-video run (no images)
    still requires a prompt. The requested model id is resolved onto a real fal
    endpoint; the returned video bytes are saved through media storage (with a
    poster frame when the endpoint produced one) and the record lands in the
    workspace history like every other generation. A fal upstream failure
    surfaces as StudioUpstreamError (router → 502); bad input is ValueError
    (router → 400).
    """
    model = (req.model or "").strip()
    if not model:
        raise ValueError("model is required")

    prompt = (req.prompt or "").strip()
    image_urls = [u for u in (req.inputImageUrls or []) if u and u.strip()]
    if not image_urls and not prompt:
        raise ValueError("prompt is required for text-to-video")

    # Image-to-video runs without a typed prompt — fal_video applies its own
    # default motion prompt, so a user can wire images and hit Generate directly.
    effective_prompt = prompt or fal_video.DEFAULT_I2V_PROMPT
    final_prompt = await _apply_style(effective_prompt, req.styleId)

    input_data_urls: list[str] | None = None
    if image_urls:
        input_data_urls = []
        for url in image_urls:
            data_url, _ = await _resolve_source_data_url(url)
            input_data_urls.append(data_url)

    try:
        video_bytes, video_mime, poster_bytes, poster_mime = await fal_video.run_fal_video(
            prompt=final_prompt,
            duration_sec=req.durationSec,
            aspect_ratio=req.aspectRatio,
            model=model,
            image_urls=input_data_urls,
        )
    except fal_video.FalVideoError as exc:
        raise StudioUpstreamError(str(exc)) from exc

    if not video_bytes:
        raise StudioUpstreamError("fal video generation returned no data")

    mime = video_mime or "video/mp4"
    video_url = await _save_image_bytes(video_bytes, mime=mime, ext=_video_ext(mime))
    assets: list[dict[str, Any]] = [{"id": _seq_id("asset"), "url": video_url, "mime": mime}]
    if poster_bytes:
        poster_mime_val = poster_mime or "image/jpeg"
        poster_url = await _save_image_bytes(
            poster_bytes, mime=poster_mime_val, ext=_video_poster_ext(poster_mime_val)
        )
        assets[0]["posterUrl"] = poster_url

    params: dict[str, Any] = {
        "kind": "video",
        "model": model,
        "aspectRatio": req.aspectRatio,
        "count": 1,
        "styleId": req.styleId,
        "negativePrompt": req.negativePrompt,
        "seed": req.seed,
        "durationSec": req.durationSec,
        "inputImageCount": len(input_data_urls) if input_data_urls else None,
    }
    record = _new_generation(
        gen_id=_seq_id("gen"),
        prompt=final_prompt,
        kind="video",
        model=model,
        params=params,
        assets=assets,
        status="succeeded",
    )
    _append_history({**record.model_dump(), "_workspace": workspace_id})
    return record


_MIME_BY_EXT: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}


def _mime_for_filename(name: str) -> str:
    """Best-effort mime from a media filename's extension (defaults to png)."""
    return _MIME_BY_EXT.get(Path(name).suffix.lower(), "image/png")


async def _resolve_source_data_url(source_url: str) -> tuple[str, str]:
    """Resolve an EditRequest ``sourceUrl`` into ``(data_url, mime)`` fal accepts
    as an ``image_url`` input.

    Handles three shapes the frontend can send:
      * ``data:`` URL             — pass through as-is (mime parsed from header).
      * ``http(s)://`` URL        — fetch the bytes, encode as a data URL.
      * ``/api/v1/media/<name>``  — read the stored bytes via the media adapter
                                    and encode as a data URL (the common case:
                                    editing a previously generated asset).
    """
    s = source_url.strip()
    if s.startswith("data:"):
        mime = s[5:].split(";", 1)[0] or "image/png"
        return s, mime
    if s.startswith(("http://", "https://")):
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.get(s)
            resp.raise_for_status()
        mime = resp.headers.get("content-type", "image/png").split(";")[0].strip() or "image/png"
        return fal_edit.encode_bytes(resp.content, mime), mime
    name = s.rsplit("/", 1)[-1]
    if not name or ".." in name or name != Path(name).name:
        raise ValueError("sourceUrl must be a valid media path")
    adapter = media_storage.get_adapter()
    key = media_storage.media_key(name)
    if not await adapter.exists(key):
        raise ValueError(f"source media '{name}' not found")
    chunks = [c async for c in adapter.open(key)]
    data = b"".join(chunks)
    mime = _mime_for_filename(name)
    return fal_edit.encode_bytes(data, mime), mime


# fal's motion-control image cap (from the endpoint schema): images up to
# 3850×3850px are accepted. Oversized uploads (e.g. a ~12K×18K photo) would be
# rejected, so we downscale the long side to fit before dispatch. We never
# upscale — a too-small image is left for fal to flag with a clear 400.
FAL_IMAGE_MAX_DIM = 3850


def _fit_character_image(data_url: str, mime: str) -> tuple[str, str]:
    """Downscale an oversized character image to fal's 3850px cap (aspect
    preserving), re-encoding as JPEG. Images already within the cap pass through
    untouched. A non-decoding image also passes through (fal's own validation
    then reports it) rather than turning into a 500."""
    header, sep, b64 = data_url.partition(",")
    if not sep:
        return data_url, mime
    try:
        raw = base64.b64decode(b64)
    except ValueError as exc:
        raise ValueError("could not decode the character image") from exc

    try:
        from PIL import Image
    except ImportError:  # pragma: no cover — Pillow is a pocketpaw dep
        return data_url, mime

    try:
        with Image.open(io.BytesIO(raw)) as img:
            width, height = img.size
            if width <= FAL_IMAGE_MAX_DIM and height <= FAL_IMAGE_MAX_DIM:
                return data_url, mime
            scale = FAL_IMAGE_MAX_DIM / max(width, height)
            new_size = (max(1, round(width * scale)), max(1, round(height * scale)))
            if img.mode in ("RGBA", "LA", "P"):
                rgba = img.convert("RGBA")
                flattened = Image.new("RGB", rgba.size, (255, 255, 255))
                flattened.paste(rgba, mask=rgba.split()[-1])
                img = flattened
            else:
                img = img.convert("RGB")
            img = img.resize(new_size, Image.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=90)
    except Exception:  # noqa: BLE001 — never 500 the request over image processing
        logger.warning("character image downscale failed (serving original)", exc_info=True)
        return data_url, mime
    return (
        f"data:image/jpeg;base64,{base64.b64encode(buf.getvalue()).decode('ascii')}",
        "image/jpeg",
    )


async def edit(req: schemas.EditRequest, *, workspace_id: str) -> schemas.Generation:
    """Run a canvas edit op (inpaint/expand/upscale/variations/remove-bg/edit/
    sketch-to-image) DIRECTLY against fal.ai — the LiteLLM gateway serves
    generation models only and has no route for fal's image-edit endpoints.

    The source asset is resolved to a ``data:`` URL, dispatched to the op's
    fal endpoint (or the ``req.model`` override), and every output image is
    persisted through media storage. Returns a NEW ``succeeded`` Generation
    (kept in the workspace history so the gallery + filmstrip grow). A missing
    prompt / bad model surfaces as ValueError (router → 400); an upstream fal
    failure surfaces as StudioUpstreamError (router → 502).
    """
    op = (req.op or "").strip()
    if op not in fal_edit.SUPPORTED_OPS:
        raise StudioNotSupported(
            f"Edit op '{op}' is not supported. Supported: "
            f"{', '.join(sorted(fal_edit.SUPPORTED_OPS))}."
        )
    if not (req.sourceUrl or "").strip():
        raise ValueError("sourceUrl is required for an edit")

    source_data_url, _ = await _resolve_source_data_url(req.sourceUrl)

    try:
        results = await fal_edit.run_fal_edit(
            op=op,
            image_data_url=source_data_url,
            mask_data_url=req.maskDataUrl,
            prompt=req.prompt,
            direction=req.direction,
            factor=req.factor,
            model=req.model,
        )
    except fal_edit.FalEditError as exc:
        raise StudioUpstreamError(str(exc)) from exc

    if not results:
        raise StudioUpstreamError("fal edit produced no output images")

    assets: list[dict[str, Any]] = []
    for data, mime in results:
        url = await _save_image_bytes(data, mime=mime, ext=fal_edit.mime_to_ext(mime))
        assets.append({"id": _seq_id("asset"), "url": url, "mime": mime})

    model = (req.model or "").strip() or fal_edit.DEFAULT_EDIT_MODELS[op]
    params: dict[str, Any] = {
        "kind": "image",
        "model": model,
        "aspectRatio": "1:1",
        "count": len(assets),
    }
    record = _new_generation(
        gen_id=_seq_id("gen"),
        prompt=(req.prompt or op).strip() or op,
        kind="image",
        model=model,
        params=params,
        assets=assets,
        status="succeeded",
    )
    _append_history({**record.model_dump(), "_workspace": workspace_id})
    return record


# ── Video editing (Kling Elements) ───────────────────────────────────────────
# The /studio "Edit video" panel: a source video (≤30s) + up to 20 element/
# reference images + a prompt, dispatched DIRECTLY against fal's Kling Elements
# endpoint (``fal_elements``). Inputs resolve to ``data:`` URLs (media path /
# http / data pass through via ``_resolve_source_data_url``) so fal never needs
# a route back into the deployment's private media storage. The produced video
# (+ optional poster) persists through media storage like every other generation.


async def generate_video_elements(
    req: schemas.VideoElementsRequest, *, workspace_id: str
) -> schemas.Generation:
    """Run a Kling Elements video edit and return a NEW ``succeeded`` Generation.

    The source video (optional) and each element image (max 20) are resolved to
    ``data:`` URLs and passed to fal_elements; the returned video is saved through
    media storage (with a poster frame when the endpoint produced one) and the
    record lands in the workspace history. A source video longer than 30s or more
    than 20 element images is a ValueError (router → 400); a fal upstream failure
    surfaces as StudioUpstreamError (router → 502).
    """
    prompt = (req.prompt or "").strip()
    image_urls = [u for u in (req.inputImageUrls or []) if u and u.strip()]
    video_url = (req.videoUrl or "").strip()

    if not prompt and not video_url and not image_urls:
        raise ValueError("prompt, a source video, or element images are required")
    if len(image_urls) > fal_elements.MAX_ELEMENT_IMAGES:
        raise ValueError(f"at most {fal_elements.MAX_ELEMENT_IMAGES} element images are allowed")
    if req.sourceDurationSec is not None and req.sourceDurationSec > 30:
        raise ValueError("source video must be 30 seconds or less")

    # Kling Elements needs a prompt to describe the edit; fall back to a default
    # motion instruction when the user edited a video without typing one.
    effective_prompt = prompt or "edit the video with the provided elements"
    final_prompt = await _apply_style(effective_prompt, None)

    input_data_urls: list[str] | None = None
    if image_urls:
        input_data_urls = []
        for url in image_urls:
            data_url, _ = await _resolve_source_data_url(url)
            input_data_urls.append(data_url)

    video_data_url: str | None = None
    if video_url:
        video_data_url, _ = await _resolve_source_data_url(video_url)

    model = (req.model or "").strip() or fal_elements.DEFAULT_ELEMENTS_MODEL
    try:
        video_bytes, video_mime, poster_bytes, poster_mime = await fal_elements.run_fal_elements(
            prompt=final_prompt,
            input_image_urls=input_data_urls,
            video_url=video_data_url,
            duration_sec=req.durationSec,
            aspect_ratio=req.aspectRatio,
            model=model,
        )
    except fal_elements.FalElementsError as exc:
        raise StudioUpstreamError(str(exc)) from exc

    if not video_bytes:
        raise StudioUpstreamError("fal elements returned no video data")

    mime = video_mime or "video/mp4"
    video_url_out = await _save_image_bytes(video_bytes, mime=mime, ext=_video_ext(mime))
    assets: list[dict[str, Any]] = [{"id": _seq_id("asset"), "url": video_url_out, "mime": mime}]
    if poster_bytes:
        poster_mime_val = poster_mime or "image/jpeg"
        poster_url_out = await _save_image_bytes(
            poster_bytes, mime=poster_mime_val, ext=_video_poster_ext(poster_mime_val)
        )
        assets[0]["posterUrl"] = poster_url_out

    params: dict[str, Any] = {
        "kind": "video",
        "model": model,
        "aspectRatio": req.aspectRatio,
        "count": 1,
        "durationSec": req.durationSec,
        "inputImageCount": len(input_data_urls) if input_data_urls else None,
    }
    record = _new_generation(
        gen_id=_seq_id("gen"),
        prompt=final_prompt,
        kind="video",
        model=model,
        params=params,
        assets=assets,
        status="succeeded",
    )
    _append_history({**record.model_dump(), "_workspace": workspace_id})
    return record


# ── Motion control (Kling Motion Control) ───────────────────────────────────
# The /studio "Motion control" panel: a character image (visible face and body)
# is animated to follow a reference motion clip, dispatched DIRECTLY against
# fal's Kling Motion Control endpoint (``fal_motion``). Inputs resolve to
# ``data:`` URLs (media path / http / data pass through via
# ``_resolve_source_data_url``) so fal never needs a route back into the
# deployment's private media storage. The produced video (+ optional poster)
# persists through media storage like every other generation.


async def generate_video_motion(
    req: schemas.VideoMotionRequest, *, workspace_id: str
) -> schemas.Generation:
    """Run a Kling Motion Control call and return a NEW ``succeeded`` Generation.

    The character image and reference motion video are resolved to ``data:`` URLs
    and passed to fal_motion; the returned video is saved through media storage
    (with a poster frame when the endpoint produced one) and the record lands in
    the workspace history. A missing character image / motion video is a
    ValueError (router → 400); a fal upstream failure surfaces as
    StudioUpstreamError (router → 502).
    """
    image_url = (req.imageUrl or "").strip()
    video_url = (req.videoUrl or "").strip()
    if not image_url:
        raise ValueError("a character image is required for motion control")
    if not video_url:
        raise ValueError("a motion reference video is required for motion control")

    image_data_url, image_mime = await _resolve_source_data_url(image_url)
    image_data_url, _ = _fit_character_image(image_data_url, image_mime)

    # The reference motion video is typically a PUBLIC URL (the hardcoded preset).
    # fal fetches public URLs directly (and re-encoding a multi-MB clip into a
    # base64 data URI both wastes bandwidth and risks hitting request limits), so
    # pass a hosted URL straight through. Private media paths still resolve to
    # data URLs so fal never needs a route back into our storage.
    if video_url.startswith(("http://", "https://")):
        video_arg = video_url
    else:
        video_arg, _ = await _resolve_source_data_url(video_url)

    model = (req.model or "").strip() or fal_motion.DEFAULT_MOTION_MODEL
    try:
        video_bytes, video_mime, poster_bytes, poster_mime = await fal_motion.run_fal_motion(
            image_url=image_data_url,
            video_url=video_arg,
            character_orientation=req.characterOrientation,
            model=model,
        )
    except fal_motion.FalMotionValidationError as exc:
        raise ValueError(str(exc)) from exc
    except fal_motion.FalMotionError as exc:
        raise StudioUpstreamError(str(exc)) from exc

    if not video_bytes:
        raise StudioUpstreamError("fal motion-control returned no video data")

    mime = video_mime or "video/mp4"
    video_url_out = await _save_image_bytes(video_bytes, mime=mime, ext=_video_ext(mime))
    assets: list[dict[str, Any]] = [{"id": _seq_id("asset"), "url": video_url_out, "mime": mime}]
    if poster_bytes:
        poster_mime_val = poster_mime or "image/jpeg"
        poster_url_out = await _save_image_bytes(
            poster_bytes, mime=poster_mime_val, ext=_video_poster_ext(poster_mime_val)
        )
        assets[0]["posterUrl"] = poster_url_out

    params: dict[str, Any] = {
        "kind": "video",
        "model": model,
        "aspectRatio": req.aspectRatio,
        "count": 1,
        "durationSec": req.durationSec,
    }
    record = _new_generation(
        gen_id=_seq_id("gen"),
        prompt="Motion control",
        kind="video",
        model=model,
        params=params,
        assets=assets,
        status="succeeded",
    )
    _append_history({**record.model_dump(), "_workspace": workspace_id})
    return record


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
    "generate_video_elements",
    "generate_video_motion",
    "suggest_prompt",
]
