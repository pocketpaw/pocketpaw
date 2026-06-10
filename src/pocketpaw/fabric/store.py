# Fabric store — async SQLite operations for the ontology layer.
# Created: 2026-03-28 — CRUD for object types, objects, and links.
# Updated: 2026-04-19 (Cluster C / PR3) — Added list_links() for the new
#   GET /api/v1/fabric/links endpoint that the Links sub-tab in
#   PocketDataPanel now consumes instead of its hardcoded placeholder.
# Updated: 2026-06-10 (W0d) — query() now honors FabricQuery.filters. Property
#   filters were previously parsed into the model but silently dropped, so
#   "leases where rent > X" returned ALL objects of the type. Filters are
#   applied against the JSON properties bag via json_extract with whitelisted
#   operators; property names go through a fixed validation gate and values are
#   always bound parameters (no value interpolation). Comparison operators use
#   CAST(... AS REAL) so numeric comparisons stay numeric regardless of param
#   type. New helper _build_filter_conditions() keeps the change localized to
#   the filter logic so a later workspace_id-scoping change merges cleanly.
# Updated: 2026-06-10 (W4a — workspace-scope fabric store) — closes a
#   cross-tenant data leak on shared deployments (the micro tier / an agency
#   running multiple client tenants share one ``fabric.db``). Objects and links
#   now carry a ``workspace_id`` column. Writes (``create_object`` / ``link``)
#   stamp the caller's workspace; reads (``query`` / ``list_links`` /
#   ``get_object`` / ``get_linked_objects``) take an optional ``workspace_id``
#   and, when supplied, restrict results to that tenant. The scoping is an
#   ADDITIONAL WHERE condition layered ALONGSIDE W0d's property filters in
#   ``query()`` — the filter logic is untouched. ``workspace_id`` crosses from
#   the EE router as a PLAIN str (the OSS store never imports pocketpaw_ee).
#   Legacy/NULL treatment: rows written before this change (or by a non-cloud
#   OSS caller that passes no workspace) have NULL ``workspace_id``. A scoped
#   read matches ``workspace_id = ? OR workspace_id IS NULL`` so legacy/global
#   data predating tenancy stays visible to every tenant (it cannot be safely
#   attributed to one workspace after the fact, and single-tenant deployments
#   must keep working). New writes always stamp a workspace when one is given,
#   so going-forward data is cleanly isolated. A read with ``workspace_id=None``
#   applies no scoping at all (full backward-compat for OSS / agent-tool
#   callers). Additive ALTER migration mirrors the W2b assignee/hash-chain
#   pattern — no crash on a pre-existing DB.
# Updated: 2026-06-10 (FIX 3 — hardening) — tightened the filter property-name
#   validator in _build_filter_conditions() from ``c.isalnum() or c in "_-"`` to
#   ``c.isalnum() or c == "_"``. Hyphens were not a vulnerability (json_extract
#   reads ``$.a-b`` as a literal key) but were unnecessarily permissive and made
#   ``$.a-b`` ambiguous in a SQL trace. No object type uses hyphenated property
#   names, so this loses nothing. W0d filter tests unchanged and still pass.

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import aiosqlite

from pocketpaw.fabric.models import (
    FabricLink,
    FabricObject,
    FabricQuery,
    FabricQueryResult,
    ObjectType,
    PropertyDef,
)

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS fabric_object_types (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT DEFAULT '',
    icon TEXT DEFAULT 'box',
    color TEXT DEFAULT '#0A84FF',
    properties_schema TEXT DEFAULT '[]',
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS fabric_objects (
    id TEXT PRIMARY KEY,
    type_id TEXT NOT NULL REFERENCES fabric_object_types(id),
    type_name TEXT DEFAULT '',
    properties TEXT NOT NULL DEFAULT '{}',
    source_connector TEXT,
    source_id TEXT,
    -- Tenancy (W4a): the owning workspace. NULL = legacy/global row written
    -- before tenancy or by a non-cloud OSS caller; a scoped read still sees it.
    workspace_id TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS fabric_links (
    id TEXT PRIMARY KEY,
    from_object_id TEXT NOT NULL REFERENCES fabric_objects(id),
    to_object_id TEXT NOT NULL REFERENCES fabric_objects(id),
    link_type TEXT NOT NULL,
    properties TEXT DEFAULT '{}',
    -- Tenancy (W4a): same workspace semantics as fabric_objects. A link is
    -- scoped to the workspace of the caller that created it.
    workspace_id TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_objects_type ON fabric_objects(type_id);
CREATE INDEX IF NOT EXISTS idx_objects_source ON fabric_objects(source_connector, source_id);
CREATE INDEX IF NOT EXISTS idx_links_from ON fabric_links(from_object_id);
CREATE INDEX IF NOT EXISTS idx_links_to ON fabric_links(to_object_id);
CREATE INDEX IF NOT EXISTS idx_links_type ON fabric_links(link_type);
"""

# Tenancy indexes are created AFTER the ALTER migration (see _ensure_schema),
# NOT in SCHEMA_SQL above. On a pre-W4a DB the table already exists, so
# CREATE TABLE IF NOT EXISTS is a no-op and the workspace_id column is added by
# ALTER — a CREATE INDEX on workspace_id inside the same executescript would
# run before that ALTER and fail with "no such column". (Bug found by live
# smoke 2026-06-10; see tests/cloud/test_w4a_migration.py.)
_WORKSPACE_INDEX_SQL = (
    "CREATE INDEX IF NOT EXISTS idx_objects_workspace ON fabric_objects(workspace_id)",
    "CREATE INDEX IF NOT EXISTS idx_links_workspace ON fabric_links(workspace_id)",
)

# Whitelist of filter operators -> SQL operator. User input never reaches the
# SQL string except through this fixed mapping; an unknown operator raises
# rather than being interpolated. Both symbolic and word aliases are accepted
# so callers (agent tool, REST body) can use whichever reads cleaner.
_FILTER_OPS: dict[str, str] = {
    "=": "=",
    "==": "=",
    "eq": "=",
    "!=": "!=",
    "ne": "!=",
    ">": ">",
    "gt": ">",
    ">=": ">=",
    "gte": ">=",
    "<": "<",
    "lt": "<",
    "<=": "<=",
    "lte": "<=",
}

# Operators whose comparison must be numeric. For these we CAST both the stored
# JSON value and the bound parameter to REAL so that "rent > 1000" compares as
# numbers, never as the text affinity SQLite would otherwise pick when one side
# is TEXT. Equality / inequality stay un-CAST so string eq ("status" = "active")
# and numeric eq both behave naturally.
_NUMERIC_OPS: frozenset[str] = frozenset({">", ">=", "<", "<="})


def _is_number(value: Any) -> bool:
    """True for ints/floats but not bools (bool is an int subclass in Python)."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _workspace_scope(
    workspace_id: str | None, *, column: str = "workspace_id"
) -> tuple[str | None, list[Any]]:
    """Build the tenancy WHERE fragment + bound params for a scoped read (W4a).

    Returns ``(condition, params)``:

    - ``workspace_id is None`` -> ``(None, [])`` — no scoping. OSS / agent-tool
      callers that don't carry a workspace see everything, exactly as before.
    - a concrete workspace -> ``("(<col> = ? OR <col> IS NULL)", [workspace_id])``
      — the caller's own rows PLUS legacy/global NULL-workspace rows that predate
      tenancy (see the module-header note on the legacy boundary). The value is
      always a bound parameter; ``column`` is a fixed caller-supplied literal
      (``"workspace_id"`` or ``"o.workspace_id"``), never user input.
    """
    if workspace_id is None:
        return None, []
    return f"({column} = ? OR {column} IS NULL)", [workspace_id]


def _build_filter_conditions(filters: dict[str, Any]) -> tuple[list[str], list[Any]]:
    """Translate FabricQuery.filters into SQL WHERE fragments + bound params.

    Two value forms are supported per property key, matching the existing
    ``dict[str, Any]`` shape rather than inventing a new one:

    - scalar      -> equality, e.g. ``{"status": "active"}``
    - operator map -> comparison, e.g. ``{"rent": {">": 1000}}`` or
      ``{"rent": {"gte": 1000}}`` (multiple ops on one key are AND-ed).

    Property names are validated against a conservative identifier charset
    before being placed into the ``$.<name>`` JSON path (SQLite cannot bind a
    JSON path as a parameter). Operator symbols are mapped through the
    ``_FILTER_OPS`` whitelist. Filter VALUES are always emitted as ``?``
    placeholders — never interpolated — so there is no value-side injection
    surface.
    """
    conditions: list[str] = []
    params: list[Any] = []
    for raw_key, raw_val in filters.items():
        key = str(raw_key)
        # Restrict property names to a safe identifier set: alphanumerics plus
        # underscore. Anything else (a quote, a dot, a bracket, a hyphen) is
        # rejected so it can never break out of the JSON path literal.
        #
        # FIX 3 (2026-06-10): tightened from ``c in "_-"`` to ``c == "_"``.
        # Hyphens were never a vulnerability — json_extract treats ``$.a-b`` as
        # a literal key, not an expression — but they are unnecessarily
        # permissive and ``$.a-b`` reads ambiguously in a SQL trace (subtraction
        # vs. a literal key). No object type in the codebase uses hyphenated
        # property names, so the underscore-only identifier rule loses nothing.
        if not key or not all(c.isalnum() or c == "_" for c in key):
            raise ValueError(f"Invalid filter property name: {raw_key!r}")
        path = f"$.{key}"

        # An operator map => one condition per operator; a scalar => equality.
        op_map = raw_val if isinstance(raw_val, dict) else {"=": raw_val}
        for op_token, value in op_map.items():
            sql_op = _FILTER_OPS.get(str(op_token).lower())
            if sql_op is None:
                raise ValueError(f"Unsupported filter operator: {op_token!r}")
            if sql_op in _NUMERIC_OPS and _is_number(value):
                # Numeric comparison: force REAL affinity on both sides.
                conditions.append(f"CAST(json_extract(o.properties, ?) AS REAL) {sql_op} ?")
                params.extend([path, float(value)])
            else:
                conditions.append(f"json_extract(o.properties, ?) {sql_op} ?")
                params.extend([path, value])
    return conditions, params


class FabricStore:
    """Async SQLite store for Fabric ontology data."""

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = str(db_path)
        self._initialized = False

    async def _ensure_schema(self) -> None:
        if self._initialized:
            return
        async with aiosqlite.connect(self._db_path) as db:
            await db.executescript(SCHEMA_SQL)
            # Additive migration (W4a): tenancy columns on a pre-existing DB.
            # CREATE TABLE IF NOT EXISTS won't add a column to a table that
            # already exists, so ALTER and swallow the duplicate-column error
            # that fires on every subsequent boot — same pattern as the W2b
            # instinct hash-chain / assignee migrations. Pre-existing rows keep
            # NULL workspace_id (legacy/global; see the module header).
            for _tbl in ("fabric_objects", "fabric_links"):
                try:
                    await db.execute(f"ALTER TABLE {_tbl} ADD COLUMN workspace_id TEXT")
                except aiosqlite.OperationalError:
                    pass
            # Create the tenancy indexes only after the column is guaranteed to
            # exist (fresh DB via CREATE TABLE, or pre-existing DB via the ALTER
            # above). Doing this inside SCHEMA_SQL would fail on a pre-W4a DB.
            for _idx in _WORKSPACE_INDEX_SQL:
                await db.execute(_idx)
            await db.commit()
        self._initialized = True

    def _conn(self) -> aiosqlite.Connection:
        """Return a new connection context manager. Use with `async with`."""
        return aiosqlite.connect(self._db_path)

    # --- Object Types ---

    async def define_type(
        self,
        name: str,
        properties: list[PropertyDef],
        description: str = "",
        icon: str = "box",
        color: str = "#0A84FF",
    ) -> ObjectType:
        obj_type = ObjectType(
            name=name,
            description=description,
            icon=icon,
            color=color,
            properties=properties,
        )
        await self._ensure_schema()
        async with self._conn() as db:
            await db.execute(
                "INSERT INTO fabric_object_types"
                " (id, name, description, icon, color, properties_schema)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (
                    obj_type.id,
                    obj_type.name,
                    obj_type.description,
                    obj_type.icon,
                    obj_type.color,
                    json.dumps([p.model_dump() for p in properties]),
                ),
            )
            await db.commit()
        return obj_type

    async def get_type(self, type_id: str) -> ObjectType | None:
        await self._ensure_schema()
        async with self._conn() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM fabric_object_types WHERE id = ?", (type_id,)
            ) as cur:
                row = await cur.fetchone()
                if not row:
                    return None
                return self._row_to_type(row)

    async def get_type_by_name(self, name: str) -> ObjectType | None:
        await self._ensure_schema()
        async with self._conn() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM fabric_object_types WHERE LOWER(name) = LOWER(?)", (name,)
            ) as cur:
                row = await cur.fetchone()
                if not row:
                    return None
                return self._row_to_type(row)

    async def list_types(self) -> list[ObjectType]:
        await self._ensure_schema()
        async with self._conn() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM fabric_object_types ORDER BY name") as cur:
                return [self._row_to_type(row) async for row in cur]

    async def remove_type(self, type_id: str) -> None:
        await self._ensure_schema()
        async with self._conn() as db:
            # Cascade: delete links involving objects of this type, then objects, then type
            await db.execute(
                "DELETE FROM fabric_links"
                " WHERE from_object_id IN"
                " (SELECT id FROM fabric_objects WHERE type_id = ?)"
                " OR to_object_id IN"
                " (SELECT id FROM fabric_objects WHERE type_id = ?)",
                (type_id, type_id),
            )
            await db.execute("DELETE FROM fabric_objects WHERE type_id = ?", (type_id,))
            await db.execute("DELETE FROM fabric_object_types WHERE id = ?", (type_id,))
            await db.commit()

    # --- Objects ---

    async def create_object(
        self,
        type_id: str,
        properties: dict[str, Any],
        source_connector: str | None = None,
        source_id: str | None = None,
        workspace_id: str | None = None,
    ) -> FabricObject:
        obj_type = await self.get_type(type_id)
        obj = FabricObject(
            type_id=type_id,
            type_name=obj_type.name if obj_type else "",
            properties=properties,
            source_connector=source_connector,
            source_id=source_id,
        )
        await self._ensure_schema()
        async with self._conn() as db:
            await db.execute(
                "INSERT INTO fabric_objects"
                " (id, type_id, type_name, properties,"
                " source_connector, source_id, workspace_id)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    obj.id,
                    obj.type_id,
                    obj.type_name,
                    json.dumps(properties),
                    source_connector,
                    source_id,
                    workspace_id,
                ),
            )
            await db.commit()
        return obj

    async def get_object(self, obj_id: str, workspace_id: str | None = None) -> FabricObject | None:
        """Fetch one object by id, optionally scoped to ``workspace_id`` (W4a).

        When ``workspace_id`` is supplied, a row belonging to another tenant
        returns ``None`` (a 404 to the caller) — the cross-tenant read leak this
        task closes. A legacy NULL-workspace row stays visible.
        """
        ws_cond, ws_params = _workspace_scope(workspace_id)
        sql = "SELECT * FROM fabric_objects WHERE id = ?"
        params: list[Any] = [obj_id]
        if ws_cond:
            sql += f" AND {ws_cond}"
            params.extend(ws_params)
        await self._ensure_schema()
        async with self._conn() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(sql, params) as cur:
                row = await cur.fetchone()
                if not row:
                    return None
                return self._row_to_object(row)

    async def update_object(self, obj_id: str, properties: dict[str, Any]) -> FabricObject | None:
        existing = await self.get_object(obj_id)
        if not existing:
            return None
        merged = {**existing.properties, **properties}
        await self._ensure_schema()
        async with self._conn() as db:
            await db.execute(
                "UPDATE fabric_objects"
                " SET properties = ?, updated_at = datetime('now')"
                " WHERE id = ?",
                (json.dumps(merged), obj_id),
            )
            await db.commit()
        return await self.get_object(obj_id)

    async def remove_object(self, obj_id: str) -> None:
        await self._ensure_schema()
        async with self._conn() as db:
            await db.execute(
                "DELETE FROM fabric_links WHERE from_object_id = ? OR to_object_id = ?",
                (obj_id, obj_id),
            )
            await db.execute("DELETE FROM fabric_objects WHERE id = ?", (obj_id,))
            await db.commit()

    # --- Links ---

    async def link(
        self,
        from_id: str,
        to_id: str,
        link_type: str,
        properties: dict[str, Any] | None = None,
        workspace_id: str | None = None,
    ) -> FabricLink:
        lnk = FabricLink(
            from_object_id=from_id,
            to_object_id=to_id,
            link_type=link_type,
            properties=properties or {},
        )
        await self._ensure_schema()
        async with self._conn() as db:
            await db.execute(
                "INSERT INTO fabric_links"
                " (id, from_object_id, to_object_id,"
                " link_type, properties, workspace_id)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (
                    lnk.id,
                    lnk.from_object_id,
                    lnk.to_object_id,
                    lnk.link_type,
                    json.dumps(lnk.properties),
                    workspace_id,
                ),
            )
            await db.commit()
        return lnk

    async def list_links(
        self,
        from_id: str | None = None,
        to_id: str | None = None,
        link_type: str | None = None,
        limit: int = 100,
        offset: int = 0,
        workspace_id: str | None = None,
    ) -> tuple[list[FabricLink], int]:
        """List links with optional filters on endpoints and link_type.

        Returns ``(links, total)`` where ``total`` is the unpaginated count.
        All filter arguments are bound parameters — no query-string
        concatenation, so SQL injection through link_type is not possible.

        ``workspace_id`` (W4a) restricts both the count and the page to the
        caller's tenant (plus legacy NULL-workspace links); ``None`` leaves the
        listing unscoped for OSS callers.
        """
        conditions: list[str] = []
        params: list[Any] = []
        if from_id:
            conditions.append("from_object_id = ?")
            params.append(from_id)
        if to_id:
            conditions.append("to_object_id = ?")
            params.append(to_id)
        if link_type:
            conditions.append("link_type = ?")
            params.append(link_type)
        ws_cond, ws_params = _workspace_scope(workspace_id)
        if ws_cond:
            conditions.append(ws_cond)
            params.extend(ws_params)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""

        await self._ensure_schema()
        async with self._conn() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                f"SELECT COUNT(*) AS cnt FROM fabric_links {where}", params
            ) as cur:
                row = await cur.fetchone()
                total = row["cnt"] if row else 0

            async with db.execute(
                f"SELECT * FROM fabric_links {where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
                [*params, limit, offset],
            ) as cur:
                links = [self._row_to_link(row) async for row in cur]

        return links, total

    async def unlink(self, link_id: str) -> None:
        await self._ensure_schema()
        async with self._conn() as db:
            await db.execute("DELETE FROM fabric_links WHERE id = ?", (link_id,))
            await db.commit()

    async def get_linked_objects(
        self, obj_id: str, link_type: str | None = None, workspace_id: str | None = None
    ) -> list[FabricObject]:
        """Traverse links from ``obj_id`` to the objects on the other end.

        ``workspace_id`` (W4a) scopes the RETURNED objects to the caller's tenant
        (plus legacy NULL-workspace objects) so a traversal can't surface another
        workspace's objects even if a link somehow spanned the boundary.
        """
        # Scope on the returned object's workspace (alias ``o``) — that is the
        # row the caller reads back. Layered as an extra AND on the existing
        # join filter; the link-traversal logic itself is unchanged.
        ws_cond, ws_params = _workspace_scope(workspace_id, column="o.workspace_id")
        await self._ensure_schema()
        async with self._conn() as db:
            db.row_factory = aiosqlite.Row
            if link_type:
                query = (
                    "SELECT o.* FROM fabric_objects o JOIN fabric_links l "
                    "ON (o.id = l.to_object_id AND l.from_object_id = ?) "
                    "OR (o.id = l.from_object_id AND l.to_object_id = ?) "
                    "WHERE l.link_type = ?"
                )
                params: list[Any] = [obj_id, obj_id, link_type]
            else:
                query = (
                    "SELECT o.* FROM fabric_objects o JOIN fabric_links l "
                    "ON (o.id = l.to_object_id AND l.from_object_id = ?) "
                    "OR (o.id = l.from_object_id AND l.to_object_id = ?)"
                )
                params = [obj_id, obj_id]
            if ws_cond:
                # Append to the WHERE: a link_type query already has WHERE; the
                # no-link_type branch has none yet, so add one.
                query += (" AND " if link_type else " WHERE ") + ws_cond
                params.extend(ws_params)
            async with db.execute(query, params) as cur:
                return [self._row_to_object(row) async for row in cur]

    # --- Query ---

    async def query(self, q: FabricQuery, workspace_id: str | None = None) -> FabricQueryResult:
        """Run a FabricQuery, optionally scoped to a tenant (W4a).

        ``workspace_id`` is a separate method argument rather than a
        ``FabricQuery`` field: tenancy is a server-side authorization concern
        threaded from the request's workspace context, never something a client
        sets on the query body. When supplied, results are restricted to that
        workspace (plus legacy NULL-workspace rows). When ``None``, the query is
        unscoped, exactly as before W4a (OSS / agent-tool callers).
        """
        conditions: list[str] = []
        params: list[Any] = []

        if q.type_id:
            conditions.append("o.type_id = ?")
            params.append(q.type_id)
        elif q.type_name:
            conditions.append("LOWER(o.type_name) = LOWER(?)")
            params.append(q.type_name)

        if q.linked_to:
            if q.link_type:
                link_cond = (
                    "o.id IN ("
                    "SELECT to_object_id FROM fabric_links"
                    " WHERE from_object_id = ? AND link_type = ? "
                    "UNION "
                    "SELECT from_object_id FROM fabric_links"
                    " WHERE to_object_id = ? AND link_type = ?"
                    ")"
                )
                conditions.append(link_cond)
                params.extend([q.linked_to, q.link_type, q.linked_to, q.link_type])
            else:
                link_cond = (
                    "o.id IN ("
                    "SELECT to_object_id FROM fabric_links WHERE from_object_id = ? "
                    "UNION "
                    "SELECT from_object_id FROM fabric_links WHERE to_object_id = ?"
                    ")"
                )
                conditions.append(link_cond)
                params.extend([q.linked_to, q.linked_to])

        # Property filters against the JSON properties bag. Kept as a localized
        # block (see _build_filter_conditions) so concurrent work on this method
        # — e.g. workspace_id scoping — merges without touching this logic.
        if q.filters:
            filter_conditions, filter_params = _build_filter_conditions(q.filters)
            conditions.extend(filter_conditions)
            params.extend(filter_params)

        # Tenancy scope (W4a) — an ADDITIONAL condition ANDed alongside the W0d
        # property filters above, never a replacement for them. Restricts the
        # result set to the caller's workspace plus legacy NULL-workspace rows.
        ws_cond, ws_params = _workspace_scope(workspace_id, column="o.workspace_id")
        if ws_cond:
            conditions.append(ws_cond)
            params.extend(ws_params)

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""

        await self._ensure_schema()
        async with self._conn() as db:
            db.row_factory = aiosqlite.Row
            # Count
            async with db.execute(
                f"SELECT COUNT(*) as cnt FROM fabric_objects o {where}", params
            ) as cur:
                row = await cur.fetchone()
                total = row["cnt"] if row else 0

            # Fetch
            async with db.execute(
                f"SELECT o.* FROM fabric_objects o {where}"
                " ORDER BY o.created_at DESC LIMIT ? OFFSET ?",
                [*params, q.limit, q.offset],
            ) as cur:
                objects = [self._row_to_object(row) async for row in cur]

        return FabricQueryResult(objects=objects, total=total)

    # --- Stats ---

    async def stats(self) -> dict[str, int]:
        await self._ensure_schema()
        async with self._conn() as db:
            types = await db.execute_fetchall("SELECT COUNT(*) FROM fabric_object_types")
            objects = await db.execute_fetchall("SELECT COUNT(*) FROM fabric_objects")
            links = await db.execute_fetchall("SELECT COUNT(*) FROM fabric_links")
            return {
                "types": types[0][0] if types else 0,
                "objects": objects[0][0] if objects else 0,
                "links": links[0][0] if links else 0,
            }

    # --- Helpers ---

    def _row_to_type(self, row: Any) -> ObjectType:
        props_raw = json.loads(row["properties_schema"]) if row["properties_schema"] else []
        return ObjectType(
            id=row["id"],
            name=row["name"],
            description=row["description"] or "",
            icon=row["icon"] or "box",
            color=row["color"] or "#0A84FF",
            properties=[PropertyDef(**p) for p in props_raw],
        )

    def _row_to_object(self, row: Any) -> FabricObject:
        return FabricObject(
            id=row["id"],
            type_id=row["type_id"],
            type_name=row["type_name"] or "",
            properties=json.loads(row["properties"]) if row["properties"] else {},
            source_connector=row["source_connector"],
            source_id=row["source_id"],
        )

    def _row_to_link(self, row: Any) -> FabricLink:
        return FabricLink(
            id=row["id"],
            from_object_id=row["from_object_id"],
            to_object_id=row["to_object_id"],
            link_type=row["link_type"],
            properties=json.loads(row["properties"]) if row["properties"] else {},
        )
