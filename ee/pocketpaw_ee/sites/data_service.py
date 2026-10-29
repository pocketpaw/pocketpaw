# ee/pocketpaw_ee/sites/data_service.py — the one entry point of the Sites Data
# dashboard: which site, which lane, which stores, and the read operations over them
# (inventory, one table's schema, a page of rows).
#
# ``resolve_target`` reads the pocket through the pockets service (NotFound /
# Forbidden for a missing or unreadable pocket, NotFound for another workspace's),
# picks the lane and the Site doc's D1 / KV / R2 ids. Lanes:
#   * project — engine ``project`` with a database: tables come from introspecting
#     the live D1 (``site_data.d1``). No database at all is ``sites.not_dynamic``.
#   * dynamic — a ripple / svelte site with declared ``objects``: the spec is the
#     table whitelist, cross-checked against the live PRAGMA; a declared table the
#     database doesn't have yet lists with ``row_count=0`` and reads empty.
# Local mode answers ``available=false`` / ``live_on_cloudflare_only`` and a site with
# no database yet ``not_published`` on every read, with no Cloudflare call.
#
# Every Cloudflare call goes through ``site_data.budget`` (429 ``sites.data_busy``)
# on the data-token client (``service._data_cf_client``). Reads only; the write
# actions (``sites.data_write`` / ``sites.data_restore``) only feed the
# ``can_write`` / ``can_restore`` flags here.
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Literal

from pocketpaw_ee.cloud._core.errors import CloudError, NotFound
from pocketpaw_ee.sites import service as sites_service
from pocketpaw_ee.sites.project_data import masked_columns
from pocketpaw_ee.sites.site_data import budget, d1
from pocketpaw_ee.sites.site_data.dto import (
    ColumnInfo,
    ConnectorRef,
    DataInventory,
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
        ConnectorRef(provider=v.provider, status=str(getattr(v.status, "value", v.status)))
        for v in views
    ]


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
    return DataInventory(
        pocket_id=pocket_id,
        available=not target.reason,
        reason=target.reason,
        tables=tables,
        files=[StoreRef(binding=b, label=_label(b)) for b in sorted(target.r2)],
        kv=[StoreRef(binding=b, label=_label(b)) for b in sorted(target.kv)],
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
