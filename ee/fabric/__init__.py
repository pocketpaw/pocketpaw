# Fabric — lightweight ontology layer for Paw OS.
# Created: 2026-03-28 — Objects, links, properties in SQLite.
# Maps raw data into typed business objects with relationships
# so agents can reason across data.

from ee.fabric.models import FabricLink, FabricObject, FabricQuery, ObjectType, PropertyDef
from ee.fabric.store import FabricStore

__all__ = [
    "ObjectType",
    "PropertyDef",
    "FabricObject",
    "FabricLink",
    "FabricQuery",
    "FabricStore",
]
