# pocketpaw_ee/discovery/ — sovereign zero-setup ontology discovery (digesters).
#
# Created: 2026-06-19 (SZD-3 / feat/szd-3-digester) — the digester layer for
# sovereign zero-setup discovery. A Digester samples a connector's records and
# reverse-engineers a candidate ontology (an OntologyDraft) that downstream code
# turns into FabricMapping objects (for fabric_ingest.ingest_records) and a
# fabric-objects proposal (object_types + objects + links).
#
# Pure logic: no DB, no network, no async. The first impl is
# ``StructuredShapeDigester`` (structured ``{type: [records]}`` input).

from __future__ import annotations

from pocketpaw_ee.discovery.digester import Digester, StructuredShapeDigester
from pocketpaw_ee.discovery.models import (
    DraftLink,
    DraftObject,
    DraftObjectType,
    OntologyDraft,
)

__all__ = [
    "Digester",
    "StructuredShapeDigester",
    "OntologyDraft",
    "DraftObjectType",
    "DraftObject",
    "DraftLink",
]
