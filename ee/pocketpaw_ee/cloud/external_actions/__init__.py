# ee/cloud/external_actions/__init__.py — the generic gated external-action
# Instinct proposal type (the third gated kind, alongside ``_pocket_write`` and
# ``_code_change``).
# Created: 2026-06-11 (feat/external-action-proposal) — a proposed call to an
#   external system through a bound connector. An agent proposes "run action
#   ``approveApplication`` on connector ``X`` with params ``Y``"; a human
#   approves or rejects in The Tray; on approve the executor performs the
#   connector call through the cloud connector path. Fully generic — no
#   domain-specific logic.
#
# Re-exports the propose helper + the apply-on-approve executor so callers
# (the agent MCP surface, the instinct router) import from the package root,
# mirroring how the pocket-write bridge and belt executor are imported.

from __future__ import annotations

from pocketpaw_ee.cloud.external_actions.executor import execute_approved_external_action
from pocketpaw_ee.cloud.external_actions.propose import (
    EXTERNAL_ACTION_KIND,
    EXTERNAL_ACTION_PARAM_KEY,
    EXTERNAL_ACTION_SCHEMA,
    compute_params_hash,
    propose_external_action,
)

__all__ = [
    "EXTERNAL_ACTION_KIND",
    "EXTERNAL_ACTION_PARAM_KEY",
    "EXTERNAL_ACTION_SCHEMA",
    "compute_params_hash",
    "execute_approved_external_action",
    "propose_external_action",
]
