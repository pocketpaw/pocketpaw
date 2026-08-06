# _tenancy.py — workspace resolution + fail-closed guard for the BUILTIN
#   Fabric / Instinct agent tools.
# Created: 2026-08-06 (C4-a — builtin MCP tools: stop cross-tenant leakage).
#
# Why this exists: the in-process MCP servers
# (``ee/pocketpaw_ee/agent/mcp_servers/{fabric,instinct}.py``) resolve the
# caller's tenant from the per-stream agent identity ContextVars and pass
# ``workspace_id=`` into every store call, so an agent on the claude_agent_sdk
# backend can neither read nor stamp another tenant's rows. Their BUILTIN
# (BaseTool) siblings — the only Fabric/Instinct path the NON-SDK backends
# (deep_agents, google_adk, openai_agents) ever see — passed NO workspace at
# all. Two consequences, both live:
#
#   * every write landed with ``workspace_id = NULL``. That is not merely
#     "unscoped": ``_workspace_scope()`` (instinct/store.py, fabric/store.py)
#     renders a scoped read as ``(workspace_id = ? OR workspace_id IS NULL)``
#     for W4a legacy compatibility, so a NULL row is ACTIVELY RETURNED to
#     every tenant's scoped query against that database file. An Action
#     proposed through ``instinct_propose`` surfaced in other tenants'
#     approval queues.
#   * every read ran unfiltered, so a query saw whatever rows shared the file.
#
# ISO-1/ISO-2 physical per-workspace files (``~/.pocketpaw/workspaces/<id>/``)
# reduce the blast radius when a workspace IS in context, but they do not fix
# either half: the row still lands NULL inside the tenant's own file, and when
# NO workspace resolves and ``POCKETPAW_REQUIRE_WORKSPACE_SCOPE`` is unset the
# factory hands back the SHARED legacy file — where a NULL row is visible to
# every tenant that reads it.
#
# The fail-closed marker, and why it is NOT a process-global: refusing whenever
# a workspace is missing would break every legitimate workspace-less run (OSS
# self-hosted, CLI, background jobs, automations). Gating on a process-global
# such as ``is_multi_tenant_cloud()`` is the specific mistake that turned dev
# red in #1570 — it is true whenever the cloud Mongo client is connected, so it
# fires for every unrelated run in the process. We key on the POSITIVE per-run
# marker ``current_cloud_chat_run()`` instead (added by fix/cloud-artifacts-
# reland for exactly this distinction, and set by ``run_core.execute_run`` via
# ``mark_cloud_chat_run`` BEFORE identity is bound — so a run that reaches the
# backend without binding identity, the real mis-tenanting bug, still trips
# the guard).
#
# EE→OSS boundary: this module lives in OSS core and imports ``pocketpaw_ee``
# only lazily, inside try/except, exactly like the sibling ``library_verbs.py``
# / ``edit_document.py`` resolvers. A community install without the enterprise
# package resolves ``None`` for both signals and keeps its legacy unscoped
# behavior.

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

__all__ = [
    "current_workspace",
    "is_tenant_scoped_run",
    "workspace_required_message",
]


def current_workspace() -> str | None:
    """Resolve the active workspace for this run, or ``None``.

    Resolution order — both sources are set together by
    ``attach_agent_identity``, so they agree whenever a cloud chat stream is
    live; the fallback covers a pure-OSS process (connector ingest, tests)
    that sets only the core ContextVar:

        EE per-stream agent identity  →  OSS ``stores.current_workspace``

    A blank / whitespace-only value from either source is treated as "no
    workspace" so an empty string can never satisfy the fail-closed check nor
    be stamped onto a row.
    """
    try:
        from pocketpaw_ee.cloud.chat.agent_service import current_workspace_id

        candidate = current_workspace_id()
        if candidate and candidate.strip():
            return candidate.strip()
    except Exception:  # noqa: BLE001 — no EE package, or no live stream
        pass

    try:
        from pocketpaw.stores import current_workspace as _oss_current_workspace

        candidate = _oss_current_workspace.get()
        if candidate and candidate.strip():
            return candidate.strip()
    except Exception:  # noqa: BLE001 — defensive; the import is in-package
        logger.debug("OSS workspace ContextVar unreadable", exc_info=True)

    return None


def is_tenant_scoped_run() -> bool:
    """True when this run MUST carry a workspace — the fail-closed trigger.

    A POSITIVE per-run marker, never a process-global: ``current_cloud_chat_run``
    is set by ``run_core.execute_run`` for the duration of one cloud chat
    dispatch and reset in a finally, so it distinguishes "a chat run that
    should have bound a tenant" from "a legitimately workspace-less run in a
    cloud-connected process" (CLI, background job, direct backend test). See
    the module header for why ``is_multi_tenant_cloud()`` is the wrong signal.

    Absent the EE package or outside a chat run, this is ``False`` and the
    tools keep their legacy unscoped behavior.
    """
    try:
        from pocketpaw_ee.cloud.chat.agent_service import current_cloud_chat_run

        return bool(current_cloud_chat_run())
    except Exception:  # noqa: BLE001 — no EE package / older EE build
        return False


def workspace_required_message(tool_name: str) -> str:
    """The refusal string a builtin tool returns when tenancy is unresolvable.

    Phrased like the sibling MCP servers' ``_error_response`` so the agent
    relays the same reason on either backend.
    """
    return (
        f"{tool_name} requires workspace context (call from a cloud chat session). "
        "Refusing rather than falling back to unscoped access."
    )
