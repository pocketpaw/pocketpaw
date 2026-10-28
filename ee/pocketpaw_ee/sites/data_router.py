# ee/pocketpaw_ee/sites/data_router.py — REST surface of the Sites Data dashboard,
# under /sites/by-pocket/{pocket_id}/data:
#
#   GET   /inventory                              DataInventory
#   GET   /tables/{table}                         TableSchema
#   GET   /tables/{table}/rows                    RowPage
#         ?q=&sort=&dir=asc|desc&f=<JSON list of {col, op, value}>&limit=&offset=
#   POST  /tables/{table}/rows                    {values}          -> {row}
#   PATCH /tables/{table}/rows/{key}              {set, expected}   -> {row}
#   POST  /tables/{table}/rows:delete             {keys}  -> {deleted, keys, restore_point}
#   POST  /tables/{table}/rows:duplicate          {key, values?}    -> {row}
#   GET   /tables/{table}/rows/{key}/history      -> {events}
#
# Thin handlers over ``sites.data_service``; errors are CloudErrors mapped by
# ``_core.http`` (422 for a bad query or value, 404 for an unknown table, row or
# pocket, 409 ``sites.data_conflict`` / ``sites.data_unavailable``, 429
# ``sites.data_busy`` when the Cloudflare budget is spent). Every route takes
# ``request_context`` + ``fabric.read``; every write route also requires
# ``sites.data_write`` (ADMIN, overridable per member) and refuses a body over
# 1 MB (413 ``sites.data_body_too_large``). ``can_write`` /
# ``can_restore`` on the inventory come from the non-raising checks. ``{key}`` is
# the row's ``__key``, URL-encoded; it may contain ``/``.
#
# Mounting: ``sites.router`` includes this router BEFORE its own routes, so it is
# mounted (and plan-gated) wherever the sites router is. That order matters:
# ``.../data/inventory`` would otherwise be swallowed by the legacy
# ``.../data/{table}``. A table literally named ``inventory`` is still reachable
# through ``.../data/tables/inventory``.
"""Sites Data dashboard routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request

from pocketpaw_ee.cloud._core.context import RequestContext, request_context
from pocketpaw_ee.cloud._core.deps import action_allowed_any_workspace, require_action_any_workspace
from pocketpaw_ee.cloud._core.errors import PayloadTooLarge
from pocketpaw_ee.sites import data_service
from pocketpaw_ee.sites.site_data.dto import (
    DataInventory,
    DeleteRowsBody,
    DeleteRowsOut,
    DuplicateRowBody,
    InsertRowBody,
    RowHistory,
    RowOut,
    RowPage,
    TableSchema,
    UpdateRowBody,
)

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


_READ = require_action_any_workspace("fabric.read")
_WRITE = require_action_any_workspace("sites.data_write")
MAX_WRITE_BODY = 1_000_000


async def _write_body_limit(request: Request) -> None:
    """Refuse a write body over 1 MB before any of it is coerced or sent to D1."""
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > MAX_WRITE_BODY:
        raise PayloadTooLarge("sites.data_body_too_large", "That change is too large.")
    if len(await request.body()) > MAX_WRITE_BODY:
        raise PayloadTooLarge("sites.data_body_too_large", "That change is too large.")


@router.post(_BASE + "/tables/{table}/rows", response_model=RowOut)
async def site_data_insert_row(
    pocket_id: str,
    table: str,
    body: InsertRowBody,
    ctx: RequestContext = Depends(request_context),
    _: object = Depends(_READ),
    __: object = Depends(_WRITE),
    ___: None = Depends(_write_body_limit),
) -> RowOut:
    """Add a row; 422 with ``details.fields`` for a missing or bad value."""
    return await data_service.insert_row(
        workspace_id=ctx.workspace_id or "",
        user_id=ctx.user_id,
        pocket_id=pocket_id,
        table=table,
        values=body.values,
    )


@router.post(_BASE + "/tables/{table}/rows:delete", response_model=DeleteRowsOut)
async def site_data_delete_rows(
    pocket_id: str,
    table: str,
    body: DeleteRowsBody,
    ctx: RequestContext = Depends(request_context),
    _: object = Depends(_READ),
    __: object = Depends(_WRITE),
    ___: None = Depends(_write_body_limit),
) -> DeleteRowsOut:
    """Delete up to 500 rows by key, all or none."""
    return await data_service.delete_rows(
        workspace_id=ctx.workspace_id or "",
        user_id=ctx.user_id,
        pocket_id=pocket_id,
        table=table,
        keys=list(body.keys),
    )


@router.post(_BASE + "/tables/{table}/rows:duplicate", response_model=RowOut)
async def site_data_duplicate_row(
    pocket_id: str,
    table: str,
    body: DuplicateRowBody,
    ctx: RequestContext = Depends(request_context),
    _: object = Depends(_READ),
    __: object = Depends(_WRITE),
    ___: None = Depends(_write_body_limit),
) -> RowOut:
    """Copy a row into a new one (``values`` override copied fields)."""
    return await data_service.duplicate_row(
        workspace_id=ctx.workspace_id or "",
        user_id=ctx.user_id,
        pocket_id=pocket_id,
        table=table,
        key=body.key,
        values=body.values,
    )


@router.get(_BASE + "/tables/{table}/rows/{key:path}/history", response_model=RowHistory)
async def site_data_row_history(
    pocket_id: str,
    table: str,
    key: str,
    ctx: RequestContext = Depends(request_context),
    _: object = Depends(_READ),
) -> RowHistory:
    """The dashboard's changes to one row, newest first (max 50)."""
    return await data_service.row_history(
        workspace_id=ctx.workspace_id or "",
        user_id=ctx.user_id,
        pocket_id=pocket_id,
        table=table,
        key=key,
    )


@router.patch(_BASE + "/tables/{table}/rows/{key:path}", response_model=RowOut)
async def site_data_update_row(
    pocket_id: str,
    table: str,
    key: str,
    body: UpdateRowBody,
    ctx: RequestContext = Depends(request_context),
    _: object = Depends(_READ),
    __: object = Depends(_WRITE),
    ___: None = Depends(_write_body_limit),
) -> RowOut:
    """Change a row if it still holds ``expected``; 409 ``sites.data_conflict`` with
    ``details.current`` when it changed."""
    return await data_service.update_row(
        workspace_id=ctx.workspace_id or "",
        user_id=ctx.user_id,
        pocket_id=pocket_id,
        table=table,
        key=key,
        set=body.set_,
        expected=body.expected,
    )
