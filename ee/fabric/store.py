# Fabric store — async SQLite operations for the ontology layer.
# Created: 2026-03-28 — CRUD for object types, objects, and links.

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import aiosqlite

from ee.fabric.models import (
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
