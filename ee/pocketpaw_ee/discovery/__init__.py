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
#
# Updated 2026-06-19 (SZD-6 / feat/szd-6-integration): added the orchestrate
# layer (``run_discovery_and_propose`` + ``assemble_discovery_pocket``) that wires
# a DiscoveryRun's OntologyDraft into the two gated Instinct proposals (Fabric
# objects + a starter Pocket) a human reviews, with key-confidence gating and
# supersede-on-rerun.
#
# Updated 2026-06-20 (S2-K1 / feat/szd-slice2-discovery): added
# ``KbCompileDigester`` — a second Digester for UNSTRUCTURED exhaust (ticket /
# email / chat text). It compiles the text into a kb-go wiki ON-BOX via the
# keyless ``kb convo ingest`` path (never ``kb ingest`` / ``kb build``, which
# POST to Anthropic) and infers the OntologyDraft from the compiled articles.

from __future__ import annotations

from pocketpaw_ee.discovery.digester import Digester, StructuredShapeDigester
from pocketpaw_ee.discovery.kb_compile import KbCompileDigester
from pocketpaw_ee.discovery.models import (
    DraftLink,
    DraftObject,
    DraftObjectType,
    OntologyDraft,
)
from pocketpaw_ee.discovery.orchestrate import (
    DISCOVERY_MARKER_KEY,
    KEY_CONFIDENCE_FLOOR,
    DiscoveryProposalResult,
    assemble_discovery_pocket,
    run_discovery_and_propose,
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
    "KbCompileDigester",
    "OntologyDraft",
    "DraftObjectType",
    "DraftObject",
    "DraftLink",
    "DiscoveryRun",
    "DiscoveryRunOptions",
    "ReadAction",
    "DEFAULT_SAMPLE_CAP",
    "run_discovery_and_propose",
    "assemble_discovery_pocket",
    "DiscoveryProposalResult",
    "KEY_CONFIDENCE_FLOOR",
    "DISCOVERY_MARKER_KEY",
]
