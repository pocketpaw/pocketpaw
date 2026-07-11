# ee/cloud/fabric_conflicts/__init__.py — the conflict-stewardship Instinct
# proposal type (a peer of ``_instinct_rule`` / ``_fabric_objects`` /
# ``_pocket_create`` / ``_pocket_write`` / ``_code_change`` /
# ``_external_action`` / ``_belt_plan`` / ``_artifact_change`` /
# ``_admin_action``).
# Created: 2026-07-10 (FST-6 — the conflict lifecycle) — the hybrid conflict UX:
#   the source-truth resolver auto-resolves every conflict it can RANK; the
#   un-rankable残り (``Resolution.unresolvable=True``) is swept into ONE Instinct
#   proposal per conflicted property for a human steward to arbitrate. Approve
#   (optionally editing the choice) → the executor PINs the chosen statement via
#   the canonical OSS steward verb (``FabricStore.pin_statement``); reject → the
#   policy's provisional winner stands, no statement change (router owns the
#   reject-close). Precedent mirrored: ``instinct_rule_proposals`` (one proposal
#   per subject, propose stages / executor fires on approve).
#
# Re-exports the propose + sweep helpers, the apply-on-approve executor, and the
# blob constants so callers (the ingest sweep, the instinct router) import from
# the package root, mirroring the instinct-rule gate.

from __future__ import annotations

from pocketpaw_ee.cloud.fabric_conflicts.executor import execute_approved_fabric_conflict
from pocketpaw_ee.cloud.fabric_conflicts.propose import (
    FABRIC_CONFLICT_KIND,
    FABRIC_CONFLICT_PARAM_KEY,
    FABRIC_CONFLICT_SCHEMA,
    STEWARDSHIP_QUEUE_WARN_THRESHOLD,
    propose_fabric_conflict,
    sweep_conflicts_to_proposals,
)

__all__ = [
    "FABRIC_CONFLICT_KIND",
    "FABRIC_CONFLICT_PARAM_KEY",
    "FABRIC_CONFLICT_SCHEMA",
    "STEWARDSHIP_QUEUE_WARN_THRESHOLD",
    "execute_approved_fabric_conflict",
    "propose_fabric_conflict",
    "sweep_conflicts_to_proposals",
]
