# dto.py — Request/validation schemas for the Firestore→Fabric ingest worker.
# Created: 2026-06-11 — generic Firestore→Fabric ingestion worker.
# Updated: 2026-07-11 (feat/real-pipeline-s1) — FieldMappingSpec mirrors the
#   model's new additive source discriminator (``source_kind`` +
#   ``connector_id``) so connector-source mappings validate at entry like
#   firestore ones. Also added the transform-surface DTOs the new
#   ``fabric_ingest/router.py`` uses: ``MappingUpsertRequest`` (author a
#   mapping), ``MappingsListResponse`` / ``MappingResponse`` (read side),
#   ``RunNowRequest`` / ``RunNowResponse`` (run-now dispatch result).
#
# Per cloud rule §4 (request/response split) and §6 (validate at entry): the
# service functions re-parse their inputs through these even when called by
# internal callers (the sweep, the scheduler). The mapping config is validated
# here too — a malformed mapping (blank collection, blank object type, a link
# rule missing a field) is rejected before any Firestore read, so a bad
# per-deployment config fails loudly at the entry point rather than silently
# mirroring nothing or crashing mid-sweep.

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


class IngestCollectionRequest(BaseModel):
    """Input to ``ingest_collection`` — the tenant + the source to mirror.

    ``source_id`` is the Firestore collection path. Both are required and
    non-blank: a blank workspace would un-scope the tenant filter and a blank
    collection has nothing to read.
    """

    workspace_id: str = Field(min_length=1)
    source_id: str = Field(min_length=1)

    @field_validator("workspace_id", "source_id")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("must not be blank")
        return v


class SweepRequest(BaseModel):
    """Input to ``run_ingest_sweep`` — the concurrency cap for the fan-out."""

    concurrency: int = Field(default=4, ge=1, le=64)


class LinkRuleSpec(BaseModel):
    """Validation shape for one link rule on a mapping.

    Mirrors ``models.fabric_ingest_state.FabricLinkRule`` but lives in the DTO
    layer so the service can validate a raw config dict at entry without
    importing the Beanie model into router/dto (cloud chokepoint rule).
    """

    to_type: str = Field(min_length=1)
    link_type: str = Field(min_length=1)
    via_field: str = Field(min_length=1)

    @field_validator("to_type", "link_type", "via_field")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("must not be blank")
        return v


class FieldMappingSpec(BaseModel):
    """Validation shape for one collection→object-type mapping.

    A mapping with no ``field_map`` is allowed (the worker still stamps the
    tenancy + provenance fields and can resolve links), but the collection and
    object type are mandatory — without them there is nothing to mirror and
    nowhere to put it.
    """

    collection: str = Field(min_length=1)
    object_type_id: str = Field(min_length=1)
    field_map: dict[str, str] = Field(default_factory=dict)
    cursor_field: str = ""
    link_rules: list[LinkRuleSpec] = Field(default_factory=list)
    # Source discriminator (feat/real-pipeline-s1) — mirrors the model field.
    # "connector" mappings pull records through the OSS ingestor registry;
    # ``connector_id`` defaults to ``collection`` when unset (the collection
    # holds the connector name for connector mappings by convention).
    source_kind: Literal["firestore", "connector"] = "firestore"
    connector_id: str | None = None

    @field_validator("collection", "object_type_id")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("must not be blank")
        return v

    @field_validator("field_map")
    @classmethod
    def _no_blank_property_names(cls, v: dict[str, str]) -> dict[str, str]:
        for src, prop in v.items():
            if not str(src).strip() or not str(prop).strip():
                raise ValueError("field_map keys and values must not be blank")
        return v


class IngestConfigRequest(BaseModel):
    """Validation shape for a whole per-workspace ingest config.

    Used at entry whenever a config is set or read back into the worker. A
    config with an empty ``mappings`` list is structurally valid (the worker
    just has nothing to do for that workspace); each present mapping is fully
    validated by ``FieldMappingSpec``.
    """

    workspace_id: str = Field(min_length=1)
    enabled: bool = True
    mappings: list[FieldMappingSpec] = Field(default_factory=list)

    @field_validator("workspace_id")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("workspace_id must not be blank")
        return v


# ---------------------------------------------------------------------------
# Transform-surface DTOs (feat/real-pipeline-s1) — the fabric_ingest router's
# request/response split (cloud rule §4). The workspace never travels in the
# body; the router injects the caller's active workspace (cloud rule §7).
# ---------------------------------------------------------------------------


class MappingUpsertRequest(FieldMappingSpec):
    """Author one mapping (create-or-replace, keyed on ``collection``).

    Same validated shape as ``FieldMappingSpec`` — subclassed so the router's
    request type names its intent while the service keeps one validation
    source of truth.
    """


class MappingResponse(FieldMappingSpec):
    """One mapping as read back from the workspace's config."""


class MappingsListResponse(BaseModel):
    """The workspace's authored mappings."""

    mappings: list[MappingResponse] = Field(default_factory=list)


class RunNowRequest(BaseModel):
    """Trigger one mapping's ingest immediately. ``collection`` is the
    routing key (the connector name for connector mappings)."""

    collection: str = Field(min_length=1)

    @field_validator("collection")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("collection must not be blank")
        return v


class RunNowResponse(BaseModel):
    """The result dict of ``ingest_collection``, typed for the wire.

    ``status`` is ``"ok"`` or ``"error"`` — a misconfigured mapping (missing
    connector, unregistered ingestor, no mapping) reports here rather than as
    an HTTP 5xx, matching the sweep's never-raise contract.
    """

    workspace_id: str
    source_id: str
    status: str
    mode: str
    objects: int
    cursor: str
    errors: list[Any] = Field(default_factory=list)
