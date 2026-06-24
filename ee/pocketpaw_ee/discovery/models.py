# pocketpaw_ee/discovery/models.py — the OntologyDraft shape produced by a Digester.
#
# Created: 2026-06-19 (SZD-3 / feat/szd-3-digester) — the data contract for
# sovereign zero-setup discovery. A Digester samples a connector's records and
# reverse-engineers a candidate ontology into an ``OntologyDraft``. The draft is
# designed to be DIRECTLY usable two ways:
#
#   1. Build a ``pocketpaw.connectors.fabric_ingest.FabricMapping`` per type —
#      each ``DraftObjectType`` carries the inferred ``source_id_field`` and a
#      ``field_map`` projection (the two things ``ingest_records`` requires
#      beyond ``type_name``), plus the ``PropertyDef`` list for the ObjectType.
#   2. Feed a fabric-objects proposal — ``object_types`` + ``objects`` +
#      ``links`` map onto ObjectType / FabricObject / FabricLink.
#
# Every inference (type, property type, primary key, link) carries a
# ``confidence`` in [0, 1] so a downstream gate can sort strong signals from
# weak ones. Pure data, no I/O — depends only on pydantic and the OSS
# ``PropertyDef`` model.

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from pocketpaw.fabric.models import PropertyDef


def _clamp(value: float) -> float:
    """Clamp a confidence score into [0.0, 1.0]."""
    return max(0.0, min(1.0, value))


class DraftObjectType(BaseModel):
    """An inferred object type plus everything needed to build its FabricMapping.

    The ``source_id_field`` / ``field_map`` pair is exactly what
    ``fabric_ingest.FabricMapping`` needs beyond ``type_name`` (the projection
    + the idempotency key). ``properties`` is the ObjectType's PropertyDef list.
    ``key_confidence`` reflects how strong the primary-key signal was; when no
    usable key was found, ``source_id_field`` is ``None`` and confidence is low
    (objects-only — not safe to dedup or link on).
    """

    name: str
    properties: list[PropertyDef] = Field(default_factory=list)
    # The inferred primary-key / idempotency field. ``None`` when no stable key
    # could be inferred — the type then degrades to objects-only.
    source_id_field: str | None = None
    # {fabric_property: record_key} projection — feeds FabricMapping.field_map.
    field_map: dict[str, str] = Field(default_factory=dict)
    # Confidence in the type itself (shape consistency across records).
    confidence: float = 0.0
    # Confidence in the inferred primary key specifically.
    key_confidence: float = 0.0
    record_count: int = 0

    def to_fabric_mapping_kwargs(self) -> dict[str, Any]:
        """Return kwargs ready to construct a ``FabricMapping``.

        Usage::

            from pocketpaw.connectors.fabric_ingest import FabricMapping
            mapping = FabricMapping(**draft_type.to_fabric_mapping_kwargs())

        Only valid when ``source_id_field`` is set; callers should check
        ``key_confidence`` / ``source_id_field`` first for low-confidence types.
        """
        return {
            "type_name": self.name,
            "source_id_field": self.source_id_field or "",
            "field_map": dict(self.field_map),
            "properties": list(self.properties),
        }


class DraftObject(BaseModel):
    """A single inferred object row, ready to become a FabricObject.

    ``source_id`` is the value extracted from the type's inferred
    ``source_id_field`` (``None`` when the type has no key). ``properties`` is
    the raw projected record.
    """

    type_name: str
    source_id: str | None = None
    properties: dict[str, Any] = Field(default_factory=dict)


class DraftLink(BaseModel):
    """An inferred relationship between two objects (by source_id).

    A link is inferred when a field on one type holds values that match the
    primary keys of another type (a foreign-key shape). The endpoints are
    referenced by ``(type_name, source_id)`` rather than Fabric object ids,
    since the draft pre-dates persistence — the proposal step resolves these to
    real ``FabricObject`` ids.
    """

    from_type: str
    from_source_id: str
    to_type: str
    to_source_id: str
    link_type: str
    # The field on ``from_type`` that carried the foreign key.
    via_field: str
    confidence: float = 0.0


class OntologyDraft(BaseModel):
    """A candidate ontology reverse-engineered from sampled connector records.

    Directly consumable to (a) build per-type ``FabricMapping`` objects for
    ``ingest_records`` and (b) feed a fabric-objects proposal (object_types +
    objects + links). Degrades cleanly: empty records → empty draft; no clear
    key → objects-only with low confidence and no links.
    """

    object_types: list[DraftObjectType] = Field(default_factory=list)
    objects: list[DraftObject] = Field(default_factory=list)
    links: list[DraftLink] = Field(default_factory=list)
    # Free-form provenance / notes from the digester (connector name, sample
    # size, degradation flags) — never load-bearing for ingest.
    meta: dict[str, Any] = Field(default_factory=dict)

    def type_by_name(self, name: str) -> DraftObjectType | None:
        """Look up an inferred type by name (case-sensitive), or None."""
        for ot in self.object_types:
            if ot.name == name:
                return ot
        return None

    @property
    def is_empty(self) -> bool:
        return not self.object_types and not self.objects
