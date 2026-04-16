# Fabric — lightweight ontology layer for Paw OS.
# Created: 2026-03-28 — Objects, links, properties in SQLite.
# Maps raw data into typed business objects with relationships
# so agents can reason across data.
#
# Updated: 2026-04-16 (feat/fabric-journal-projection) — exported the journal-backed
# slice (FabricJournalStore, FabricProjection, policy helpers, event payload types).
# The legacy SQLite FabricStore still ships here for types + links; object lifecycle
# + scope-filtered queries are the journal path. See ee/fabric/journal_store.py for
# the rationale (Wave 3 / Org Architecture RFC, Phase 3; supersedes #938).

from ee.fabric.events import (
    ACTION_OBJECT_ARCHIVED,
    ACTION_OBJECT_CREATED,
    ACTION_OBJECT_UPDATED,
    ALL_FABRIC_ACTIONS,
    FABRIC_ACTION_PREFIX,
    object_archived_payload,
    object_created_payload,
    object_updated_payload,
)
from ee.fabric.journal_store import FabricJournalStore
from ee.fabric.models import FabricLink, FabricObject, FabricQuery, ObjectType, PropertyDef
from ee.fabric.policy import (
    DEFAULT_ALLOW_UNSCOPED,
    PolicyDecision,
    decide,
    filter_visible,
    visible,
)
from ee.fabric.projection import FabricProjection
from ee.fabric.store import FabricStore

__all__ = [
    # Legacy SQLite store — still the home for types + links.
    "FabricStore",
    # Journal-backed object lifecycle (Wave 3).
    "FabricJournalStore",
    "FabricProjection",
    # Event payload shape — callers emitting Fabric events out of band should
    # use these helpers instead of building payload dicts by hand.
    "ACTION_OBJECT_ARCHIVED",
    "ACTION_OBJECT_CREATED",
    "ACTION_OBJECT_UPDATED",
    "ALL_FABRIC_ACTIONS",
    "FABRIC_ACTION_PREFIX",
    "object_archived_payload",
    "object_created_payload",
    "object_updated_payload",
    # Scope policy — shared with the retrieval log (same containment rules
    # everywhere so results don't diverge between Fabric and paw-runtime).
    "DEFAULT_ALLOW_UNSCOPED",
    "PolicyDecision",
    "decide",
    "filter_visible",
    "visible",
    # Pydantic models (unchanged).
    "ObjectType",
    "PropertyDef",
    "FabricObject",
    "FabricLink",
    "FabricQuery",
]
