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
#   POST  /tables/{table}/import:preview          multipart ``file`` -> ImportPreview
#   POST  /tables/{table}/import                  {upload_id, mapping, mode} -> ImportResult
#   GET   /tables/{table}/export.csv              ?q=&sort=&dir=&f=  -> text/csv stream
#   GET   /restore             ?at=<iso> | ?restore_point=<bookmark> -> RestoreStatus
#   POST  /restore             {at | restore_point, confirm_name} -> RestoreOut
#   POST  /restore:undo               {previous_bookmark, confirm_name} -> RestoreOut
#   POST  /sql                        {sql, confirm?: write|unbounded} -> SqlResult
#   GET   /schema                                 DataSchema
#   GET   /files/{binding}                        ?prefix=&cursor=  -> FileList
#   GET   /files/{binding}/object?key=            the object, streamed as an attachment
#   PUT   /files/{binding}/object?key=&overwrite= raw body (<= 50 MB) -> FileObjectOut
#   POST  /files/{binding}/object:move            {from, to, overwrite?} -> FileObjectOut
#   DELETE /files/{binding}/object?key=           204
#   GET   /kv/{binding}/keys                      ?prefix=&cursor=  -> KvKeyList
#   GET   /kv/{binding}/keys/{key}                KvKeyValue
#   PUT   /kv/{binding}/keys/{key}      {value, metadata?, expiration_ttl?} -> KvPutOut
#   DELETE /kv/{binding}/keys/{key}               204
#
# Thin handlers over ``sites.data_service``; errors are CloudErrors mapped by
# ``_core.http`` (422 for a bad query or value, 404 for an unknown table, row or
# pocket, 409 ``sites.data_conflict`` / ``sites.data_unavailable``, 429
# ``sites.data_busy`` when the Cloudflare budget is spent). Every route takes
# ``request_context`` + ``fabric.read``; every write route also requires
# ``sites.data_write`` (ADMIN, overridable per member) and refuses a body over
# 1 MB (413 ``sites.data_body_too_large``; a CSV upload is capped at 5 MB, 413
# ``sites.data_csv_too_large``). The three restore routes require
# ``sites.data_restore`` (ADMIN) instead. ``/sql`` takes ``fabric.read`` and passes
# ``sites.data_write`` in as a bool: the statement's kind decides which it needs.
# Three reads are guarded like writes
# (``sites.data_write`` plus edit access): the KV key list, one KV key, and a file
# download, because KV names can be session tokens and uploads can be private; the
# file LISTING stays on ``fabric.read``. ``can_write`` /
# ``can_restore`` on the inventory come from the non-raising checks. ``{key}`` is
# the row's ``__key`` (or the KV key), URL-encoded; it may contain ``/``. A file
# upload streams its raw body (413 ``sites.data_file_too_large`` past 50 MB; the body
# ceiling middleware stops a bigger one first), a KV write is capped at 3 MB of JSON,
# and a download always answers ``attachment``, ``nosniff`` and a sandboxing CSP.
#
# Mounting: ``sites.router`` includes this router BEFORE its own routes, so it is
# mounted (and plan-gated) wherever the sites router is. That order matters:
# ``.../data/inventory`` would otherwise be swallowed by the legacy
# ``.../data/{table}``. A table literally named ``inventory`` is still reachable
# through ``.../data/tables/inventory``.
"""Sites Data dashboard routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, Query, Request, Response, UploadFile
from fastapi.responses import StreamingResponse

from pocketpaw_ee.cloud._core.context import RequestContext, request_context
from pocketpaw_ee.cloud._core.deps import action_allowed_any_workspace, require_action_any_workspace
from pocketpaw_ee.cloud._core.errors import PayloadTooLarge
from pocketpaw_ee.sites import data_service
from pocketpaw_ee.sites.site_data import csv_io
from pocketpaw_ee.sites.site_data.dto import (
    DataInventory,
    DataSchema,
    DeleteRowsBody,
    DeleteRowsOut,
    DuplicateRowBody,
    FileList,
    FileObjectOut,
    ImportBody,
    ImportPreview,
    ImportResult,
    InsertRowBody,
    KvKeyList,
    KvKeyValue,
    KvPutBody,
    KvPutOut,
    MoveFileBody,
    RestoreBody,
    RestoreOut,
    RestoreStatus,
    RestoreUndoBody,
    RowHistory,
    RowOut,
    RowPage,
    SqlBody,
    SqlResult,
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


# --------------------------------------------------------------------------- CSV


async def _read_csv(file: UploadFile) -> bytes:
    """The upload's bytes, refusing past 5 MB without reading further."""
    chunks: list[bytes] = []
    total = 0
    while chunk := await file.read(64 * 1024):
        total += len(chunk)
        if total > csv_io.MAX_BYTES:
            raise PayloadTooLarge("sites.data_csv_too_large", "A CSV file can be at most 5 MB.")
        chunks.append(chunk)
    return b"".join(chunks)


@router.post(_BASE + "/tables/{table}/import:preview", response_model=ImportPreview)
async def site_data_import_preview(
    pocket_id: str,
    table: str,
    file: UploadFile = File(...),
    ctx: RequestContext = Depends(request_context),
    _: object = Depends(_READ),
    __: object = Depends(_WRITE),
) -> ImportPreview:
    """Read a CSV (multipart ``file``) and say how it would import. Writes nothing."""
    data = await _read_csv(file)
    return await data_service.import_preview(
        workspace_id=ctx.workspace_id or "",
        user_id=ctx.user_id,
        pocket_id=pocket_id,
        table=table,
        filename=file.filename or "",
        data=data,
    )


@router.post(_BASE + "/tables/{table}/import", response_model=ImportResult)
async def site_data_import(
    pocket_id: str,
    table: str,
    body: ImportBody,
    ctx: RequestContext = Depends(request_context),
    _: object = Depends(_READ),
    __: object = Depends(_WRITE),
    ___: None = Depends(_write_body_limit),
) -> ImportResult:
    """Write a previewed upload's rows under ``mapping`` (``insert`` or ``upsert``)."""
    return await data_service.import_commit(
        workspace_id=ctx.workspace_id or "",
        user_id=ctx.user_id,
        pocket_id=pocket_id,
        table=table,
        upload_id=body.upload_id,
        mapping=body.mapping,
        mode=body.mode,
    )


@router.get(_BASE + "/tables/{table}/export.csv")
async def site_data_export(
    pocket_id: str,
    table: str,
    q: str | None = Query(None),
    sort: str | None = Query(None),
    dir: str | None = Query(None),  # noqa: A002 — the wire name
    f: str | None = Query(None),
    ctx: RequestContext = Depends(request_context),
    _: object = Depends(_READ),
) -> StreamingResponse:
    """The rows under the search, filters and sort as a CSV download (no credentials,
    at most 100,000 rows)."""
    filename, body = await data_service.export_csv(
        workspace_id=ctx.workspace_id or "",
        user_id=ctx.user_id,
        pocket_id=pocket_id,
        table=table,
        q=q,
        sort=sort,
        dir=dir,
        f=f,
    )
    return StreamingResponse(
        body,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )


# --------------------------------------------------------------------------- restore

_RESTORE = require_action_any_workspace("sites.data_restore")


@router.get(_BASE + "/restore", response_model=RestoreStatus)
async def site_data_restore_status(
    pocket_id: str,
    at: str | None = Query(None, max_length=64),
    restore_point: str | None = Query(None, max_length=200),
    ctx: RequestContext = Depends(request_context),
    _: object = Depends(_READ),
    __: object = Depends(_RESTORE),
) -> RestoreStatus:
    """Whether the database can be restored, how far back, and what ``at`` would do."""
    return await data_service.restore_status(
        workspace_id=ctx.workspace_id or "",
        user_id=ctx.user_id,
        pocket_id=pocket_id,
        at=at,
        restore_point=restore_point,
    )


@router.post(_BASE + "/restore", response_model=RestoreOut)
async def site_data_restore(
    pocket_id: str,
    body: RestoreBody,
    ctx: RequestContext = Depends(request_context),
    _: object = Depends(_READ),
    __: object = Depends(_RESTORE),
) -> RestoreOut:
    """Restore the whole database to ``at``; ``confirm_name`` must be the site's name."""
    return await data_service.restore(
        workspace_id=ctx.workspace_id or "",
        user_id=ctx.user_id,
        pocket_id=pocket_id,
        at=body.at,
        restore_point=body.restore_point,
        confirm_name=body.confirm_name,
    )


@router.post(_BASE + "/restore:undo", response_model=RestoreOut)
async def site_data_restore_undo(
    pocket_id: str,
    body: RestoreUndoBody,
    ctx: RequestContext = Depends(request_context),
    _: object = Depends(_READ),
    __: object = Depends(_RESTORE),
) -> RestoreOut:
    """Undo a restore of this site with the ``previous_bookmark`` it returned."""
    return await data_service.restore_undo(
        workspace_id=ctx.workspace_id or "",
        user_id=ctx.user_id,
        pocket_id=pocket_id,
        previous_bookmark=body.previous_bookmark,
        confirm_name=body.confirm_name,
    )


# --------------------------------------------------------------------------- SQL console


@router.post(_BASE + "/sql", response_model=SqlResult)
async def site_data_sql(
    pocket_id: str,
    body: SqlBody,
    ctx: RequestContext = Depends(request_context),
    _: object = Depends(_READ),
    can_write: bool = Depends(action_allowed_any_workspace("sites.data_write")),
    ___: None = Depends(_write_body_limit),
) -> SqlResult:
    """Run one statement: a read for anyone who reads rows, a row change for
    ``sites.data_write`` with ``confirm``; structure changes are refused."""
    return await data_service.run_sql(
        workspace_id=ctx.workspace_id or "",
        user_id=ctx.user_id,
        pocket_id=pocket_id,
        sql=body.sql,
        confirm=body.confirm,
        can_write=can_write,
    )


@router.get(_BASE + "/schema", response_model=DataSchema)
async def site_data_schema(
    pocket_id: str,
    ctx: RequestContext = Depends(request_context),
    _: object = Depends(_READ),
) -> DataSchema:
    """Every table's columns, indexes and CREATE text, read only."""
    return await data_service.get_schema(
        workspace_id=ctx.workspace_id or "", user_id=ctx.user_id, pocket_id=pocket_id
    )


# --------------------------------------------------------------------------- files (R2)

# What a download may never do: render, run, or be cached on the way.
_DOWNLOAD_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "default-src 'none'; sandbox",
    "Cache-Control": "no-store",
}


@router.get(_BASE + "/files/{binding}", response_model=FileList)
async def site_data_files(
    pocket_id: str,
    binding: str,
    prefix: str | None = Query(None, max_length=1100),
    cursor: str | None = Query(None, max_length=2048),
    ctx: RequestContext = Depends(request_context),
    _: object = Depends(_READ),
) -> FileList:
    """One folder of a file store: sub-folders and objects, 200 per page."""
    return await data_service.list_files(
        workspace_id=ctx.workspace_id or "",
        user_id=ctx.user_id,
        pocket_id=pocket_id,
        binding=binding,
        prefix=prefix,
        cursor=cursor,
    )


@router.get(_BASE + "/files/{binding}/object")
async def site_data_file_download(
    pocket_id: str,
    binding: str,
    key: str = Query(..., max_length=4096),
    ctx: RequestContext = Depends(request_context),
    _: object = Depends(_READ),
    __: object = Depends(_WRITE),
) -> StreamingResponse:
    """The object as a download (never rendered inline)."""
    got = await data_service.download_file(
        workspace_id=ctx.workspace_id or "",
        user_id=ctx.user_id,
        pocket_id=pocket_id,
        binding=binding,
        key=key,
    )
    headers = {"Content-Disposition": got.disposition, **_DOWNLOAD_HEADERS}
    if got.size is not None:
        headers["Content-Length"] = str(got.size)
    return StreamingResponse(got.body, media_type=got.content_type, headers=headers)


@router.put(_BASE + "/files/{binding}/object", response_model=FileObjectOut)
async def site_data_file_upload(
    pocket_id: str,
    binding: str,
    request: Request,
    key: str = Query(..., max_length=4096),
    overwrite: bool = Query(False),
    ctx: RequestContext = Depends(request_context),
    _: object = Depends(_READ),
    __: object = Depends(_WRITE),
) -> FileObjectOut:
    """Upload the raw request body (at most 50 MB) as ``key``; 409
    ``sites.data_file_exists`` when ``key`` exists, unless ``overwrite``."""
    declared = request.headers.get("content-length", "")
    return await data_service.upload_file(
        workspace_id=ctx.workspace_id or "",
        user_id=ctx.user_id,
        pocket_id=pocket_id,
        binding=binding,
        key=key,
        content_type=request.headers.get("content-type"),
        body=request.stream(),
        declared_size=int(declared) if declared.isdigit() else None,
        overwrite=overwrite,
    )


@router.post(_BASE + "/files/{binding}/object:move", response_model=FileObjectOut)
async def site_data_file_move(
    pocket_id: str,
    binding: str,
    body: MoveFileBody,
    ctx: RequestContext = Depends(request_context),
    _: object = Depends(_READ),
    __: object = Depends(_WRITE),
    ___: None = Depends(_write_body_limit),
) -> FileObjectOut:
    """Rename / move an object; 409 ``sites.data_file_exists`` unless ``overwrite``."""
    return await data_service.move_file(
        workspace_id=ctx.workspace_id or "",
        user_id=ctx.user_id,
        pocket_id=pocket_id,
        binding=binding,
        from_key=body.from_,
        to_key=body.to,
        overwrite=body.overwrite,
    )


@router.delete(_BASE + "/files/{binding}/object", status_code=204)
async def site_data_file_delete(
    pocket_id: str,
    binding: str,
    key: str = Query(..., max_length=4096),
    ctx: RequestContext = Depends(request_context),
    _: object = Depends(_READ),
    __: object = Depends(_WRITE),
) -> Response:
    """Delete an object (files can't be restored)."""
    await data_service.delete_file(
        workspace_id=ctx.workspace_id or "",
        user_id=ctx.user_id,
        pocket_id=pocket_id,
        binding=binding,
        key=key,
    )
    return Response(status_code=204)


# --------------------------------------------------------------------------- key-values (KV)

# A 1 MB value (``files_kv.MAX_KV_EDIT_BYTES``) plus JSON escaping and metadata.
MAX_KV_BODY = 3 * 1024 * 1024


async def _kv_body_limit(request: Request) -> None:
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > MAX_KV_BODY:
        raise PayloadTooLarge("sites.data_kv_value_too_large", "A value can be at most 1 MB here.")
    if len(await request.body()) > MAX_KV_BODY:
        raise PayloadTooLarge("sites.data_kv_value_too_large", "A value can be at most 1 MB here.")


@router.get(_BASE + "/kv/{binding}/keys", response_model=KvKeyList)
async def site_data_kv_keys(
    pocket_id: str,
    binding: str,
    prefix: str | None = Query(None, max_length=600),
    cursor: str | None = Query(None, max_length=2048),
    ctx: RequestContext = Depends(request_context),
    _: object = Depends(_READ),
    __: object = Depends(_WRITE),
) -> KvKeyList:
    """A page of keys (200) under ``prefix``; credential-named keys are ``masked``."""
    return await data_service.list_kv_keys(
        workspace_id=ctx.workspace_id or "",
        user_id=ctx.user_id,
        pocket_id=pocket_id,
        binding=binding,
        prefix=prefix,
        cursor=cursor,
    )


@router.get(_BASE + "/kv/{binding}/keys/{key:path}", response_model=KvKeyValue)
async def site_data_kv_key(
    pocket_id: str,
    binding: str,
    key: str,
    ctx: RequestContext = Depends(request_context),
    _: object = Depends(_READ),
    __: object = Depends(_WRITE),
) -> KvKeyValue:
    """One key's value, metadata and expiry."""
    return await data_service.get_kv_key(
        workspace_id=ctx.workspace_id or "",
        user_id=ctx.user_id,
        pocket_id=pocket_id,
        binding=binding,
        key=key,
    )


@router.put(_BASE + "/kv/{binding}/keys/{key:path}", response_model=KvPutOut)
async def site_data_kv_put(
    pocket_id: str,
    binding: str,
    key: str,
    body: KvPutBody,
    ctx: RequestContext = Depends(request_context),
    _: object = Depends(_READ),
    __: object = Depends(_WRITE),
    ___: None = Depends(_kv_body_limit),
) -> KvPutOut:
    """Write a key's value, metadata and expiry together (live within about a minute)."""
    return await data_service.put_kv_key(
        workspace_id=ctx.workspace_id or "",
        user_id=ctx.user_id,
        pocket_id=pocket_id,
        binding=binding,
        key=key,
        value=body.value,
        metadata=body.metadata,
        expiration_ttl=body.expiration_ttl,
    )


@router.delete(_BASE + "/kv/{binding}/keys/{key:path}", status_code=204)
async def site_data_kv_delete(
    pocket_id: str,
    binding: str,
    key: str,
    ctx: RequestContext = Depends(request_context),
    _: object = Depends(_READ),
    __: object = Depends(_WRITE),
) -> Response:
    """Delete a key."""
    await data_service.delete_kv_key(
        workspace_id=ctx.workspace_id or "",
        user_id=ctx.user_id,
        pocket_id=pocket_id,
        binding=binding,
        key=key,
    )
    return Response(status_code=204)
