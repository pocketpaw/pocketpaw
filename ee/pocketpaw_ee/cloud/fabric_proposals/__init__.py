# ee/cloud/fabric_proposals/__init__.py — the gated Fabric-ontology Instinct
# proposal type (a peer of ``_pocket_write`` / ``_code_change`` /
# ``_external_action`` / ``_belt_plan`` / ``_artifact_change``).
# Created: 2026-06-19 (SZD-5a — _fabric_objects proposal type) — a proposed
#   Fabric ontology (object types + objects + links) staged for human review
#   through the Instinct gate. "Sovereign zero-setup discovery" infers a tenant's
#   ontology from connector data and proposes "create these Fabric objects"; a
#   human approves or rejects in The Tray; on approve the executor materialises
#   the ontology in Fabric via the canonical, workspace-scoped, idempotent
#   ``connectors.fabric_ingest.ingest_records`` upsert loop. Fully generic — no
#   domain-specific logic.
#
# Re-exports the propose helper + the apply-on-approve executor so callers (the
# discovery surface, the instinct router) import from the package root, mirroring
# how the external-action gate is imported.

from __future__ import annotations

from pocketpaw_ee.cloud.fabric_proposals.executor import execute_approved_fabric_objects
from pocketpaw_ee.cloud.fabric_proposals.propose import (
    FABRIC_OBJECTS_KIND,
    FABRIC_OBJECTS_PARAM_KEY,
    FABRIC_OBJECTS_SCHEMA,
    propose_fabric_objects,
)

__all__ = [
    "FABRIC_OBJECTS_KIND",
    "FABRIC_OBJECTS_PARAM_KEY",
    "FABRIC_OBJECTS_SCHEMA",
    "execute_approved_fabric_objects",
    "propose_fabric_objects",
]
