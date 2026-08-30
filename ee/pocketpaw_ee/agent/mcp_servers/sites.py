# sites.py — in-process MCP server exposing the Paw Sites publish action to
# agent backends (claude_agent_sdk). Created: 2026-06-01 (Phase 4 — chat→
# create-site). Mirrors the layout of the sibling mcp_servers (tasks.py /
# pockets.py): a single ``create_sdk_mcp_server`` with an SDK import-guard, the
# ``SERVER_NAME`` / ``*_TOOL_ID`` allowlist constants, and ContextVar-sourced
# identity (the same ``current_workspace_id`` / ``current_user_id`` accessors in
# ``ee.cloud.chat.agent_service`` the pocket specialist + tasks servers read).
# Tool ids namespace as ``mcp__pocketpaw_sites_manager__<tool>`` so the Claude
# Code allowlist machinery matches them. The create tools (create_landing_site /
# create_svelte_site / create_dynamic_site) register on this SAME server object
# via sites_create.py — a second create_sdk_mcp_server under this name would
# clobber it (claude_sdk keys servers by name).
#
# Updated 2026-06-17 (feat/sites-svelte-component-edit, SE-2): the
# ``edit_svelte_component`` tool also registers on this SAME server (built via
# the factory in sites_create.py). It rewrites ONE file of a published svelte
# site's source map and republishes — so the create → publish → edit hops sit on
# one allowlisted server. Its id rides ``SITES_TOOL_IDS``, so the per-surface
# allowlist (extensions.py + surface/service.py) picks it up automatically.
#
# Updated 2026-06-14 (feat/dynamic-sites-authoring, RFC 12 A2): the
# ``create_dynamic_site`` tool also registers on this SAME server. Dynamic sites
# are ripple-engine sites whose spec carries live-data bindings; publish carries
# those through to the paw-sites generator.
#
# Updated 2026-07-12 (feat/sites-html-create-tool, HE-6): the ``create_html_site``
# tool also registers on this SAME server. An html site is a raw {path: contents}
# HTML/CSS/JS map with no framework; publish materializes it and skips the Node
# build. Its id rides ``SITES_TOOL_IDS`` so the per-surface allowlist picks it up
# automatically. Opt-in — the default marketing brain stays create_landing_site
# (ripple); the default flip to html is HE-12.
#
# Updated 2026-08-11 (RX-4 — the publish response tells the agent whether the site is
# actually live): ``_publish_handler``'s success body hand-built five keys (id /
# pocket_id / name / url / deployed) while ``_to_response`` — the wire the FRONTEND
# polls — also carries ``build_status`` / ``build_reason`` / ``build_job_id``. The
# agent got none of the three, and react is the one engine where that breaks the happy
# path, because it is the only engine with ``build_runs_async(engine) is True``
# unconditionally. Updated 2026-08-21 (SL-4): STATIC svelte answers True as well once
# ``PAW_SITES_SVELTE_ASYNC_BUILD`` is on, so everything below now describes two engines
# rather than one. Nothing here needed changing for that — the keys ride the response for
# whatever the gate flips, which is why they were added to the response and not to a
# react branch.
#
#   * FIRST publish — ``_enqueue_static_build`` creates the Site doc with ``url=""``
#     and ``deployed=False``, honestly (nothing is serving yet; the worker flips both
#     on success). Meanwhile ``pocketpaw-create-react-site`` STEP 4 tells the agent to
#     "show the user the returned url". So it showed an empty string, or invented one.
#   * RE-publish — ``url`` / ``deployed`` deliberately KEEP the previous deploy's
#     values so a rebuild never reports a working site as down. Right for the
#     frontend, which reads ``build_status`` beside them; for the agent it meant
#     reporting the OLD url as though the edit were already live.
#
# So the body now carries the three raw fields VERBATIM (never normalised — see
# ``_to_response``), plus ``build_in_progress`` and ``is_live``, plus a ``message``
# stating the conclusion in prose. The derivation lives in
# ``sites.service.build_wire_state`` and is SHARED with the new status tool, because
# the two surfaces disagreeing about whether a site is live would be worse than either
# being wrong alone. A boolean is not enough on its own here: the original defect was
# narration, not data, so the message exists to be relayed.
#
# Also added the READ-ONLY ``get_site_build_status`` tool. Without it the queued state
# is a dead end — an async publish returns before the build starts, so the agent could
# learn a build was enqueued and never find out how it ended. It rides
# ``SITES_TOOL_IDS`` like every other tool here; the /sites allow-list is a hard
# whitelist that filters an absent id out silently.
#
# Updated 2026-08-19 (fix/sites-read-source-tool — the edit lane could write but not
# read): added an ELEVENTH tool, the READ-ONLY ``read_site_source``. The surface could
# WRITE a site's source three ways and READ it zero ways, which is not a missing
# convenience but a hole the three edit tools fall through. Each of them PREFERS its
# ``edits`` (search/replace) form, whose ``old_string`` must be copied VERBATIM from the
# current file and match exactly once, and each description duly said "read it first" —
# naming no tool, because none existed. ``get_pocket`` does carry ``source``, but it
# lives on the ``pockets`` server and ``sites_allow`` is a hard whitelist
# (SITES|STOCK|ICON|PALETTE|DESIGN_SYSTEM|ASK), so on /sites it is filtered out with no
# error; the profile separately drops the file/shell built-ins on the stated assumption
# that "the source map is a tool ARGUMENT", which holds only while the agent still has
# the source it just authored in context. So the only REACHABLE edit form was a
# whole-file ``new_source`` composed from memory — precisely the shape the edit
# descriptions warn about, which silently drops a ``<form>``'s ``action`` and its hidden
# ``paw_site_id`` / ``paw_key`` / ``paw_redirect`` inputs and sends every future enquiry
# nowhere. Two modes keep the fix from causing the problem it prevents: no ``file_path``
# returns a manifest of paths + byte sizes (a react source map inlined whole would
# swallow the context the edit needs), and a ``file_path`` returns that one file
# verbatim. Engine-agnostic, unlike the edit tools — a read is safe everywhere and the
# agent often does not know the engine until it looks.
#
# Updated 2026-08-11 (RX-3 — the react track gets an EDIT lane): the
# ``edit_react_component`` tool also registers on this SAME server (built via the
# factory in sites_create.py), so create → publish → edit sit together for react
# exactly as they do for svelte. Before it, ``edit_svelte_component`` was the ONLY
# edit tool on the server: a react site could be created and published but never
# changed, so the agent answered "shorten the hero headline" by calling
# ``create_react_site`` again and minting a SECOND site pocket. It writes ONE file
# of the pocket's react source map as a reviewable DRAFT — no republish, no build
# enqueued (a react publish is async, so there is no synchronous outcome to gate
# on). Its id rides ``SITES_TOOL_IDS``, so the per-surface allowlist picks it up.
#
# Updated 2026-08-13 (HE-10 — the html track gets an EDIT lane): the
# ``edit_html_file`` tool also registers on this SAME server, completing the set —
# every engine that can be CREATED from chat can now be CHANGED from chat. It had
# the same hole RX-3 closed for react, one engine over: ``edit_svelte_component``
# raises ``pocket.not_svelte_site`` on an html pocket and ``edit_react_component``
# raises ``pocket.not_react_site``, so no tool on this server would accept "change
# the phone number in the footer" and the agent's only move was a second
# ``create_html_site`` — a second pocket at a second url, leaving the site the user
# was looking at untouched. It writes ONE file of the pocket's html source map as a
# reviewable DRAFT and does NOT republish; html runs no build and therefore has no
# smoke gate, so a republish here would push unvalidated markup straight to a live
# site with nothing in between. Named ``edit_html_file`` rather than
# ``edit_html_component`` because an html site genuinely has no component model —
# its source map is the raw {path: contents} tree the edge serves verbatim.
#
# Updated 2026-08-07 (RX-2 — the agent can select the react engine): the
# ``create_react_site`` tool also registers on this SAME server. A react site is a
# {path: contents} map of hand-written React files; publish runs a Vite SSG build
# that prerenders it to a static ``dist/`` and deploys it assets-only. Its id
# rides ``SITES_TOOL_IDS``, so the per-surface allowlist picks it up automatically
# — which is what makes the /sites react-create surface able to CALL the tool its
# preamble names. Opt-in: the description steers the agent here only on an
# explicit React request or a genuine interactivity need.
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

from ._audit import record_tool_call

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
# The targeted svelte-component edit tool — also registers on this SAME server
# (see sites_create.py).
EDIT_SVELTE_COMPONENT_TOOL_ID = f"mcp__{SERVER_NAME}__edit_svelte_component"
# The dynamic-track create tool (RFC 12 A2) also registers on this SAME server
# (see sites_create.py). Dynamic sites are ripple-engine sites whose spec carries
# live-data bindings; publish carries those through to the paw-sites generator.
CREATE_DYNAMIC_SITE_TOOL_ID = f"mcp__{SERVER_NAME}__create_dynamic_site"
# The html-track create tool (HE-6) also registers on this SAME server (see
# sites_create.py). An html site is a raw {path: contents} HTML/CSS/JS map with no
# framework; publish materializes it and skips the Node build.
CREATE_HTML_SITE_TOOL_ID = f"mcp__{SERVER_NAME}__create_html_site"
# The react-track create tool (RX-2) also registers on this SAME server (see
# sites_create.py). A react site is a {path: contents} map of hand-written React
# files; publish runs a Vite SSG build that prerenders it to a static dist/.
CREATE_REACT_SITE_TOOL_ID = f"mcp__{SERVER_NAME}__create_react_site"
# The targeted react-component edit tool (RX-3) — also registers on this SAME
# server (see sites_create.py). It writes ONE file of a react site's source map as a
# reviewable DRAFT; it does NOT publish and does NOT enqueue a build.
EDIT_REACT_COMPONENT_TOOL_ID = f"mcp__{SERVER_NAME}__edit_react_component"
# The targeted html-file edit tool (HE-10) — also registers on this SAME server
# (see sites_create.py). It writes ONE file of an html site's source map as a
# reviewable DRAFT; it does NOT publish. Named for a FILE rather than a component
# because an html site has no component model — its source map is the raw
# {path: contents} tree the edge serves.
EDIT_HTML_FILE_TOOL_ID = f"mcp__{SERVER_NAME}__edit_html_file"
# The READ-ONLY build-status tool (RX-4). It exists because react publishes are
# ASYNC: ``publish`` returns before the build starts, so without a way to ask again
# on a later turn the agent learns a build was queued and can never discover it
# finished. Must ride SITES_TOOL_IDS like the rest — the /sites allow-list is a hard
# whitelist and filters an absent id out with no error.
GET_SITE_BUILD_STATUS_TOOL_ID = f"mcp__{SERVER_NAME}__get_site_build_status"
# The READ-ONLY source tool — also registers on this SAME server (see
# sites_create.py). It closes the hole the three edit tools were written over:
# each PREFERS an `edits` diff whose ``old_string`` must be copied verbatim from
# the current file, and each says "read it first", but /sites had no reader. The
# ``pockets`` server's ``get_pocket`` does carry ``source`` and is filtered out by
# the hard allowlist; the file/shell built-ins are dropped by the profile on the
# assumption that "the source map is a tool ARGUMENT" — which holds only while the
# agent still has the source it just authored in context. Must ride SITES_TOOL_IDS
# like the rest: an absent id is filtered out with no error at all.
READ_SITE_SOURCE_TOOL_ID = f"mcp__{SERVER_NAME}__read_site_source"

# The OWNER'S OWN IMAGES (feat/sites-public-asset-uploads). Without this the agent
# cannot use a logo or photo the user uploaded: nothing in the authoring context
# mentions them, so it either invents a path that 404s or falls back to a stock
# photo of somebody else's product. Read-only, and its id rides ``SITES_TOOL_IDS``
# so the hard /sites allow-list picks it up — an id absent there is filtered out
# and the tool is silently unreachable.
LIST_SITE_ASSETS_TOOL_ID = f"mcp__{SERVER_NAME}__list_site_assets"

SITES_TOOL_IDS = (
    PUBLISH_TOOL_ID,
    LIST_SITE_ASSETS_TOOL_ID,
    CREATE_LANDING_SITE_TOOL_ID,
    CREATE_SVELTE_SITE_TOOL_ID,
    EDIT_SVELTE_COMPONENT_TOOL_ID,
    CREATE_DYNAMIC_SITE_TOOL_ID,
    CREATE_HTML_SITE_TOOL_ID,
    CREATE_REACT_SITE_TOOL_ID,
    EDIT_REACT_COMPONENT_TOOL_ID,
    EDIT_HTML_FILE_TOOL_ID,
    GET_SITE_BUILD_STATUS_TOOL_ID,
    READ_SITE_SOURCE_TOOL_ID,
)


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

    record_tool_call(
        workspace_id=workspace_id,
        user_id=user_id,
        tool_server="pocketpaw_sites_manager",
        tool_name="_publish",
        status="ok",
        ok=True,
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

    # RX-4 — the build lane's state rides the response. Without it this body was five
    # keys (id / pocket_id / name / url / deployed), and on the react happy path that
    # is actively misleading in two different ways, because react is the only engine
    # with ``build_runs_async(engine) is True`` unconditionally — and since SL-4 a STATIC
    # svelte publish takes the same path whenever the lane is switched on, so read every
    # "react" below as "any engine the gate flips":
    #
    #   * FIRST publish — ``_enqueue_static_build`` creates the Site doc with
    #     ``url=""`` and ``deployed=False``, honestly, because nothing is serving yet.
    #     The agent was handed an empty string while ``pocketpaw-create-react-site``
    #     STEP 4 tells it to "show the user the returned url", so it showed nothing or
    #     invented something.
    #   * RE-publish — ``url`` and ``deployed`` deliberately keep the PREVIOUS
    #     deploy's values so a rebuild never reports a working site as down. Correct
    #     for the frontend, which reads ``build_status`` beside them. For an agent
    #     that could not see ``build_status``, it meant reporting the OLD url as
    #     though the change were already live.
    #
    # ``build_wire_state`` owns the derivation (shared with get_site_build_status, so
    # the two can never disagree) and passes ``build_status`` through VERBATIM.
    # ``is_live`` is the field to gate "show the user this url" on; ``message`` says
    # the same thing in prose, because a boolean the agent does not look at is not a
    # fix — the prompt-level failure here was narration, not data.
    state = sites_service.build_wire_state(doc)
    if state["is_live"]:
        message = "The site is live. Show the user the `url`."
    elif state["build_in_progress"]:
        message = (
            "The build is not finished, so the site is NOT live yet and `url` is "
            "either empty or still serving the PREVIOUS version. Do NOT show the "
            "user a url and do NOT say it is live. Tell them the build is running "
            "and that you can check again in a moment with "
            "`get_site_build_status`."
        )
    elif state["build_status"] == "failed":
        message = (
            "The build FAILED, so the site is not live. Relay `build_reason` to the "
            "user rather than a url, and offer to fix the page and publish again."
        )
    else:
        message = (
            "The publish was accepted but nothing is serving yet. Do NOT show a url "
            "or say the site is live; check `get_site_build_status` before "
            "reporting one."
        )
    return _success_response(
        {
            "ok": True,
            "message": message,
            "site": {
                "id": str(doc.id),
                "pocket_id": doc.pocket_id,
                "name": doc.name,
                **state,
            },
        }
    )


async def _get_site_build_status_handler(args: dict) -> dict:
    """MCP handler for ``sites_manager__get_site_build_status`` (RX-4).

    READ-ONLY. The answer to "is it up yet?" on a turn after the publish, and the
    reason the queued state a react publish returns is not a dead end: with
    ``build_runs_async("react") is True`` the publish call returns before the build
    starts, so without this the agent could learn a build was enqueued and then never
    discover it finished.

    Delegates to ``sites_service.site_build_status``, which resolves the canonical
    Site doc tenant-scoped on the workspace and derives the same fields the widened
    publish response carries (one shared derivation, so the two cannot disagree).
    A pocket that was never published reports ``published=False`` rather than an
    error — from the agent's side that is the useful answer.
    """
    workspace_id, user_id = _identity()
    if not workspace_id or not user_id:
        return _error_response(
            "get_site_build_status requires workspace and user context (call from a "
            "cloud chat session)."
        )

    record_tool_call(
        workspace_id=workspace_id,
        user_id=user_id,
        tool_server="pocketpaw_sites_manager",
        tool_name="_get_site_build_status",
        status="ok",
        ok=True,
    )

    pocket_id = args.get("pocket_id")
    if not isinstance(pocket_id, str) or not pocket_id:
        return _error_response(
            "get_site_build_status requires a `pocket_id` — the id of the site "
            "pocket whose build you are checking."
        )

    from pocketpaw_ee.cloud._core.errors import CloudError
    from pocketpaw_ee.sites import service as sites_service

    try:
        state = await sites_service.site_build_status(
            workspace_id=workspace_id, pocket_id=pocket_id
        )
    except CloudError as exc:
        return _error_response(f"{exc.code}: {exc.message}")
    except Exception as exc:  # noqa: BLE001
        logger.warning("get_site_build_status failed", exc_info=True)
        return _error_response(f"could not read the build status: {exc}")

    if not state["published"]:
        message = (
            "This site has never been published, so there is no build and nothing "
            "is live. Offer to publish it."
        )
    elif state["is_live"]:
        message = "The build finished and the site is live. Show the user the `url`."
    elif state["build_in_progress"]:
        message = (
            "The build is still running, so the site is NOT live at this version "
            "yet. Do NOT show a url as if it were current. Tell the user it is "
            "still building and offer to check again."
        )
    elif state["build_status"] == "failed":
        message = (
            "The build FAILED. Relay `build_reason` to the user, not a url, and "
            "offer to fix the page and publish again."
        )
    else:
        message = (
            "There is no build in flight and the site is not serving a current "
            "deploy. Do NOT report it as live."
        )
    return _success_response({"ok": True, "message": message, **state})


async def _list_site_assets_handler(args: dict) -> dict:
    """MCP handler for ``sites_manager__list_site_assets``.

    Returns the images the site's owner uploaded, each with a durable public URL
    the agent can put straight into markup. Workspace comes from the per-stream
    ContextVars, never from the args — otherwise a prompt-injected pocket id
    could read another tenant's assets.
    """
    from pocketpaw_ee.sites.public_assets import public_asset_store

    pocket_id = str(args.get("pocket_id") or "").strip()
    if not pocket_id:
        return _error_response("pocket_id is required.")

    workspace_id, _user_id = _identity()
    if not workspace_id:
        return _error_response("No active workspace — cannot read this site's assets.")

    store = public_asset_store()
    if store is None:
        # A deployment fact, not a failure of this call. Say so plainly so the
        # agent stops looking for uploads instead of inventing a URL.
        return _success_response(
            {
                "ok": True,
                "count": 0,
                "assets": [],
                "message": (
                    "Public asset storage is not configured on this deployment, so this "
                    "site has no uploaded images. Use stock photography instead."
                ),
            }
        )

    try:
        assets = await store.list(workspace_id=workspace_id, pocket_id=pocket_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("sites.list_site_assets failed for pocket %s", pocket_id, exc_info=True)
        return _error_response(f"Could not read this site's assets: {exc}")

    return _success_response(
        {
            "ok": True,
            "count": len(assets),
            "assets": [
                {
                    "url": a.url,
                    "filename": a.filename,
                    "mime": a.mime,
                    "size": a.size,
                }
                for a in assets
            ],
            "message": (
                f"{len(assets)} image(s) uploaded by the site owner."
                if assets
                else "The owner has not uploaded any images for this site yet."
            ),
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

    @tool(
        "get_site_build_status",
        (
            "Check whether a published Paw Site's build has finished and whether the "
            "site is LIVE. READ-ONLY — it changes nothing. Call this when the user "
            "asks 'is it up yet?', 'did it deploy?', 'is my site live?', or when a "
            "previous `publish` came back with `build_in_progress: true` and you now "
            "need to know the outcome. This is the ONLY way to find out: a react "
            "site's build runs asynchronously, so `publish` returns BEFORE the build "
            "starts and its response can never tell you how the build ended.\n"
            "Args: `pocket_id` (required — the site pocket). Returns {ok, message, "
            "pocket_id, site_id, name, published, url, deployed, build_status, "
            "build_reason, build_job_id, build_in_progress, is_live}.\n"
            "HOW TO READ IT: gate everything on `is_live`. Show the user the `url` "
            "ONLY when `is_live` is true. When `build_in_progress` is true the build "
            "is still running and `url` is either empty or still serving the "
            "PREVIOUS version — say it is still building, do NOT show a url, and do "
            "NOT say it is live. When `build_status` is 'failed', relay "
            "`build_reason` and offer to fix the page and publish again. When "
            "`published` is false the site has never been published at all. Relay "
            "the `message` — it already states the correct conclusion. Never invent "
            "a url, and never report a site as live off `deployed` or a non-empty "
            "`url` alone: a rebuild deliberately keeps the previous deploy's values "
            "so a working site is not reported as down mid-build."
        ),
        {
            "type": "object",
            "properties": {
                "pocket_id": {
                    "type": "string",
                    "minLength": 1,
                    "description": ("Id of the site pocket whose build status you are checking."),
                },
            },
            "required": ["pocket_id"],
            "additionalProperties": False,
        },
    )
    async def get_site_build_status(args):  # type: ignore[no-untyped-def]
        return await _get_site_build_status_handler(args)

    @tool(
        "list_site_assets",
        (
            "List the images the SITE OWNER uploaded for this site, with durable "
            "public URLs you can embed directly. READ-ONLY. Call this BEFORE "
            "choosing imagery for a site you are building or editing — the owner's "
            "own logo, product shots and team photos live here, and they beat stock "
            "photography every time. Also call it when the user says 'use my logo', "
            "'the image I uploaded', 'the photo I added', or names a file.\n"
            "Args: `pocket_id` (required — the site pocket). Returns {ok, count, "
            "assets:[{url, filename, mime, size}], message}.\n"
            "HOW TO READ IT: `url` is absolute, permanent and public — put it in "
            "`src` verbatim, do NOT rewrite it, do NOT copy the file into the "
            "source map, and do NOT prefix it with a path. An empty `assets` means "
            "the owner uploaded nothing; fall back to `search_stock_images` and "
            "NEVER invent an asset URL or guess a filename."
        ),
        {
            "type": "object",
            "properties": {
                "pocket_id": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Id of the site pocket whose uploaded images you want.",
                },
            },
            "required": ["pocket_id"],
            "additionalProperties": False,
        },
    )
    async def list_site_assets(args):  # type: ignore[no-untyped-def]
        return await _list_site_assets_handler(args)

    # Register the deterministic landing-site create tool on this SAME server.
    # The SKILL flow is: produce the `content` copy → create_landing_site →
    # publish, so the two hops sit on one allowlisted server. Built here (not as a
    # separate create_sdk_mcp_server) because the claude_sdk registration loop
    # keys servers by name and a second server under this name would clobber it.
    from pocketpaw_ee.agent.mcp_servers.sites_create import (
        make_create_dynamic_site_tool,
        make_create_html_site_tool,
        make_create_landing_site_tool,
        make_create_react_site_tool,
        make_create_svelte_site_tool,
        make_edit_html_file_tool,
        make_edit_react_component_tool,
        make_edit_svelte_component_tool,
        make_read_site_source_tool,
    )

    create_landing_site = make_create_landing_site_tool(tool)
    # The svelte-track create tool — same server, so the author-source-map →
    # create_svelte_site → publish hops sit on one allowlisted server.
    create_svelte_site = make_create_svelte_site_tool(tool)
    # The targeted svelte-component edit tool — same server, so the
    # create → publish → edit hops sit on one allowlisted server.
    edit_svelte_component = make_edit_svelte_component_tool(tool)
    # The dynamic-track create tool (RFC 12 A2) — same server, so the
    # author-spec → create_dynamic_site → publish hops sit on one allowlisted
    # server. Publish carries the spec's dynamic blocks through to the generator.
    create_dynamic_site = make_create_dynamic_site_tool(tool)
    # The html-track create tool (HE-6) — same server, so the author-html-map →
    # create_html_site → publish hops sit on one allowlisted server. Opt-in: the
    # tool steers the agent to it only on an explicit raw-HTML request.
    create_html_site = make_create_html_site_tool(tool)
    # The react-track create tool (RX-2) — same server, so the author-source-map →
    # create_react_site → publish hops sit on one allowlisted server. Opt-in like
    # html: the tool steers the agent to it only on an explicit React request or a
    # genuine interactivity need.
    create_react_site = make_create_react_site_tool(tool)
    # The react-track EDIT tool (RX-3) — same server, so create → publish → edit
    # sit together for react exactly as they do for svelte. Registering it is what
    # stops the agent answering "shorten the hero headline" with a second
    # create_react_site call and a second site pocket.
    edit_react_component = make_edit_react_component_tool(tool)
    # The html-track EDIT tool (HE-10) — same server, completing the set: every
    # engine that can be created from chat can now be CHANGED from chat. Registering
    # it is what stops the agent answering "change the phone number in the footer"
    # with a second create_html_site call and a second site pocket.
    edit_html_file = make_edit_html_file_tool(tool)
    # The READ tool — same server, so read → edit → publish sit together. It is the
    # precondition for the `edits` form the three edit tools above prefer: their
    # `old_string` has to be copied out of the CURRENT file, and this is the only
    # thing on /sites that can hand the agent that file.
    read_site_source = make_read_site_source_tool(tool)

    server = create_sdk_mcp_server(
        name=SERVER_NAME,
        version="1.0.0",
        tools=[
            publish,
            get_site_build_status,
            list_site_assets,
            create_landing_site,
            create_svelte_site,
            edit_svelte_component,
            create_dynamic_site,
            create_html_site,
            create_react_site,
            edit_react_component,
            edit_html_file,
            read_site_source,
        ],
    )
    return SERVER_NAME, server


__all__ = [
    "CREATE_DYNAMIC_SITE_TOOL_ID",
    "LIST_SITE_ASSETS_TOOL_ID",
    "CREATE_HTML_SITE_TOOL_ID",
    "CREATE_LANDING_SITE_TOOL_ID",
    "CREATE_REACT_SITE_TOOL_ID",
    "CREATE_SVELTE_SITE_TOOL_ID",
    "EDIT_HTML_FILE_TOOL_ID",
    "EDIT_REACT_COMPONENT_TOOL_ID",
    "EDIT_SVELTE_COMPONENT_TOOL_ID",
    "GET_SITE_BUILD_STATUS_TOOL_ID",
    "PUBLISH_TOOL_ID",
    "READ_SITE_SOURCE_TOOL_ID",
    "SERVER_NAME",
    "SITES_TOOL_IDS",
    "build_sites_manager_server",
]
