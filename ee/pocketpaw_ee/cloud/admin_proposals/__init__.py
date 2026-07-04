# ee/cloud/admin_proposals/__init__.py — the gated workspace-ADMIN-action proposal
#   kind (the 8th Instinct gate kind, alongside _external_action / _pocket_write /
#   _code_change / _fabric_objects / _pocket_create / _instinct_rule /
#   _artifact_change).
# Created: 2026-07-03 (feat/workspace-admin-tools, WA-2) — an agent-proposed
#   workspace-admin write (e.g. a member role change) files an Instinct Action
#   carrying an ``_admin_action`` blob. A human approves it in the Tray; only then
#   does the executor fire the whitelisted workspace-admin service call — after
#   RE-CHECKING the proposer's CURRENT RBAC role at execute time (fail-closed if
#   the proposer was demoted since proposing). This package is the propose + apply
#   halves of that gate.
"""Gated workspace-admin-action proposals (the ``_admin_action`` Instinct kind)."""

from __future__ import annotations

from pocketpaw_ee.cloud.admin_proposals.propose import (
    ADMIN_ACTION_KIND,
    ADMIN_ACTION_PARAM_KEY,
    ADMIN_ACTION_SCHEMA,
    compute_args_hash,
    propose_admin_action,
)

__all__ = [
    "ADMIN_ACTION_KIND",
    "ADMIN_ACTION_PARAM_KEY",
    "ADMIN_ACTION_SCHEMA",
    "compute_args_hash",
    "propose_admin_action",
]
