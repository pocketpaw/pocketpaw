# Connector -> Fabric ingestion — the reusable record-to-typed-object mapper.
# Created: 2026-06-11 (gap1-connfabric slice).
# Updated: 2026-06-11 (gap-housekeeping) — ingest_records now threads
#   ``workspace_id`` into the update_object() call on the re-ingest (UPDATE)
#   path, matching the workspace it already passes to get_object_by_source() and
#   create_object(). The store's update_object grew its own W4a tenancy guard, so
#   an idempotent re-sync stays inside the caller's tenant on the write as well
#   as the read.
# Updated: 2026-06-11 (firestore-fabric-ingest) — FabricMapping gains an
#   optional ``type_id``: when set, ensure_type() returns it directly instead
#   of resolving/defining by name. Lets callers whose config references an
#   existing ObjectType by id (the EE Firestore→Fabric worker's per-workspace
#   mapping) ride this same canonical upsert loop instead of hand-rolling a
#   parallel one. Additive; name-based callers are unchanged.
# Updated: 2026-06-19 (SZD-2 — workspace-scope object TYPES) — ensure_type() now
#   threads ``workspace_id`` into both the get_type_by_name() resolve and the
#   define_type() create, so the type catalog stays per-tenant: a connector
#   ingesting into workspace A resolves/defines its ObjectType inside A, and the
#   same logical type name in workspace B resolves to B's own type (or is
#   defined fresh for B) rather than silently reusing A's. ``ingest_records``
#   already carried ``workspace_id`` for the object writes; it now forwards it to
#   ensure_type so the TYPE mint is scoped too. ``None`` (OSS / single-tenant
#   callers) keeps the prior unscoped behavior.
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
    # Optional: a concrete ObjectType id. When set, ``ensure_type`` returns it
    # directly instead of resolving/defining by name — for callers whose config
    # already references an existing type by id (the EE Firestore→Fabric
    # worker's per-workspace mapping does). ``type_name`` then serves only as
    # the reporting label on IngestResult.
    type_id: str | None = None

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


async def ensure_type(
    store: FabricStore, mapping: FabricMapping, workspace_id: str | None = None
) -> str:
    """Ensure the mapping's ObjectType exists; return its ``type_id``.

    When the mapping carries an explicit ``type_id`` (the caller's config
    references an existing type by id), return it directly — no name lookup,
    no implicit define.

    Otherwise, define-once-by-name: if a type with this name already exists in
    the caller's workspace (a prior sync, a manually-defined ontology type, the
    EE fabric registry), reuse it rather than creating a second type that would
    split the same logical objects across two ``type_id``s. Name match is
    case-insensitive (see get_type_by_name).

    SZD-2: ``workspace_id`` scopes both the resolve and the define so the type
    catalog is per-tenant — workspace A's "CalendarEvent" type is neither found
    nor reused when ingesting into workspace B, and a fresh define for B is
    stamped with B's workspace. ``None`` keeps the unscoped behavior for OSS /
    single-tenant callers.
    """
    if mapping.type_id:
        return mapping.type_id
    existing = await store.get_type_by_name(mapping.type_name, workspace_id=workspace_id)
    if existing is not None:
        return existing.id
    created = await store.define_type(
        name=mapping.type_name,
        properties=mapping.properties,
        description=mapping.type_description,
        icon=mapping.type_icon,
        color=mapping.type_color,
        workspace_id=workspace_id,
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
    type_id = await ensure_type(store, mapping, workspace_id=workspace_id)
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
            updated = await store.update_object(existing.id, properties, workspace_id=workspace_id)
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
