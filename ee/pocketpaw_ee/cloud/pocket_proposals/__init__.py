# ee/cloud/pocket_proposals/__init__.py — the gated starter-Pocket-create Instinct
# proposal type (a peer of ``_fabric_objects`` / ``_pocket_write`` /
# ``_code_change`` / ``_external_action`` / ``_belt_plan`` / ``_artifact_change``).
# Created: 2026-06-19 (SZD-5b — _pocket_create proposal type) — a proposed starter
#   Pocket (a rippleSpec + name, optional template slug) staged for human review
#   through the Instinct gate. "Sovereign zero-setup discovery" infers a starter
#   Pocket for a tenant and proposes "create this Pocket"; a human approves or
#   rejects in The Tray; on approve the executor creates the Pocket via the
#   canonical, workspace-scoped ``pockets.service.create`` write path. Fully
#   generic — no domain-specific logic.
#
# Re-exports the propose helper + the apply-on-approve executor so callers (the
# discovery surface, the instinct router) import from the package root, mirroring
# how the Fabric-objects gate is imported.

from __future__ import annotations

from pocketpaw_ee.cloud.pocket_proposals.executor import execute_approved_pocket_create
from pocketpaw_ee.cloud.pocket_proposals.propose import (
    POCKET_CREATE_KIND,
    POCKET_CREATE_PARAM_KEY,
    POCKET_CREATE_SCHEMA,
    propose_pocket,
)

__all__ = [
    "POCKET_CREATE_KIND",
    "POCKET_CREATE_PARAM_KEY",
    "POCKET_CREATE_SCHEMA",
    "execute_approved_pocket_create",
    "propose_pocket",
]
