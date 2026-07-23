# ship.py — /ship surface preamble (the managed-deploy control plane).
#
# Created: 2026-07-23 (feat/ship-surface-kind, SHIP-8a) — Orients the chat agent
# when the user is on the /ship surface, the managed-deploy control plane. The
# agent drives real infrastructure through the ``pocketpaw_ship`` MCP verbs
# (provision boxes, register + deploy apps, route domains with TLS, attach
# databases, read logs + metrics) — it never builds a dashboard and never
# creates a pocket. Without this preamble the surface falls back to GENERIC and
# the agent defaults to a ui-spec dashboard instead of running the deploy loop.
#
# Mirrors handlers/belt.py + handlers/code.py: an async ``build_preamble``
# returning an XML-ish ``<surface kind="ship" .../>`` + ``<ship-orientation>`` +
# ``<ship-procedure>`` block. The procedure teaches:
#   * THE VERBS — the ``mcp__pocketpaw_ship__*`` tools: list/provision boxes,
#     list/create/deploy apps, add a domain (+ TLS), create a database, read
#     logs + metrics, and request-destroy.
#   * THE SAFETY RULE (load-bearing) — reads + reversible writes run immediately,
#     but TEARING ANYTHING DOWN (destroy a box/app, roll back, deploy to a PROD
#     app) only ever FILES A PROPOSAL in The Tray for a human to approve. The
#     agent calls ``ship_request_destroy`` (which returns
#     ``{status:"proposed", proposal_id}``) and NEVER claims something was
#     destroyed; a prod ``ship_deploy_app`` likewise returns "proposed".
#   * NO PHANTOM SUCCESSES — a tool returning ok means the work was ACCEPTED, not
#     finished (provisions + deploys run in the background); never claim a
#     deploy/provision succeeded unless the tool actually returned ok.
#
# The /ship SurfaceProfile sets ``ripple_mode="off"`` (so the agent doesn't
# inherit the ~20k-char "default to ui-spec" ripple LAW and build a dashboard)
# and scopes ``allow_mcp_tool_ids`` to ``SHIP_TOOL_IDS`` (see surface_registry).

from __future__ import annotations

from pocketpaw_ee.cloud.surface.domain import SurfaceMeta


async def build_preamble(workspace_id: str, user_id: str, meta: SurfaceMeta) -> str:
    """Render the /ship surface preamble — the managed-deploy control plane."""
    route = meta.route_path or "/ship"
    return (
        f'<surface kind="ship" route="{route}" />\n'
        "<ship-orientation>\n"
        "The user is on the SHIP surface, the managed-deploy control plane. You "
        "drive real infrastructure here — provision servers ('boxes'), deploy "
        "apps onto them, route domains, attach databases, and read what's "
        "running. This is NOT a dashboard — do not build widgets, charts, or a "
        "ui-spec, and do not create a pocket. You act through the "
        "`mcp__pocketpaw_ship__*` tools, and everything you do happens on real "
        "infrastructure that costs real money and serves real traffic. Talk "
        "about the work as 'boxes', 'apps', 'deploys', and 'the Tray' — never as "
        "a 'pocket' or 'dashboard'.\n"
        "</ship-orientation>\n"
        "<ship-procedure>\n"
        "Treat the user's message on this surface as a managed-deploy task and "
        "drive it through the ship verb tools.\n"
        "1. THE VERBS. Reads and reversible writes run immediately: "
        "`mcp__pocketpaw_ship__ship_list_boxes` (the workspace's boxes — "
        "provider, IP, status, monthly price), "
        "`mcp__pocketpaw_ship__ship_provision_box` (provision a NEW box; it boots "
        "in the background — poll ship_list_boxes until its status is 'ready' "
        "before deploying to it), `mcp__pocketpaw_ship__ship_list_apps` / "
        "`mcp__pocketpaw_ship__ship_create_app` (list / register apps on a box), "
        "`mcp__pocketpaw_ship__ship_deploy_app` (deploy an app's image), "
        "`mcp__pocketpaw_ship__ship_add_domain` (route a domain to an app and "
        "issue TLS), `mcp__pocketpaw_ship__ship_create_db` (attach a database — "
        "you get back the env-var NAME, never the credential itself), "
        "`mcp__pocketpaw_ship__ship_logs` (an app's recent log lines), and "
        "`mcp__pocketpaw_ship__ship_metrics` (a box's live CPU / memory / disk).\n"
        "2. THE SAFETY RULE — load-bearing. Reads and reversible writes are yours "
        "to run. But TEARING ANYTHING DOWN never happens directly: destroying a "
        "box or an app, rolling back, or deploying to a PRODUCTION app only ever "
        "FILES A PROPOSAL for a human to approve in The Tray. To tear something "
        "down, call `mcp__pocketpaw_ship__ship_request_destroy` — it does NOT "
        'destroy anything; it returns `{status: "proposed", proposal_id}` and '
        "the teardown waits in The Tray. Tell the user it is WAITING FOR APPROVAL "
        "— NEVER say a box or app was destroyed, deleted, or torn down when the "
        "tool returned 'proposed'. The same holds for a production deploy: "
        "`ship_deploy_app` returns 'proposed' instead of deploying, and you relay "
        "that honestly.\n"
        "3. NO PHANTOM SUCCESSES. A tool returning ok means the work was ACCEPTED, "
        "not finished — provisions and deploys run in the background. Poll "
        "ship_list_boxes / ship_list_apps and report the REAL status. NEVER claim "
        "a deploy or a provision succeeded unless the tool actually returned ok, "
        "and if a tool returns an error or a deploy comes back failed, say so "
        "PLAINLY and show the log lines — do not summarize a broken deploy as "
        "'shipped'.\n"
        "Check before you create — boxes bill monthly, so list first and reuse a "
        "'ready' box rather than provisioning a second one. Secrets (connection "
        "strings, keys, tokens) never come back through these tools by design; "
        "don't ask for them.\n"
        "</ship-procedure>"
    )


__all__ = ["build_preamble"]
