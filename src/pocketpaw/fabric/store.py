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
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS fabric_links (
    id TEXT PRIMARY KEY,
    from_object_id TEXT NOT NULL REFERENCES fabric_objects(id),
    to_object_id TEXT NOT NULL REFERENCES fabric_objects(id),
    link_type TEXT NOT NULL,
    properties TEXT DEFAULT '{}',
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_objects_type ON fabric_objects(type_id);
CREATE INDEX IF NOT EXISTS idx_objects_source ON fabric_objects(source_connector, source_id);
CREATE INDEX IF NOT EXISTS idx_links_from ON fabric_links(from_object_id);
CREATE INDEX IF NOT EXISTS idx_links_to ON fabric_links(to_object_id);
CREATE INDEX IF NOT EXISTS idx_links_type ON fabric_links(link_type);
"""

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
        # Restrict property names to a safe identifier set. Anything else (a
        # quote, a dot, a bracket) is rejected so it can never break out of the
        # JSON path literal.
        if not key or not all(c.isalnum() or c in "_-" for c in key):
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
                " source_connector, source_id)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (
                    obj.id,
                    obj.type_id,
                    obj.type_name,
                    json.dumps(properties),
                    source_connector,
                    source_id,
                ),
            )
            await db.commit()
        return obj

    async def get_object(self, obj_id: str) -> FabricObject | None:
        await self._ensure_schema()
        async with self._conn() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM fabric_objects WHERE id = ?", (obj_id,)) as cur:
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
        self, from_id: str, to_id: str, link_type: str, properties: dict[str, Any] | None = None
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
                " link_type, properties)"
                " VALUES (?, ?, ?, ?, ?)",
                (
                    lnk.id,
                    lnk.from_object_id,
                    lnk.to_object_id,
                    lnk.link_type,
                    json.dumps(lnk.properties),
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
    ) -> tuple[list[FabricLink], int]:
        """List links with optional filters on endpoints and link_type.

        Returns ``(links, total)`` where ``total`` is the unpaginated count.
        All filter arguments are bound parameters — no query-string
        concatenation, so SQL injection through link_type is not possible.
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
        self, obj_id: str, link_type: str | None = None
    ) -> list[FabricObject]:
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
                params = (obj_id, obj_id, link_type)
            else:
                query = (
                    "SELECT o.* FROM fabric_objects o JOIN fabric_links l "
                    "ON (o.id = l.to_object_id AND l.from_object_id = ?) "
                    "OR (o.id = l.from_object_id AND l.to_object_id = ?)"
                )
                params = (obj_id, obj_id)
            async with db.execute(query, params) as cur:
                return [self._row_to_object(row) async for row in cur]

    # --- Query ---

    async def query(self, q: FabricQuery) -> FabricQueryResult:
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
