# ee/pocketpaw_ee/catalog/router.py — the Model Catalog read surface (MCG-1).
#
# Two tenant-independent reads (the available-models set is deployment-global, not
# workspace-scoped — same shape as GET /billing/plans), license-gated:
#   * GET /catalog/models?modality=&provider=&q=&capability=
#       -> the filtered catalog (ModelCatalogList: models + total).
#   * GET /catalog/models/{id}
#       -> one entry. ``id`` is the URL-ENCODED canonical key "<provider>/<model>"
#          (the slash arrives as %2F; FastAPI decodes it into the path value, so
#          we capture the full remainder with a :path converter).
#
# THIN adapter per the EE "primitive = service + thin adapters" shape — assembly,
# the TTL cache, and filtering all live in ``catalog.service``; the proxy read +
# mapping live in ``catalog.litellm_client``. A LiteLLM proxy failure surfaces as
# 502 (the catalog's source of truth is unreachable) rather than a misleading
# empty 200. Mounted in ``mount_cloud()``.
#
# Created 2026-06-26 (feat/mcg-1-catalog-api, MCG-1): new entity router.

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from pocketpaw_ee.catalog import service as catalog_service
from pocketpaw_ee.catalog.litellm_client import CatalogUpstreamError
from pocketpaw_ee.catalog.models import Modality, ModelCatalogEntry, ModelCatalogList
from pocketpaw_ee.cloud.license import require_license

router = APIRouter(prefix="/catalog", tags=["Catalog"], dependencies=[Depends(require_license)])


@router.get("/models", response_model=ModelCatalogList)
async def list_catalog_models(
    modality: str | None = Query(
        None,
        description="Filter by modality bucket: chat | embedding | image | "
        "audio_tts | audio_stt | video.",
    ),
    provider: str | None = Query(None, description="Filter by provider (exact, case-insensitive)."),
    q: str | None = Query(
        None, description="Substring search over id, display name, and description."
    ),
    capability: str | None = Query(
        None,
        description="Filter to models exposing this capability token "
        "(e.g. tool_call, vision, streaming, json_mode).",
    ),
) -> ModelCatalogList:
    """List catalog entries from the LiteLLM proxy, grouped by modality and
    filtered by any supplied query params (all AND together).

    The catalog is the UNION of what the proxy describes (/model/info) and what it
    routes (/v1/models), so the catalog is never narrower than the routable set.
    A proxy outage returns 502 — the source of truth is unreachable, which is an
    upstream error, not an empty catalog.
    """
    # Validate the modality value early so a bad filter is a clear 422-style 400
    # rather than a silent empty list.
    if modality is not None and modality.strip().lower() not in {m.value for m in Modality}:
        raise HTTPException(
            400,
            f"Unknown modality '{modality}'. Expected one of: "
            f"{', '.join(m.value for m in Modality)}.",
        )
    try:
        entries: list[ModelCatalogEntry] = await catalog_service.list_models(
            modality=modality,
            provider=provider,
            q=q,
            capability=capability,
        )
    except CatalogUpstreamError as exc:
        raise HTTPException(502, f"Model catalog source unavailable: {exc}") from exc
    return ModelCatalogList(models=entries, total=len(entries))


@router.get("/models/{model_id:path}", response_model=ModelCatalogEntry)
async def get_catalog_model(model_id: str) -> ModelCatalogEntry:
    """Return one catalog entry by its canonical id.

    ``model_id`` is the URL-encoded "<provider>/<model>" key; the ``:path``
    converter captures the decoded value (including the slash) so e.g.
    GET /catalog/models/anthropic%2Fclaude-3-5-sonnet resolves to the
    ``anthropic/claude-3-5-sonnet`` entry.
    """
    try:
        entry = await catalog_service.get_model(model_id)
    except CatalogUpstreamError as exc:
        raise HTTPException(502, f"Model catalog source unavailable: {exc}") from exc
    if entry is None:
        raise HTTPException(404, f"Model '{model_id}' not found in catalog")
    return entry
