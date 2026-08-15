# sites_create.py — in-process MCP server exposing the DETERMINISTIC Paw Site
# create action. Created: 2026-06-04 (feat/sites-deterministic-fastpath).
#
# Updated: 2026-08-11 (RX-3 — the react track gets an EDIT lane) — added a SEVENTH
# tool ``edit_react_component`` plus its handler, mirroring
# ``make_edit_svelte_component_tool``'s shape (identity → record_tool_call →
# input validation → plan gate → service → draft-framed success body). It closes a
# hole this file was half of: ``edit_svelte_component`` was the ONLY edit tool
# registered, so on a react site the agent's only move for "shorten the hero
# headline" was to call ``create_react_site`` again, minting a SECOND site pocket.
# Three things about it differ from the svelte edit tool, all deliberate:
#   * DRAFT-ONLY, with no ``SmokeGateFailed`` branch to map. Nothing is built, so
#     nothing can fail a smoke gate — and a react publish enqueues its build and
#     returns before any outcome exists, so there would be nothing to roll back to
#     anyway. The success body says draft and publishing stays the user's call.
#   * A ``create`` flag, because ``set_react_source_file`` (like its svelte peer)
#     refuses a path that is not already in the map, and "add a testimonials
#     section" needs a NEW file plus an ``src/App.tsx`` edit. It requires
#     ``new_source`` and requires the path to be ABSENT, so it cannot overwrite a
#     real component.
#   * It runs ``_require_sites_plan_or_error``. ``edit_svelte_component`` does not,
#     which is an asymmetry in that tool rather than a precedent for this one.
# The reserved-path guard is the load-bearing part: without the same normalization
# create uses, ``edit_react_component(component_path="package.json", create=true)``
# writes the dependency manifest, defeating the generator's dependency allowlist
# and with it the supply-chain release-age floor. So the policy moved OUT of this
# file into ``pocketpaw_ee.sites.react_paths`` and both writers call it — the
# constants below are now re-exports.
#
# Updated: 2026-08-07 (RX-2 — the agent can select the react engine) — added a
# FIFTH create tool ``create_react_site`` for the Paw Sites "react track" (the
# engine RX-1 registered). It mirrors ``create_html_site`` (the agent IS the
# author; a {relative_path: file_contents} source MAP is persisted verbatim via
# ``agent_create(engine="react", source=<map>, type_="site", pattern="landing",
# ripple_spec=None, trusted=True)`` — no ``assemble_*`` step, no rippleSpec, no
# catalog gate) with two react-specific differences:
#
#   * VALIDATION is two-sided. The required key is the ``src/App.tsx``
#     composition root (both generated entries import it by that exact path), and
#     the map is ALSO rejected when it writes a generator-owned path — the build
#     shell (index.html / package.json / vite.config.ts / paw-prerender.mjs) or
#     the ``src/paw/`` namespace. paw-sites' react-scaffold.ts throws on the same
#     collision; checking here turns a build-time throw far from the authoring
#     turn into an actionable create error.
#   * There is an ``interactive`` argument, the react spelling of MT-1's
#     per-site ``keeps_client_bundle``. Edited (feat/sites-js-by-default): it is
#     now TRI-STATE. Omitting it persists ``None`` — no declaration — and publish
#     resolves that from ``sites_keep_client_bundle_default`` (True by default),
#     so an unflagged interactive component now HYDRATES instead of silently
#     doing nothing. It is still forwarded explicitly when the agent passes it,
#     because an explicit True/False beats the setting in both directions and
#     ``False`` is the only way to ship a page with no bundle at all. Coercing
#     the omitted case to ``False`` here (the pre-edit behaviour) would record a
#     decision the agent never made and hold every react site out of the default.
#
# This is OPT-IN like html was: the description steers the agent here ONLY on an
# explicit React request or a genuine interactivity need; the default create stays
# create_html_site. The authoring brain is the bundled
# ``pocketpaw-create-react-site`` skill, which composes with (never restates)
# ``pocketpaw-design-taste``.
#
# Updated: 2026-07-17 (fix/sites-draft-visible — a DRAFT lists in the gallery) —
# every create handler (landing / svelte / html / dynamic) now mints a DRAFT Site
# doc right after the pocket is persisted, via the shared best-effort helper
# ``_mint_draft_site`` → ``sites.service.create_draft_site``. Draft-first create
# (pocketpaw#1744) persisted the site POCKET but no Site doc, and the /sites gallery
# lists Site docs — so a plain create appeared in neither the All nor the Draft
# filter until a publish first minted one. Minting the draft doc (``deployed=False``
# — a draft, NOT a deploy, NO build, NO billing) keyed on the stable per-pocket id
# publish upserts makes the draft list immediately and flip live in place on a later
# publish (one doc per pocket). Best-effort: the pocket already exists (the primary
# contract), so a mint failure logs and returns rather than failing the create.
#
# Updated 2026-06-14 (feat/dynamic-sites-authoring — Paw Sites "Dynamic track",
# RFC 12 A2) — added the create tool ``create_dynamic_site``. It mirrors
# ``create_svelte_site`` (the agent IS the author; the payload is persisted
# verbatim via ``agent_create`` with ``trusted=True`` and the post-create
# session-bind + SSE side effects on the SAME ``pocketpaw_sites_manager`` server;
# it also runs the same identity → record_tool_call → validate →
# _require_sites_plan_or_error plan gate the other create handlers do) but the
# payload is a rippleSpec carrying the DYNAMIC blocks (``objects`` / ``sources`` /
# ``actions`` / ``auth``) that back the published site with the customer's own live
# Cloudflare D1. There is no ``assemble_*`` step. It persists via
# ``agent_create(type_="site", pattern="dynamic", ripple_spec=<spec>,
# engine="ripple", trusted=True)``: a dynamic site IS a ripple-engine site whose
# spec carries the dynamic declarations as sibling keys, so publish_pocket carries
# them through generator_client.build() unchanged and the paw-sites generator
# scaffolds the D1 migration + read/write remote functions off the same spec.
# ``_validate_dynamic_spec`` fails the create CLOSED on a spec that isn't actually
# dynamic (no objects, or no sources/actions/auth) so the agent fixes it instead
# of persisting a static page through the dynamic tool.
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
# Updated: 2026-06-17 (fix/sites-plan-gate-asymmetry) — the create handlers now
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
#
# Updated: 2026-07-12 (feat/sites-html-create-tool, HE-6) — added a FOURTH create
# tool ``create_html_site`` for the Paw Sites "html track". It mirrors
# ``create_svelte_site`` (the agent IS the author; a raw {relative_path:
# file_contents} source MAP is persisted verbatim via ``agent_create(
# engine="html", source=<map>, type_="site", pattern="landing", ripple_spec=None,
# trusted=True)`` — no ``assemble_*`` step, no rippleSpec, no catalog gate) but the
# map is plain HTML/CSS/JS with NO SvelteKit scaffold and NO bundler. Validation is
# lighter than svelte's §4.3: the only required key is the entry ``index.html`` (the
# edge serves it at the root); ``_missing_html_keys`` fails the create CLOSED
# without it. html has NO live-data binding siblings (that is the svelte/ripple
# dynamic track), so the whole map is {path: str} and every value must be a content
# string — no exemption list. Publishing an html site skips the Node build entirely
# (generator_client.needs_node_build("html") is False). This is OPT-IN: the tool
# description steers the agent to it ONLY on an explicit raw/plain-HTML request; the
# default marketing brain stays create_landing_site (ripple). The default flip is
# HE-12, gated behind HE-11.
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

from pocketpaw.agents.mcp_arg_coercion import coerce_json_object_args
from pocketpaw_ee.sites import react_paths

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
# The dynamic-track create tool (RFC 12 A2) — also the SAME server. The skill flow
# is: author the dynamic rippleSpec (UI + objects/sources/actions) →
# create_dynamic_site → publish (publish carries the dynamic blocks through to the
# paw-sites generator, which scaffolds the per-tenant D1 + read/write remote fns).
CREATE_DYNAMIC_SITE_TOOL_ID = f"mcp__{SERVER_NAME}__create_dynamic_site"
# The html-track create tool (HE-6) — also the SAME server. The skill flow is:
# author a raw HTML/CSS/JS source map → create_html_site → publish. Publishing an
# html site skips the Node build entirely (generator_client.needs_node_build is
# False for html); the raw markup is materialized and served as-is.
CREATE_HTML_SITE_TOOL_ID = f"mcp__{SERVER_NAME}__create_html_site"

# RX-2 — the react-track create tool. Same shape as the html id above; the
# authoring hop is author a React source map → create_react_site → publish.
# Publishing a react site DOES run a Node build (a Vite SSG) but deploys the
# prerendered static output assets-only, with no server entry.
CREATE_REACT_SITE_TOOL_ID = f"mcp__{SERVER_NAME}__create_react_site"

# RX-3 — the react-track EDIT tool. Same server again. Without it the agent's only
# response to "shorten the hero headline" on a react site was to call
# ``create_react_site`` a second time, which mints a SECOND site pocket instead of
# changing the one the user is looking at.
EDIT_REACT_COMPONENT_TOOL_ID = f"mcp__{SERVER_NAME}__edit_react_component"

# HE-10 — the html-track EDIT tool, and the last engine to get one. Same server
# again. Its absence had the same shape as RX-3's: ``edit_svelte_component``
# rejects an html pocket and so does ``edit_react_component``, so "change the phone
# number in the footer" had no tool that would take it and the agent's only move was
# a second ``create_html_site`` — a second pocket at a second url. Named for a FILE,
# not a component, because an html site has no component model to name.
EDIT_HTML_FILE_TOOL_ID = f"mcp__{SERVER_NAME}__edit_html_file"

SITES_CREATE_TOOL_IDS = (
    CREATE_LANDING_SITE_TOOL_ID,
    CREATE_SVELTE_SITE_TOOL_ID,
    EDIT_SVELTE_COMPONENT_TOOL_ID,
    CREATE_DYNAMIC_SITE_TOOL_ID,
    CREATE_HTML_SITE_TOOL_ID,
    CREATE_REACT_SITE_TOOL_ID,
    EDIT_REACT_COMPONENT_TOOL_ID,
    EDIT_HTML_FILE_TOOL_ID,
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


# ── html-track source map (HE-6) ────────────────────────────────────────────
# An html-engine ``source`` map is a raw {relative_path: file_contents} map of
# HTML/CSS/JS — no SvelteKit scaffold, no bundler. The only hard requirement is
# the entry document ``index.html``: the paw-sites generator materializes the map
# verbatim (html-scaffold.ts:materializeHtml) and the edge serves ``index.html``
# at the site root, so a map without it is unservable. Everything else (styles,
# scripts, extra pages, assets) is optional and passes through as authored.
HTML_REQUIRED_KEYS = ("index.html",)


def _missing_html_keys(source: dict[str, Any]) -> list[str]:
    """Return the required html keys absent from ``source`` (empty list = valid).

    Only ``index.html`` is required — it is the entry the generator materializes
    and the edge serves at the site root. Used to fail the create closed with an
    actionable message rather than persisting a site with no servable entry."""
    return [k for k in HTML_REQUIRED_KEYS if k not in source]


# ── react-track source map (RX-2) ───────────────────────────────────────────
# A react-engine ``source`` map is a {relative_path: file_contents} map of
# hand-written React files. The generator (paw-sites react-scaffold.ts) owns the
# build shell — index.html, package.json, vite.config.ts, paw-prerender.mjs and
# the two ``src/paw/`` entries — and materializes this map on top of it.
#
# The only required key is the composition root ``src/App.tsx``: BOTH generated
# entries (client and server) import it by that exact path, so a map without it
# builds nothing. The generator asserts the same key, but failing here names it
# before a build is ever started.
REACT_REQUIRED_KEYS = ("src/App.tsx",)

# Paths the generator owns and a source map may NOT write. Mirrors
# ``RESERVED_FILES`` + ``RESERVED_NAMESPACE`` in paw-sites' react-scaffold.ts,
# which throws on a collision — this is the same guard moved to create time, so
# the agent gets an actionable message instead of a build failure at publish.
#
# Reserving them is not tidiness: an author who could overwrite ``index.html`` or
# ``paw-prerender.mjs`` could remove the prerender outlet or the pass that fills
# it, turning the site back into a shell that is blank with JavaScript disabled —
# exactly what this engine exists to refuse to ship. And an author who could
# overwrite ``package.json`` would be writing the dependency manifest, which is
# where the supply-chain release-age floor is enforced.
#
# RX-3: the policy itself now lives in ``pocketpaw_ee.sites.react_paths`` because
# the EDIT lane is a second writer of the same map and two copies of a guard drift.
# These names are re-exported here (and stay in ``__all__``) so every importer of
# the create-time spelling keeps working.
REACT_RESERVED_FILES = react_paths.REACT_RESERVED_FILES
REACT_RESERVED_PREFIX = react_paths.REACT_RESERVED_PREFIX


def _missing_react_keys(source: dict[str, Any]) -> list[str]:
    """Return the required react keys absent from ``source`` (empty list = valid).

    Only ``src/App.tsx`` is required — the composition root both generated entries
    import. Everything else (the components it imports, its CSS, ``public/``
    assets) is the author's business."""
    return [k for k in REACT_REQUIRED_KEYS if k not in source]


def _reserved_react_keys(source: dict[str, Any]) -> list[str]:
    """Return the source-map keys that collide with a generator-owned path.

    A thin alias for ``react_paths.reserved_react_keys`` (RX-3), which normalizes
    backslashes and ``.``/``..`` segments so ``./index.html`` and
    ``src\\paw\\x.tsx`` cannot slip past the check. Kept under this name because
    the create handler and its tests call it."""
    return react_paths.reserved_react_keys(source)


# ── Dynamic-track spec surface (RFC 12 A2) ──────────────────────────────────
# A dynamic Paw Site is a normal ripple-engine site whose rippleSpec ALSO carries
# the optional top-level blocks the paw-sites generator reads to back the page
# with the customer's own live D1 (docs/dynamic-spec-surface.md in paw-sites):
#   - ``objects``  : the D1 schema (table defs) — derives migrations/0001_init.sql
#   - ``sources``  : read bindings — compile to ``query`` remote fns (D1 → page)
#   - ``actions``  : write bindings — compile to ``form`` remote fns (page → D1)
#   - ``auth``     : a top-level bool gating the site behind end-customer accounts
# A spec is dynamic when it declares any ``sources``, any ``actions``, or
# ``auth: true`` (this mirrors paw-sites' parseBindings.isDynamic). The generator
# already routes a ripple build through the dynamic path on these keys, so the
# create tool only has to PERSIST them verbatim on the rippleSpec — publish carries
# the whole spec through generator_client.build() unchanged.


def _validate_dynamic_spec(spec: dict) -> list[str]:
    """Return a list of human-readable problems with a dynamic-site ``spec``
    (empty list = valid). Fails the create CLOSED with an actionable message
    rather than persisting a spec the generator can't turn into a live site.

    Checks the minimum contract for a dynamic site (matching paw-sites'
    ``parseBindings``): a ``ui`` tree, an ``objects`` block, at least one live
    binding (``sources`` / ``actions`` / ``auth``), and that every source/action
    references a declared object. Pure — no identity / Mongo needed."""
    problems: list[str] = []
    ui = spec.get("ui")
    if not (isinstance(ui, dict) and ui.get("type")):
        problems.append("a `ui` tree (a node with a `type`) that renders the page")

    objects = spec.get("objects")
    object_names: set[str] = set()
    if not isinstance(objects, list) or not objects:
        problems.append("an `objects` array declaring at least one D1 table")
    else:
        for obj in objects:
            if isinstance(obj, dict) and isinstance(obj.get("name"), str):
                object_names.add(obj["name"])

    sources = spec.get("sources") if isinstance(spec.get("sources"), list) else []
    actions = spec.get("actions") if isinstance(spec.get("actions"), list) else []
    is_dynamic = bool(sources) or bool(actions) or spec.get("auth") is True
    if not is_dynamic:
        problems.append(
            "at least one live binding — a `sources` entry (read), an `actions` "
            "entry (write), or `auth: true`. Without one the site is static, not "
            "dynamic; use create_landing_site instead"
        )

    # Every source/action must reference a declared object (the generator errors
    # otherwise). Only check when objects parsed, so the message stays specific.
    if object_names:
        for s in sources:
            if isinstance(s, dict) and s.get("object") not in object_names:
                problems.append(
                    f"source `{s.get('name', '?')}` references undeclared "
                    f"object `{s.get('object')}`"
                )
        for a in actions:
            if isinstance(a, dict) and a.get("object") not in object_names:
                problems.append(
                    f"action `{a.get('name', '?')}` references undeclared "
                    f"object `{a.get('object')}`"
                )
    return problems


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


async def _mint_draft_site(workspace_id: str, user_id: str, pocket_id: str, name: str) -> None:
    """Mint the DRAFT Site doc for a freshly created site pocket so it lists in the
    /sites gallery immediately (fix/sites-draft-visible).

    Draft-first create persists a site POCKET but no Site doc, and the gallery reads
    Site docs — so without this a plain create shows in neither the All nor the Draft
    filter until a publish first mints one. This creates ONE canonical Site doc
    (``deployed=False`` — a draft, NOT a deploy, NO build, NO billing) keyed on the
    stable per-pocket id publish upserts, so a later publish flips this SAME doc live
    (one doc per pocket). Best-effort: the pocket already exists in Mongo (the primary
    contract), so a mint failure logs and returns rather than undoing a successful
    create — the site is simply not yet listable (the prior behaviour), never a hard
    error. The plan gate already ran before ``agent_create``, so this adds no gate."""
    try:
        from pocketpaw_ee.sites.service import create_draft_site

        await create_draft_site(
            workspace_id=workspace_id, user_id=user_id, pocket_id=pocket_id, name=name
        )
    except Exception:  # noqa: BLE001 — draft-doc mint is best-effort, never fails a create
        logger.warning(
            "create-site: draft Site doc mint failed for pocket %s (non-fatal)",
            pocket_id,
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

    # Decode a `content` object the model serialized as a JSON string. Note:
    # for a LARGE payload the model's stringified JSON is often malformed
    # (unescaped quotes in the copy), which json.loads can't recover — those
    # still fall to the error below. This rescues the well-formed-string case.
    args = coerce_json_object_args(args, ("content",))
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

    # Mint the DRAFT Site doc so the new site lists in the /sites gallery right away
    # (draft-first create persists the pocket but no Site doc; the gallery reads Site
    # docs). Best-effort — a draft, NOT a publish, and it never fails the create.
    await _mint_draft_site(workspace_id, user_id, new_pocket_id, name)

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
            "`mcp__pocketpaw_sites_manager__publish` to publish ONLY when the user "
            "asks to go live (draft-first: a plain create stops at the draft for "
            "in-app preview). ok=false with an "
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


async def _create_dynamic_site_handler(args: dict) -> dict:
    """MCP handler for ``sites_manager__create_dynamic_site`` (the Dynamic track,
    RFC 12 A2).

    Reads workspace/user identity from the per-stream ContextVars, validates the
    ``spec`` (a rippleSpec carrying the dynamic blocks — ``objects`` + at least
    one ``sources`` / ``actions`` / ``auth``), and persists it DIRECTLY via
    ``agent_create`` (type="site", pattern="dynamic", ripple_spec=<spec>,
    engine="ripple", trusted=True). Returns ``{ok, pocket_id, pocket}`` on
    success; sets ``is_error`` when identity is missing, ``spec`` is absent /
    not dynamic / malformed, or the persist fails.

    Unlike ``create_landing_site`` there is no ``assemble_*`` step — the agent
    authored the dynamic spec (UI + data bindings) via the
    pocketpaw-create-dynamic-site skill, so it is persisted verbatim. The dynamic
    blocks ride the rippleSpec as sibling keys, so ``publish_pocket`` carries them
    through ``generator_client.build()`` unchanged and the paw-sites generator
    scaffolds the D1 migration + read/write remote functions. ``trusted=True``
    skips the STRICT catalog gate (same reason as create_landing_site: the
    published widget manifest is stale; the logged catalog walk + embed audit
    still run) — the dynamic blocks are not widgets and pass through untouched."""
    workspace_id, user_id = _identity()
    if not workspace_id or not user_id:
        return _error_response(
            "create_dynamic_site requires workspace and user context (call from a "
            "cloud chat session)."
        )

    record_tool_call(
        workspace_id=workspace_id,
        user_id=user_id,
        tool_server="pocketpaw_sites",
        tool_name="_create_dynamic_site",
        status="ok",
        ok=True,
    )

    args = coerce_json_object_args(args, ("spec",))
    spec = args.get("spec")
    if not isinstance(spec, dict) or not spec:
        return _error_response(
            "create_dynamic_site requires a `spec` object — the rippleSpec for the "
            "dynamic site: a `ui` tree PLUS the dynamic blocks (`objects` for the "
            "D1 schema, `sources` for reads, `actions` for writes, optional "
            "`auth`). You author the spec; this tool persists it."
        )

    problems = _validate_dynamic_spec(spec)
    if problems:
        return _error_response(
            "create_dynamic_site `spec` is not a valid dynamic site — it needs "
            + "; ".join(problems)
            + ". See the pocketpaw-create-dynamic-site skill for the shape."
        )

    # Plan gate (Sites = "sites"): reject a free-plan workspace here so the
    # create can't bypass the router's require_plan_feature("sites") gate.
    if (gate := await _require_sites_plan_or_error(workspace_id)) is not None:
        return gate

    name_raw = args.get("name")
    name = name_raw.strip() if isinstance(name_raw, str) and name_raw.strip() else "Dynamic site"
    description_raw = args.get("description")
    description = description_raw if isinstance(description_raw, str) else ""
    icon_raw = args.get("icon")
    icon = icon_raw if isinstance(icon_raw, str) else ""
    color_raw = args.get("color")
    color = color_raw if isinstance(color_raw, str) else ""

    # Persist DIRECTLY through the pockets service — NO pocket_specialist, NO
    # draft/redraft loop. ``type_="site"`` keeps the site identity the rest of the
    # pipeline (publish, /sites listing) keys on; ``pattern="dynamic"`` marks the
    # live-data track (informational — publish routes on the rippleSpec's dynamic
    # blocks, not the pattern). ``engine="ripple"`` (the default): a dynamic site
    # IS a ripple-engine site whose spec carries dynamic declarations, so the
    # generator compiles the rippleSpec AND scaffolds the D1 path off the same spec.
    from pocketpaw_ee.cloud.pockets.service import agent_create

    try:
        view, new_pocket_id, err = await agent_create(
            workspace_id=workspace_id,
            owner_id=user_id,
            name=name,
            description=description,
            type_="site",
            pattern="dynamic",
            icon=icon,
            color=color,
            ripple_spec=spec,
            trusted=True,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("create_dynamic_site: persist raised", exc_info=True)
        return _error_response(f"create failed: {exc}")

    if err is not None or view is None or new_pocket_id is None:
        return _error_response(f"create failed: {err or 'create returned no view'}")

    # Mint the DRAFT Site doc so the new site lists in the /sites gallery right away
    # (draft-first create persists the pocket but no Site doc; the gallery reads Site
    # docs). Best-effort — a draft, NOT a publish, and it never fails the create.
    await _mint_draft_site(workspace_id, user_id, new_pocket_id, name)

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


def make_create_dynamic_site_tool(tool: Any) -> Any:
    """Build the ``create_dynamic_site`` SDK tool object using the SDK's ``tool``
    decorator (passed in by the caller that already imported it).

    Registered on the SAME ``pocketpaw_sites_manager`` server as publish +
    create_landing_site + create_svelte_site (see ``make_create_landing_site_tool``
    for why one server)."""

    @tool(
        "create_dynamic_site",
        (
            "Create a DYNAMIC Paw Site — a published website backed by the "
            "customer's OWN LIVE DATA (a per-tenant Cloudflare D1), with reads and "
            "writes, NOT a static brochure. You AUTHOR a rippleSpec that carries "
            "both the UI and the dynamic data bindings, and pass it as `spec`; the "
            "tool persists it and stamps the pocket type='site', pattern='dynamic'. "
            "Use this when the user wants a site that LISTS live records and/or has "
            "a form that SAVES records (a guestbook, a booking list, a submissions "
            "board, an order tracker). For a static marketing page use "
            "create_landing_site instead. The `spec` (a rippleSpec) carries these "
            "DYNAMIC blocks as top-level keys (see the pocketpaw-create-dynamic-site "
            "skill): `objects` [{name, fields:{col: text|integer|real|boolean|"
            "timestamp}, primaryKey}] = the D1 tables; `sources` [{name, "
            "kind:'data', object, where?, orderBy?, limit?, refresh:'pocket_open'|"
            "'interval'|'manual'|'live'}] = READ bindings (the UI binds a table to "
            "'{<source name>}'); `actions` [{name, object, op:'insert', confirm?, "
            "requiresOwnerReview?}] = WRITE bindings (rendered as a native form); "
            "optional `auth: true` gates the site behind end-customer accounts. A "
            "spec must declare `objects` AND at least one source/action/auth, and "
            "every source/action must reference a declared object. Returns {ok, "
            "pocket_id, pocket}; hand `pocket_id` to "
            "`mcp__pocketpaw_sites_manager__publish` to publish ONLY when the user "
            "asks to go live (draft-first: a plain create stops at the draft for "
            "in-app preview). ok=false with an "
            "error means relay the reason, do NOT report a created pocket."
        ),
        {
            "type": "object",
            "properties": {
                "spec": {
                    "type": "object",
                    "description": (
                        "The dynamic rippleSpec you authored — a `ui` tree PLUS the "
                        "dynamic blocks (`objects`, `sources`, `actions`, optional "
                        "`auth`). See the tool description / skill for the shape."
                    ),
                    "additionalProperties": True,
                },
                "name": {
                    "type": "string",
                    "description": "Optional pocket/site name. Defaults to 'Dynamic site'.",
                },
                "description": {
                    "type": "string",
                    "description": "Optional one-line pocket description.",
                },
                "icon": {"type": "string", "description": "Optional lucide icon name."},
                "color": {"type": "string", "description": "Optional accent color hex."},
            },
            "required": ["spec"],
            "additionalProperties": False,
        },
    )
    async def create_dynamic_site(args):  # type: ignore[no-untyped-def]
        return await _create_dynamic_site_handler(args)

    return create_dynamic_site


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

    args = coerce_json_object_args(args, ("source",))
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

    # Mint the DRAFT Site doc so the new site lists in the /sites gallery right away
    # (draft-first create persists the pocket but no Site doc; the gallery reads Site
    # docs). Best-effort — a draft, NOT a publish, and it never fails the create.
    await _mint_draft_site(workspace_id, user_id, new_pocket_id, name)

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
            "`mcp__pocketpaw_sites_manager__publish` to publish ONLY when the user "
            "asks to go live (draft-first: a plain create stops at the draft for "
            "in-app preview). ok=false with an "
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


async def _create_html_site_handler(args: dict) -> dict:
    """MCP handler for ``sites_manager__create_html_site`` (the html track, HE-6).

    Reads workspace/user identity from the per-stream ContextVars, validates the
    ``source`` map (a raw {relative_path: file_contents} HTML/CSS/JS map that MUST
    carry ``index.html``), and persists it DIRECTLY via ``agent_create``
    (engine="html", source=<map>, type="site", pattern="landing", ripple_spec=None,
    trusted=True). Returns ``{ok, pocket_id, pocket}`` on success; sets ``is_error``
    when identity is missing, ``source`` is absent/malformed/incomplete, or the
    persist fails.

    Like ``create_svelte_site`` there is no ``assemble_*`` step — the agent authored
    the markup, so the map is persisted verbatim and the generator materializes it
    at publish. Unlike svelte, an html publish skips the Node build entirely
    (``generator_client.needs_node_build("html")`` is False): the raw files are
    served as authored. html has NO live-data binding siblings — the whole map is
    {path: str}, so every value must be a content string."""
    workspace_id, user_id = _identity()
    if not workspace_id or not user_id:
        return _error_response(
            "create_html_site requires workspace and user context (call from a cloud chat session)."
        )

    record_tool_call(
        workspace_id=workspace_id,
        user_id=user_id,
        tool_server="pocketpaw_sites",
        tool_name="_create_html_site",
        status="ok",
        ok=True,
    )

    args = coerce_json_object_args(args, ("source",))
    source = args.get("source")
    if not isinstance(source, dict) or not source:
        return _error_response(
            "create_html_site requires a `source` object — the raw HTML/CSS/JS "
            "source map { relative_path: file_contents } you authored. It must "
            "include `index.html` (the page the edge serves); add stylesheets, "
            "scripts, and assets as sibling entries. You write the markup; this "
            "tool persists it."
        )
    # The whole map is {path: contents} — every value is a file content string.
    # html has no live-data binding siblings (that is the svelte/ripple dynamic
    # track), so unlike create_svelte_site there is no exemption list.
    bad = [k for k, v in source.items() if not isinstance(v, str)]
    if bad:
        return _error_response(
            "create_html_site `source` file values must be content strings; these "
            f"keys are not strings: {', '.join(sorted(bad)[:8])}."
        )
    missing = _missing_html_keys(source)
    if missing:
        return _error_response(
            "create_html_site `source` is missing required files: "
            f"{', '.join(missing)}. An html site needs an `index.html` entry "
            "document — the edge serves it at the site root."
        )

    # Plan gate (Sites = "sites"): reject a free-plan workspace here so the
    # create can't bypass the router's require_plan_feature("sites") gate.
    if (gate := await _require_sites_plan_or_error(workspace_id)) is not None:
        return gate

    name_raw = args.get("name")
    name = name_raw.strip() if isinstance(name_raw, str) and name_raw.strip() else "HTML site"
    description_raw = args.get("description")
    description = description_raw if isinstance(description_raw, str) else ""
    icon_raw = args.get("icon")
    icon = icon_raw if isinstance(icon_raw, str) else ""
    color_raw = args.get("color")
    color = color_raw if isinstance(color_raw, str) else ""

    # Persist DIRECTLY through the pockets service — NO pocket_specialist, NO
    # rippleSpec, NO catalog gate (there is no spec to gate). ``engine="html"``
    # + ``source`` stamp the html track so the generator materializes the map
    # verbatim and publish skips the Node build; ``type_="site"`` +
    # ``pattern="landing"`` keep the site identity the rest of the pipeline
    # (publish, /sites listing) keys on. ``trusted=True`` short-circuits the strict
    # catalog gate, which only runs on a non-null rippleSpec anyway — the html
    # path passes ``ripple_spec=None``.
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
            engine="html",
            source=source,
            trusted=True,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("create_html_site: persist raised", exc_info=True)
        return _error_response(f"create failed: {exc}")

    if err is not None or view is None or new_pocket_id is None:
        return _error_response(f"create failed: {err or 'create returned no view'}")

    # Mint the DRAFT Site doc so the new site lists in the /sites gallery right away
    # (draft-first create persists the pocket but no Site doc; the gallery reads Site
    # docs). Best-effort — a draft, NOT a publish, and it never fails the create.
    await _mint_draft_site(workspace_id, user_id, new_pocket_id, name)

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


def make_create_html_site_tool(tool: Any) -> Any:
    """Build the ``create_html_site`` SDK tool object using the SDK's ``tool``
    decorator (passed in by the caller that already imported it).

    Registered on the SAME ``pocketpaw_sites_manager`` server as publish +
    create_landing_site + create_svelte_site + create_dynamic_site (see
    ``make_create_landing_site_tool`` for why one server)."""

    @tool(
        "create_html_site",
        (
            "Create a Paw Site landing page on the HTML TRACK — a raw, "
            "hand-authored static site (plain HTML/CSS/JS, no framework, no build "
            "step). Use this ONLY when the user EXPLICITLY asks for a plain / "
            "raw / single-file HTML site or 'no framework' ('give me a bare HTML "
            "landing page', 'just an index.html', 'no Svelte'). For a normal "
            "marketing request the default is create_landing_site (ripple) — do "
            "NOT pick this one by default. You AUTHOR the markup yourself and pass "
            "it as a `source` MAP { relative_path: file_contents }; the tool "
            "persists the map and stamps the pocket type='site', pattern='landing', "
            "engine='html'. You do NOT compose a rippleSpec, do NOT author Svelte, "
            "do NOT call pocket_specialist. The map MUST include `index.html` (the "
            "page the edge serves at the root); add stylesheets, scripts, extra "
            "pages, and assets as sibling entries — every value is a content "
            "STRING. Publishing an html site skips the Node build entirely: the raw "
            "files are served exactly as authored, so the page must be complete on "
            "its own (inline or linked CSS/JS, real copy — never 'TBD'/'Lorem "
            "ipsum'). Returns {ok, pocket_id, pocket}; hand `pocket_id` to "
            "`mcp__pocketpaw_sites_manager__publish` to publish ONLY when the user "
            "asks to go live (draft-first: a plain create stops at the draft for "
            "in-app preview). ok=false with an "
            "error means relay the reason, do NOT report a created pocket."
        ),
        {
            "type": "object",
            "properties": {
                "source": {
                    "type": "object",
                    "description": (
                        "The raw HTML/CSS/JS source map you authored — a "
                        "{ relative_path: file_contents } map, paths relative to the "
                        "site root, every value a content STRING. MUST include "
                        "`index.html` (the entry document served at the root). Add "
                        "styles, scripts, and assets as sibling entries."
                    ),
                    "additionalProperties": {"type": "string"},
                },
                "name": {
                    "type": "string",
                    "description": "Optional pocket/site name. Defaults to 'HTML site'.",
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
    async def create_html_site(args):  # type: ignore[no-untyped-def]
        return await _create_html_site_handler(args)

    return create_html_site


async def _create_react_site_handler(args: dict) -> dict:
    """MCP handler for ``sites_manager__create_react_site`` (the react track, RX-2).

    Reads workspace/user identity from the per-stream ContextVars, validates the
    ``source`` map (a {relative_path: file_contents} map of hand-written React
    files that MUST carry the ``src/App.tsx`` composition root and MUST NOT write
    a generator-owned path), and persists it DIRECTLY via ``agent_create``
    (engine="react", source=<map>, type="site", pattern="landing",
    ripple_spec=None, trusted=True). Returns ``{ok, pocket_id, pocket}`` on
    success; sets ``is_error`` when identity is missing, ``source`` is
    absent/malformed/incomplete, or the persist fails.

    Like ``create_svelte_site`` and ``create_html_site`` there is no ``assemble_*``
    step — the agent authored the components, so the map is persisted verbatim and
    the generator materializes it at publish. Unlike html, a react publish DOES run
    a per-site Node build (``needs_node_build("react")`` is True) — but that build
    is an SSG that prerenders to a static ``dist/`` with no server entry, which is
    why react deploys assets-only like html does (``emits_server_worker`` is False
    for both — see ``sites/engines.py``).

    ``interactive`` is the react-track spelling of the per-site
    ``keeps_client_bundle`` declaration. It is a SEPARATE argument rather than an
    inference over the source because "does this component need the browser?" is a
    question about authorial intent that reading JSX cannot answer reliably: a
    ``useState`` that never changes needs nothing, and a component that only
    registers a ``matchMedia`` listener does.

    It is TRI-STATE (feat/sites-js-by-default). OMITTING it records NO decision
    and lets publish apply ``sites_keep_client_bundle_default`` — ``True`` by
    default, so an omitted ``interactive`` now ships the bundle rather than
    withholding it. That inverts the old failure mode: leaving it off used to
    silently break an interactive page (built, deployed, menu never opens), and
    now the silent cost is the opposite and much cheaper — a purely static page
    ships a bundle it never needed. Pass ``interactive=False`` EXPLICITLY to opt
    such a page out; an explicit value beats the default in both directions.

    react has NO live-data binding siblings (that is the svelte/ripple dynamic
    track), so the whole map is {path: str} and every value must be a content
    string — no exemption list."""
    workspace_id, user_id = _identity()
    if not workspace_id or not user_id:
        return _error_response(
            "create_react_site requires workspace and user context (call from a "
            "cloud chat session)."
        )

    record_tool_call(
        workspace_id=workspace_id,
        user_id=user_id,
        tool_server="pocketpaw_sites",
        tool_name="_create_react_site",
        status="ok",
        ok=True,
    )

    args = coerce_json_object_args(args, ("source",))
    source = args.get("source")
    if not isinstance(source, dict) or not source:
        return _error_response(
            "create_react_site requires a `source` object — the React source map "
            "{ relative_path: file_contents } you authored. It must include "
            "`src/App.tsx` (the composition root); add your section components "
            "under `src/components/` and your stylesheet as sibling entries. You "
            "write the components; this tool persists them."
        )
    # The whole map is {path: contents} — every value is a file content string.
    # react has no live-data binding siblings (that is the svelte/ripple dynamic
    # track), so unlike create_svelte_site there is no exemption list.
    bad = [k for k, v in source.items() if not isinstance(v, str)]
    if bad:
        return _error_response(
            "create_react_site `source` file values must be content strings; these "
            f"keys are not strings: {', '.join(sorted(bad)[:8])}."
        )
    missing = _missing_react_keys(source)
    if missing:
        return _error_response(
            "create_react_site `source` is missing required files: "
            f"{', '.join(missing)}. A react site needs a `src/App.tsx` composition "
            "root — the generated client and server entries both import it, so "
            "without it there is nothing to render or prerender."
        )
    # Fail here rather than at publish: the generator throws on a reserved-path
    # collision, and a build-time throw names the path far from the authoring turn
    # that caused it.
    reserved = _reserved_react_keys(source)
    if reserved:
        return _error_response(
            "create_react_site `source` may not write generator-owned paths: "
            f"{', '.join(reserved)}. The build shell (index.html, package.json, "
            "vite.config.ts, paw-prerender.mjs) and the `src/paw/` namespace are "
            "generated — they carry the prerender contract that keeps the page "
            "from shipping blank without JavaScript. Author under `src/` (outside "
            "`src/paw/`) and `public/`."
        )

    # Plan gate (Sites = "sites"): reject a free-plan workspace here so the
    # create can't bypass the router's require_plan_feature("sites") gate.
    if (gate := await _require_sites_plan_or_error(workspace_id)) is not None:
        return gate

    name_raw = args.get("name")
    name = name_raw.strip() if isinstance(name_raw, str) and name_raw.strip() else "React site"
    description_raw = args.get("description")
    description = description_raw if isinstance(description_raw, str) else ""
    icon_raw = args.get("icon")
    icon = icon_raw if isinstance(icon_raw, str) else ""
    color_raw = args.get("color")
    color = color_raw if isinstance(color_raw, str) else ""
    # RX-2: the authored "this site's own JavaScript is load-bearing" declaration,
    # spelled ``interactive`` on the wire because that is the authoring question
    # the agent can actually answer. Persisted as the engine-agnostic
    # ``keeps_client_bundle`` (MT-1) so publish reads ONE field for every engine.
    # TRI-STATE (feat/sites-js-by-default): an OMITTED ``interactive`` persists as
    # ``None`` — no declaration — which publish resolves from
    # ``sites_keep_client_bundle_default``. Coercing the absent case to ``False``
    # here would record a decision the agent never made and lock every react site
    # that skips the argument out of the default.
    _interactive_raw = args.get("interactive")
    interactive = None if _interactive_raw is None else bool(_interactive_raw)

    # Persist DIRECTLY through the pockets service — NO pocket_specialist, NO
    # rippleSpec, NO catalog gate (there is no spec to gate). ``engine="react"``
    # + ``source`` stamp the react track so the generator materializes the map onto
    # its Vite skeleton and prerenders it; ``type_="site"`` + ``pattern="landing"``
    # keep the site identity the rest of the pipeline (publish, /sites listing)
    # keys on. ``trusted=True`` short-circuits the strict catalog gate, which only
    # runs on a non-null rippleSpec anyway — the react path passes
    # ``ripple_spec=None``.
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
            engine="react",
            source=source,
            keeps_client_bundle=interactive,
            trusted=True,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("create_react_site: persist raised", exc_info=True)
        return _error_response(f"create failed: {exc}")

    if err is not None or view is None or new_pocket_id is None:
        return _error_response(f"create failed: {err or 'create returned no view'}")

    # Mint the DRAFT Site doc so the new site lists in the /sites gallery right away
    # (draft-first create persists the pocket but no Site doc; the gallery reads Site
    # docs). Best-effort — a draft, NOT a publish, and it never fails the create.
    await _mint_draft_site(workspace_id, user_id, new_pocket_id, name)

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


def make_create_react_site_tool(tool: Any) -> Any:
    """Build the ``create_react_site`` SDK tool object using the SDK's ``tool``
    decorator (passed in by the caller that already imported it).

    Registered on the SAME ``pocketpaw_sites_manager`` server as publish +
    create_landing_site + create_svelte_site + create_html_site +
    create_dynamic_site (see ``make_create_landing_site_tool`` for why one
    server)."""

    @tool(
        "create_react_site",
        (
            "Create a Paw Site landing page on the REACT TRACK — hand-written React "
            "components, PRERENDERED to static HTML at build time. Use this ONLY "
            "when the user EXPLICITLY asks for React ('build it in React', 'a React "
            "landing page') or the page genuinely needs React-shaped client "
            "interactivity. For a normal marketing request the default is "
            "create_html_site — do NOT pick this one by default. You AUTHOR the "
            "components yourself and pass them as a `source` MAP { relative_path: "
            "file_contents }; the tool persists the map and stamps the pocket "
            "type='site', pattern='landing', engine='react'. You do NOT compose a "
            "rippleSpec, do NOT call get_widget_spec, do NOT call pocket_specialist. "
            "The map MUST include `src/App.tsx` (the composition root both generated "
            "entries import); add section components under `src/components/*.tsx` "
            "and a stylesheet App.tsx imports — every value is a content STRING. The "
            "build shell is GENERATED and reserved: the map may NOT write "
            "index.html, package.json, vite.config.ts, paw-prerender.mjs, or "
            "anything under `src/paw/`. The project has react, react-dom and vite "
            "and NOTHING else — no router, no CSS framework, no state or animation "
            "library, and you cannot add dependencies; it is ONE page. CRITICAL "
            "authoring rule: the page is PRERENDERED, so every component must render "
            "its resting/final state in its RETURNED MARKUP — useEffect does not run "
            "at prerender time (a count-up initialized to 0 bakes '0'; initialize it "
            "to the final value). SECOND RULE: sites ship their client JavaScript by "
            "DEFAULT, so React hydrates and a menu toggle, tabs or a counter work "
            "without any extra argument. Still pass `interactive=true` explicitly "
            "whenever a component needs the browser — it records the intent and "
            "survives a deployment that turns the default off. Pass "
            "`interactive=false` for a purely static page (CSS-only hover/keyframe "
            "motion, anchors, a native form POST) to opt out of shipping a bundle it "
            "never uses. Returns {ok, "
            "pocket_id, pocket}; hand `pocket_id` to "
            "`mcp__pocketpaw_sites_manager__publish` to publish ONLY when the user "
            "asks to go live (draft-first: a plain create stops at the draft for "
            "in-app preview). ok=false with an error means relay the reason, do NOT "
            "report a created pocket."
        ),
        {
            "type": "object",
            "properties": {
                "source": {
                    "type": "object",
                    "description": (
                        "The React source map you authored — a { relative_path: "
                        "file_contents } map, paths relative to the project root, "
                        "every value a content STRING. MUST include `src/App.tsx` "
                        "(the composition root, which imports your stylesheet and "
                        "renders the sections). Add `src/components/*.tsx` sections "
                        "and `public/*` assets as sibling entries. May NOT write "
                        "index.html, package.json, vite.config.ts, paw-prerender.mjs "
                        "or anything under `src/paw/` — those are generated."
                    ),
                    "additionalProperties": {"type": "string"},
                },
                "interactive": {
                    "type": "boolean",
                    "description": (
                        "TRUE when this site's own JavaScript is load-bearing — any "
                        "changing useState/useReducer, any onClick/onChange/onSubmit "
                        "that does something, any useEffect, a canvas you draw into. "
                        "The page is prerendered either way; this decides whether "
                        "React HYDRATES on top of the baked markup. OMIT it and the "
                        "deployment's default decides — which ships the bundle and "
                        "hydrates, so an interactive component left unflagged still "
                        "works. Pass TRUE anyway when the browser is genuinely "
                        "needed: it records the intent instead of relying on a "
                        "setting. Pass FALSE for a purely static page (CSS-only "
                        "motion, anchors, a native form POST) — an explicit value "
                        "wins over the default, so this is the way to ship no "
                        "JavaScript at all."
                    ),
                },
                "name": {
                    "type": "string",
                    "description": "Optional pocket/site name. Defaults to 'React site'.",
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
    async def create_react_site(args):  # type: ignore[no-untyped-def]
        return await _create_react_site_handler(args)

    return create_react_site


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
    args = coerce_json_object_args(args, ("edits",))
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


async def _edit_react_component_handler(args: dict) -> dict:
    """MCP handler for ``sites_manager__edit_react_component`` (RX-3).

    Reads workspace/user identity from the per-stream ContextVars, runs the same
    plan gate the create handlers run, validates the inputs, and delegates to
    ``sites_service.edit_react_component`` — which resolves the edit and persists
    the one file as a reviewable DRAFT.

    It does NOT republish and does NOT enqueue a build, so unlike the svelte edit
    handler there is no ``SmokeGateFailed`` case to map: nothing is built, so
    nothing can fail a smoke gate, and there would be nothing to roll back if it
    could (a react publish enqueues its build and returns before any outcome
    exists). The success body says draft, and publishing stays the user's call.

    Returns ``{ok, status:"draft", is_live:false, pocket_id, component_path,
    created, message}``; sets ``is_error`` when identity is missing, the plan lacks
    Sites, the inputs are malformed, or the service rejects the edit (a reserved
    path, a wrong-engine pocket, a missing/colliding component, an ambiguous
    ``old_string``) — each relayed by code so the agent can fix and retry rather
    than report a change that did not happen.
    """
    workspace_id, user_id = _identity()
    if not workspace_id or not user_id:
        return _error_response(
            "edit_react_component requires workspace and user context (call from a "
            "cloud chat session)."
        )

    record_tool_call(
        workspace_id=workspace_id,
        user_id=user_id,
        tool_server="pocketpaw_sites",
        tool_name="_edit_react_component",
        status="ok",
        ok=True,
    )

    pocket_id = args.get("pocket_id")
    if not isinstance(pocket_id, str) or not pocket_id:
        return _error_response(
            "edit_react_component requires a `pocket_id` — the id of the react site "
            "pocket whose component you are editing."
        )
    component_path = args.get("component_path")
    if not isinstance(component_path, str) or not component_path:
        return _error_response(
            "edit_react_component requires a `component_path` — the relative path of "
            "the file to write (e.g. 'src/components/Hero.tsx'). It must already "
            "exist in the site's source map unless you pass `create=true`."
        )
    args = coerce_json_object_args(args, ("edits",))
    edits = args.get("edits")
    new_source = args.get("new_source")
    has_edits = edits is not None
    has_new_source = new_source is not None
    if has_edits == has_new_source:
        return _error_response(
            "edit_react_component requires exactly one of `edits` (a targeted "
            "search/replace diff — PREFERRED for small changes) or `new_source` "
            "(the FULL new file contents — for large rewrites and for `create`). "
            "Provide one, not both and not neither."
        )
    if has_new_source and not isinstance(new_source, str):
        return _error_response(
            "edit_react_component `new_source` must be the FULL new contents of the "
            "file as a string (the tool replaces the whole file, not a patch)."
        )
    if has_edits:
        # Validate the diff SHAPE here so a malformed payload gets a clear error
        # before the service runs. The MATCH-uniqueness contract belongs to the
        # service's apply_edits (relayed below as a ValidationError).
        if not isinstance(edits, list) or not edits:
            return _error_response(
                "edit_react_component `edits` must be a non-empty list of "
                "{old_string, new_string} blocks (like the built-in Edit tool)."
            )
        for i, block in enumerate(edits):
            if (
                not isinstance(block, dict)
                or not isinstance(block.get("old_string"), str)
                or not isinstance(block.get("new_string"), str)
            ):
                return _error_response(
                    f"edit_react_component `edits` block {i} must be an object with "
                    "string `old_string` and `new_string`."
                )
    create = bool(args.get("create"))
    if create and not has_new_source:
        return _error_response(
            "edit_react_component `create=true` needs `new_source` — the full "
            "contents of the new file. There is nothing for `edits` to search "
            "against in a file that does not exist yet."
        )

    # Plan gate (Sites = "sites"): an edit mutates a site pocket, so it is gated on
    # the same feature the create tools are. Without this a workspace that lost the
    # plan could keep editing a site its own /sites list 403s.
    if (gate := await _require_sites_plan_or_error(workspace_id)) is not None:
        return gate

    from pocketpaw_ee.cloud._core.errors import CloudError
    from pocketpaw_ee.sites import service as sites_service

    try:
        result = await sites_service.edit_react_component(
            user_id=user_id,
            pocket_id=pocket_id,
            component_path=component_path,
            new_source=new_source if has_new_source else None,
            edits=edits if has_edits else None,
            create=create,
        )
    except CloudError as exc:
        # ValidationError (reserved path / outside src+public / not a react site /
        # already exists / a bad diff) or NotFound (unknown pocket or component).
        # Relay the code + message so the agent knows WHICH guard rejected it.
        return _error_response(f"{exc.code}: {exc.message}")
    except Exception as exc:  # noqa: BLE001
        logger.warning("edit_react_component failed", exc_info=True)
        return _error_response(f"edit failed: {exc}")

    # The edit is a DRAFT. The chat agent narrates this payload, so — exactly like
    # the svelte edit tool — it must not contain a completed-state publish claim.
    return _success_response(
        {
            "ok": True,
            "status": "draft",
            "is_live": False,
            "pocket_id": result["pocket_id"],
            "component_path": result["component_path"],
            "created": result["created"],
            "message": (
                "Saved to the site's draft — it is NOT online yet, and no build was "
                "started. Tell the user the change is in the draft they can preview "
                "under /sites, and offer to publish it; only call the publish tool "
                "when they ask."
            ),
        }
    )


def make_edit_react_component_tool(tool: Any) -> Any:
    """Build the ``edit_react_component`` SDK tool object using the SDK's ``tool``
    decorator (passed in by the caller that already imported it).

    Registered on the SAME ``pocketpaw_sites_manager`` server as create + publish +
    ``edit_svelte_component`` (see ``make_create_landing_site_tool`` for why one
    server)."""

    @tool(
        "edit_react_component",
        (
            "Change an EXISTING react Paw Site. Use this WHENEVER the user asks to "
            "alter a react site that already exists — 'shorten the hero headline', "
            "'make the pricing cards darker', 'fix the typo in the FAQ', 'add a "
            "testimonials section'. NEVER call `create_react_site` again for a "
            "change: that mints a SECOND site pocket and leaves the one the user is "
            "looking at untouched. The change is saved to the site's DRAFT — it is "
            "NOT published, nothing is built, and nothing goes live.\n"
            "Give the change ONE of two ways (exactly one, not both):\n"
            "  * `edits` — PREFER THIS for small/targeted changes. A list of "
            "search/replace blocks [{old_string, new_string}, ...], exactly like "
            "the built-in Edit tool: each `old_string` is copied VERBATIM from the "
            "current file and must match EXACTLY ONCE (include enough surrounding "
            "context to be unique), and `new_string` is what it becomes. You send "
            "ONLY the change, not the whole file — far fewer tokens and faster. If "
            "you have not read the file this turn, read it first so old_string "
            "matches.\n"
            "  * `new_source` — the FULL new file contents as a string (REPLACES "
            "the whole file). For large rewrites, and the only form `create` "
            "accepts.\n"
            "To ADD a section: call this twice — once with `create=true` and "
            "`new_source` for the new `src/components/<Name>.tsx`, then once with "
            "`edits` on `src/App.tsx` to import and render it. `create=true` "
            "REQUIRES the path to be new; editing an existing file with it is "
            "rejected so you cannot overwrite a component by accident. Without "
            "`create` the path must already exist, so a typo is an error and never "
            "a stray new file.\n"
            "You may only write under `src/` (outside `src/paw/`) and `public/`. "
            "`index.html`, `package.json`, `vite.config.ts`, `paw-prerender.mjs` "
            "and `src/paw/` are GENERATED and rejected — they carry the prerender "
            "contract and the dependency list, and there is no way to add a "
            "dependency. PRERENDER RULE (same as create_react_site): every "
            "component must render its resting/final state in its RETURNED MARKUP, "
            "because `useEffect` does not run at prerender time.\n"
            "Args: `pocket_id` (the react site pocket), `component_path`, and one "
            "of `edits` / `new_source`, plus optional `create`. Returns {ok, "
            "status:'draft', is_live:false, pocket_id, component_path, created, "
            "message}. Relay the `message`: the change is in the DRAFT the user can "
            "preview under /sites, and publishing is their call — do NOT tell them "
            "it is live. ok=false with an error means NOTHING was saved: an "
            "old_string that matched 0 or >1 times means make it more specific and "
            "retry; a reserved-path, wrong-engine, already-exists or not-found "
            "error means relay the reason. Do NOT report a successful edit when "
            "ok=false."
        ),
        {
            "type": "object",
            "properties": {
                "pocket_id": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Id of the react site pocket to edit.",
                },
                "component_path": {
                    "type": "string",
                    "minLength": 1,
                    "description": (
                        "Relative path of the file to write (e.g. "
                        "'src/components/Hero.tsx'). Must already exist in the "
                        "site's source map unless `create` is true. Only `src/` "
                        "(outside `src/paw/`) and `public/` may be written."
                    ),
                },
                "edits": {
                    "type": "array",
                    "description": (
                        "PREFERRED for small changes: a list of search/replace "
                        "blocks applied to the file's CURRENT contents. Each "
                        "`old_string` must match exactly once. Send this INSTEAD of "
                        "`new_source` so you emit only the diff. Not valid with "
                        "`create`."
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
                        "whole file — not a diff). Use for large rewrites, and "
                        "always with `create`."
                    ),
                },
                "create": {
                    "type": "boolean",
                    "description": (
                        "Create a NEW file at `component_path` instead of editing an "
                        "existing one. Requires `new_source`, and the path must NOT "
                        "already exist. Use it to add a section, then edit "
                        "`src/App.tsx` to render it."
                    ),
                },
            },
            "required": ["pocket_id", "component_path"],
            "additionalProperties": False,
        },
    )
    async def edit_react_component(args):  # type: ignore[no-untyped-def]
        return await _edit_react_component_handler(args)

    return edit_react_component


async def _edit_html_file_handler(args: dict) -> dict:
    """MCP handler for ``sites_manager__edit_html_file`` (HE-10).

    Reads workspace/user identity from the per-stream ContextVars, runs the same
    plan gate the create handlers run, validates the inputs, and delegates to
    ``sites_service.edit_html_file`` — which resolves the edit and persists the one
    file as a reviewable DRAFT.

    Mirrors ``_edit_react_component_handler`` almost line for line, and the places
    it does NOT are the html-specific ones: the argument is ``file_path`` (an html
    site has files, not components), and the path guidance names the ``_paw/``
    namespace rather than react's build shell.

    Like the react handler it does NOT republish, so there is no ``SmokeGateFailed``
    case to map. The reason differs and is worth keeping straight: react cannot roll
    back because its build is async, whereas html has no build to gate on AT ALL —
    which means a republish here would push unvalidated markup straight to a live
    site. Draft-only is the safer contract, not merely the convenient one.

    Returns ``{ok, status:"draft", is_live:false, pocket_id, file_path, created,
    message}``; sets ``is_error`` when identity is missing, the plan lacks Sites,
    the inputs are malformed, or the service rejects the edit (a reserved or
    escaping path, a wrong-engine pocket, a missing/colliding file, an ambiguous
    ``old_string``) — each relayed by code so the agent can fix and retry rather
    than report a change that did not happen.
    """
    workspace_id, user_id = _identity()
    if not workspace_id or not user_id:
        return _error_response(
            "edit_html_file requires workspace and user context (call from a cloud chat session)."
        )

    record_tool_call(
        workspace_id=workspace_id,
        user_id=user_id,
        tool_server="pocketpaw_sites",
        tool_name="_edit_html_file",
        status="ok",
        ok=True,
    )

    pocket_id = args.get("pocket_id")
    if not isinstance(pocket_id, str) or not pocket_id:
        return _error_response(
            "edit_html_file requires a `pocket_id` — the id of the html site "
            "pocket whose file you are editing."
        )
    file_path = args.get("file_path")
    if not isinstance(file_path, str) or not file_path:
        return _error_response(
            "edit_html_file requires a `file_path` — the relative path of the file "
            "to write (e.g. 'index.html' or 'css/site.css'). It must already exist "
            "in the site's source map unless you pass `create=true`."
        )
    args = coerce_json_object_args(args, ("edits",))
    edits = args.get("edits")
    new_source = args.get("new_source")
    has_edits = edits is not None
    has_new_source = new_source is not None
    if has_edits == has_new_source:
        return _error_response(
            "edit_html_file requires exactly one of `edits` (a targeted "
            "search/replace diff — PREFERRED for small changes) or `new_source` "
            "(the FULL new file contents — for large rewrites and for `create`). "
            "Provide one, not both and not neither."
        )
    if has_new_source and not isinstance(new_source, str):
        return _error_response(
            "edit_html_file `new_source` must be the FULL new contents of the file "
            "as a string (the tool replaces the whole file, not a patch)."
        )
    if has_edits:
        # Validate the diff SHAPE here so a malformed payload gets a clear error
        # before the service runs. The MATCH-uniqueness contract belongs to the
        # service's apply_edits (relayed below as a ValidationError).
        if not isinstance(edits, list) or not edits:
            return _error_response(
                "edit_html_file `edits` must be a non-empty list of "
                "{old_string, new_string} blocks (like the built-in Edit tool)."
            )
        for i, block in enumerate(edits):
            if (
                not isinstance(block, dict)
                or not isinstance(block.get("old_string"), str)
                or not isinstance(block.get("new_string"), str)
            ):
                return _error_response(
                    f"edit_html_file `edits` block {i} must be an object with "
                    "string `old_string` and `new_string`."
                )
    create = bool(args.get("create"))
    if create and not has_new_source:
        return _error_response(
            "edit_html_file `create=true` needs `new_source` — the full contents of "
            "the new file. There is nothing for `edits` to search against in a file "
            "that does not exist yet."
        )

    # Plan gate (Sites = "sites"): an edit mutates a site pocket, so it is gated on
    # the same feature the create tools are. Without this a workspace that lost the
    # plan could keep editing a site its own /sites list 403s.
    if (gate := await _require_sites_plan_or_error(workspace_id)) is not None:
        return gate

    from pocketpaw_ee.cloud._core.errors import CloudError
    from pocketpaw_ee.sites import service as sites_service

    try:
        result = await sites_service.edit_html_file(
            user_id=user_id,
            pocket_id=pocket_id,
            file_path=file_path,
            new_source=new_source if has_new_source else None,
            edits=edits if has_edits else None,
            create=create,
        )
    except CloudError as exc:
        # ValidationError (reserved `_paw/` path / escapes the site dir / not an
        # html site / already exists / a bad diff) or NotFound (unknown pocket or
        # file). Relay the code + message so the agent knows WHICH guard rejected it.
        return _error_response(f"{exc.code}: {exc.message}")
    except Exception as exc:  # noqa: BLE001
        logger.warning("edit_html_file failed", exc_info=True)
        return _error_response(f"edit failed: {exc}")

    # The edit is a DRAFT. The chat agent narrates this payload, so — exactly like
    # both sibling edit tools — it must not contain a completed-state publish claim.
    return _success_response(
        {
            "ok": True,
            "status": "draft",
            "is_live": False,
            "pocket_id": result["pocket_id"],
            "file_path": result["file_path"],
            "created": result["created"],
            "message": (
                "Saved to the site's draft — it is NOT online yet. Tell the user the "
                "change is in the draft they can preview under /sites, and offer to "
                "publish it; only call the publish tool when they ask."
            ),
        }
    )


def make_edit_html_file_tool(tool: Any) -> Any:
    """Build the ``edit_html_file`` SDK tool object using the SDK's ``tool``
    decorator (passed in by the caller that already imported it).

    Registered on the SAME ``pocketpaw_sites_manager`` server as create + publish +
    the two sibling edit tools (see ``make_create_landing_site_tool`` for why one
    server)."""

    @tool(
        "edit_html_file",
        (
            "Change an EXISTING html Paw Site (a raw HTML/CSS/JS site — the kind "
            "`create_html_site` makes, and the kind a site imported from a URL is). "
            "Use this WHENEVER the user asks to alter an html site that already "
            "exists — 'change the phone number in the footer', 'shorten the hero "
            "headline', 'fix the typo on the about page', 'add a contact page'. "
            "NEVER call `create_html_site` again for a change: that mints a SECOND "
            "site pocket at a SECOND url and leaves the one the user is looking at "
            "untouched. The change is saved to the site's DRAFT — it is NOT "
            "published and nothing goes live.\n"
            "Give the change ONE of two ways (exactly one, not both):\n"
            "  * `edits` — PREFER THIS, and prefer it harder here than on the other "
            "tracks. A list of search/replace blocks [{old_string, new_string}, "
            "...], exactly like the built-in Edit tool: each `old_string` is copied "
            "VERBATIM from the current file and must match EXACTLY ONCE (include "
            "enough surrounding context to be unique), and `new_string` is what it "
            "becomes. An html page is ONE flat document with no components, so a "
            "full rewrite means re-emitting the entire page to change a phone "
            "number. If you have not read the file this turn, read it first so "
            "old_string matches.\n"
            "  * `new_source` — the FULL new file contents as a string (REPLACES "
            "the whole file). For large rewrites, and the only form `create` "
            "accepts.\n"
            "To ADD a page: call this twice — once with `create=true` and "
            "`new_source` for the new file (e.g. `about.html`), then once with "
            "`edits` on `index.html` to link to it. `create=true` REQUIRES the path "
            "to be new; editing an existing file with it is rejected so you cannot "
            "overwrite a page by accident. Without `create` the path must already "
            "exist, so a typo is an error and never a stray new file.\n"
            "PATHS: an html site's files are project-relative and MOST LIVE AT THE "
            "ROOT — `index.html` is the page the edge serves, alongside things like "
            "`styles.css`, `about.html`, `img/logo.svg`. This is NOT like the react "
            "track: do NOT prefix paths with `src/`. The ONLY forbidden paths are "
            "the generated `_paw/` namespace and anything that climbs out of the "
            "site with `..`.\n"
            "KEEP THE FORM PLUMBING. If the file contains a `<form>` posting to a "
            "`/capture/form` endpoint, leave its `action` and its hidden "
            "`paw_site_id` / `paw_key` / `paw_redirect` inputs EXACTLY as they are "
            "unless the user asks to change the form itself — they are what makes "
            "submissions arrive as leads, and a rewrite that drops them silently "
            "sends every future enquiry nowhere.\n"
            "Args: `pocket_id` (the html site pocket), `file_path`, and one of "
            "`edits` / `new_source`, plus optional `create`. Returns {ok, "
            "status:'draft', is_live:false, pocket_id, file_path, created, "
            "message}. Relay the `message`: the change is in the DRAFT the user can "
            "preview under /sites, and publishing is their call — do NOT tell them "
            "it is live. ok=false with an error means NOTHING was saved: an "
            "old_string that matched 0 or >1 times means make it more specific and "
            "retry; a reserved-path, wrong-engine, already-exists or not-found "
            "error means relay the reason. Do NOT report a successful edit when "
            "ok=false."
        ),
        {
            "type": "object",
            "properties": {
                "pocket_id": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Id of the html site pocket to edit.",
                },
                "file_path": {
                    "type": "string",
                    "minLength": 1,
                    "description": (
                        "Relative path of the file to write (e.g. 'index.html', "
                        "'styles.css', 'about.html'). Files usually sit at the site "
                        "ROOT — do NOT prefix with 'src/'. Must already exist in the "
                        "site's source map unless `create` is true. The generated "
                        "`_paw/` namespace is not writable."
                    ),
                },
                "edits": {
                    "type": "array",
                    "description": (
                        "PREFERRED for small changes: a list of search/replace "
                        "blocks applied to the file's CURRENT contents. Each "
                        "`old_string` must match exactly once. Send this INSTEAD of "
                        "`new_source` so you emit only the diff. Not valid with "
                        "`create`."
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
                        "whole file — not a diff). Use for large rewrites, and "
                        "always with `create`."
                    ),
                },
                "create": {
                    "type": "boolean",
                    "description": (
                        "Create a NEW file at `file_path` instead of editing an "
                        "existing one. Requires `new_source`, and the path must NOT "
                        "already exist. Use it to add a page, then edit "
                        "`index.html` to link to it."
                    ),
                },
            },
            "required": ["pocket_id", "file_path"],
            "additionalProperties": False,
        },
    )
    async def edit_html_file(args):  # type: ignore[no-untyped-def]
        return await _edit_html_file_handler(args)

    return edit_html_file


__all__ = [
    "CREATE_DYNAMIC_SITE_TOOL_ID",
    "CREATE_HTML_SITE_TOOL_ID",
    "CREATE_LANDING_SITE_TOOL_ID",
    "CREATE_REACT_SITE_TOOL_ID",
    "CREATE_SVELTE_SITE_TOOL_ID",
    "EDIT_HTML_FILE_TOOL_ID",
    "EDIT_REACT_COMPONENT_TOOL_ID",
    "EDIT_SVELTE_COMPONENT_TOOL_ID",
    "HTML_REQUIRED_KEYS",
    "REACT_REQUIRED_KEYS",
    "REACT_RESERVED_FILES",
    "REACT_RESERVED_PREFIX",
    "SERVER_NAME",
    "SITES_CREATE_TOOL_IDS",
    "SVELTE_REQUIRED_EXACT_KEYS",
    "SVELTE_REQUIRED_PREFIXES",
    "make_create_dynamic_site_tool",
    "make_create_html_site_tool",
    "make_create_landing_site_tool",
    "make_create_react_site_tool",
    "make_create_svelte_site_tool",
    "make_edit_html_file_tool",
    "make_edit_react_component_tool",
    "make_edit_svelte_component_tool",
]
