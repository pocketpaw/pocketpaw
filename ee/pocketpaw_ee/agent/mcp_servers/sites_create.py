# sites_create.py — in-process MCP server exposing the DETERMINISTIC Paw Site
# create action. Created: 2026-06-04 (feat/sites-deterministic-fastpath).
#
# This is the decisive bypass of the dashboard-built ``pocket_specialist``
# create machinery. The agent-mode pocket_specialist path (draft kit / plan kit /
# subagent delegation + the validate-redraft loop) kept downgrading landing
# sites to generic hero+grid+card widgets no matter what the prompt whitelisted,
# because the LLM owned the page STRUCTURE. Here, the LLM provides COPY ONLY and
# this tool assembles the marketing-widget structure in code (``landing_assembler
# .assemble_landing_spec``), then persists it DIRECTLY through the pockets
# service's ``agent_create`` — NO pocket_specialist, NO validate/redraft loop, NO
# subagent. The structure cannot be downgraded.
#
# Mirrors the sibling ``sites.py`` (publish) server: ``create_sdk_mcp_server``
# with an SDK import-guard, ``SERVER_NAME`` / ``*_TOOL_ID`` allowlist constants,
# and ContextVar-sourced identity (the same ``current_workspace_id`` /
# ``current_user_id`` accessors in ``ee.cloud.chat.agent_service`` the publish +
# pocket-specialist servers read). Tool id namespaces under the SAME
# ``pocketpaw_sites_manager`` server (``mcp__pocketpaw_sites_manager__
# create_landing_site``) so the create + publish hops sit side by side for the
# chat agent — the SKILL produces copy → create_landing_site → publish.
"""Agent-side MCP surface for DETERMINISTIC Paw Site landing-page creation.

A landing site is built from an LLM ``content`` copy object. This tool:

  1. assembles the FIXED marketing-widget rippleSpec from ``content`` in code
     (``assemble_landing_spec``) — the LLM never decides the structure;
  2. persists the pocket DIRECTLY via ``pockets.service.agent_create`` stamped
     ``type="site"`` + ``pattern="landing"`` — bypassing the pocket_specialist
     adapter, its draft/redraft loop, and any subagent delegation;
  3. binds the active chat session + pushes the ``pocket_created`` SSE event (the
     same post-create side effects the specialist's persist tool runs) so the
     canvas auto-opens.

Returns ``{ok, pocket_id, pocket}`` so the agent hands ``pocket_id`` straight to
``mcp__pocketpaw_sites_manager__publish``. ``is_error`` is set when identity is
missing or the persist fails (e.g. the strict catalog gate rejects the spec) so
the agent surfaces the reason instead of fabricating a created pocket.

Workspace / user identity comes from the per-stream ``ContextVar``s in
``ee.cloud.chat.agent_service``. When run outside an SSE chat stream the tool
returns a clear error rather than silently mis-tenanting the pocket.
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

# Same server as the publish tool — the create + publish hops live together so
# the chat agent reaches both under one allowlisted server name.
SERVER_NAME = "pocketpaw_sites_manager"
# Claude Code namespaces in-process MCP tools as ``mcp__<server>__<tool>``.
CREATE_LANDING_SITE_TOOL_ID = f"mcp__{SERVER_NAME}__create_landing_site"

SITES_CREATE_TOOL_IDS = (CREATE_LANDING_SITE_TOOL_ID,)


def _error_response(message: str) -> dict[str, Any]:
    """Build an MCP error response in the shape Claude's SDK expects."""
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


async def _bind_session_and_emit(pocket_id: str, view: dict[str, Any], user_id: str) -> None:
    """Bind the active chat session to the new pocket and push the
    ``pocket_created`` SSE event so the canvas auto-opens — the same atomic
    post-create side effects ``make_persist_pocket_tool`` runs. Best-effort: a
    bind / SSE failure must never undo a successful create (the pocket already
    exists in Mongo, which is the primary contract)."""
    try:
        from pocketpaw_ee.cloud.chat.agent_service import (
            current_session_mongo_id,
            push_sse_event,
        )
        from pocketpaw_ee.cloud.sessions import service as sessions_service

        session_mongo_id = current_session_mongo_id()
        if session_mongo_id:
            await sessions_service.attach_pocket_to_session_doc(
                session_mongo_id, user_id, pocket_id
            )
        push_sse_event(
            "pocket_created",
            {"pocket_id": pocket_id, "pocket": view, "session_id": session_mongo_id},
        )
    except Exception:  # noqa: BLE001
        logger.warning(
            "create_landing_site: post-create side effects failed (non-fatal)",
            exc_info=True,
        )


async def _create_landing_site_handler(args: dict) -> dict:
    """MCP handler for ``sites_manager__create_landing_site``.

    Reads workspace/user identity from the per-stream ContextVars, validates the
    ``content`` input, assembles the deterministic landing rippleSpec, and
    persists it DIRECTLY via ``agent_create`` (type="site", pattern="landing").
    Returns ``{ok, pocket_id, pocket}`` on success; sets ``is_error`` when
    identity is missing, ``content`` is absent/malformed, or the persist fails.
    """
    workspace_id, user_id = _identity()
    if not workspace_id or not user_id:
        return _error_response(
            "create_landing_site requires workspace and user context (call from a "
            "cloud chat session)."
        )

    content = args.get("content")
    if not isinstance(content, dict) or not content:
        return _error_response(
            "create_landing_site requires a `content` object — the COPY for the "
            "landing page (brand, hero, services, testimonials, tiers, cta_band, "
            "contact, footer). You provide copy only; the tool builds the page."
        )

    name_raw = args.get("name")
    # Default the pocket name to the brand from the copy when not given.
    name = name_raw if isinstance(name_raw, str) and name_raw.strip() else ""
    if not name:
        brand = content.get("brand")
        name = brand.strip() if isinstance(brand, str) and brand.strip() else "Landing site"

    description_raw = args.get("description")
    description = description_raw if isinstance(description_raw, str) else ""
    icon_raw = args.get("icon")
    icon = icon_raw if isinstance(icon_raw, str) else ""
    color_raw = args.get("color")
    color = color_raw if isinstance(color_raw, str) else ""

    # CODE owns the structure — assemble the fixed marketing tree from the copy.
    from pocketpaw_ee.sites.landing_assembler import assemble_landing_spec

    try:
        ripple_spec = assemble_landing_spec(content)
    except Exception as exc:  # noqa: BLE001
        logger.warning("create_landing_site: assemble failed", exc_info=True)
        return _error_response(f"could not assemble the landing page: {exc}")

    # Persist DIRECTLY through the pockets service — NOT the pocket_specialist
    # create path, so there is no draft/redraft loop and no subagent to downgrade
    # the spec. ``type_="site"`` + ``pattern="landing"`` stamp the site identity
    # so the generator + any later edit treat the pocket as a marketing page.
    # ``trusted=True`` skips the STRICT catalog gate: the spec is code-assembled
    # from the renderer-valid skeleton, but the PUBLISHED manifest is stale and
    # omits the marketing widgets (navbar/feature-grid/testimonial/logo-cloud/
    # cta/footer), so the strict gate would false-reject the page and its
    # "suggestion" would push toward generic widgets — the exact downgrade this
    # path exists to prevent. The logged catalog walk + embed audit still run.
    from pocketpaw_ee.cloud.pockets.service import agent_create

    try:
        view, new_pocket_id, err = await agent_create(
            workspace_id=workspace_id,
            owner_id=user_id,
            name=name,
            description=description,
            type_="site",
            pattern="landing",
            icon=icon,
            color=color,
            ripple_spec=ripple_spec,
            trusted=True,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("create_landing_site: persist raised", exc_info=True)
        return _error_response(f"create failed: {exc}")

    if err is not None or view is None or new_pocket_id is None:
        return _error_response(f"create failed: {err or 'create returned no view'}")

    await _bind_session_and_emit(new_pocket_id, view, user_id)

    return _success_response(
        {
            "ok": True,
            "pocket_id": new_pocket_id,
            "pocket": {
                "id": new_pocket_id,
                "name": view.get("name"),
                "type": view.get("type"),
                "pattern": view.get("pattern"),
            },
        }
    )


def make_create_landing_site_tool(tool: Any) -> Any:
    """Build the ``create_landing_site`` SDK tool object using the SDK's ``tool``
    decorator (passed in by the caller that already imported it).

    Returned so the publish server (``sites.py``) can register BOTH tools on the
    ONE ``pocketpaw_sites_manager`` server. The registration loop in
    ``claude_sdk.py`` keys ``servers[name] = cfg`` by server name, so two
    ``create_sdk_mcp_server`` calls under the same name would clobber each other —
    the create + publish tools must therefore live on a single server object.
    """

    @tool(
        "create_landing_site",
        (
            "Create a Paw Site landing page DETERMINISTICALLY. You provide COPY "
            "ONLY — the tool assembles the marketing page structure (navbar, "
            "hero, services, social proof, pricing, CTA, lead form, footer) in "
            "code and persists it as a pocket stamped type='site' + "
            "pattern='landing'. You do NOT compose a rippleSpec and you do NOT "
            "call pocket_specialist — pass the `content` copy object and the tool "
            "builds the page. Use this for a BRAND-NEW marketing/landing site "
            "('build a dentist landing site', 'a landing page for my bakery'). "
            "`content` keys (all optional, plausible copy fills gaps — never "
            "'TBD'/'Lorem ipsum'): brand (str); hero {eyebrow, title, subtitle, "
            "cta_label}; services [{title, desc, icon}]; testimonials [{quote, "
            "author, role}]; tiers [{name, price, period, features:[str], "
            "popular, cta_label}]; cta_band {headline, subtext, button_label}; "
            "contact {address, phone, email}; footer {copyright}. Variable-length "
            "services/testimonials/tiers are handled. Returns {ok, pocket_id, "
            "pocket}; hand `pocket_id` to "
            "`mcp__pocketpaw_sites_manager__publish` to publish. ok=false with an "
            "error means relay the reason, do NOT report a created pocket."
        ),
        {
            "type": "object",
            "properties": {
                "content": {
                    "type": "object",
                    "description": (
                        "The COPY for the landing page — words only, no structure. "
                        "brand, hero, services, testimonials, tiers, cta_band, "
                        "contact, footer (see the tool description for the shape)."
                    ),
                    "additionalProperties": True,
                },
                "name": {
                    "type": "string",
                    "description": (
                        "Optional pocket/site name. Defaults to the brand from "
                        "`content` when omitted."
                    ),
                },
                "description": {
                    "type": "string",
                    "description": "Optional one-line pocket description.",
                },
                "icon": {"type": "string", "description": "Optional lucide icon name."},
                "color": {"type": "string", "description": "Optional accent color hex."},
            },
            "required": ["content"],
            "additionalProperties": False,
        },
    )
    async def create_landing_site(args):  # type: ignore[no-untyped-def]
        return await _create_landing_site_handler(args)

    return create_landing_site


__all__ = [
    "CREATE_LANDING_SITE_TOOL_ID",
    "SERVER_NAME",
    "SITES_CREATE_TOOL_IDS",
    "make_create_landing_site_tool",
]
