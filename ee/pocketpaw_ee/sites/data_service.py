# ee/pocketpaw_ee/sites/data_service.py — the one entry point of the Sites Data
# dashboard: which site, which lane, which stores; reads (inventory, schema, a page
# of rows), row writes, CSV import / export, point-in-time restore, and the site's
# R2 files and KV keys.
#
# ``resolve_target`` reads the pocket through the pockets service (NotFound /
# Forbidden for a missing or unreadable pocket, NotFound for another workspace's),
# picks the lane and the Site doc's D1 / KV / R2 ids and the site's name. Lanes:
#   * project — engine ``project`` with a database: tables come from introspecting
#     the live D1 (``site_data.d1``). No database at all is ``sites.not_dynamic``.
#   * dynamic — a ripple / svelte site with declared ``objects``: the spec is the
#     table whitelist, cross-checked against the live PRAGMA.
# Local mode answers ``available=false`` / ``live_on_cloudflare_only`` and a site with
# no database yet ``not_published``, with no Cloudflare call.
#
# Writes (rows and imports) go through ``_writable_table``: EDIT access to the pocket
# (403 ``pocket.access_denied``), a known table (404 before any row SQL), a reachable
# database (409 ``sites.data_unavailable``), an editable table (``sites.data_read_only``).
# Imports also refuse every login table. Each written row records one audit event
# (``site_data.row_*``, target ``<pocket>:<table>:<key>``, credentials removed), an
# import one ``site_data.import`` (counts and mapping, no values, target
# ``<pocket>:<table>``); an audit failure never fails the write. A bulk delete and an
# import take a Time Travel bookmark first and return it as ``restore_point``
# (null when the database has none). An import claims its upload (imports once) and
# reports what landed if it stops; export pages by key, credentials left out. One
# import or export runs per workspace at a time (429 ``sites.data_job_busy``).
#
# Restore (``restore_status`` / ``restore`` / ``restore_undo``) needs edit access
# here and ``sites.data_restore`` in the router; a ``production`` D1, a moment inside
# ``PAW_CF_D1_TT_DAYS`` (default 30), ``confirm_name`` equal to the site's name (case
# and spaces ignored), and at most 3 restores per database per 10 minutes. Each
# restore is audited (target ``<pocket>``) with its ``previous_bookmark`` and database
# id. Instead of ``at`` a restore can name a ``restore_point``: only one this
# site's own import / bulk delete event recorded for this database inside the
# window. Undo takes the confirm name too and only the newest restore event's bookmark,
# within 24 h and once (a ``budget.claim_once`` marker). A moment before the
# database's ``created_at`` or its history (Cloudflare 7400) is out of window. A
# restore drops every cache of the database.
#
# Files (R2) and key-values (KV): see the section header below. A store is named by a
# binding the Site doc records (else 404), files are typed so they can't run as a page
# (``site_data.files_kv``), credential-named KV keys read ``MASK``, and each write is
# audited under target type ``site_store`` (key, size, type; never contents). The
# inventory's connectors carry the provider's ``dashboard_url`` when it builds one.
#
# Every Cloudflare call goes through ``site_data.budget`` (429 ``sites.data_busy``)
# on the data-token client (``service._data_cf_client``); long imports and exports
# wait out their own workspace's spent minute a few times, never the global bucket
# (``_patient``).
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import tempfile
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from pocketpaw_ee.cloud._core.errors import (
    CloudError,
    Forbidden,
    NotFound,
    PayloadTooLarge,
    RateLimited,
    ValidationError,
)
from pocketpaw_ee.sites import service as sites_service
from pocketpaw_ee.sites.project_data import auth_table, masked_columns
from pocketpaw_ee.sites.site_data import budget, csv_io, d1, files_kv, writes
from pocketpaw_ee.sites.site_data.dto import (
    ColumnInfo,
    ConnectorRef,
    DataInventory,
    DeleteRowsOut,
    FileList,
    FileObject,
    FileObjectOut,
    HistoryEvent,
    ImportFailure,
    ImportPreview,
    ImportResult,
    KvKeyList,
    KvKeyRef,
    KvKeyValue,
    KvPutOut,
    RestoreOut,
    RestoreStatus,
    RowHistory,
    RowOut,
    RowPage,
    StoreRef,
    TableSchema,
    TableSummary,
)

logger = logging.getLogger(__name__)

_REASON_LOCAL = "live_on_cloudflare_only"
_REASON_NOT_PUBLISHED = "not_published"


@dataclass(frozen=True)
class DataTarget:
    pocket_id: str
    workspace_id: str
    lane: Literal["project", "dynamic"]
    d1_id: str  # "" before publish / legacy placeholder
    kv: dict[str, str] = field(default_factory=dict)  # binding -> namespace id
    r2: dict[str, str] = field(default_factory=dict)  # binding -> bucket name
    spec_objects: list[dict[str, Any]] = field(default_factory=list)  # dynamic lane only
    reason: str = ""  # why the database can't be read now; "" when it can
    site_name: str = ""  # the Site doc's name, else the pocket's (restore's confirm)

    def spec_object(self, table: str) -> dict[str, Any] | None:
        return next((o for o in self.spec_objects if o.get("name") == table), None)


async def resolve_target(
    *, workspace_id: str, user_id: str, pocket_id: str, _cloudflare: Any | None = None
) -> DataTarget:
    from pocketpaw_ee.cloud.pockets import service as pockets_service

    pocket = await pockets_service.get(pocket_id, user_id)
    owner_ws = pocket.get("workspace")
    if owner_ws and owner_ws != workspace_id:
        raise NotFound("pocket", pocket_id)

    objects: list[dict[str, Any]] = []
    if sites_service._is_project_pocket(pocket):
        lane: Literal["project", "dynamic"] = "project"
        d1_id = await sites_service._project_database_id(workspace_id, pocket_id, pocket)
        doc = await sites_service._canonical_site_doc(workspace_id, pocket_id)
    else:
        lane = "dynamic"
        _content, objects = sites_service._dynamic_objects_of(pocket)
        doc = await sites_service._canonical_site_doc(workspace_id, pocket_id)
        stored = ((getattr(doc, "d1_database_id", "") or "") if doc is not None else "").strip()
        # A dynamic site binds the stored id, else the derived one; no Site doc
        # means it was never published, so there is no database to read.
        d1_id = (
            ""
            if doc is None
            else (stored or sites_service._derive_d1_database_id(workspace_id, pocket_id))
        )

    if sites_service._local_mode() and _cloudflare is None:
        reason = _REASON_LOCAL
    elif not d1_id:
        reason = _REASON_NOT_PUBLISHED
    else:
        reason = ""
    return DataTarget(
        pocket_id=pocket_id,
        workspace_id=workspace_id,
        lane=lane,
        d1_id=d1_id,
        kv=dict(getattr(doc, "kv_namespaces", None) or {}),
        r2=dict(getattr(doc, "r2_buckets", None) or {}),
        spec_objects=objects,
        reason=reason,
        site_name=(getattr(doc, "name", "") or "").strip() or str(pocket.get("name") or "").strip(),
    )


def _client(target: DataTarget, injected: Any | None) -> budget.Budgeted:
    return budget.Budgeted(injected or sites_service._data_cf_client(), target.workspace_id)


def _spec_fields(obj: dict[str, Any] | None) -> dict[str, str]:
    fields = (obj or {}).get("fields")
    if not isinstance(fields, dict):
        return {}
    return {str(k): str(v) for k, v in fields.items()}


def _label(binding: str) -> str:
    words = binding.replace("_", " ").replace("-", " ").split()
    return " ".join(words).lower().capitalize() or binding


def _spec_only_schema(target: DataTarget, table: str) -> TableSchema:
    """A declared table read from the spec alone (unreachable or not yet created)."""
    obj = target.spec_object(table) or {}
    fields = _spec_fields(obj)
    pk = obj.get("primaryKey") if isinstance(obj.get("primaryKey"), str) else None
    hidden = masked_columns(table, list(fields))
    columns = [
        ColumnInfo(
            name=name,
            sql_type=kind_name.lower(),
            kind=d1.infer_kind(name, "", None, [], spec_type=kind_name),  # type: ignore[arg-type]
            nullable=name != pk,
            has_default=False,
            auto=False,
            primary_key=name == pk,
            masked=name in hidden,
            writable=False,
            references=None,
        )
        for name, kind_name in fields.items()
    ]
    available = not target.reason
    return TableSchema(
        pocket_id=target.pocket_id,
        name=table,
        available=available,
        reason=target.reason,
        columns=columns,
        key=pk or None,
        editable=False,
        read_only_reason="not_created" if available else "",
        row_count=0 if available else None,
    )


async def _connectors(workspace_id: str, user_id: str, pocket_id: str) -> list[ConnectorRef]:
    from pocketpaw_ee.integrations import bindings

    try:
        views = await bindings.list_for_pocket(
            workspace_id=workspace_id, user_id=user_id, pocket_id=pocket_id
        )
    except CloudError:
        return []  # listing bindings needs edit access; a reader just sees none
    except Exception:  # noqa: BLE001 — a connector read must not fail the inventory
        logger.warning("site_data: could not list connectors for %s", pocket_id, exc_info=True)
        return []
    return [
        ConnectorRef(
            provider=v.provider,
            status=str(getattr(v.status, "value", v.status)),
            dashboard_url=_dashboard_url(v.provider, getattr(v, "settings", None) or {}),
        )
        for v in views
    ]


def _dashboard_url(provider_id: str, settings: dict[str, Any]) -> str | None:
    """The provider's own page for this binding, from its visible settings (never a
    secret), or None when the provider builds none or the ids don't check out."""
    from pocketpaw_ee.integrations.providers import get_provider

    build = getattr(get_provider(provider_id), "dashboard_url", None)
    if build is None:
        return None
    try:
        url = build(settings)
    except Exception:  # noqa: BLE001 — a link is optional; never fail the inventory
        logger.warning("site_data: dashboard_url failed for %s", provider_id, exc_info=True)
        return None
    return url if isinstance(url, str) and url.startswith("https://") else None


async def get_inventory(
    *,
    workspace_id: str,
    user_id: str,
    pocket_id: str,
    can_write: bool = False,
    can_restore: bool = False,
    _cloudflare: Any | None = None,
) -> DataInventory:
    """Tables (with capped row counts), file and KV stores, connected sources, and
    what the caller may do. ``GET /sites/by-pocket/{pocket_id}/data/inventory``."""
    target = await resolve_target(
        workspace_id=workspace_id, user_id=user_id, pocket_id=pocket_id, _cloudflare=_cloudflare
    )
    tables: list[TableSummary] = []
    if target.reason:
        # Nothing to introspect; a dynamic site still shows what it declares.
        tables = [TableSummary(name=o["name"]) for o in target.spec_objects]
    else:
        cf = _client(target, _cloudflare)
        schema = await d1.introspect(cf, target.d1_id)
        if target.lane == "dynamic":
            names = [o["name"] for o in target.spec_objects]
        else:
            names = list(schema.tables)
        live = [n for n in names if n in schema.tables]
        counts = await d1.count_tables(cf, target.d1_id, schema, live)
        for name in names:
            t = schema.tables.get(name)
            if t is None:
                tables.append(TableSummary(name=name, row_count=0, read_only_reason="not_created"))
            else:
                tables.append(
                    TableSummary(
                        name=name,
                        row_count=counts.get(name),
                        editable=t.editable,
                        read_only_reason=t.read_only_reason,  # type: ignore[arg-type]
                    )
                )
    store_writable = can_write and not _store_reason(target)
    return DataInventory(
        pocket_id=pocket_id,
        available=not target.reason,
        reason=target.reason,
        tables=tables,
        files=[
            StoreRef(binding=b, label=_label(b), writable=store_writable) for b in sorted(target.r2)
        ],
        kv=[
            StoreRef(binding=b, label=_label(b), writable=store_writable) for b in sorted(target.kv)
        ],
        connectors=await _connectors(workspace_id, user_id, pocket_id),
        can_write=can_write,
        can_restore=can_restore,
    )


async def _live_table(target: DataTarget, table: str, cf: budget.Budgeted) -> d1.Table | None:
    """The introspected table, or None for a declared-but-missing dynamic table.
    A project table the database doesn't have is NotFound, before any row SQL (the
    caller has already refused a dynamic table the spec doesn't declare)."""
    schema = await d1.introspect(cf, target.d1_id)
    found = schema.tables.get(table)
    if found is None and target.lane == "project":
        raise NotFound("site_table", table)
    return found


async def get_table_schema(
    *,
    workspace_id: str,
    user_id: str,
    pocket_id: str,
    table: str,
    _cloudflare: Any | None = None,
) -> TableSchema:
    """One table's columns (with kinds sampled from its first page), key, edit
    state and capped row count. ``GET .../data/tables/{table}``."""
    target = await resolve_target(
        workspace_id=workspace_id, user_id=user_id, pocket_id=pocket_id, _cloudflare=_cloudflare
    )
    if target.lane == "dynamic" and target.spec_object(table) is None:
        raise NotFound("site_table", table)
    if target.reason:
        if target.lane == "dynamic":
            return _spec_only_schema(target, table)
        return TableSchema(pocket_id=pocket_id, name=table, available=False, reason=target.reason)
    cf = _client(target, _cloudflare)
    t = await _live_table(target, table, cf)
    if t is None:
        return _spec_only_schema(target, table)
    fields = _spec_fields(target.spec_object(table))
    query = d1.build_page_query(t, spec_fields=fields)
    raw, _rows, total = await d1.read_page(cf, target.d1_id, t, query)
    return TableSchema(
        pocket_id=pocket_id,
        name=table,
        available=True,
        columns=d1.column_infos(t, raw, fields),
        key=t.key,
        editable=t.editable,
        insertable=d1.insertable(t),
        read_only_reason=t.read_only_reason,  # type: ignore[arg-type]
        row_count=total,
    )


async def get_rows(
    *,
    workspace_id: str,
    user_id: str,
    pocket_id: str,
    table: str,
    q: str | None = None,
    sort: str | None = None,
    dir: str | None = None,  # noqa: A002 — the query parameter's name
    f: str | None = None,
    limit: int | None = None,
    offset: int | None = 0,
    _cloudflare: Any | None = None,
) -> RowPage:
    """One page of rows, filtered, searched and sorted, each carrying ``__key``, with
    a total capped at 10,000 (``None`` above). ``GET .../data/tables/{table}/rows``."""
    filters = d1.parse_filters(f)
    target = await resolve_target(
        workspace_id=workspace_id, user_id=user_id, pocket_id=pocket_id, _cloudflare=_cloudflare
    )
    if target.lane == "dynamic" and target.spec_object(table) is None:
        raise NotFound("site_table", table)
    page_limit = d1.LIMIT_DEFAULT if limit is None else limit
    if target.reason:
        return RowPage(
            pocket_id=pocket_id,
            table=table,
            available=False,
            reason=target.reason,
            total=None,
            limit=page_limit,
            offset=offset or 0,
        )
    cf = _client(target, _cloudflare)
    t = await _live_table(target, table, cf)
    if t is None:  # declared, not created yet: nothing to read
        return RowPage(
            pocket_id=pocket_id,
            table=table,
            available=True,
            columns=list(_spec_fields(target.spec_object(table))),
            total=0,
            limit=page_limit,
            offset=offset or 0,
        )
    query = d1.build_page_query(
        t,
        q=q,
        sort=sort,
        direction=dir,
        filters=filters,
        limit=limit,
        offset=offset,
        spec_fields=_spec_fields(target.spec_object(table)),
    )
    _raw, rows, total = await d1.read_page(cf, target.d1_id, t, query)
    return RowPage(
        pocket_id=pocket_id,
        table=table,
        available=True,
        columns=query.columns,
        rows=rows,
        total=total,
        limit=query.limit,
        offset=query.offset,
        search_truncated=query.search_truncated,
    )


# --------------------------------------------------------------------------- writes

_AUDIT_TARGET = "site_data"
_AUDIT_CONCURRENCY = 25


@dataclass(frozen=True)
class _Writable:
    target: DataTarget
    cf: budget.Budgeted
    schema: d1.Schema
    table: d1.Table
    kinds: dict[str, str]


async def _editable_target(
    *, workspace_id: str, user_id: str, pocket_id: str, _cloudflare: Any | None
) -> DataTarget:
    """The site, or 403 ``pocket.access_denied`` when the caller can read the pocket
    but not edit it (every write and restore needs edit access)."""
    from pocketpaw_ee.cloud.pockets import service as pockets_service

    target = await resolve_target(
        workspace_id=workspace_id, user_id=user_id, pocket_id=pocket_id, _cloudflare=_cloudflare
    )
    if not await pockets_service.has_edit_access(pocket_id, user_id):
        raise Forbidden("pocket.access_denied", "You don't have edit access to this site.")
    return target


async def _writable_table(
    *, workspace_id: str, user_id: str, pocket_id: str, table: str, _cloudflare: Any | None
) -> _Writable:
    """The site and the editable live table a write addresses, or the refusal: no
    edit access to the pocket 403, an unknown table 404, an unreachable database
    409, a read-only table."""
    target = await _editable_target(
        workspace_id=workspace_id, user_id=user_id, pocket_id=pocket_id, _cloudflare=_cloudflare
    )
    if target.lane == "dynamic" and target.spec_object(table) is None:
        raise NotFound("site_table", table)
    if target.reason:
        raise writes.unavailable(target.reason)
    cf = _client(target, _cloudflare)
    schema = await d1.introspect(cf, target.d1_id)
    t = schema.tables.get(table)
    if t is None:
        if target.lane == "project":
            raise NotFound("site_table", table)
        raise writes.read_only("not_created")
    if not t.editable:
        raise writes.read_only(t.read_only_reason)
    kinds = writes.kinds_of(t, _spec_fields(target.spec_object(table)))
    return _Writable(target=target, cf=cf, schema=schema, table=t, kinds=kinds)


def _masked(table: d1.Table, row: dict[str, Any]) -> dict[str, Any]:
    return d1.mask_page(table, [row])[0]


async def _audit(
    w: _Writable,
    user_id: str,
    action: str,
    key: Any,
    *,
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
    extra: dict[str, Any] | None = None,
) -> None:
    from pocketpaw_ee.cloud.audit import service as audit_service

    try:
        await audit_service.record(
            w.target.workspace_id,
            user_id,
            action,
            target_type=_AUDIT_TARGET,
            target_id=f"{w.target.pocket_id}:{w.table.name}:{writes.target_key(key)}",
            metadata={
                "table": w.table.name,
                "keys": [key],
                "before": writes.audit_row(w.table, before),
                "after": writes.audit_row(w.table, after),
                **(extra or {}),
            },
        )
    except Exception:  # noqa: BLE001 — the write landed; a lost audit row must not undo it
        logger.warning("site_data: audit failed for %s on %s", action, w.table.name, exc_info=True)


async def insert_row(
    *,
    workspace_id: str,
    user_id: str,
    pocket_id: str,
    table: str,
    values: dict[str, Any],
    _cloudflare: Any | None = None,
) -> RowOut:
    """Add one row. ``POST .../data/tables/{table}/rows``."""
    w = await _writable_table(
        workspace_id=workspace_id,
        user_id=user_id,
        pocket_id=pocket_id,
        table=table,
        _cloudflare=_cloudflare,
    )
    if not d1.insertable(w.table):
        raise writes.read_only("login_table")
    row = await writes.insert(w.cf, w.target.d1_id, w.table, w.kinds, values)
    d1.forget_counts(w.target.d1_id)
    await _audit(w, user_id, "site_data.row_insert", row.get("__key"), before=None, after=row)
    return RowOut(row=_masked(w.table, row))


async def update_row(
    *,
    workspace_id: str,
    user_id: str,
    pocket_id: str,
    table: str,
    key: Any,
    set: dict[str, Any],  # noqa: A002 — the wire name
    expected: dict[str, Any],
    _cloudflare: Any | None = None,
) -> RowOut:
    """Change one row if it still holds ``expected``.
    ``PATCH .../data/tables/{table}/rows/{key}``."""
    w = await _writable_table(
        workspace_id=workspace_id,
        user_id=user_id,
        pocket_id=pocket_id,
        table=table,
        _cloudflare=_cloudflare,
    )
    row_key = writes.key_value(w.table, w.kinds, key)
    before, after = await writes.update(
        w.cf,
        w.target.d1_id,
        w.table,
        w.kinds,
        row_key,
        set,
        expected,
        mask=lambda r: _masked(w.table, r),
    )
    await _audit(w, user_id, "site_data.row_update", row_key, before=before, after=after)
    return RowOut(row=_masked(w.table, after))


async def delete_rows(
    *,
    workspace_id: str,
    user_id: str,
    pocket_id: str,
    table: str,
    keys: list[Any],
    _cloudflare: Any | None = None,
) -> DeleteRowsOut:
    """Delete up to 500 rows by key, atomically (a user's sessions and accounts
    with them). ``POST .../data/tables/{table}/rows:delete``."""
    w = await _writable_table(
        workspace_id=workspace_id,
        user_id=user_id,
        pocket_id=pocket_id,
        table=table,
        _cloudflare=_cloudflare,
    )
    row_keys = writes.delete_keys(w.table, w.kinds, keys)
    # One row is undone by re-inserting it; a bulk delete gets a restore point.
    restore_point = await _restore_point(w) if len(row_keys) > 1 else None
    deleted, cascade = await writes.delete(w.cf, w.target.d1_id, w.schema, w.table, row_keys)
    d1.forget_counts(w.target.d1_id)
    gone = {r.get("__key") for r in deleted}
    slots = asyncio.Semaphore(_AUDIT_CONCURRENCY)

    async def _one(row: dict[str, Any]) -> None:
        async with slots:
            key = row.get("__key")
            extra: dict[str, Any] = {"cascade": cascade[key]} if key in cascade else {}
            if restore_point:
                extra["restore_point"] = restore_point
                extra["database_id"] = w.target.d1_id
            await _audit(
                w, user_id, "site_data.row_delete", key, before=row, after=None, extra=extra
            )

    await asyncio.gather(*(_one(r) for r in deleted))
    return DeleteRowsOut(
        deleted=len(deleted),
        keys=[k for k in row_keys if k in gone],
        restore_point=restore_point,
    )


async def duplicate_row(
    *,
    workspace_id: str,
    user_id: str,
    pocket_id: str,
    table: str,
    key: Any,
    values: dict[str, Any] | None = None,
    _cloudflare: Any | None = None,
) -> RowOut:
    """Copy one row into a new one. ``POST .../data/tables/{table}/rows:duplicate``."""
    w = await _writable_table(
        workspace_id=workspace_id,
        user_id=user_id,
        pocket_id=pocket_id,
        table=table,
        _cloudflare=_cloudflare,
    )
    if not d1.insertable(w.table):
        raise writes.read_only("login_table")
    source = writes.key_value(w.table, w.kinds, key)
    row = await writes.duplicate(w.cf, w.target.d1_id, w.table, w.kinds, source, values or {})
    d1.forget_counts(w.target.d1_id)
    await _audit(
        w,
        user_id,
        "site_data.row_insert",
        row.get("__key"),
        before=None,
        after=row,
        extra={"duplicate_of": source},
    )
    return RowOut(row=_masked(w.table, row))


async def row_history(
    *,
    workspace_id: str,
    user_id: str,
    pocket_id: str,
    table: str,
    key: str,
    _cloudflare: Any | None = None,
) -> RowHistory:
    """The dashboard's changes to one row, newest first, at most 50 (visitors'
    writes are not audited). ``GET .../data/tables/{table}/rows/{key}/history``."""
    from pocketpaw_ee.cloud.audit import service as audit_service

    target = await resolve_target(
        workspace_id=workspace_id, user_id=user_id, pocket_id=pocket_id, _cloudflare=_cloudflare
    )
    if target.lane == "dynamic" and target.spec_object(table) is None:
        raise NotFound("site_table", table)
    key_text = key
    if not target.reason:
        cf = _client(target, _cloudflare)
        t = await _live_table(target, table, cf)
        if t is not None and t.key:
            kinds = writes.kinds_of(t, _spec_fields(target.spec_object(table)))
            key_text = writes.target_key(writes.key_value(t, kinds, key))
    events = await audit_service.list_target_events(
        target.workspace_id,
        target_type=_AUDIT_TARGET,
        target_id=f"{pocket_id}:{table}:{key_text}",
        limit=50,
    )
    events = [e for e in events if (e.metadata or {}).get("table") == table]
    try:
        names = await audit_service.actor_names(
            target.workspace_id, sorted({e.actor_id for e in events})
        )
    except Exception:  # noqa: BLE001 — names are a nicety; the history still reads
        logger.warning("site_data: could not resolve history actor names", exc_info=True)
        names = {}
    out: list[HistoryEvent] = []
    for e in events:
        meta = e.metadata or {}
        at = e.at if e.at.tzinfo is not None else e.at.replace(tzinfo=UTC)
        before, after = meta.get("before"), meta.get("after")
        out.append(
            HistoryEvent(
                action=e.action,
                actor_id=e.actor_id,
                actor_name=names.get(e.actor_id),
                at=at.isoformat(),
                before=d1.json_safe_row(before) if isinstance(before, dict) else None,
                after=d1.json_safe_row(after) if isinstance(after, dict) else None,
            )
        )
    return RowHistory(events=out)


# --------------------------------------------------------------------------- restore points

_BUSY_RETRIES = 3
_BUSY_MAX_SLEEP_S = 30
_JOB_TTL_S = 15 * 60  # a crashed job's slot frees itself


async def _restore_point(w: _Writable) -> str | None:
    """The database's current bookmark, taken before a bulk write, or None when it
    has no Time Travel (an alpha database) or Cloudflare can't answer. A spent
    budget still refuses the write: nothing has been written yet."""
    try:
        return await w.cf.d1_bookmark(w.target.d1_id)
    except budget.DataBusy:
        raise
    except CloudError:
        logger.warning("site_data: no restore point for %s", w.target.pocket_id, exc_info=True)
        return None


async def _patient(call: Any) -> Any:
    """Await ``call()``, waiting out a spent data budget a few times (a long import
    or export spans more calls than one minute's workspace budget)."""
    for attempt in range(_BUSY_RETRIES + 1):
        try:
            return await call()
        except budget.DataBusy as exc:
            # Only this workspace's own minute is worth waiting out; a spent global
            # bucket is everyone's, so a long job gives way at once.
            if attempt == _BUSY_RETRIES or exc.scope != "workspace":
                raise
            wait = int((exc.details or {}).get("retry_after") or 1)
            await asyncio.sleep(min(max(wait, 1), _BUSY_MAX_SLEEP_S))
    raise AssertionError("unreachable")  # pragma: no cover


# --------------------------------------------------------------------------- CSV import


async def _importable(
    *, workspace_id: str, user_id: str, pocket_id: str, table: str, _cloudflare: Any | None
) -> _Writable:
    """``_writable_table``, and never a login table: sign-in data isn't imported."""
    w = await _writable_table(
        workspace_id=workspace_id,
        user_id=user_id,
        pocket_id=pocket_id,
        table=table,
        _cloudflare=_cloudflare,
    )
    if auth_table(w.table.name) or not d1.insertable(w.table):
        raise writes.read_only("login_table")
    return w


async def _formats(w: _Writable, columns: list[str]) -> dict[str, tuple[str, str | None]]:
    return await writes.time_formats(w.cf, w.target.d1_id, w.table, w.kinds, columns)


async def import_preview(
    *,
    workspace_id: str,
    user_id: str,
    pocket_id: str,
    table: str,
    filename: str,
    data: bytes,
    _cloudflare: Any | None = None,
) -> ImportPreview:
    """Read an uploaded CSV, keep it 15 minutes, and say how it would import: the
    first 20 rows, an auto-matched mapping and the cell errors under it. Writes
    nothing. ``POST .../data/tables/{table}/import:preview``."""
    w = await _importable(
        workspace_id=workspace_id,
        user_id=user_id,
        pocket_id=pocket_id,
        table=table,
        _cloudflare=_cloudflare,
    )
    text = csv_io.decode(data)
    parsed = csv_io.parse(text)
    targets = csv_io.targets(w.table, w.kinds)
    auto = csv_io.auto_mapping(parsed.header, [t.name for t in targets])
    mapped = {src: dest for src, dest in auto.items() if dest is not None}
    formats = await _formats(w, list(mapped.values()))
    _rows, errors, bad = csv_io.check_rows(w.table, w.kinds, parsed, mapped, formats)
    name = (filename or "upload.csv")[:200]
    upload_id = await csv_io.save_upload(
        workspace_id=w.target.workspace_id,
        pocket_id=pocket_id,
        user_id=user_id,
        table=w.table.name,
        filename=name,
        text=text,
    )
    return ImportPreview(
        pocket_id=pocket_id,
        table=w.table.name,
        upload_id=upload_id,
        expires_in=csv_io.UPLOAD_TTL_S,
        filename=name,
        header=parsed.header,
        rows=[cells for _n, cells in parsed.rows[: csv_io.PREVIEW_ROWS]],
        total_rows=len(parsed.rows),
        mapping=auto,
        targets=targets,
        key=csv_io.upsert_key(w.table),
        errors=errors,
        error_count=bad,
    )


_BATCH_REFUSED = (
    "The database refused these rows: a value may already be taken, or point at a row "
    "that doesn't exist."
)


@dataclass
class _Written:
    inserted: int = 0
    updated: int = 0
    failed: dict[int, str] = field(default_factory=dict)
    failed_count: int = 0
    stopped: bool = False

    def fail(self, rows: Any, message: str) -> None:
        for row in rows:
            self.failed.setdefault(row.number, message)
            self.failed_count += 1


async def _write_rows(w: _Writable, rows: list[csv_io.Row], mode: str) -> _Written:
    """Write ``rows`` in D1 batches. A batch the database refuses fails its rows and
    the rest go on; anything else (a spent budget, a dropped connection) stops the
    import there and fails every row not yet sent."""
    out = _Written()
    key = csv_io.upsert_key(w.table) if mode == "upsert" else None
    seen: set[Any] = set()
    pending = csv_io.batches(csv_io.plan(w.table, rows, mode))
    for index, batch in enumerate(pending):
        statements = [st for chunk in batch for st in chunk.statements]
        try:
            results = await _patient(
                lambda st=statements: w.cf.query_d1_batch(database_id=w.target.d1_id, statements=st)
            )
        except ValidationError as exc:
            if exc.code != "sites.cloudflare_error":
                raise
            out.fail((r for chunk in batch for r in chunk.rows), _BATCH_REFUSED)
            continue
        except Exception as exc:  # noqa: BLE001 — report what landed rather than a 500
            busy = isinstance(exc, budget.DataBusy)
            if not busy:
                logger.warning("site_data: import into %s stopped", w.table.name, exc_info=True)
            message = (
                "Not imported: the site's data is busy."
                if busy
                else "Not imported: the import stopped."
            )
            out.fail((r for rest in pending[index:] for chunk in rest for r in chunk.rows), message)
            out.stopped = True
            break
        at = 0
        for chunk in batch:
            existing: set[Any] = set()
            if len(chunk.statements) == 2:
                existing = {r.get("__key") for r in results[at]}
            at += len(chunk.statements)
            for row in chunk.rows:
                k = row.values.get(key) if key else None
                if k is not None and (k in existing or k in seen):
                    out.updated += 1
                else:
                    out.inserted += 1
                if k is not None:
                    seen.add(k)
    return out


async def _claim_job(workspace_id: str) -> str:
    """The workspace's one import / export slot, or 429 ``sites.data_job_busy``."""
    slot = f"site_data:job:{workspace_id or '-'}"
    if not await budget.claim_once(slot, _JOB_TTL_S):
        raise RateLimited(
            "sites.data_job_busy",
            "Another import or export is running for this workspace. Try again when it ends.",
        )
    return slot


async def import_commit(
    *,
    workspace_id: str,
    user_id: str,
    pocket_id: str,
    table: str,
    upload_id: str,
    mapping: dict[str, str | None],
    mode: str = "insert",
    _cloudflare: Any | None = None,
) -> ImportResult:
    """Write a previewed upload's rows under ``mapping``: ``insert`` adds them,
    ``upsert`` updates the rows whose key matches and adds the rest. Each D1 batch
    (at most 50 statements) lands whole or not at all. The upload is claimed first,
    so it imports once; it is put back only when the commit is refused before any
    write. A restore point is taken before the first write. One import or export
    runs per workspace at a time. ``POST .../data/tables/{table}/import``."""
    w = await _importable(
        workspace_id=workspace_id,
        user_id=user_id,
        pocket_id=pocket_id,
        table=table,
        _cloudflare=_cloudflare,
    )
    slot = await _claim_job(w.target.workspace_id)
    try:
        claimed = await csv_io.claim_upload(
            upload_id,
            workspace_id=w.target.workspace_id,
            pocket_id=pocket_id,
            user_id=user_id,
            table=w.table.name,
        )
        try:
            parsed = csv_io.parse(claimed.text)
            mapped = csv_io.check_mapping(w.table, w.kinds, parsed.header, mapping, mode)
            formats = await _formats(w, list(mapped.values()))
            rows, errors, bad = csv_io.check_rows(w.table, w.kinds, parsed, mapped, formats)
            restore_point = await _restore_point(w) if rows else None
        except CloudError:
            await csv_io.put_back(claimed)  # nothing was written: the owner can fix and retry
            raise
        written = await _write_rows(w, rows, mode)
    finally:
        await budget.release(slot)

    failed = dict(written.failed)
    for e in errors:
        failed.setdefault(e.row, f"{e.column}: {e.error}" if e.column else e.error)
    failed_count = bad + written.failed_count
    d1.forget_counts(w.target.d1_id)
    await _audit_event(
        w.target,
        user_id,
        "site_data.import",
        f"{pocket_id}:{w.table.name}",
        {
            "table": w.table.name,
            "mode": mode,
            "filename": claimed.filename,
            "mapping": mapped,
            "rows": len(parsed.rows),
            "inserted": written.inserted,
            "updated": written.updated,
            "failed": failed_count,
            "stopped": written.stopped,
            "restore_point": restore_point,
            "database_id": w.target.d1_id,
        },
    )
    return ImportResult(
        inserted=written.inserted,
        updated=written.updated,
        failed=[ImportFailure(row=n, error=m) for n, m in sorted(failed.items())][
            : csv_io.MAX_ERRORS
        ],
        failed_count=failed_count,
        stopped=written.stopped,
        restore_point=restore_point,
    )


async def _audit_event(
    target: DataTarget, user_id: str, action: str, target_id: str, metadata: dict[str, Any]
) -> None:
    from pocketpaw_ee.cloud.audit import service as audit_service

    try:
        await audit_service.record(
            target.workspace_id,
            user_id,
            action,
            target_type=_AUDIT_TARGET,
            target_id=target_id,
            metadata=metadata,
        )
    except Exception:  # noqa: BLE001 — the change landed; a lost audit row must not undo it
        logger.warning("site_data: audit failed for %s", action, exc_info=True)


# --------------------------------------------------------------------------- CSV export


async def export_csv(
    *,
    workspace_id: str,
    user_id: str,
    pocket_id: str,
    table: str,
    q: str | None = None,
    sort: str | None = None,
    dir: str | None = None,  # noqa: A002 — the query parameter's name
    f: str | None = None,
    _cloudflare: Any | None = None,
) -> tuple[str, AsyncIterator[bytes]]:
    """``(filename, body)``: the rows under the search, filters and sort as CSV,
    500 per D1 read, credential columns left out: at most 100,000 rows paged by key
    in the default order, 20,000 under an owner-chosen sort. Holds the workspace's
    one import / export slot until the body is done. The first page is
    read before this returns, so a bad query or a busy budget is a clean error.
    ``GET .../data/tables/{table}/export.csv``."""
    filters = d1.parse_filters(f)
    target = await resolve_target(
        workspace_id=workspace_id, user_id=user_id, pocket_id=pocket_id, _cloudflare=_cloudflare
    )
    if target.lane == "dynamic" and target.spec_object(table) is None:
        raise NotFound("site_table", table)
    if target.reason:
        raise writes.unavailable(target.reason)
    cf = _client(target, _cloudflare)
    t = await _live_table(target, table, cf)
    filename = csv_io.export_filename(table, datetime.now(UTC).date())
    if t is None:  # declared, not created yet: a header and no rows
        names = list(_spec_fields(target.spec_object(table)))
        hidden = masked_columns(table, names)
        header = csv_io.header_line([n for n in names if n not in hidden]).encode("utf-8")

        async def _empty() -> AsyncIterator[bytes]:
            yield header

        return filename, _empty()

    fields = _spec_fields(target.spec_object(table))
    columns = csv_io.export_columns(t)
    # Page by key (WHERE key < last) when the owner kept the default order: every
    # page costs the same. An owner-chosen sort pages by OFFSET, capped lower.
    key_col = t.column(t.key) if t.key and t.key != "rowid" else None
    keyset = (
        not sort
        and t.key_sql is not None
        and (t.key == "rowid" or t.key == t.rowid_alias or bool(key_col and key_col.notnull))
    )
    cap = csv_io.EXPORT_MAX_ROWS if keyset else csv_io.EXPORT_MAX_SORTED_ROWS
    page_size = csv_io.EXPORT_PAGE

    def _query(offset: int, after: Any) -> d1.PageQuery:
        return d1.build_page_query(
            t,
            q=q,
            sort=sort,
            direction=dir,
            filters=filters,
            limit=page_size,
            offset=offset,
            spec_fields=fields,
            max_limit=page_size,
            keyset=keyset,
            after_key=after,
        )

    async def _page(offset: int, after: Any) -> list[dict[str, Any]]:
        query = _query(offset, after)
        return await _patient(
            lambda: cf.query_d1(database_id=target.d1_id, sql=query.sql, params=query.params)
        )

    _query(0, None)  # a bad sort or filter is a 422 before the slot is taken
    slot = await _claim_job(target.workspace_id)
    try:
        first = await _page(0, None)
    except BaseException:
        await budget.release(slot)
        raise

    async def _body() -> AsyncIterator[bytes]:
        try:
            yield csv_io.header_line(columns).encode("utf-8")
            rows, offset, sent = first, 0, 0
            while True:
                chunk = rows[: cap - sent]
                if chunk:
                    yield csv_io.rows_text(chunk, columns).encode("utf-8")
                sent += len(chunk)
                if len(rows) < page_size or sent >= cap:
                    return
                offset += page_size
                rows = await _page(0 if keyset else offset, rows[-1].get("__key"))
        finally:
            await budget.release(slot)

    return filename, _body()


# --------------------------------------------------------------------------- restore

_TT_DAYS_ENV = "PAW_CF_D1_TT_DAYS"
_TT_DAYS_DEFAULT = 30
_CLOCK_SKEW = timedelta(minutes=1)
_BOOKMARK = re.compile(r"^[A-Za-z0-9-]{1,200}$")
_RESTORE_ACTIONS = ["site_data.restore", "site_data.restore_undo"]
_REASON_NOT_PRODUCTION = "not_production"
_UNDO_WINDOW = timedelta(hours=24)


def window_days() -> int:
    """How far back Time Travel reaches: ``PAW_CF_D1_TT_DAYS`` (30 on a paid
    account, 7 on free), clamped to 1-30."""
    raw = (os.environ.get(_TT_DAYS_ENV) or "").strip()
    try:
        days = int(raw) if raw else _TT_DAYS_DEFAULT
    except ValueError:
        logger.warning("%s=%r is not an int; using %d", _TT_DAYS_ENV, raw, _TT_DAYS_DEFAULT)
        days = _TT_DAYS_DEFAULT
    return max(1, min(days, 30))


def _restore_error(code: str, message: str) -> ValidationError:
    return ValidationError(f"sites.data_restore_{code}", message)


def _moment(at: str, now: datetime, days: int) -> datetime:
    """``at`` as a UTC moment inside the Time Travel window, or a 422."""
    try:
        text = at.strip()
        moment = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text)
        moment = moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)
    except (AttributeError, ValueError, OverflowError):
        # OverflowError: year 1 or 9999 with an offset falls off datetime's range.
        raise _restore_error("bad_time", "Pick a time like 2026-10-27T15:30:00Z.") from None
    if moment < now - timedelta(days=days) or moment > now + _CLOCK_SKEW:
        raise _restore_error("out_of_window", f"Pick a time within the last {days} days.")
    return moment


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _created(info: dict[str, Any]) -> datetime | None:
    """When the database was created (``d1_info``'s ``created_at``), if it says."""
    raw = info.get("created_at")
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip()
    try:
        moment = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text)
        return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)
    except (ValueError, OverflowError):
        return None


def _not_before_creation(moment: datetime, created: datetime | None) -> None:
    if created is not None and moment < created:
        raise _restore_error("out_of_window", "That time is before this database was created.")


async def _database_state(
    target: DataTarget, cf: budget.Budgeted | None
) -> tuple[str, datetime | None]:
    """``(why it can't be restored, or ""; when it was created)``."""
    if target.reason or cf is None:
        return target.reason or _REASON_NOT_PUBLISHED, None
    info = (await cf.d1_info(target.d1_id)) or {}
    reason = "" if info.get("version") == "production" else _REASON_NOT_PRODUCTION
    return reason, _created(info)


_RESTORE_POINT_ACTIONS = ["site_data.import", "site_data.row_delete"]


def _bad_request() -> ValidationError:
    return _restore_error("bad_request", "Send either at or restore_point.")


async def _recorded_restore_point(
    target: DataTarget, restore_point: str, now: datetime, days: int
) -> datetime:
    """When this site's own import or bulk delete recorded ``restore_point`` for its
    current database, inside the Time Travel window; else 422
    ``sites.data_restore_unknown_bookmark``. The bookmark shape is checked before
    it reaches the audit query."""
    from pocketpaw_ee.cloud.audit import service as audit_service

    unknown = _restore_error("unknown_bookmark", "That isn't a restore point of this site.")
    if not isinstance(restore_point, str) or not _BOOKMARK.match(restore_point):
        raise unknown
    if target.reason:
        raise writes.unavailable(target.reason)
    event = await audit_service.find_target_event(
        target.workspace_id,
        target_type=_AUDIT_TARGET,
        target_root=target.pocket_id,
        actions=_RESTORE_POINT_ACTIONS,
        metadata={"restore_point": restore_point, "database_id": target.d1_id},
    )
    if event is None:
        raise unknown
    at = event.at if event.at.tzinfo is not None else event.at.replace(tzinfo=UTC)
    if at < now - timedelta(days=days):
        raise unknown
    return at


def _same_name(given: str, expected: str) -> bool:
    return bool(expected) and given.strip().casefold() == expected.strip().casefold()


async def restore_status(
    *,
    workspace_id: str,
    user_id: str,
    pocket_id: str,
    at: str | None = None,
    restore_point: str | None = None,
    _cloudflare: Any | None = None,
) -> RestoreStatus:
    """Whether the site's database can be restored, how far back, and (for ``at``,
    or a ``restore_point`` this site's import / bulk delete recorded) the bookmark it
    would go to and how many dashboard changes came since.
    ``GET .../data/restore?at=<iso>`` or ``?restore_point=<bookmark>``."""
    from pocketpaw_ee.cloud.audit import service as audit_service

    if at is not None and restore_point is not None:
        raise _bad_request()
    target = await _editable_target(
        workspace_id=workspace_id, user_id=user_id, pocket_id=pocket_id, _cloudflare=_cloudflare
    )
    days = window_days()
    now = datetime.now(UTC)
    moment = _moment(at, now, days) if at else None
    if restore_point is not None:
        moment = await _recorded_restore_point(target, restore_point, now, days)
    cf = None if target.reason else _client(target, _cloudflare)
    reason, created = await _database_state(target, cf)
    earliest = now - timedelta(days=days)
    if created is not None and created > earliest:
        earliest = created
    if moment is not None and not reason:
        _not_before_creation(moment, created)
    status = RestoreStatus(
        pocket_id=pocket_id,
        available=not reason,
        reason=reason,
        site_name=target.site_name,
        window_days=days,
        earliest=earliest.isoformat(),
        at=moment.isoformat() if moment else None,
    )
    if reason or cf is None or moment is None:
        return status
    bookmark = restore_point or await cf.d1_bookmark(target.d1_id, _iso(moment))
    changes = await audit_service.count_target_events(
        target.workspace_id,
        target_type=_AUDIT_TARGET,
        target_root=pocket_id,
        since=moment,
        action_prefix="site_data.",
    )
    return status.model_copy(update={"bookmark": bookmark, "changes_since": changes})


async def _do_restore(
    target: DataTarget,
    cf: budget.Budgeted,
    user_id: str,
    action: str,
    *,
    bookmark: str | None = None,
    timestamp: str | None = None,
    meta_extra: dict[str, Any] | None = None,
) -> RestoreOut:
    await budget.acquire_restore(target.d1_id)
    result = await cf.d1_restore(target.d1_id, bookmark=bookmark, timestamp=timestamp)
    d1.forget_database(target.d1_id)
    out = RestoreOut(
        bookmark=str(result.get("bookmark") or ""),
        previous_bookmark=str(result["previous_bookmark"]),
    )
    meta: dict[str, Any] = {
        "database_id": target.d1_id,
        "bookmark": out.bookmark,
        "previous_bookmark": out.previous_bookmark,
    }
    if timestamp:
        meta["at"] = timestamp
    meta.update(meta_extra or {})
    await _audit_event(target, user_id, action, target.pocket_id, meta)
    return out


async def restore(
    *,
    workspace_id: str,
    user_id: str,
    pocket_id: str,
    confirm_name: str,
    at: str | None = None,
    restore_point: str | None = None,
    _cloudflare: Any | None = None,
) -> RestoreOut:
    """Put the site's whole database back the way it was at ``at``, or at a
    ``restore_point`` this site's own import / bulk delete recorded (exactly one;
    422 ``sites.data_restore_bad_request`` otherwise), in place. ``confirm_name``
    must be the site's name. Returns the ``previous_bookmark`` that undoes it.
    ``POST .../data/restore``."""
    if (at is None) == (restore_point is None):
        raise _bad_request()
    target = await _editable_target(
        workspace_id=workspace_id, user_id=user_id, pocket_id=pocket_id, _cloudflare=_cloudflare
    )
    if not _same_name(confirm_name, target.site_name):
        raise _restore_error("confirm_mismatch", "Type the site's name to confirm the restore.")
    now = datetime.now(UTC)
    if restore_point is not None:
        moment = await _recorded_restore_point(target, restore_point, now, window_days())
    else:
        moment = _moment(at or "", now, window_days())
    cf = None if target.reason else _client(target, _cloudflare)
    reason, created = await _database_state(target, cf)
    if reason or cf is None:
        raise writes.unavailable(reason or _REASON_NOT_PUBLISHED)
    if restore_point is not None:
        return await _do_restore(
            target,
            cf,
            user_id,
            "site_data.restore",
            bookmark=restore_point,
            meta_extra={"restore_point": restore_point, "at": moment.isoformat()},
        )
    _not_before_creation(moment, created)
    return await _do_restore(target, cf, user_id, "site_data.restore", timestamp=_iso(moment))


async def restore_undo(
    *,
    workspace_id: str,
    user_id: str,
    pocket_id: str,
    previous_bookmark: str,
    confirm_name: str,
    _cloudflare: Any | None = None,
) -> RestoreOut:
    """Undo the NEWEST restore (or undo) of this site's database, within 24 hours of
    it and only once: ``previous_bookmark`` must be the one that event recorded, and
    ``confirm_name`` the site's name. ``POST .../data/restore:undo``."""
    from pocketpaw_ee.cloud.audit import service as audit_service

    target = await _editable_target(
        workspace_id=workspace_id, user_id=user_id, pocket_id=pocket_id, _cloudflare=_cloudflare
    )
    if not _same_name(confirm_name, target.site_name):
        raise _restore_error("confirm_mismatch", "Type the site's name to confirm the undo.")
    unknown = _restore_error("unknown_bookmark", "That isn't a restore of this site to undo.")
    if not isinstance(previous_bookmark, str) or not _BOOKMARK.match(previous_bookmark):
        raise unknown
    if target.reason:
        raise writes.unavailable(target.reason)
    newest = await audit_service.find_target_event(
        target.workspace_id,
        target_type=_AUDIT_TARGET,
        target_id=pocket_id,
        actions=_RESTORE_ACTIONS,
        metadata={"database_id": target.d1_id},
    )
    if newest is None or (newest.metadata or {}).get("previous_bookmark") != previous_bookmark:
        raise unknown
    at = newest.at if newest.at.tzinfo is not None else newest.at.replace(tzinfo=UTC)
    if datetime.now(UTC) - at > _UNDO_WINDOW:
        raise unknown
    claim = f"site_data:undo:{newest.id}"
    if not await budget.claim_once(claim, int(_UNDO_WINDOW.total_seconds())):
        raise unknown  # another request is undoing it, or already has
    cf = _client(target, _cloudflare)
    try:
        return await _do_restore(
            target,
            cf,
            user_id,
            "site_data.restore_undo",
            bookmark=previous_bookmark,
            meta_extra={"undoes": previous_bookmark},
        )
    except BaseException:
        await budget.release(claim)
        raise


# --------------------------------------------------------------------------- files and KV
#
# A store is addressed by its binding name, which must be one the Site doc records
# (``target.r2`` / ``target.kv``): any other name is a 404 before any Cloudflare call,
# so a request can never name another site's bucket or namespace. Lists answer
# ``available=false`` in local mode; every other route there is a 409
# ``sites.data_unavailable``. Writes need edit access to the pocket. Their audit
# events use their own target type (``site_store``, target ``<pocket>:<binding>:<key>``)
# so a database restore never counts a file or key change, and carry the key, size and
# content type, never a value or file bytes.

_STORE_AUDIT_TARGET = "site_store"
_FILE_PAGE = 200
_KV_PAGE = 200
_SPOOL_IN_MEMORY = 4 * 1024 * 1024
_CHUNK = 256 * 1024


def _store_reason(target: DataTarget) -> str:
    """Why the site's stores can't be reached now ("" when they can). Only local
    mode: R2 and KV don't depend on the database being published."""
    return _REASON_LOCAL if target.reason == _REASON_LOCAL else ""


def _bucket(target: DataTarget, binding: str) -> str:
    bucket = target.r2.get(binding) if isinstance(binding, str) else None
    if not bucket:
        raise NotFound("site_file_store", binding)
    return bucket


def _namespace(target: DataTarget, binding: str) -> str:
    namespace = target.kv.get(binding) if isinstance(binding, str) else None
    if not namespace:
        raise NotFound("site_kv_store", binding)
    return namespace


async def _store_audit(
    target: DataTarget, user_id: str, action: str, binding: str, key: str, meta: dict[str, Any]
) -> None:
    from pocketpaw_ee.cloud.audit import service as audit_service

    try:
        await audit_service.record(
            target.workspace_id,
            user_id,
            action,
            target_type=_STORE_AUDIT_TARGET,
            target_id=f"{target.pocket_id}:{binding}:{key}",
            metadata={"binding": binding, "key": key, **meta},
        )
    except Exception:  # noqa: BLE001 — the change landed; a lost audit row must not undo it
        logger.warning("site_data: audit failed for %s", action, exc_info=True)


def _file_object(key: str, size: int, content_type: str, modified: Any) -> FileObject:
    return FileObject(
        key=key,
        name=key.rsplit("/", 1)[-1],
        size=int(size or 0),
        content_type=content_type or "",
        modified=str(modified) if modified else None,
    )


async def _store_target(
    *, workspace_id: str, user_id: str, pocket_id: str, edit: bool, _cloudflare: Any | None
) -> DataTarget:
    if edit:
        return await _editable_target(
            workspace_id=workspace_id,
            user_id=user_id,
            pocket_id=pocket_id,
            _cloudflare=_cloudflare,
        )
    return await resolve_target(
        workspace_id=workspace_id, user_id=user_id, pocket_id=pocket_id, _cloudflare=_cloudflare
    )


async def list_files(
    *,
    workspace_id: str,
    user_id: str,
    pocket_id: str,
    binding: str,
    prefix: str | None = None,
    cursor: str | None = None,
    _cloudflare: Any | None = None,
) -> FileList:
    """One folder of a file store: its sub-folders and objects, 200 per page.
    ``GET .../data/files/{binding}``."""
    prefix = files_kv.check_prefix(prefix)
    target = await _store_target(
        workspace_id=workspace_id,
        user_id=user_id,
        pocket_id=pocket_id,
        edit=False,
        _cloudflare=_cloudflare,
    )
    bucket = _bucket(target, binding)
    reason = _store_reason(target)
    if reason:
        return FileList(
            pocket_id=pocket_id, binding=binding, available=False, reason=reason, prefix=prefix
        )
    page = await _client(target, _cloudflare).r2_list(
        bucket, prefix=prefix, delimiter="/", cursor=cursor or None, per_page=_FILE_PAGE
    )
    return FileList(
        pocket_id=pocket_id,
        binding=binding,
        available=True,
        prefix=prefix,
        folders=list(page["folders"]),
        objects=[
            _file_object(o["key"], o["size"], o["content_type"], o["last_modified"])
            for o in page["objects"]
        ],
        cursor=page["cursor"],
    )


@dataclass(frozen=True)
class FileDownload:
    disposition: str  # the Content-Disposition value (always ``attachment``)
    content_type: str
    size: int | None
    body: AsyncIterator[bytes]


async def download_file(
    *,
    workspace_id: str,
    user_id: str,
    pocket_id: str,
    binding: str,
    key: str,
    _cloudflare: Any | None = None,
) -> FileDownload:
    """One object as a stream, typed for a safe download (see ``files_kv``).
    ``GET .../data/files/{binding}/object?key=``."""
    key = files_kv.check_file_key(key)
    target = await _store_target(
        workspace_id=workspace_id,
        user_id=user_id,
        pocket_id=pocket_id,
        edit=False,
        _cloudflare=_cloudflare,
    )
    bucket = _bucket(target, binding)
    if reason := _store_reason(target):
        raise writes.unavailable(reason)
    obj = await _client(target, _cloudflare).r2_get(bucket, key)
    if obj is None:
        raise NotFound("site_file", key)
    return FileDownload(
        disposition=files_kv.content_disposition(key),
        content_type=files_kv.download_content_type(obj.content_type, key),
        size=obj.size,
        body=obj.iter_bytes(),
    )


def _too_large() -> PayloadTooLarge:
    return PayloadTooLarge("sites.data_file_too_large", "A file can be at most 50 MB here.")


async def _spool(chunks: AsyncIterator[bytes], limit: int) -> tuple[Any, int]:
    """``chunks`` into a temporary file (in memory up to 4 MB), refusing past
    ``limit`` bytes. Returns the file, rewound, and its size."""
    spool = tempfile.SpooledTemporaryFile(max_size=_SPOOL_IN_MEMORY)  # noqa: SIM115
    size = 0
    try:
        async for chunk in chunks:
            size += len(chunk)
            if size > limit:
                raise _too_large()
            spool.write(chunk)
    except BaseException:
        spool.close()
        raise
    spool.seek(0)
    return spool, size


async def _read_spool(spool: Any) -> AsyncIterator[bytes]:
    while chunk := spool.read(_CHUNK):
        yield chunk


async def upload_file(
    *,
    workspace_id: str,
    user_id: str,
    pocket_id: str,
    binding: str,
    key: str,
    content_type: str | None,
    body: AsyncIterator[bytes],
    declared_size: int | None = None,
    _cloudflare: Any | None = None,
) -> FileObjectOut:
    """Write ``body`` (at most 50 MB) to ``key``, replacing what is there, under a
    content type that can't run as a page or script.
    ``PUT .../data/files/{binding}/object?key=``."""
    key = files_kv.check_file_key(key)
    if declared_size is not None and declared_size > files_kv.MAX_FILE_BYTES:
        raise _too_large()
    target = await _store_target(
        workspace_id=workspace_id,
        user_id=user_id,
        pocket_id=pocket_id,
        edit=True,
        _cloudflare=_cloudflare,
    )
    bucket = _bucket(target, binding)
    if reason := _store_reason(target):
        raise writes.unavailable(reason)
    stored_type = files_kv.stored_content_type(content_type, key)
    spool, size = await _spool(body, files_kv.MAX_FILE_BYTES)
    try:
        out = await _client(target, _cloudflare).r2_put(
            bucket, key, _read_spool(spool), content_type=stored_type, size=size
        )
    finally:
        spool.close()
    await _store_audit(
        target,
        user_id,
        "site_data.file_put",
        binding,
        key,
        {"size": size, "content_type": stored_type},
    )
    return FileObjectOut(
        object=_file_object(key, out.get("size") or size, stored_type, out.get("uploaded"))
    )


async def move_file(
    *,
    workspace_id: str,
    user_id: str,
    pocket_id: str,
    binding: str,
    from_key: str,
    to_key: str,
    overwrite: bool = False,
    _cloudflare: Any | None = None,
) -> FileObjectOut:
    """Rename / move an object (at most 50 MB): copy it to ``to_key``, then delete
    ``from_key``. An existing ``to_key`` is a 409 ``sites.data_file_exists`` unless
    ``overwrite``. ``POST .../data/files/{binding}/object:move``."""
    from_key = files_kv.check_file_key(from_key)
    to_key = files_kv.check_file_key(to_key)
    if from_key == to_key:
        raise ValidationError("sites.data_bad_file_key", "Pick a different name.")
    target = await _store_target(
        workspace_id=workspace_id,
        user_id=user_id,
        pocket_id=pocket_id,
        edit=True,
        _cloudflare=_cloudflare,
    )
    bucket = _bucket(target, binding)
    if reason := _store_reason(target):
        raise writes.unavailable(reason)
    cf = _client(target, _cloudflare)
    if not overwrite:
        found = await cf.r2_list(bucket, prefix=to_key, delimiter=None, per_page=1)
        if any(o["key"] == to_key for o in found["objects"]):
            raise CloudError(409, "sites.data_file_exists", "A file with that name already exists.")
    obj = await cf.r2_get(bucket, from_key)
    if obj is None:
        raise NotFound("site_file", from_key)
    if obj.size is not None and obj.size > files_kv.MAX_FILE_BYTES:
        await obj.aclose()
        raise _too_large()
    stored_type = files_kv.stored_content_type(obj.content_type, to_key)
    spool, size = await _spool(obj.iter_bytes(), files_kv.MAX_FILE_BYTES)
    try:
        out = await cf.r2_put(
            bucket, to_key, _read_spool(spool), content_type=stored_type, size=size
        )
    finally:
        spool.close()
    await cf.r2_delete(bucket, from_key)
    await _store_audit(
        target,
        user_id,
        "site_data.file_move",
        binding,
        to_key,
        {"from": from_key, "to": to_key, "size": size, "content_type": stored_type},
    )
    return FileObjectOut(
        object=_file_object(to_key, out.get("size") or size, stored_type, out.get("uploaded"))
    )


async def delete_file(
    *,
    workspace_id: str,
    user_id: str,
    pocket_id: str,
    binding: str,
    key: str,
    _cloudflare: Any | None = None,
) -> None:
    """Delete one object (a missing one is done). ``DELETE .../object?key=``."""
    key = files_kv.check_file_key(key)
    target = await _store_target(
        workspace_id=workspace_id,
        user_id=user_id,
        pocket_id=pocket_id,
        edit=True,
        _cloudflare=_cloudflare,
    )
    bucket = _bucket(target, binding)
    if reason := _store_reason(target):
        raise writes.unavailable(reason)
    await _client(target, _cloudflare).r2_delete(bucket, key)
    await _store_audit(target, user_id, "site_data.file_delete", binding, key, {})


def _kv_ref(row: dict[str, Any]) -> KvKeyRef:
    masked = files_kv.masked_kv_key(row["name"])
    return KvKeyRef(
        name=row["name"],
        expiration=row.get("expiration"),
        metadata=None if masked else row.get("metadata"),
        masked=masked,
    )


async def list_kv_keys(
    *,
    workspace_id: str,
    user_id: str,
    pocket_id: str,
    binding: str,
    prefix: str | None = None,
    cursor: str | None = None,
    _cloudflare: Any | None = None,
) -> KvKeyList:
    """A page of a key-value store's keys (200 at a time) under ``prefix``.
    ``GET .../data/kv/{binding}/keys``."""
    prefix = prefix or ""
    if len(prefix.encode("utf-8")) > files_kv.MAX_KV_KEY_BYTES:
        raise ValidationError("sites.data_bad_kv_key", "A key name is at most 512 bytes.")
    target = await _store_target(
        workspace_id=workspace_id,
        user_id=user_id,
        pocket_id=pocket_id,
        edit=False,
        _cloudflare=_cloudflare,
    )
    namespace = _namespace(target, binding)
    reason = _store_reason(target)
    if reason:
        return KvKeyList(
            pocket_id=pocket_id, binding=binding, available=False, reason=reason, prefix=prefix
        )
    page = await _client(target, _cloudflare).kv_list_keys(
        namespace, prefix=prefix, cursor=cursor or None, limit=_KV_PAGE
    )
    return KvKeyList(
        pocket_id=pocket_id,
        binding=binding,
        available=True,
        prefix=prefix,
        keys=[_kv_ref(k) for k in page["keys"]],
        cursor=page["cursor"],
    )


def _is_json(text: str) -> bool:
    if not text.lstrip().startswith(("{", "[")):
        return False
    try:
        return isinstance(json.loads(text), dict | list)
    except ValueError:
        return False


async def get_kv_key(
    *,
    workspace_id: str,
    user_id: str,
    pocket_id: str,
    binding: str,
    key: str,
    _cloudflare: Any | None = None,
) -> KvKeyValue:
    """One key: its value (text, or base64 when binary; nothing past 1 MB, ``MASK``
    when its name looks like a credential), metadata and expiry.
    ``GET .../data/kv/{binding}/keys/{key}``."""
    key = files_kv.check_kv_key(key)
    target = await _store_target(
        workspace_id=workspace_id,
        user_id=user_id,
        pocket_id=pocket_id,
        edit=False,
        _cloudflare=_cloudflare,
    )
    namespace = _namespace(target, binding)
    if reason := _store_reason(target):
        raise writes.unavailable(reason)
    cf = _client(target, _cloudflare)
    got = await cf.kv_get(namespace, key, max_bytes=files_kv.MAX_KV_EDIT_BYTES)
    if got is None:
        raise NotFound("site_kv_key", key)
    # The list row carries the expiry and metadata (a key sorts first under itself).
    listed = await cf.kv_list_keys(namespace, prefix=key, limit=10)
    row = next((k for k in listed["keys"] if k["name"] == key), {"name": key})
    masked = files_kv.masked_kv_key(key)
    out = KvKeyValue(
        binding=binding,
        name=key,
        size=got.size,
        expiration=row.get("expiration"),
        masked=masked,
        too_large=got.truncated,
    )
    if masked:
        return out.model_copy(update={"value": files_kv.MASK})
    update: dict[str, Any] = {"metadata": row.get("metadata")}
    if not got.truncated:
        if got.text is not None:
            update.update(value=got.text, is_json=_is_json(got.text), editable=True)
        else:
            update["value_base64"] = base64.b64encode(got.data).decode("ascii")
    return out.model_copy(update=update)


async def put_kv_key(
    *,
    workspace_id: str,
    user_id: str,
    pocket_id: str,
    binding: str,
    key: str,
    value: str,
    metadata: Any = None,
    expiration_ttl: int | None = None,
    _cloudflare: Any | None = None,
) -> KvPutOut:
    """Write a key's text value, metadata and expiry together (a PUT is the whole
    key: no ``metadata`` clears it, no ``expiration_ttl`` makes it never expire).
    ``PUT .../data/kv/{binding}/keys/{key}``."""
    key = files_kv.check_kv_key(key)
    raw = value.encode("utf-8")
    if len(raw) > files_kv.MAX_KV_EDIT_BYTES:
        raise PayloadTooLarge("sites.data_kv_value_too_large", "A value can be at most 1 MB here.")
    if expiration_ttl is not None and expiration_ttl < files_kv.MIN_TTL:
        raise ValidationError(
            "sites.data_bad_kv_ttl", "An expiry must be at least 60 seconds away."
        )
    if metadata is not None:
        packed = json.dumps(metadata, separators=(",", ":")).encode("utf-8")
        if len(packed) > files_kv.MAX_KV_METADATA_BYTES:
            raise ValidationError(
                "sites.data_bad_kv_metadata", "Metadata can be at most 1,024 bytes as JSON."
            )
    target = await _store_target(
        workspace_id=workspace_id,
        user_id=user_id,
        pocket_id=pocket_id,
        edit=True,
        _cloudflare=_cloudflare,
    )
    namespace = _namespace(target, binding)
    if reason := _store_reason(target):
        raise writes.unavailable(reason)
    if files_kv.masked_kv_key(key):
        raise Forbidden(
            "sites.data_masked_key", "Keys named like a password or token can't be edited here."
        )
    await _client(target, _cloudflare).kv_put(
        namespace, key, raw, metadata=metadata, expiration_ttl=expiration_ttl
    )
    await _store_audit(
        target,
        user_id,
        "site_data.kv_put",
        binding,
        key,
        {"size": len(raw), "expiration_ttl": expiration_ttl, "has_metadata": metadata is not None},
    )
    expiration = int(time.time()) + expiration_ttl if expiration_ttl else None
    return KvPutOut(key=KvKeyRef(name=key, expiration=expiration, metadata=metadata), size=len(raw))


async def delete_kv_key(
    *,
    workspace_id: str,
    user_id: str,
    pocket_id: str,
    binding: str,
    key: str,
    _cloudflare: Any | None = None,
) -> None:
    """Delete a key (a missing one is done). ``DELETE .../data/kv/{binding}/keys/{key}``."""
    key = files_kv.check_kv_key(key)
    target = await _store_target(
        workspace_id=workspace_id,
        user_id=user_id,
        pocket_id=pocket_id,
        edit=True,
        _cloudflare=_cloudflare,
    )
    namespace = _namespace(target, binding)
    if reason := _store_reason(target):
        raise writes.unavailable(reason)
    await _client(target, _cloudflare).kv_delete(namespace, key)
    await _store_audit(target, user_id, "site_data.kv_delete", binding, key, {})
