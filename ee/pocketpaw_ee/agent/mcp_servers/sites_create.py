# sites_create.py — in-process MCP server exposing the DETERMINISTIC Paw Site
# create action. Created: 2026-06-04 (feat/sites-deterministic-fastpath).
#
# Updated 2026-06-09 (feat/landing-assembler-enrich): the create_landing_site
# tool + content schema now document the OPTIONAL ``faqs`` copy field
# ([{question, answer}]) the enriched ``assemble_landing_spec`` consumes to emit a
# native-<details> FAQ section. No handler-logic change — the assembler owns the
# structure; this just teaches the chat agent the new field exists.
#
# Updated: 2026-06-04 (feat/sites-svelte-engine) — added the SECOND deterministic
# create tool ``create_svelte_site`` for the Paw Sites "Svelte track". It mirrors
# ``create_landing_site`` (same direct ``agent_create`` persistence, same
# session-bind + SSE side effects, same one server) but the payload is a
# hand-written SvelteKit ``source`` MAP ``{relative_path: file_contents}`` (the
# pocketpaw-create-svelte-site skill authors it via the design skills) instead of
# a ``content`` copy object. There is no ``assemble_*`` step — the agent IS the
# author — so the tool persists ``source`` verbatim via ``agent_create(
# engine="svelte", source=<map>, type_="site", pattern="landing",
# ripple_spec=None, trusted=True)``. ``trusted=True`` is correct here for a
# different reason than the landing tool: there is NO rippleSpec to gate at all
# (the catalog walk only runs on a non-null spec), so the source files pass
# straight through to persistence and the generator materializes them at publish.
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
# The svelte-track create tool — registers on the SAME server (see the publish
# server in sites.py). The skill flow is: author the source map → create_svelte_site
# → publish.
CREATE_SVELTE_SITE_TOOL_ID = f"mcp__{SERVER_NAME}__create_svelte_site"

SITES_CREATE_TOOL_IDS = (CREATE_LANDING_SITE_TOOL_ID, CREATE_SVELTE_SITE_TOOL_ID)


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


# ── Svelte-track source map (design spec §4.3) ──────────────────────────────
# The keys a svelte-engine ``source`` map MUST carry — paths relative to the
# SvelteKit project root. These are the composition root + the resting-frame
# essentials the paw-sites generator materializes onto the skeleton; the
# skeleton itself provides everything else (package.json, svelte.config, vite,
# adapter, app.html, api/submit). ``src/lib/components/*`` and ``src/lib/*.js``
# are validated by PREFIX (variable filenames — Hero/Pricing/Faq, reveal.js).
SVELTE_REQUIRED_EXACT_KEYS = (
    "src/routes/+page.svelte",  # composes the section components
    "src/routes/+layout.svelte",  # imports ../app.css (finding #5)
    "src/routes/+page.ts",  # export const prerender = true
    "src/app.css",  # tokens, fonts, base
)
SVELTE_REQUIRED_PREFIXES = (
    "src/lib/components/",  # at least one section component (Hero, ...)
)


def _missing_source_keys(source: dict[str, str]) -> list[str]:
    """Return the §4.3 required keys absent from ``source`` (empty list = valid).

    Checks the exact composition/resting-frame keys plus that at least one
    ``src/lib/components/*.svelte`` section exists. Used to fail the create
    closed with an actionable message rather than persisting a half-authored
    map the generator can't build into a page."""
    missing = [k for k in SVELTE_REQUIRED_EXACT_KEYS if k not in source]
    for prefix in SVELTE_REQUIRED_PREFIXES:
        if not any(k.startswith(prefix) for k in source):
            missing.append(f"{prefix}*.svelte")
    return missing


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
            "popular, cta_label}]; faqs [{question, answer}] (OPTIONAL — renders a "
            "native-<details> FAQ section after pricing; omit it and no FAQ shows); "
            "cta_band {headline, subtext, button_label}; contact {address, phone, "
            "email}; footer {copyright}. Variable-length "
            "services/testimonials/tiers/faqs are handled. Returns {ok, pocket_id, "
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
                        "brand, hero, services, testimonials, tiers, faqs "
                        "(optional), cta_band, contact, footer (see the tool "
                        "description for the shape)."
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


async def _create_svelte_site_handler(args: dict) -> dict:
    """MCP handler for ``sites_manager__create_svelte_site`` (the Svelte track).

    Reads workspace/user identity from the per-stream ContextVars, validates the
    ``source`` map against the §4.3 required keys, and persists it DIRECTLY via
    ``agent_create`` (engine="svelte", source=<map>, type="site",
    pattern="landing", ripple_spec=None, trusted=True). Returns
    ``{ok, pocket_id, pocket}`` on success; sets ``is_error`` when identity is
    missing, ``source`` is absent/malformed/incomplete, or the persist fails.

    Unlike ``create_landing_site`` there is no ``assemble_*`` step — the agent
    authored the SvelteKit components (via the design skills), so the map is
    persisted verbatim and the generator materializes it at publish.
    """
    workspace_id, user_id = _identity()
    if not workspace_id or not user_id:
        return _error_response(
            "create_svelte_site requires workspace and user context (call from a "
            "cloud chat session)."
        )

    source = args.get("source")
    if not isinstance(source, dict) or not source:
        return _error_response(
            "create_svelte_site requires a `source` object — the SvelteKit source "
            "map { relative_path: file_contents } you authored (the +page.svelte "
            "composition root, +layout.svelte, +page.ts, app.css, and the "
            "src/lib/components/*.svelte sections). You write the components; this "
            "tool persists them."
        )
    # Every value must be a string (file contents) — the map is {path: contents}.
    bad = [k for k, v in source.items() if not isinstance(v, str)]
    if bad:
        return _error_response(
            "create_svelte_site `source` values must be file-content strings; "
            f"these keys are not strings: {', '.join(sorted(bad)[:8])}."
        )
    missing = _missing_source_keys(source)
    if missing:
        return _error_response(
            "create_svelte_site `source` is missing required SvelteKit files "
            f"(design spec §4.3): {', '.join(missing)}. Author them before "
            "creating — the page can't prerender without the composition root, "
            "+layout.svelte (imports app.css), +page.ts (prerender=true), app.css, "
            "and at least one section component."
        )

    name_raw = args.get("name")
    name = name_raw.strip() if isinstance(name_raw, str) and name_raw.strip() else "Svelte site"
    description_raw = args.get("description")
    description = description_raw if isinstance(description_raw, str) else ""
    icon_raw = args.get("icon")
    icon = icon_raw if isinstance(icon_raw, str) else ""
    color_raw = args.get("color")
    color = color_raw if isinstance(color_raw, str) else ""

    # Persist DIRECTLY through the pockets service — NO pocket_specialist, NO
    # rippleSpec, NO catalog gate (there is no spec to gate). ``engine="svelte"``
    # + ``source`` stamp the svelte track so the generator materializes the map;
    # ``type_="site"`` + ``pattern="landing"`` keep the site identity the rest of
    # the pipeline (publish, refine, /sites listing) keys on. ``trusted=True``
    # short-circuits the strict catalog gate, which only runs on a non-null
    # rippleSpec anyway — the svelte path passes ``ripple_spec=None``.
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
            ripple_spec=None,
            engine="svelte",
            source=source,
            trusted=True,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("create_svelte_site: persist raised", exc_info=True)
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
                "engine": view.get("engine"),
            },
        }
    )


def make_create_svelte_site_tool(tool: Any) -> Any:
    """Build the ``create_svelte_site`` SDK tool object using the SDK's ``tool``
    decorator (passed in by the caller that already imported it).

    Registered on the SAME ``pocketpaw_sites_manager`` server as publish +
    create_landing_site (see ``make_create_landing_site_tool`` for why one
    server)."""

    @tool(
        "create_svelte_site",
        (
            "Create a Paw Site landing page on the SVELTE TRACK. You AUTHOR the "
            "SvelteKit components yourself (premium hand-written Svelte via the "
            "design skills — the quality bar is the proven spike) and pass them as "
            "a `source` MAP { relative_path: file_contents }; the tool persists "
            "the map and stamps the pocket type='site', pattern='landing', "
            "engine='svelte'. You do NOT compose a rippleSpec, do NOT call "
            "get_widget_spec, do NOT call pocket_specialist. Required `source` "
            "keys (design spec §4.3): 'src/routes/+page.svelte' (imports + "
            "composes the section components), 'src/routes/+layout.svelte' "
            "(<script>import '../app.css'</script>), 'src/routes/+page.ts' "
            "(export const prerender = true), 'src/app.css' (tokens/fonts/base), "
            "and at least one 'src/lib/components/*.svelte' section (Hero, "
            "Pricing, Faq, ...); add 'src/lib/*.js' helpers (e.g. reveal.js) as "
            "needed. CRITICAL authoring rule: every component must render its "
            "resting/final state in MARKUP — never set it only in onMount — "
            "because the page is PRERENDERED and onMount does not run at prerender "
            "time (a count-up initialized to 0 bakes '$0.00'; initialize it to the "
            "final value). Returns {ok, pocket_id, pocket}; hand `pocket_id` to "
            "`mcp__pocketpaw_sites_manager__publish` to publish. ok=false with an "
            "error means relay the reason, do NOT report a created pocket."
        ),
        {
            "type": "object",
            "properties": {
                "source": {
                    "type": "object",
                    "description": (
                        "The SvelteKit source map { relative_path: file_contents } "
                        "you authored — paths relative to the project root, values "
                        "are the file contents as strings. Must include the §4.3 "
                        "required files (see the tool description)."
                    ),
                    "additionalProperties": {"type": "string"},
                },
                "name": {
                    "type": "string",
                    "description": "Optional pocket/site name. Defaults to 'Svelte site'.",
                },
                "description": {
                    "type": "string",
                    "description": "Optional one-line pocket description.",
                },
                "icon": {"type": "string", "description": "Optional lucide icon name."},
                "color": {"type": "string", "description": "Optional accent color hex."},
            },
            "required": ["source"],
            "additionalProperties": False,
        },
    )
    async def create_svelte_site(args):  # type: ignore[no-untyped-def]
        return await _create_svelte_site_handler(args)

    return create_svelte_site


__all__ = [
    "CREATE_LANDING_SITE_TOOL_ID",
    "CREATE_SVELTE_SITE_TOOL_ID",
    "SERVER_NAME",
    "SITES_CREATE_TOOL_IDS",
    "SVELTE_REQUIRED_EXACT_KEYS",
    "SVELTE_REQUIRED_PREFIXES",
    "make_create_landing_site_tool",
    "make_create_svelte_site_tool",
]
