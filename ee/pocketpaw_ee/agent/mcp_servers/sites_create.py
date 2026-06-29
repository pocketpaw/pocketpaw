# sites_create.py — in-process MCP server exposing the DETERMINISTIC Paw Site
# create action. Created: 2026-06-04 (feat/sites-deterministic-fastpath).
#
# Updated: 2026-06-21 (DSV-5 — dynamic svelte sites write-side) — create_svelte_site
# now accepts a DYNAMIC svelte site. The ``source`` envelope may carry live-data
# bindings (``objects``/``sources``/``actions``/``auth``) as SIBLING keys on the
# {path: contents} file map: the per-value string check now EXEMPTS those binding
# keys (their values are lists/bools), the JSON schema's ``source`` is loosened to
# ``additionalProperties: true``, and the handler stamps ``pattern="dynamic"`` when
# any binding is present (else ``pattern="landing"`` for a static marketing site —
# unchanged). The generator (generator_client._split_svelte_source) peels the
# bindings out of ``source`` at publish and passes them as flat siblings on the
# DSV-1 GenerateInput. Required-file validation is untouched — binding keys never
# satisfy a §4.3 required FILE key, so they are simply ignored by the missing-keys
# check.
#
# Updated: 2026-06-17 (fix/sites-plan-gate-asymmetry) — both create handlers now
# call _require_sites_plan_or_error(workspace_id) right after input validation,
# delegating to the shared sites.service.require_sites_plan gate. Sites is the
# "sites" plan feature (go+); these create tools reach agent_create directly and
# bypassed the REST router's require_plan_feature("sites") gate, so a free-plan
# workspace could create + (then publish) a live site that GET /sites 403'd. On a
# disallowed plan the handler now returns the plan.feature_denied MCP error
# ("Sites requires the Go plan — upgrade, or switch workspace") so the chat
# agent surfaces the upgrade message instead of a phantom-created site.
# Updated: 2026-06-25 (decouple-sites-from-fabric) — the gate moved off the
# overloaded "fabric" flag onto the dedicated "sites" flag (go+); the Fabric
# ontology keeps "fabric" (enterprise-only).
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
# Updated: 2026-06-17 (feat/sites-svelte-component-edit, SE-2) — added a THIRD
# tool ``edit_svelte_component`` for targeted edits of a PUBLISHED svelte site.
# It takes ``pocket_id`` + ``component_path`` + the FULL ``new_source`` for one
# file, and delegates to ``sites_service.edit_svelte_component`` (persist the one
# file via the pockets service, then republish; roll back + leave the prior
# deploy if the rebuild's smoke gate fails). It registers on the SAME
# ``pocketpaw_sites_manager`` server as create + publish (see sites.py) so the
# create → publish → edit hops sit side by side for the chat agent.
#
# Updated: 2026-06-18 (fix/sites-edit-draft-not-publish, BUG 2) — an edit now
# stages a DRAFT PREVIEW, not a live publish, so the success result no longer
# narrates "published"/"republished"/"live at". ``_edit_svelte_component_handler``
# returns ``{ok, status:"draft", is_live:false, site:{..., preview_url, deployed},
# component_path, message}`` with a message that says the change is a draft preview
# (not live), the url is a PREVIEW (not the published site), and the user must click
# "Submit for review" to publish. The tool docstring the agent reads was reworded to
# match. The create + publish tools are unchanged (publish is a real live deploy).
#
# Updated: 2026-06-18 (feat/sites-diff-edit, P3) — ``edit_svelte_component`` now
# accepts a TARGETED diff: ``edits=[{old_string, new_string}, ...]`` (search/replace
# blocks like the built-in Edit tool) as an ALTERNATIVE to the full ``new_source``,
# so the agent emits ONLY the change for a small edit instead of regenerating the
# whole file (the dominant edit-latency cost). Exactly one of ``edits`` /
# ``new_source`` is required (``new_source`` dropped from the schema's ``required``).
# The handler validates the diff SHAPE (a non-empty list of string {old_string,
# new_string} dicts) and forwards both fields to ``sites_service.edit_svelte_component``,
# which applies the blocks to the pocket's CURRENT source (apply_edits) — a 0/>1
# match raises ValidationError, relayed to the agent by code so it can retry. The
# tool description steers the agent to PREFER ``edits`` for small changes and reserve
# ``new_source`` for large rewrites.
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

from ._audit import record_tool_call

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
# The targeted svelte-component edit tool — rewrites ONE file of a published
# svelte site's source map and safely republishes. Registers on the SAME server
# (see sites.py) so the create → publish → edit hops sit side by side.
EDIT_SVELTE_COMPONENT_TOOL_ID = f"mcp__{SERVER_NAME}__edit_svelte_component"

SITES_CREATE_TOOL_IDS = (
    CREATE_LANDING_SITE_TOOL_ID,
    CREATE_SVELTE_SITE_TOOL_ID,
    EDIT_SVELTE_COMPONENT_TOOL_ID,
)


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

# DSV-5: a DYNAMIC svelte site carries its live-data bindings as SIBLING keys on
# the same ``source`` envelope that holds the {path: contents} SvelteKit files —
# ``objects`` (the D1 table defs) / ``sources`` (reads) / ``actions`` (writes) —
# lists — and ``auth`` — a bool. These are NOT file entries: their values are
# lists/bools, not content strings, and they are NOT validated against the §4.3
# required-file set. The generator (generator_client._split_svelte_source) peels
# them out of ``source`` at publish and passes them as flat siblings on the DSV-1
# GenerateInput. Their presence is what makes a svelte site dynamic.
SVELTE_BINDING_KEYS = ("objects", "sources", "actions", "auth")


def _has_svelte_bindings(source: dict[str, Any]) -> bool:
    """True when the ``source`` envelope carries any NON-EMPTY live-data binding
    (DSV-5) — i.e. the svelte site is DYNAMIC.

    A binding key present but empty (``objects: []`` / ``auth: false``) does NOT
    make the site dynamic — the same emptiness the generator's ``isDynamic``
    classifier uses (sources/actions non-empty OR auth true). Used to decide the
    create ``pattern`` (``"dynamic"`` vs ``"landing"``)."""
    return any(bool(source.get(k)) for k in SVELTE_BINDING_KEYS)


def _missing_source_keys(source: dict[str, Any]) -> list[str]:
    """Return the §4.3 required keys absent from ``source`` (empty list = valid).

    Checks the exact composition/resting-frame keys plus that at least one
    ``src/lib/components/*.svelte`` section exists. Used to fail the create
    closed with an actionable message rather than persisting a half-authored
    map the generator can't build into a page. Binding sibling keys (DSV-5)
    never satisfy a required FILE key, so they are simply ignored here."""
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


async def _require_sites_plan_or_error(workspace_id: str) -> dict | None:
    """Gate the create on the workspace's plan. Returns an MCP ``_error_response``
    when the plan lacks the Sites ("sites") feature (so the agent surfaces the
    upgrade message instead of a phantom-created site), or ``None`` when the plan
    is allowed.

    Delegates to the SHARED service gate (``sites.service.require_sites_plan``) so
    the in-process create path is gated by the SAME plan check + feature table as
    the publish path and the HTTP ``require_plan_feature("sites")`` dependency. A
    free-plan workspace was the bug: create + publish ran in-process and bypassed
    the router gate, deploying a live site that GET /sites then 403'd."""
    from pocketpaw_ee.cloud._core.errors import CloudError
    from pocketpaw_ee.sites.service import require_sites_plan

    try:
        await require_sites_plan(workspace_id)
    except CloudError as exc:
        # Forbidden('plan.feature_denied') / NotFound('workspace'). Relay the
        # code + message so the agent tells the user to upgrade / switch
        # workspace, not "site created".
        return _error_response(f"{exc.code}: {exc.message}")
    return None


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

    record_tool_call(
        workspace_id=workspace_id,
        user_id=user_id,
        tool_server="pocketpaw_sites",
        tool_name="_create_landing_site",
        status="ok",
        ok=True,
    )

    content = args.get("content")
    if not isinstance(content, dict) or not content:
        return _error_response(
            "create_landing_site requires a `content` object — the COPY for the "
            "landing page (brand, hero, services, testimonials, tiers, cta_band, "
            "contact, footer). You provide copy only; the tool builds the page."
        )

    # Plan gate (Sites = "sites"): reject a free-plan workspace here so the
    # create can't bypass the router's require_plan_feature("sites") gate.
    if (gate := await _require_sites_plan_or_error(workspace_id)) is not None:
        return gate

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


async def _create_svelte_site_handler(args: dict) -> dict:
    """MCP handler for ``sites_manager__create_svelte_site`` (the Svelte track).

    Reads workspace/user identity from the per-stream ContextVars, validates the
    ``source`` envelope against the §4.3 required keys, and persists it DIRECTLY
    via ``agent_create`` (engine="svelte", source=<envelope>, type="site",
    ripple_spec=None, trusted=True). Returns ``{ok, pocket_id, pocket}`` on
    success; sets ``is_error`` when identity is missing, ``source`` is
    absent/malformed/incomplete, or the persist fails.

    DSV-5: the ``source`` envelope may carry live-data bindings
    (``objects``/``sources``/``actions``/``auth``) as SIBLING keys on the file map
    (their values are lists/bools, exempt from the file-string check). When any is
    present the pocket is stamped ``pattern="dynamic"`` (a per-tenant D1 site);
    otherwise ``pattern="landing"`` (a static marketing page, unchanged). The
    generator peels the bindings out of ``source`` at publish and passes them as
    flat siblings on the DSV-1 GenerateInput.

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

    record_tool_call(
        workspace_id=workspace_id,
        user_id=user_id,
        tool_server="pocketpaw_sites",
        tool_name="_create_svelte_site",
        status="ok",
        ok=True,
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
    # Every FILE value must be a string (file contents) — the map is
    # {path: contents}. DSV-5: the live-data binding siblings
    # (objects/sources/actions/auth) are NOT files — their values are lists/bools
    # — so they are excluded from the string check (the generator peels them out of
    # ``source`` at publish via _split_svelte_source and passes them as flat
    # GenerateInput siblings).
    bad = [k for k, v in source.items() if k not in SVELTE_BINDING_KEYS and not isinstance(v, str)]
    if bad:
        return _error_response(
            "create_svelte_site `source` file values must be content strings; "
            f"these keys are not strings: {', '.join(sorted(bad)[:8])}. (The "
            "live-data binding keys objects/sources/actions/auth are the only "
            "non-string siblings allowed on `source`.)"
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

    # Plan gate (Sites = "sites"): reject a free-plan workspace here so the
    # create can't bypass the router's require_plan_feature("sites") gate.
    if (gate := await _require_sites_plan_or_error(workspace_id)) is not None:
        return gate

    name_raw = args.get("name")
    name = name_raw.strip() if isinstance(name_raw, str) and name_raw.strip() else "Svelte site"
    description_raw = args.get("description")
    description = description_raw if isinstance(description_raw, str) else ""
    icon_raw = args.get("icon")
    icon = icon_raw if isinstance(icon_raw, str) else ""
    color_raw = args.get("color")
    color = color_raw if isinstance(color_raw, str) else ""

    # DSV-5: stamp ``pattern="dynamic"`` when the source envelope carries live-data
    # bindings (objects/sources/actions/auth), else ``"landing"`` for a static
    # marketing svelte site. ``pattern="dynamic"`` is what the sites pipeline keys
    # on to provision the per-tenant D1 + bind it on deploy (it is authoritative in
    # ``_is_dynamic``), and what the /sites listing + Data tab surface. A static
    # svelte site keeps ``pattern="landing"`` exactly as before.
    pattern = "dynamic" if _has_svelte_bindings(source) else "landing"

    # Persist DIRECTLY through the pockets service — NO pocket_specialist, NO
    # rippleSpec, NO catalog gate (there is no spec to gate). ``engine="svelte"``
    # + ``source`` stamp the svelte track so the generator materializes the map;
    # ``type_="site"`` + ``pattern`` keep the site identity the rest of the
    # pipeline (publish, refine, /sites listing) keys on. ``trusted=True``
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
            pattern=pattern,
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
            "a `source` ENVELOPE { relative_path: file_contents }; the tool "
            "persists the map and stamps the pocket type='site', engine='svelte'. "
            "A site can be STATIC (marketing → pattern='landing') or DYNAMIC "
            "(backed by the customer's live data → pattern='dynamic'): for a "
            "dynamic site, declare the live-data bindings as SIBLING keys on the "
            "same `source` object — `objects` (D1 tables), `sources` (reads), "
            "`actions` (writes), `auth` (bool) — and author components that consume "
            "the generated $lib/paw/ helpers; the tool stamps pattern='dynamic' "
            "when any binding is present. You do NOT compose a rippleSpec, do NOT call "
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
                        "The SvelteKit source ENVELOPE you authored. Mostly a "
                        "{ relative_path: file_contents } map — paths relative to "
                        "the project root, file values are content STRINGS, must "
                        "include the §4.3 required files (see the tool description). "
                        "For a DYNAMIC site it ALSO carries the live-data bindings "
                        "as SIBLING keys on the same object: `objects` (array of D1 "
                        "table defs {name, fields, primaryKey}), `sources` (array of "
                        "read bindings {name, kind:'data', object}), `actions` (array "
                        "of write bindings {name, object, op}), and `auth` (bool). "
                        "Present bindings → the tool stamps pattern='dynamic' and the "
                        "published site gets a per-tenant D1; absent → a static "
                        "pattern='landing' page (unchanged)."
                    ),
                    # File values are strings; the binding siblings
                    # (objects/sources/actions/auth) are arrays/bools — so the
                    # envelope allows any value type (DSV-5). The handler enforces
                    # that every NON-binding key's value is a string.
                    "additionalProperties": True,
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


async def _edit_svelte_component_handler(args: dict) -> dict:
    """MCP handler for ``sites_manager__edit_svelte_component``.

    Reads workspace/user identity from the per-stream ContextVars, validates the
    ``pocket_id`` / ``component_path`` / ``new_source`` inputs, and delegates to
    ``sites_service.edit_svelte_component`` — which rewrites the one file in the
    pocket's svelte source map and republishes, rolling the edit back if the
    republish fails its smoke gate. Returns ``{ok, site, component_path}`` on
    success; sets ``is_error`` when identity is missing, the inputs are
    malformed, the pocket / component is not found or not a svelte site, or the
    rebuild's smoke gate rejects the edit (the live site stays on the prior
    deploy and the source is rolled back).
    """
    workspace_id, user_id = _identity()
    if not workspace_id or not user_id:
        return _error_response(
            "edit_svelte_component requires workspace and user context (call from "
            "a cloud chat session)."
        )

    record_tool_call(
        workspace_id=workspace_id,
        user_id=user_id,
        tool_server="pocketpaw_sites",
        tool_name="_edit_svelte_component",
        status="ok",
        ok=True,
    )

    pocket_id = args.get("pocket_id")
    if not isinstance(pocket_id, str) or not pocket_id:
        return _error_response(
            "edit_svelte_component requires a `pocket_id` — the id of the svelte "
            "site pocket whose component you are editing."
        )
    component_path = args.get("component_path")
    if not isinstance(component_path, str) or not component_path:
        return _error_response(
            "edit_svelte_component requires a `component_path` — the relative path "
            "of the file to rewrite (e.g. 'src/lib/components/Hero.svelte'). It "
            "must already exist in the site's source map."
        )
    # P3 — the edit can be a TARGETED diff (``edits``) OR a full rewrite
    # (``new_source``); exactly one is required. ``edits`` is preferred for small
    # changes so the agent emits only the diff, not the whole file.
    edits = args.get("edits")
    new_source = args.get("new_source")
    has_edits = edits is not None
    has_new_source = new_source is not None
    if has_edits == has_new_source:
        return _error_response(
            "edit_svelte_component requires exactly one of `edits` (a targeted "
            "search/replace diff — PREFERRED for small changes) or `new_source` "
            "(the FULL new file contents — for large rewrites). Provide one, not "
            "both and not neither."
        )
    if has_new_source and not isinstance(new_source, str):
        return _error_response(
            "edit_svelte_component `new_source` must be the FULL new contents of the "
            "file as a string (the tool replaces the whole file, not a patch)."
        )
    if has_edits:
        # Validate the diff SHAPE at the surface (a list of {old_string,
        # new_string} string dicts) so a malformed payload gets a clear error
        # before the service runs. The MATCH-uniqueness contract is enforced by the
        # service's apply_edits (relayed below as a ValidationError).
        if not isinstance(edits, list) or not edits:
            return _error_response(
                "edit_svelte_component `edits` must be a non-empty list of "
                "{old_string, new_string} blocks (like the built-in Edit tool)."
            )
        for i, block in enumerate(edits):
            if (
                not isinstance(block, dict)
                or not isinstance(block.get("old_string"), str)
                or not isinstance(block.get("new_string"), str)
            ):
                return _error_response(
                    f"edit_svelte_component `edits` block {i} must be an object with "
                    "string `old_string` and `new_string`."
                )
    name_raw = args.get("name")
    name = name_raw if isinstance(name_raw, str) else ""

    from pocketpaw_ee.cloud._core.errors import CloudError
    from pocketpaw_ee.sites import service as sites_service
    from pocketpaw_ee.sites.generator_client import SmokeGateFailed

    try:
        doc = await sites_service.edit_svelte_component(
            workspace_id=workspace_id,
            user_id=user_id,
            pocket_id=pocket_id,
            component_path=component_path,
            new_source=new_source if has_new_source else None,
            edits=edits if has_edits else None,
            name=name,
        )
    except SmokeGateFailed as exc:
        # The rebuilt site failed the workerd smoke render — the edit was rolled
        # back and the live site stays on the prior deploy. Tell the agent so it
        # can fix the component and retry, NOT report a successful edit.
        return _error_response(
            f"the edit did not pass the build smoke test, so it was not staged "
            f"(the previous version is unchanged): {exc}"
        )
    except CloudError as exc:
        # NotFound / ValidationError from the pockets service (missing pocket,
        # not a svelte site, no such component) — relay the code + message.
        return _error_response(f"{exc.code}: {exc.message}")
    except Exception as exc:  # noqa: BLE001
        logger.warning("edit_svelte_component failed", exc_info=True)
        return _error_response(f"edit failed: {exc}")

    # An edit stages a DRAFT PREVIEW — it does NOT publish or go live. The chat
    # agent narrates this payload, so it must make the draft-not-live state
    # unambiguous: ``status="draft"`` / ``is_live=False``, the url is a PREVIEW (not
    # the live site), and the user must Submit for review to publish. The wording
    # deliberately avoids "published"/"republished"/"live at" so the agent does not
    # tell the user the change is live.
    return _success_response(
        {
            "ok": True,
            "status": "draft",
            "is_live": False,
            "component_path": component_path,
            "site": {
                "id": str(doc.id),
                "pocket_id": doc.pocket_id,
                "name": doc.name,
                # The preview URL — a draft preview of the edit, NOT the live site.
                "preview_url": doc.url,
                "deployed": doc.deployed,
            },
            "message": (
                "Your change is staged as a draft preview — it is NOT live yet. "
                "The preview_url shows a preview of the edit, not the live site. "
                "To take it live, the user clicks 'Submit for review' (which sends "
                "the draft for approval)."
            ),
        }
    )


def make_edit_svelte_component_tool(tool: Any) -> Any:
    """Build the ``edit_svelte_component`` SDK tool object using the SDK's
    ``tool`` decorator (passed in by the caller that already imported it).

    Registered on the SAME ``pocketpaw_sites_manager`` server as publish +
    create_landing_site + create_svelte_site (see
    ``make_create_landing_site_tool`` for why one server)."""

    @tool(
        "edit_svelte_component",
        (
            "Edit ONE component of an EXISTING svelte Paw Site and stage it as a "
            "DRAFT PREVIEW. Use this when the user asks to change a section of a "
            "svelte site — 'add a background color to the nav', 'make the hero "
            "headline bolder', 'change the pricing copy', 'restyle the FAQ'. The "
            "edit is NOT published and does NOT go live — it is staged for review. "
            "Give the edit ONE of two ways (exactly one, not both):\n"
            "  * `edits` — PREFER THIS for small/targeted changes. A list of "
            "search/replace blocks [{old_string, new_string}, ...], exactly like "
            "the built-in Edit tool: each `old_string` is copied VERBATIM from the "
            "current file and must match EXACTLY ONCE (include enough surrounding "
            "context to be unique), and `new_string` is what it becomes. You send "
            "ONLY the change, not the whole file — far fewer tokens and faster. If "
            "you have not read the file this turn, read it first so old_string "
            "matches.\n"
            "  * `new_source` — the FULL new file contents as a string (REPLACES "
            "the whole file, not a patch). Reserve this for LARGE rewrites where "
            "most of the file changes; for a small tweak use `edits`.\n"
            "Other args: `pocket_id` (the svelte site pocket), `component_path` (the "
            "relative path of the file to edit — it must already exist in the source "
            "map, e.g. 'src/lib/components/Hero.svelte'), optional `name`. CRITICAL "
            "authoring rule (same as create_svelte_site): the component must render "
            "its resting/final state in MARKUP — never set it only in onMount — "
            "because the page is PRERENDERED. Returns {ok, status:'draft', "
            "is_live:false, site: {id, name, preview_url, deployed:false, "
            "pocket_id}, component_path, message}. Relay the `message` to the user: "
            "the change is a DRAFT PREVIEW (not live), `preview_url` is a PREVIEW "
            "(not the published site), and to publish it the user clicks 'Submit for "
            "review'. Do NOT tell the user the change is published or live. ok=false "
            "with an error means the edit was NOT staged: an `edits` old_string that "
            "matched 0 or >1 times means make it more specific and retry, a "
            "smoke-test failure leaves the previous version unchanged (fix the "
            "component and retry), and a not-found / not-a-svelte-site error means "
            "relay the reason. Do NOT report a successful edit when ok=false."
        ),
        {
            "type": "object",
            "properties": {
                "pocket_id": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Id of the svelte site pocket to edit.",
                },
                "component_path": {
                    "type": "string",
                    "minLength": 1,
                    "description": (
                        "Relative path of the file to edit — must already exist "
                        "in the site's source map (e.g. "
                        "'src/lib/components/Hero.svelte')."
                    ),
                },
                "edits": {
                    "type": "array",
                    "description": (
                        "PREFERRED for small changes: a list of search/replace "
                        "blocks applied to the file's CURRENT contents. Each "
                        "`old_string` must match exactly once. Send this INSTEAD of "
                        "`new_source` so you emit only the diff."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "old_string": {
                                "type": "string",
                                "description": (
                                    "Exact text to replace, copied verbatim from the "
                                    "current file; must match exactly once."
                                ),
                            },
                            "new_string": {
                                "type": "string",
                                "description": "Replacement text (may be empty to delete).",
                            },
                        },
                        "required": ["old_string", "new_string"],
                        "additionalProperties": False,
                    },
                },
                "new_source": {
                    "type": "string",
                    "description": (
                        "The FULL new contents of the file as a string (REPLACES the "
                        "whole file — not a diff). Use for large rewrites; for small "
                        "changes prefer `edits`."
                    ),
                },
                "name": {
                    "type": "string",
                    "description": (
                        "Optional site name override on republish. Defaults to the "
                        "pocket's own name."
                    ),
                },
            },
            "required": ["pocket_id", "component_path"],
            "additionalProperties": False,
        },
    )
    async def edit_svelte_component(args):  # type: ignore[no-untyped-def]
        return await _edit_svelte_component_handler(args)

    return edit_svelte_component


__all__ = [
    "CREATE_LANDING_SITE_TOOL_ID",
    "CREATE_SVELTE_SITE_TOOL_ID",
    "EDIT_SVELTE_COMPONENT_TOOL_ID",
    "SERVER_NAME",
    "SITES_CREATE_TOOL_IDS",
    "SVELTE_REQUIRED_EXACT_KEYS",
    "SVELTE_REQUIRED_PREFIXES",
    "make_create_landing_site_tool",
    "make_create_svelte_site_tool",
    "make_edit_svelte_component_tool",
]
