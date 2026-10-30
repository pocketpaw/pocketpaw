# agent_activity — "which of MY agents are working right now", per workspace.
#
# Created: 2026-07-28 (feat/cockpit-agent-activity, HR-12a) — the product-facing
# counterpart to the herdr cockpit. The cockpit reads terminal panes on one
# operator box: it is ADMIN-gated, not paw-workspace-scoped, and a user's /chat
# agent never appears in it (it runs as an in-process SDK client, not a pane).
# This module answers the question the cockpit cannot, from the durable turn
# record instead of a box's panes.
#
# Source of truth is ``ChatRunDoc`` via ``chat.runs.service`` — durable in Mongo,
# workspace-scoped, complete across web workers and arq workers, and intact after
# a restart. The SessionSupervisor's in-memory ``_runtimes`` registry is NOT used:
# it is empty when runs execute in arq workers, partial across multiple web
# workers, and lost on restart, which makes it unsound on shared multi-tenant
# cloud — exactly where this board runs.
#
# Read-only by design: no write path, no mutation, no herdr access.
