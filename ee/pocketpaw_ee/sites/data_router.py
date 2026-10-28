# ee/pocketpaw_ee/sites/data_router.py — REST surface of the Sites Data dashboard
# (reads):
#
#   GET /sites/by-pocket/{pocket_id}/data/inventory              DataInventory
#   GET /sites/by-pocket/{pocket_id}/data/tables/{table}         TableSchema
#   GET /sites/by-pocket/{pocket_id}/data/tables/{table}/rows    RowPage
#       ?q=&sort=&dir=asc|desc&f=<JSON list of {col, op, value}>&limit=&offset=
#
# Thin handlers over ``sites.data_service``; errors are CloudErrors mapped by
# ``_core.http`` (422 for a bad query, 404 for an unknown table or pocket, 429
# ``sites.data_busy`` when the Cloudflare budget is spent). Guarded like the legacy
# data routes: ``request_context`` + ``fabric.read``; ``can_write`` / ``can_restore``
# come from the non-raising ``sites.data_write`` / ``sites.data_restore`` checks.
#
# Mounting: ``sites.router`` includes this router BEFORE its own routes, so it is
# mounted (and plan-gated) wherever the sites router is. That order matters:
# ``.../data/inventory`` would otherwise be swallowed by the legacy
# ``.../data/{table}``. A table literally named ``inventory`` is still reachable
# through ``.../data/tables/inventory``.
"""Sites Data dashboard read routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from pocketpaw_ee.cloud._core.context import RequestContext, request_context
from pocketpaw_ee.cloud._core.deps import action_allowed_any_workspace, require_action_any_workspace
from pocketpaw_ee.sites import data_service
from pocketpaw_ee.sites.site_data.dto import DataInventory, RowPage, TableSchema

router = APIRouter(tags=["Sites"])

_BASE = "/sites/by-pocket/{pocket_id}/data"


@router.get(_BASE + "/inventory", response_model=DataInventory)
async def site_data_inventory(
    pocket_id: str,
    ctx: RequestContext = Depends(request_context),
    _: object = Depends(require_action_any_workspace("fabric.read")),
    can_write: bool = Depends(action_allowed_any_workspace("sites.data_write")),
    can_restore: bool = Depends(action_allowed_any_workspace("sites.data_restore")),
) -> DataInventory:
    """Tables with row counts, file and KV stores, connected sources, and whether
    the caller may edit or restore."""
    return await data_service.get_inventory(
        workspace_id=ctx.workspace_id or "",
        user_id=ctx.user_id,
        pocket_id=pocket_id,
        can_write=can_write,
        can_restore=can_restore,
    )


@router.get(_BASE + "/tables/{table}", response_model=TableSchema)
async def site_data_table_schema(
    pocket_id: str,
    table: str,
    ctx: RequestContext = Depends(request_context),
    _: object = Depends(require_action_any_workspace("fabric.read")),
) -> TableSchema:
    """One table's columns (kind, key, masked, writable, references), edit state and
    row count."""
    return await data_service.get_table_schema(
        workspace_id=ctx.workspace_id or "", user_id=ctx.user_id, pocket_id=pocket_id, table=table
    )


@router.get(_BASE + "/tables/{table}/rows", response_model=RowPage)
async def site_data_table_rows(
    pocket_id: str,
    table: str,
    q: str | None = Query(None),
    sort: str | None = Query(None),
    dir: str | None = Query(None),  # noqa: A002 — the wire name
    f: str | None = Query(None),
    # Bounds are checked by the service so a bad value gets the CloudError envelope.
    limit: int | None = Query(None),
    offset: int = Query(0),
    ctx: RequestContext = Depends(request_context),
    _: object = Depends(require_action_any_workspace("fabric.read")),
) -> RowPage:
    """A page of rows (each with ``__key``) under the search, filters and sort."""
    return await data_service.get_rows(
        workspace_id=ctx.workspace_id or "",
        user_id=ctx.user_id,
        pocket_id=pocket_id,
        table=table,
        q=q,
        sort=sort,
        dir=dir,
        f=f,
        limit=limit,
        offset=offset,
    )
