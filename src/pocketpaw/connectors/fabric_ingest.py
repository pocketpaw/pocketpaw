# Connector -> Fabric ingestion — the reusable record-to-typed-object mapper.
# Created: 2026-06-11 (gap1-connfabric slice).
#
# WHY THIS EXISTS
# ---------------
# Until now the "connector data lands as typed Fabric objects with provenance"
# claim was hollow: connector ``sync()`` methods returned ``records_synced=0``
# stubs, and the only connector->Fabric mapping in the tree lived *inside a test*
# (tests/cloud/test_e2e_connector_to_fabric.py hand-rolls the loop). Connector
# data otherwise reached the brain only as BM25 text in kb-go, never as queryable
# typed objects.
#
# THE PATTERN (what other connectors copy)
# ----------------------------------------
# A connector declares ONE ``FabricMapping`` describing how its records become a
# typed Fabric object:
#
#   * ``type_name`` / ``type_description`` / ``properties`` — the Fabric ObjectType
#     to ensure-exists (idempotent: defined once, reused thereafter).
#   * ``source_id_field`` — the record key holding the stable upstream id
#     (a calendar event id, a Stripe invoice id). This is the idempotency key.
#   * ``field_map`` — {fabric_property: record_key} projection. A callable value
#     is allowed for derived/normalized fields.
#
# Then it calls ``ingest_records(store, connector, records, mapping, workspace_id)``.
# For each record the ingester:
#   1. ensures the ObjectType exists (define-once, by name),
#   2. extracts ``source_id`` and projects the record through ``field_map``,
#   3. UPSERTS by ``(source_connector, source_id)`` — update if the object already
#      exists (re-sync refreshes properties), else create with provenance stamped
#      (``source_connector`` + ``source_id``). Re-ingest is therefore idempotent:
#      N re-syncs of the same records = N stable objects, not N*records.
#
# SCOPE: pure OSS — depends only on the OSS FabricStore. The EE connector service
# can call this from its sync path later; nothing here imports pocketpaw_ee, so
# the EE/OSS import boundary is preserved.

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from pocketpaw.fabric.models import PropertyDef
from pocketpaw.fabric.store import FabricStore

# A field projection value is either a record key (str) or a callable that
# derives the value from the whole record (for normalization / joins).
FieldSource = str | Callable[[Mapping[str, Any]], Any]


@dataclass(frozen=True)
class FabricMapping:
    """Declares how one connector's records map to a typed Fabric object.

    This is the unit other connectors reuse. Keep it data-only: no I/O, no
    connector-specific imports, so it round-trips cleanly and is trivial to test.
    """

    type_name: str
    source_id_field: str
    field_map: dict[str, FieldSource]
    properties: list[PropertyDef] = field(default_factory=list)
    type_description: str = ""
    type_icon: str = "box"
    type_color: str = "#0A84FF"

    def extract_source_id(self, record: Mapping[str, Any]) -> str | None:
        """Pull the stable upstream id from a record, or None if absent/empty."""
        raw = record.get(self.source_id_field)
        if raw is None:
            return None
        sid = str(raw).strip()
        return sid or None

    def project(self, record: Mapping[str, Any]) -> dict[str, Any]:
        """Project a raw connector record into Fabric object properties."""
        out: dict[str, Any] = {}
        for prop, src in self.field_map.items():
            if callable(src):
                out[prop] = src(record)
            else:
                out[prop] = record.get(src)
        return out


@dataclass
class IngestResult:
    """Summary of an ingest run — what landed in Fabric and how."""

    type_name: str
    created: int = 0
    updated: int = 0
    skipped: int = 0  # records with no usable source_id
    object_ids: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return self.created + self.updated


async def ensure_type(store: FabricStore, mapping: FabricMapping) -> str:
    """Ensure the mapping's ObjectType exists; return its ``type_id``.

    Define-once-by-name: if a type with this name already exists (a prior sync,
    a manually-defined ontology type, the EE fabric registry), reuse it rather
    than creating a second type that would split the same logical objects across
    two ``type_id``s. Name match is case-insensitive (see get_type_by_name).
    """
    existing = await store.get_type_by_name(mapping.type_name)
    if existing is not None:
        return existing.id
    created = await store.define_type(
        name=mapping.type_name,
        properties=mapping.properties,
        description=mapping.type_description,
        icon=mapping.type_icon,
        color=mapping.type_color,
    )
    return created.id


async def ingest_records(
    store: FabricStore,
    connector: str,
    records: Iterable[Mapping[str, Any]],
    mapping: FabricMapping,
    workspace_id: str | None = None,
) -> IngestResult:
    """Map connector records into typed Fabric objects with provenance (idempotent).

    Args:
        store: the OSS FabricStore to persist into.
        connector: the connector name, stamped as ``source_connector`` provenance.
        records: raw connector records (e.g. Calendar event dicts).
        mapping: the FabricMapping declaring type + field projection + id field.
        workspace_id: W4a tenancy scope; threaded into both the dedup read and
            the create write so an ingest stays inside its tenant. ``None`` for
            single-tenant / OSS callers.

    Returns:
        IngestResult with created / updated / skipped counts and the object ids.

    Idempotency: keyed on ``(connector, source_id)``. A record whose source_id
    already has an object UPDATES that object (properties refreshed via the
    store's merge-update); a new source_id CREATES one with provenance. Records
    lacking a usable source_id are skipped (counted) — without a stable key they
    cannot be deduplicated and would silently duplicate on every sync.
    """
    type_id = await ensure_type(store, mapping)
    result = IngestResult(type_name=mapping.type_name)

    for record in records:
        source_id = mapping.extract_source_id(record)
        if source_id is None:
            result.skipped += 1
            continue
        properties = mapping.project(record)

        existing = await store.get_object_by_source(
            source_connector=connector,
            source_id=source_id,
            workspace_id=workspace_id,
        )
        if existing is not None:
            updated = await store.update_object(existing.id, properties)
            result.updated += 1
            result.object_ids.append((updated or existing).id)
        else:
            created = await store.create_object(
                type_id=type_id,
                properties=properties,
                source_connector=connector,
                source_id=source_id,
                workspace_id=workspace_id,
            )
            result.created += 1
            result.object_ids.append(created.id)

    return result
