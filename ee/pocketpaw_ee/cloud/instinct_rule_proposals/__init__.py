# ee/cloud/instinct_rule_proposals/__init__.py — the gated governed-rule-create
# Instinct proposal type (a peer of ``_pocket_create`` / ``_fabric_objects`` /
# ``_pocket_write`` / ``_code_change`` / ``_external_action`` / ``_belt_plan`` /
# ``_artifact_change``).
# Created: 2026-06-20 (S2-R3 — _instinct_rule proposal type) — a proposed governed
#   rule (a CEL ``when`` + a gate ``action``, scoped to a tenant, with confidence +
#   provenance) staged for human review through the Instinct gate. "Sovereign
#   zero-setup discovery" reverse-engineers a rule from a tenant's exhaust and
#   proposes "create this rule"; a human approves / edits / rejects in The Tray; on
#   approve the executor persists the rule via the canonical, workspace-scoped
#   ``rules.service.create_rule`` write path. Fully generic — no domain-specific logic.
#
# Re-exports the propose helper + the apply-on-approve executor + the three blob
# constants so callers (the discovery surface, the instinct router) import from the
# package root, mirroring how the Pocket-create gate is imported.

from __future__ import annotations

from pocketpaw_ee.cloud.instinct_rule_proposals.executor import execute_approved_instinct_rule
from pocketpaw_ee.cloud.instinct_rule_proposals.propose import (
    INSTINCT_RULE_KIND,
    INSTINCT_RULE_PARAM_KEY,
    INSTINCT_RULE_SCHEMA,
    propose_instinct_rule,
)

__all__ = [
    "INSTINCT_RULE_KIND",
    "INSTINCT_RULE_PARAM_KEY",
    "INSTINCT_RULE_SCHEMA",
    "execute_approved_instinct_rule",
    "propose_instinct_rule",
]
