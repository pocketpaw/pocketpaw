# ee/pocketpaw_ee/paw_bar/catalog_routes.py — owner routes for a site concierge's
# product catalog: the catalog store's rows, and the two import previews.
#
#   GET    /paw-bar/admin/site/{site_id}/catalog?offset&limit(≤200)&q  → {items, total}
#   PUT    /paw-bar/admin/site/{site_id}/catalog/items/{item_id}       → item
#   POST   /paw-bar/admin/site/{site_id}/catalog/items:bulk  {items}   → {upserted, total}
#   DELETE /paw-bar/admin/site/{site_id}/catalog/items       {ids}     → {deleted, total}
#   POST   /paw-bar/admin/site/{site_id}/catalog/reorder     {ids}     → {total}
#   POST   /paw-bar/admin/site/{site_id}/catalog/import/preview        → CatalogImportPreview
#   POST   /paw-bar/admin/site/{site_id}/catalog/import/csv  (file)    → CatalogImportPreview
#
# Gated like the other concierge owner routes: ``paw_bar.read`` on the GET and
# ``paw_bar.manage`` on everything else (both ADMIN), the session's active
# workspace, the site loaded workspace-scoped and its widget resolved the way the
# spec PATCH resolves it, so a foreign or malformed id is a 404 and a site with no
# widget is 404 ``no_concierge_widget``. Items are ``PawBarCatalogItem`` (422 on
# a blank id, a negative price or a repeated id in one call) plus an optional
# ``source``; the store keeps an existing item's position and appends new ones.
# A write past the cap (config ``pawbar_catalog_max_items``) is 409 with
# ``detail = {"code": "catalog_full", "limit": n}`` and writes nothing.
#
# The previews write nothing; applying one is the bulk route in chunks of
# ``MAX_BULK_ITEMS``. Site import reads the store's own pages
# (``catalog_import``); a failed import is a 200 with ``status: "failed"``. The
# CSV upload (multipart field ``file``, at most ``CSV_MAX_BYTES``, else 413
# ``too_large``) is parsed by ``catalog_csv``.
#
# A separate module so router.py does not grow. Mounted beside ``paw_bar.router``
# in ``pocketpaw_ee.cloud``.

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Body, Depends, File, HTTPException, Query, UploadFile
from pydantic import BaseModel, ConfigDict, Field
from pydantic import ValidationError as PydanticValidationError

from pocketpaw.paw_bar.catalog_store import (
    DEFAULT_CATALOG_MAX_ITEMS,
    CatalogFull,
    catalog_max_items,
)
from pocketpaw.paw_bar.models import PawBarCatalogItem, PawBarCatalogRow, PawBarWidget
from pocketpaw_ee.cloud._core.deps import current_workspace_id
from pocketpaw_ee.paw_bar import catalog_csv, catalog_import
from pocketpaw_ee.paw_bar import router as paw_bar_router
from pocketpaw_ee.paw_bar.catalog_import import CatalogImportPreview
from pocketpaw_ee.paw_bar.router import (
    _catalog_full,
    _load_site_scoped,
    _require_paw_bar_manage,
    _require_paw_bar_read,
    _resolve_site_and_widget,
)

router = APIRouter(tags=["PawBar"])

_CATALOG = "/paw-bar/admin/site/{site_id}/catalog"
MAX_BULK_ITEMS = 500
MAX_PAGE_ITEMS = 200
# Bounds on a request independent of config, so an absurd body is refused by the
# model before the store is touched.
_MAX_REORDER_IDS = 10 * DEFAULT_CATALOG_MAX_ITEMS
_MAX_QUERY_CHARS = 200

CatalogSource = Literal["manual", "shopify", "woocommerce", "jsonld", "opengraph", "csv", "site"]


class CatalogItemIn(PawBarCatalogItem):
    """One item in a write: the catalog item, plus where it came from (read off
    the id prefix when absent). Unknown keys (``position`` …) are ignored."""

    model_config = ConfigDict(extra="ignore")

    source: CatalogSource | None = None


class CatalogItemPut(CatalogItemIn):
    """Body of the PUT: the id comes from the path, so it may be left out."""

    id: str = ""


class CatalogListResponse(BaseModel):
    items: list[PawBarCatalogRow]
    total: int


class CatalogBulkRequest(BaseModel):
    items: list[CatalogItemIn] = Field(max_length=MAX_BULK_ITEMS)


class CatalogBulkResponse(BaseModel):
    upserted: int
    total: int


class CatalogIdsRequest(BaseModel):
    ids: list[str] = Field(max_length=MAX_BULK_ITEMS)


class CatalogReorderRequest(BaseModel):
    ids: list[str] = Field(max_length=_MAX_REORDER_IDS)


class CatalogDeleteResponse(BaseModel):
    deleted: int
    total: int


class CatalogTotalResponse(BaseModel):
    total: int


def _store() -> Any:
    """The router's store, looked up per call so a patched ``router._store`` holds."""
    return paw_bar_router._store()


async def _widget(site_id: str, workspace_id: str) -> PawBarWidget:
    _site, widget = await _resolve_site_and_widget(site_id, workspace_id)
    if widget is None:
        raise HTTPException(404, "no_concierge_widget")
    return widget


def _gone() -> HTTPException:
    """The widget vanished between the resolve and the write."""
    return HTTPException(404, "no_concierge_widget")


async def _upsert(widget: PawBarWidget, items: list[Any], workspace_id: str) -> tuple[int, int]:
    try:
        result = await _store().upsert_catalog_items(widget.id, items, workspace_id=workspace_id)
    except CatalogFull as exc:
        raise _catalog_full(exc) from None
    except PydanticValidationError:
        raise HTTPException(422, "invalid_item") from None
    except ValueError:
        raise HTTPException(422, "duplicate_id") from None
    if result is None:
        raise _gone()
    return result


@router.get(
    _CATALOG,
    response_model=CatalogListResponse,
    dependencies=[Depends(_require_paw_bar_read)],
)
async def list_site_catalog(
    site_id: str,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=MAX_PAGE_ITEMS),
    q: str = Query("", max_length=_MAX_QUERY_CHARS),
    workspace_id: str = Depends(current_workspace_id),
) -> CatalogListResponse:
    """A page of the catalog in owner order. ``q`` keeps the products whose name
    or description holds every word of it; ``total`` counts what matched."""
    widget = await _widget(site_id, workspace_id)
    items, total = await _store().list_catalog(
        widget.id, offset=offset, limit=limit, q=q, workspace_id=workspace_id
    )
    return CatalogListResponse(items=items, total=total)


@router.put(
    _CATALOG + "/items/{item_id}",
    response_model=PawBarCatalogRow,
    dependencies=[Depends(_require_paw_bar_manage)],
)
async def put_site_catalog_item(
    site_id: str,
    item_id: str,
    item: CatalogItemPut,
    workspace_id: str = Depends(current_workspace_id),
) -> PawBarCatalogRow:
    """Create or replace one product. A replaced product keeps its place; a new
    one goes last. A body ``id`` other than the path's is 422 ``item_id_mismatch``."""
    if item.id and item.id.strip() != item_id.strip():
        raise HTTPException(422, "item_id_mismatch")
    widget = await _widget(site_id, workspace_id)
    try:
        row = CatalogItemIn.model_validate({**item.model_dump(), "id": item_id})
    except PydanticValidationError:
        raise HTTPException(422, "invalid_item") from None
    await _upsert(widget, [row], workspace_id)
    saved = await _store().get_catalog_items(widget.id, [row.id], workspace_id=workspace_id)
    if not saved:
        raise _gone()
    return saved[0]


@router.post(
    _CATALOG + "/items:bulk",
    response_model=CatalogBulkResponse,
    dependencies=[Depends(_require_paw_bar_manage)],
)
async def bulk_upsert_site_catalog(
    site_id: str,
    body: CatalogBulkRequest,
    workspace_id: str = Depends(current_workspace_id),
) -> CatalogBulkResponse:
    """Create or replace up to ``MAX_BULK_ITEMS`` products in one transaction (how
    an import is applied). All or nothing: a cap breach writes none of them."""
    widget = await _widget(site_id, workspace_id)
    upserted, total = await _upsert(widget, list(body.items), workspace_id)
    return CatalogBulkResponse(upserted=upserted, total=total)


@router.delete(
    _CATALOG + "/items",
    response_model=CatalogDeleteResponse,
    dependencies=[Depends(_require_paw_bar_manage)],
)
async def delete_site_catalog_items(
    site_id: str,
    body: CatalogIdsRequest = Body(...),
    workspace_id: str = Depends(current_workspace_id),
) -> CatalogDeleteResponse:
    """Remove products by id; unknown ids are ignored."""
    widget = await _widget(site_id, workspace_id)
    result = await _store().delete_catalog_items(widget.id, body.ids, workspace_id=workspace_id)
    if result is None:
        raise _gone()
    deleted, total = result
    return CatalogDeleteResponse(deleted=deleted, total=total)


@router.post(
    _CATALOG + "/reorder",
    response_model=CatalogTotalResponse,
    dependencies=[Depends(_require_paw_bar_manage)],
)
async def reorder_site_catalog(
    site_id: str,
    body: CatalogReorderRequest,
    workspace_id: str = Depends(current_workspace_id),
) -> CatalogTotalResponse:
    """Put ``ids`` first, in that order; the rest keep their order after them."""
    widget = await _widget(site_id, workspace_id)
    total = await _store().reorder_catalog(widget.id, body.ids, workspace_id=workspace_id)
    if total is None:
        raise _gone()
    return CatalogTotalResponse(total=total)


@router.post(
    _CATALOG + "/import/preview",
    response_model=CatalogImportPreview,
    dependencies=[Depends(_require_paw_bar_manage)],
)
async def preview_site_catalog_import(
    site_id: str,
    workspace_id: str = Depends(current_workspace_id),
) -> CatalogImportPreview:
    """The products the site publishes, for the owner to pick from. Writes nothing.

    Awaited inline (bounded by ``catalog_import.IMPORT_WALL_CLOCK_SEC``), like the
    knowledge re-sync: the owner pressed a button and is waiting for the list.
    """
    site = await _load_site_scoped(site_id, workspace_id)
    return await catalog_import.preview_catalog_import(site)


async def _read_upload(file: UploadFile, max_bytes: int) -> bytes:
    """The upload's bytes, refusing past ``max_bytes`` without reading further."""
    chunks: list[bytes] = []
    total = 0
    while chunk := await file.read(64 * 1024):
        total += len(chunk)
        if total > max_bytes:
            raise HTTPException(413, "too_large")
        chunks.append(chunk)
    return b"".join(chunks)


@router.post(
    _CATALOG + "/import/csv",
    response_model=CatalogImportPreview,
    dependencies=[Depends(_require_paw_bar_manage)],
)
async def preview_site_catalog_csv(
    site_id: str,
    file: UploadFile = File(...),
    workspace_id: str = Depends(current_workspace_id),
) -> CatalogImportPreview:
    """The products in an uploaded CSV, for the owner to pick from. Writes nothing."""
    await _load_site_scoped(site_id, workspace_id)
    data = await _read_upload(file, catalog_csv.CSV_MAX_BYTES)
    return catalog_csv.parse_catalog_csv(data, max_items=catalog_max_items())
