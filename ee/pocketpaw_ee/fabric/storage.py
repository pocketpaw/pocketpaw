# ee/pocketpaw_ee/fabric/storage.py
# Created: 2026-05-28 (feat/wave-4c-fabric-registry) — workspace-scoped
# SQLite storage that backs the concrete ``FabricRegistry`` Protocol
# implementation in ``ee/pocketpaw_ee/fabric/registry.py``. The store
# holds the registered entity types, properties, and links that Wave
# 4b's ``tier: registered`` lint contract consumes. Sync stdlib
# ``sqlite3`` only — wrap in ``asyncio.to_thread`` at FastAPI call
# sites if needed. Sibling to ``pocketpaw.fabric.store.FabricStore``
# (OSS, async, holds the live ontology object data); the ``Workspace``
# prefix flags the workspace-scoped, registry-only role.
"""SQLite-backed workspace-scoped Fabric registry store.

The store is the write-side of the concrete ``FabricRegistry``
implementation that PR 2g promised. Wave 4b ships the OSS-side mock
(:class:`pocketpaw.bundled_templates.JSONFileFabricRegistry`); Wave 4c
adds the EE concrete impl so the same Protocol satisfies a real
workspace's data.

Schema
------

Three tables. Each row carries a ``workspace`` column and every read
filters on it — multi-tenant isolation is enforced by the index, not by
the file path. The single shared file simplifies v0 deployment (one
SQLite to back up, one to migrate); a future PR can shard to
per-workspace files without changing the public Python surface.

* ``fabric_entity_types(name, workspace, created_at)`` — registered
  ObjectType names.
* ``fabric_entity_properties(entity_type, property_name, property_type,
  workspace)`` — property declarations.
* ``fabric_registry_links(name, from_type, to_type, workspace)`` —
  directed link declarations.

Why a separate store from ``pocketpaw.fabric.FabricStore``
---------------------------------------------------------

The OSS-side ``FabricStore`` holds the live ontology data (object
instances + their relationships). This EE-side store holds the
*registry* — which types and links are *declared* — so the
``tier: registered`` lint contract has something to check against. The
data layers are intentionally split: lint-time validation should not
require booting the full Fabric object store. A future PR can
rendezvous the two stores once the EE wiring for "auto-register an
ObjectType definition when one is created via the Fabric API" lands.

Concurrency
-----------

Each method opens, executes, and closes its own ``sqlite3`` connection.
That keeps the store thread-safe under FastAPI's default thread-pool
executor without an explicit lock. Hot-path callers that want
connection pooling can build it on top; v0 favours correctness over
throughput because the registry surface is read-mostly and writes are
admin-driven, not user-driven.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS fabric_entity_types (
    name TEXT NOT NULL,
    workspace TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (workspace, name)
);

CREATE TABLE IF NOT EXISTS fabric_entity_properties (
    entity_type TEXT NOT NULL,
    property_name TEXT NOT NULL,
    property_type TEXT NOT NULL DEFAULT 'string',
    workspace TEXT NOT NULL,
    PRIMARY KEY (workspace, entity_type, property_name)
);

CREATE TABLE IF NOT EXISTS fabric_registry_links (
    name TEXT NOT NULL,
    from_type TEXT NOT NULL,
    to_type TEXT NOT NULL,
    workspace TEXT NOT NULL,
    PRIMARY KEY (workspace, name, from_type, to_type)
);

CREATE INDEX IF NOT EXISTS idx_fabric_entity_types_workspace
    ON fabric_entity_types(workspace, name);
CREATE INDEX IF NOT EXISTS idx_fabric_entity_properties_workspace
    ON fabric_entity_properties(workspace, entity_type);
CREATE INDEX IF NOT EXISTS idx_fabric_registry_links_workspace
    ON fabric_registry_links(workspace, from_type, to_type);
"""


DEFAULT_DB_PATH = Path.home() / ".pocketpaw" / "fabric_registry.db"


class WorkspaceFabricStore:
    """Workspace-scoped SQLite store for the Fabric registry contract.

    The store is the write-side; :class:`WorkspaceFabricRegistry` is the
    read-side Protocol implementation that callers hand to the Wave 4b
    validator and the runtime resolver.
    """

    __slots__ = ("_db_path",)

    def __init__(self, db_path: str | Path | None = None) -> None:
        """Create / open the registry SQLite database at ``db_path``.

        When ``db_path`` is ``None`` the default
        ``~/.pocketpaw/fabric_registry.db`` location is used. Parent
        directories are created as needed so the first registration on a
        fresh install does not fail with ``FileNotFoundError``.
        """
        path = Path(db_path) if db_path is not None else DEFAULT_DB_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db_path = path
        self._ensure_schema()

    # -- Schema bootstrap ----------------------------------------------------

    def _ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(_SCHEMA_SQL)
            conn.commit()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        # Foreign keys are not strictly needed for this schema (we don't
        # declare any), but enabling the pragma protects future
        # extensions from silent constraint violations.
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    # -- Entity types --------------------------------------------------------

    def register_entity_type(self, workspace_id: str, name: str) -> None:
        """Register ``name`` as an ObjectType in ``workspace_id``.

        Idempotent — re-registering the same type is a no-op
        (``INSERT OR IGNORE``)."""
        with self._connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO fabric_entity_types (name, workspace) VALUES (?, ?)",
                (name, workspace_id),
            )
            conn.commit()

    def list_entity_types(self, workspace_id: str) -> list[str]:
        """Return every entity-type name registered in ``workspace_id``,
        sorted alphabetically for stable callers."""
        with self._connect() as conn:
            cur = conn.execute(
                "SELECT name FROM fabric_entity_types WHERE workspace = ? ORDER BY name",
                (workspace_id,),
            )
            return [row[0] for row in cur.fetchall()]

    def entity_exists(self, workspace_id: str, name: str) -> bool:
        """Return True iff ``name`` is registered in ``workspace_id``."""
        with self._connect() as conn:
            cur = conn.execute(
                "SELECT 1 FROM fabric_entity_types WHERE workspace = ? AND name = ? LIMIT 1",
                (workspace_id, name),
            )
            return cur.fetchone() is not None

    def delete_entity_type(self, workspace_id: str, name: str) -> None:
        """Remove ``name`` from ``workspace_id``.

        Cascades to ``fabric_entity_properties`` (all rows for the
        entity) and ``fabric_registry_links`` (any link with ``name`` on
        either end). Other workspaces are not affected because every
        statement filters on ``workspace``.
        """
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM fabric_entity_properties WHERE workspace = ? AND entity_type = ?",
                (workspace_id, name),
            )
            conn.execute(
                "DELETE FROM fabric_registry_links "
                "WHERE workspace = ? AND (from_type = ? OR to_type = ?)",
                (workspace_id, name, name),
            )
            conn.execute(
                "DELETE FROM fabric_entity_types WHERE workspace = ? AND name = ?",
                (workspace_id, name),
            )
            conn.commit()

    # -- Properties ----------------------------------------------------------

    def register_property(
        self,
        workspace_id: str,
        entity_type: str,
        property_name: str,
        property_type: str = "string",
    ) -> None:
        """Declare ``property_name`` on ``entity_type`` in
        ``workspace_id``.

        ``INSERT OR REPLACE`` semantics — re-registering the same
        property updates the stored ``property_type`` rather than
        failing. v0 only stores a plain-string type; enums / foreign
        keys are out of scope for Wave 4c.
        """
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO fabric_entity_properties "
                "(entity_type, property_name, property_type, workspace) "
                "VALUES (?, ?, ?, ?)",
                (entity_type, property_name, property_type, workspace_id),
            )
            conn.commit()

    def get_properties(self, workspace_id: str, entity_type: str) -> set[str]:
        """Return the set of declared property names for ``entity_type``
        in ``workspace_id``. Unknown entity types return ``set()`` to
        match the :class:`NullFabricRegistry` contract."""
        with self._connect() as conn:
            cur = conn.execute(
                "SELECT property_name FROM fabric_entity_properties "
                "WHERE workspace = ? AND entity_type = ?",
                (workspace_id, entity_type),
            )
            return {row[0] for row in cur.fetchall()}

    # -- Links ---------------------------------------------------------------

    def register_link(
        self,
        workspace_id: str,
        name: str,
        from_type: str,
        to_type: str,
    ) -> None:
        """Register a directed link called ``name`` connecting
        ``from_type`` -> ``to_type`` in ``workspace_id``. Idempotent."""
        with self._connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO fabric_registry_links "
                "(name, from_type, to_type, workspace) VALUES (?, ?, ?, ?)",
                (name, from_type, to_type, workspace_id),
            )
            conn.commit()

    def link_exists(
        self,
        workspace_id: str,
        name: str,
        from_type: str,
        to_type: str,
    ) -> bool:
        """Return True iff a link called ``name`` connects
        ``from_type`` -> ``to_type`` in ``workspace_id``. Direction
        matters — the reverse pair is not implied."""
        with self._connect() as conn:
            cur = conn.execute(
                "SELECT 1 FROM fabric_registry_links "
                "WHERE workspace = ? AND name = ? AND from_type = ? AND to_type = ? "
                "LIMIT 1",
                (workspace_id, name, from_type, to_type),
            )
            return cur.fetchone() is not None


__all__ = [
    "DEFAULT_DB_PATH",
    "WorkspaceFabricStore",
]
