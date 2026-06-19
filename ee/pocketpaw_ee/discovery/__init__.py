# pocketpaw_ee/discovery/ — sovereign zero-setup ontology discovery (digesters).
#
# Created: 2026-06-19 (SZD-3 / feat/szd-3-digester) — the digester layer for
# sovereign zero-setup discovery. A Digester samples a connector's records and
# reverse-engineers a candidate ontology (an OntologyDraft) that downstream code
# turns into FabricMapping objects (for fabric_ingest.ingest_records) and a
# fabric-objects proposal (object_types + objects + links).
#
# The digester is pure logic (no DB, no network, no async); the first impl is
# ``StructuredShapeDigester`` (structured ``{type: [records]}`` input). The
# ``DiscoveryRun`` orchestrator (SZD-4) ties sampling → digest together: it reads
# a workspace's bound connectors via the pocket-less ``ensure_connected(name,
# "ws:<workspace_id>")`` path and feeds the sampled records to the digester.

from __future__ import annotations

from pocketpaw_ee.discovery.digester import Digester, StructuredShapeDigester
from pocketpaw_ee.discovery.models import (
    DraftLink,
    DraftObject,
    DraftObjectType,
    OntologyDraft,
)
from pocketpaw_ee.discovery.run import (
    DEFAULT_SAMPLE_CAP,
    DiscoveryRun,
    DiscoveryRunOptions,
    ReadAction,
)

__all__ = [
    "Digester",
    "StructuredShapeDigester",
    "OntologyDraft",
    "DraftObjectType",
    "DraftObject",
    "DraftLink",
    "DiscoveryRun",
    "DiscoveryRunOptions",
    "ReadAction",
    "DEFAULT_SAMPLE_CAP",
]
