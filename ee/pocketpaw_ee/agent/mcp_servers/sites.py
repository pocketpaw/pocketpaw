# sites.py — in-process MCP server exposing the Paw Sites publish action to
# agent backends (claude_agent_sdk). Created: 2026-06-01 (Phase 4 — chat→
# create-site). Mirrors the layout of the sibling mcp_servers (tasks.py /
# pockets.py): a single ``create_sdk_mcp_server`` with an SDK import-guard, the
# ``SERVER_NAME`` / ``*_TOOL_ID`` allowlist constants, and ContextVar-sourced
# identity (the same ``current_workspace_id`` / ``current_user_id`` accessors in
# ``ee.cloud.chat.agent_service`` the pocket specialist + tasks servers read).
# Tool ids namespace as ``mcp__pocketpaw_sites_manager__<tool>`` so the Claude
# Code allowlist machinery matches them.
"""Agent-side MCP surface for publishing a PocketPaw pocket as a Paw Site.

A site is published FROM a pocket: the chat agent identifies the pocket to
publish (usually the current / just-created one), then calls ``publish`` with
its id. The handler delegates to ``pocketpaw_ee.sites.service.publish_pocket``
— the SAME shared path the REST endpoint (``POST /api/v1/sites/publish``) uses,
so the chat and HTTP surfaces never diverge. That shared function reads the
pocket's rippleSpec + theme via the pockets service, generates + smoke-gates the
SvelteKit app, deploys it (Cloudflare in prod, a local static server when no CF
creds are configured), and persists the Site.

Tool registered:

  - ``publish(pocket_id, name?)`` — publish the given pocket. Returns
    ``{ok, site: {id, name, url, deployed, pocket_id}}`` so the agent can show
    the user the live URL. ``is_error`` is set when the pocket is missing /
    access-denied (NotFound / Forbidden from the pockets service) or the build /
    deploy fails — the chat agent then surfaces the reason instead of
    fabricating a "published" reply.

Workspace / user identity comes from the per-stream ``ContextVar``s in
``ee.cloud.chat.agent_service`` (same chokepoint the pocket + tasks MCP servers
use). When run outside an SSE chat stream the tool returns a clear error rather
than silently mis-tenanting the published site.
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

SERVER_NAME = "pocketpaw_sites_manager"
# Claude Code namespaces in-process MCP tools as ``mcp__<server>__<tool>``.
# Allowlist entries must use this exact form.
PUBLISH_TOOL_ID = f"mcp__{SERVER_NAME}__publish"
# The deterministic landing-site create tool registers on the SAME server (see
# sites_create.py — two create_sdk_mcp_server calls under one name would clobber
# each other, so create + publish share one server object).
CREATE_LANDING_SITE_TOOL_ID = f"mcp__{SERVER_NAME}__create_landing_site"
# The svelte-track create tool also registers on this SAME server (see
# sites_create.py).
CREATE_SVELTE_SITE_TOOL_ID = f"mcp__{SERVER_NAME}__create_svelte_site"

SITES_TOOL_IDS = (PUBLISH_TOOL_ID, CREATE_LANDING_SITE_TOOL_ID, CREATE_SVELTE_SITE_TOOL_ID)


def _error_response(message: str) -> dict[str, Any]:
    """Build an MCP error response in the shape Claude's SDK expects. The agent
    reads ``text`` and surfaces the reason."""
    return {
        "content": [{"type": "text", "text": f"Error: {message}"}],
        "is_error": True,
    }


def _success_response(body: dict[str, Any]) -> dict[str, Any]:
    """Build an MCP success response carrying ``body`` as JSON."""
    return {
        "content": [
            {
                "type": "text",
                "text": json.dumps(body, separators=(",", ":"), default=str),
            }
        ]
    }


def _identity() -> tuple[str | None, str | None]:
    """Resolve the active workspace + user id from the per-stream ContextVars set
    by the cloud chat agent runtime. Returns ``(workspace_id, user_id)``."""
    try:
        from pocketpaw_ee.cloud.chat.agent_service import current_user_id, current_workspace_id

        return current_workspace_id(), current_user_id()
    except Exception:  # noqa: BLE001
        return None, None


async def _publish_handler(args: dict) -> dict:
    """MCP handler for ``sites_manager__publish``.

    Reads workspace/user identity from the per-stream ContextVars, validates the
    ``pocket_id`` input, and delegates to the shared ``publish_pocket`` service
    function. Returns the site (with its openable ``url``) on success; sets
    ``is_error`` when identity is missing, the pocket is not found / not
    accessible, or the build/deploy fails.
    """
    workspace_id, user_id = _identity()
    if not workspace_id or not user_id:
        return _error_response(
            "publish requires workspace and user context (call from a cloud chat session)."
        )

    pocket_id = args.get("pocket_id")
    if not isinstance(pocket_id, str) or not pocket_id:
        return _error_response(
            "publish requires a `pocket_id` — pass the id of the pocket to "
            "publish as a site (usually the current or just-created pocket)."
        )
    name_raw = args.get("name")
    name = name_raw if isinstance(name_raw, str) else ""

    from pocketpaw_ee.cloud._core.errors import CloudError
    from pocketpaw_ee.sites import service as sites_service

    try:
        doc = await sites_service.publish_pocket(
            workspace_id=workspace_id,
            user_id=user_id,
            pocket_id=pocket_id,
            name=name,
        )
    except CloudError as exc:
        # NotFound / Forbidden from the pockets service surface here — relay the
        # code + message so the agent can tell the user the pocket is missing or
        # not theirs, instead of reporting a phantom publish.
        return _error_response(f"{exc.code}: {exc.message}")
    except Exception as exc:  # noqa: BLE001
        logger.warning("sites publish failed", exc_info=True)
        return _error_response(f"publish failed: {exc}")

    return _success_response(
        {
            "ok": True,
            "site": {
                "id": str(doc.id),
                "pocket_id": doc.pocket_id,
                "name": doc.name,
                "url": doc.url,
                "deployed": doc.deployed,
            },
        }
    )


def build_sites_manager_server() -> tuple[str, Any] | None:
    """Build the in-process SDK MCP server for Sites, or return ``None`` if the
    Claude Agent SDK isn't installed.

    Matches the shape returned by ``build_tasks_context_server`` /
    ``build_pocket_context_server`` (``(name, server)`` or ``None``) so the
    backend's MCP registration loop in ``claude_sdk.py`` treats it identically.
    """
    try:
        from claude_agent_sdk import create_sdk_mcp_server, tool
    except ImportError:
        logger.debug("claude_agent_sdk not installed; pocketpaw_sites_manager MCP disabled")
        return None

    @tool(
        "publish",
        (
            "Publish a PocketPaw pocket as a live Paw Site (a real, standalone "
            "website deployed to the edge). A site is always published FROM a "
            "pocket — identify the pocket to publish (usually the current or "
            "just-created one) and pass its id. Use this when the user asks to "
            "'publish X as a website/site', 'make a site from this pocket', or "
            "'put this online'. Args: `pocket_id` (required — the pocket to "
            "publish) and optional `name` (the site name; defaults to the "
            "pocket's own name). Returns {ok, site: {id, name, url, deployed, "
            "pocket_id}} — show the user the `url`. ok=false with an error "
            "means the pocket was not found / not accessible or the build "
            "failed; relay the error, do NOT report success. If the user wants "
            "a brand-new site from a description (e.g. 'build a dentist landing "
            "site'), FIRST create the pocket with "
            "`mcp__pocketpaw_pocket_specialist__create`, then call this with "
            "the new pocket's id."
        ),
        {
            "type": "object",
            "properties": {
                "pocket_id": {
                    "type": "string",
                    "minLength": 1,
                    "description": (
                        "Id of the pocket to publish as a site — the current or "
                        "just-created pocket."
                    ),
                },
                "name": {
                    "type": "string",
                    "description": (
                        "Optional site name. Defaults to the pocket's own name when omitted."
                    ),
                },
            },
            "required": ["pocket_id"],
            "additionalProperties": False,
        },
    )
    async def publish(args):  # type: ignore[no-untyped-def]
        return await _publish_handler(args)

    # Register the deterministic landing-site create tool on this SAME server.
    # The SKILL flow is: produce the `content` copy → create_landing_site →
    # publish, so the two hops sit on one allowlisted server. Built here (not as a
    # separate create_sdk_mcp_server) because the claude_sdk registration loop
    # keys servers by name and a second server under this name would clobber it.
    from pocketpaw_ee.agent.mcp_servers.sites_create import (
        make_create_landing_site_tool,
        make_create_svelte_site_tool,
    )

    create_landing_site = make_create_landing_site_tool(tool)
    # The svelte-track create tool — same server, so the author-source-map →
    # create_svelte_site → publish hops sit on one allowlisted server.
    create_svelte_site = make_create_svelte_site_tool(tool)

    server = create_sdk_mcp_server(
        name=SERVER_NAME,
        version="1.0.0",
        tools=[publish, create_landing_site, create_svelte_site],
    )
    return SERVER_NAME, server


__all__ = [
    "CREATE_LANDING_SITE_TOOL_ID",
    "CREATE_SVELTE_SITE_TOOL_ID",
    "PUBLISH_TOOL_ID",
    "SERVER_NAME",
    "SITES_TOOL_IDS",
    "build_sites_manager_server",
]
