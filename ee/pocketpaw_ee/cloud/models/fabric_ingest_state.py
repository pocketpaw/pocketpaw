# FabricIngestState + FabricIngestConfig Beanie documents — Firestore→Fabric ingest.
# Created: 2026-06-11 — generic Firestore→Fabric ingestion worker.
#
# Two collections back the generic ingestion worker:
#
# * ``fabric_ingest_state`` (FabricIngestState) — one row per
#   (workspace, source_id) where source_id is the Firestore collection path
#   being mirrored. Tracks the worker's bookkeeping for that source: whether
#   the first full backfill ran (``backfill_done`` drives backfill-vs-
#   incremental), the high-water cursor, the last run status/error, and a
#   running count of objects ingested. The cursor here is a REAL high-water
#   mark taken from document data (the configured ``cursor_field``, falling
#   back to the snapshot update_time) — NOT a run wall-clock. This is the
#   deliberate improvement over MemberIngestState's wall-clock cursor: a
#   document that arrives late but carries an older updated-at value is still
#   picked up, and re-runs don't re-scan a wall-clock window.
#
# * ``fabric_ingest_config`` (FabricIngestConfig) — one row per workspace
#   holding the per-deployment mapping: which Firestore collections to mirror,
#   which Fabric object type each maps to, the field→property map, the cursor
#   field, and optional link rules. Fully generic: no collection names, field
#   names, or object types are hard-coded in code — they all live here, set
#   per deployment.
#
# Tenancy: every row carries ``workspace`` (indexed); every service read
# filters on it (cloud rule §7). The OSS FabricStore is stamped with this same
# workspace_id on every object/link write so mirrored data is tenant-isolated.

from __future__ import annotations

from datetime import datetime

from beanie import Indexed
from pydantic import BaseModel, Field

from pocketpaw_ee.cloud.models.base import TimestampedDocument


class FabricFieldMapping(BaseModel):
    """One collection→object-type mapping inside a workspace's ingest config.

    Generic by construction — the collection path, target object type, and
    field map all come from the deployment, never from code.

    * ``collection`` — the Firestore collection path to mirror.
    * ``object_type_id`` — the Fabric ObjectType id mirrored documents become.
    * ``field_map`` — Firestore field name → Fabric property name. Only the
      mapped fields are copied onto the object's properties bag; unmapped
      Firestore fields are ignored.
    * ``cursor_field`` — the Firestore field used as the incremental high-water
      mark (typically an ``updated_at`` / ``modified`` timestamp). When a
      document has no value for it, the worker falls back to the document
      snapshot's ``update_time``.
    * ``link_rules`` — optional relationship rules; each links the mirrored
      object to another object resolved by the value of ``via_field``.
    """

    collection: str = Field(min_length=1)
    object_type_id: str = Field(min_length=1)
    field_map: dict[str, str] = Field(default_factory=dict)
    cursor_field: str = ""
    link_rules: list[FabricLinkRule] = Field(default_factory=list)


class FabricLinkRule(BaseModel):
    """An optional link rule on a mapping.

    After a document is mirrored into an object, the worker reads
    ``via_field`` off the document; if it holds the ``source_id`` of another
    already-mirrored object (its full Firestore doc path) of type ``to_type``,
    a ``link_type`` link is created from the new object to that target.

    * ``to_type`` — the object_type_id the target object should be.
    * ``link_type`` — the Fabric link type string (e.g. ``belongs_to``).
    * ``via_field`` — the Firestore field on the source document whose value
      identifies the target document (its Firestore path / source_id).
    """

    to_type: str = Field(min_length=1)
    link_type: str = Field(min_length=1)
    via_field: str = Field(min_length=1)


# Resolve the forward reference declared on FabricFieldMapping.link_rules.
FabricFieldMapping.model_rebuild()


class FabricIngestConfig(TimestampedDocument):
    """Per-workspace Firestore→Fabric mapping config.

    One row per workspace. ``mappings`` is the list of collection→object-type
    rules the worker walks on each sweep. Nothing about the mapping is
    hard-coded — a deployment writes its own collections, object types, field
    maps, and link rules here.
    """

    workspace: Indexed(str)  # type: ignore[valid-type]
    enabled: bool = True
    mappings: list[FabricFieldMapping] = Field(default_factory=list)

    class Settings(TimestampedDocument.Settings):
        name = "fabric_ingest_config"


class FabricIngestState(TimestampedDocument):
    """Per-(workspace, source) ingest bookkeeping for the Fabric worker.

    Tenancy: ``workspace`` is required and indexed; every read filters on it
    (cloud rule §7). ``source_id`` is the Firestore collection path; paired
    with ``workspace`` it is unique per mirrored source, enforced at the
    service layer (upsert-on-write, no Mongo unique index so re-runs are
    forgiving).

    ``cursor`` holds the high-water mark of the newest document ingested from
    this collection on the last successful run — the value of the mapping's
    ``cursor_field`` (or the snapshot ``update_time`` fallback) of the
    most-recently-updated document seen. The incremental pass reads only
    documents with a cursor value strictly greater than this, so a re-run never
    re-ingests an unchanged document and a late-but-older document is still
    picked up on its own merits (the wall-clock flaw in MemberIngestState is
    deliberately not copied).
    """

    workspace: Indexed(str)  # type: ignore[valid-type]
    source_id: str  # the Firestore collection path being mirrored
    status: str = "never"  # "never" | "running" | "ok" | "error"
    backfill_done: bool = False
    last_sync_at: datetime | None = None
    last_error: str = ""
    # High-water mark (string-comparable, typically an RFC3339 timestamp) of
    # the newest document ingested. Empty until the first successful run.
    cursor: str = ""
    # Running total — handy for an operator/status endpoint without a separate
    # metrics store. Not load-bearing for correctness.
    objects_ingested: int = 0

    class Settings(TimestampedDocument.Settings):
        name = "fabric_ingest_state"
