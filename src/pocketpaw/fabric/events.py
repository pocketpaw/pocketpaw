# ee/fabric/events.py — Canonical event payload types for Fabric's journal projection.
# Created: 2026-04-16 (feat/fabric-journal-projection) — Wave 3 / Org Architecture RFC,
# Phase 3. Supersedes #938, which tried to bolt scope filtering onto the legacy SQLite
# FabricStore. That design had two blockers: (1) schema migration bug where existing
# DBs never got the `scope` column, and (2) pagination leak where post-filter results
# paired with pre-filter totals let callers infer hidden objects exist.
#
# Rewriting Fabric writes as journal events resolves both blockers by construction —
# the journal is append-only (no migrations), and the projection only ever sees
# post-filter state (no way to leak a pre-filter count). This file pins the three
# event shapes that drive the projection: created, updated, archived.
#
# Actions use the `fabric.object.*` namespace so the projection can filter cheaply
# with ``journal.query(action=...)``. Payloads are JSON-serializable dicts — we
# intentionally do not embed Pydantic models into the journal to keep the stored
# representation stable across Fabric refactors.

from __future__ import annotations

from typing import Any

# ---------------------------------------------------------------------------
# Action names — the journal projection keys off these. Keep them stable;
# changing an action name requires a migration event on every existing
# journal because the projection can no longer replay the old events.
# ---------------------------------------------------------------------------

ACTION_OBJECT_CREATED = "fabric.object.created"
ACTION_OBJECT_UPDATED = "fabric.object.updated"
ACTION_OBJECT_ARCHIVED = "fabric.object.archived"

FABRIC_ACTION_PREFIX = "fabric.object."

ALL_FABRIC_ACTIONS = (
    ACTION_OBJECT_CREATED,
    ACTION_OBJECT_UPDATED,
    ACTION_OBJECT_ARCHIVED,
)


# ---------------------------------------------------------------------------
# Payload builders — small, boring functions that shape the dict we hand to
# ``EventEntry.payload``. Kept at module scope (not methods) so the store
# and the migration tool can both reach them without dragging in class state.
# ---------------------------------------------------------------------------


def object_created_payload(
    *,
    object_id: str,
    type_id: str,
    type_name: str,
    properties: dict[str, Any],
    source_connector: str | None = None,
    source_id: str | None = None,
) -> dict[str, Any]:
    """Payload for ``fabric.object.created``.

    The projection reconstructs a full FabricObject from this plus the scope
    list stored on the EventEntry itself — we do not duplicate scope inside
    the payload, since the journal's scope column is the canonical source
    of truth and the one the engine filters on.
    """

    return {
        "object_id": object_id,
        "type_id": type_id,
        "type_name": type_name,
        "properties": dict(properties),
        "source_connector": source_connector,
        "source_id": source_id,
    }


def object_updated_payload(
    *,
    object_id: str,
    properties: dict[str, Any],
) -> dict[str, Any]:
    """Payload for ``fabric.object.updated``.

    Properties are a partial dict — the projection merges on top of the
    existing object state. Full replacement semantics would require
    load-and-diff on the caller side and create more subtle race windows.
    """

    return {
        "object_id": object_id,
        "properties": dict(properties),
    }


def object_archived_payload(*, object_id: str, reason: str = "") -> dict[str, Any]:
    """Payload for ``fabric.object.archived``.

    We record archive as an event (not a delete) so replay preserves the
    object's history. The projection skips archived objects from the
    current-state view but audit queries can still walk them.
    """

    return {
        "object_id": object_id,
        "reason": reason,
    }
