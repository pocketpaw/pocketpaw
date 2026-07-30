# ee/paw_bar/router.py — HTTP surface for the Paw Bar widget layer.
# Updated: 2026-07-30 (async decision delivery) — added POST
#   /paw-bar/decision-contact: a visitor whose request is still PENDING leaves
#   an optional email before closing the tab; the delivery hook later emails
#   them the same customer-facing reply the poll returns. Same fail-closed
#   armor as chat/action via the shared ``_front_gate_for_key``; email is
#   validated (RFC-ish regex + 254 cap → 422), stamped onto PENDING rows only,
#   and NEVER echoed back on any public read (the poll response omits it).
# Updated: 2026-07-30 (feat/paw-bar-autoembed) — added GET /paw-bar/widget.js, the
#   PUBLIC loader route. The glass bar could only ever be embedded by hand, and the
#   snippet the dashboard printed pointed at ``https://pp.pocketpaw.dev/widget.js``
#   — a placeholder host nobody provisioned, so even a pasted snippet 404'd. There
#   was no way to load the bar from anywhere. This route serves the loader bundle
#   off the SAME origin as the rest of the API, which is what lets ``sites.service``
#   embed it into a published site automatically (see ``paw_bar/embed.py``). The
#   file is resolved by ``paw_bar_widget_file()``: ``PAW_BAR_WIDGET_JS`` when set,
#   else the copy vendored in this package (``static/paw-bar.js``) so the route
#   resolves on any machine rather than depending on a sibling checkout. A missing
#   bundle returns a clean 404 naming the env var, never a stack trace. PUBLIC by
#   design and deliberately tenant-BLIND: it is a world-visible script served
#   byte-identically to every visitor of every site, so it takes no key, reads no
#   Site, and carries nothing tenant-specific — the per-site config rides on the
#   embedding ``<script>`` tag's data attributes, and the credential check happens
#   downstream at /paw-bar/frame.
# Updated: 2026-07-29 (concierge conversation memory) — a concierge turn is no
#   longer answered cold. ``concierge_chat`` built its ``RunSpec`` with
#   ``history=[]``, so the agent forgot the visitor's name between one message and
#   the next: the visitor is anonymous and has no ``Message`` rows, so the authed
#   surfaces' ``load_history_for_scope`` had nothing to read. The stored run docs
#   (``user_text`` + ``partial_text``, from the transcript work below) are now ALSO
#   the memory. (1) ``_concierge_runs_for_visitor`` extracts the (workspace,
#   context_type, scope_id, user_id) query that ``_load_transcript`` already used,
#   so the owner's transcript read and the agent's rehydration share ONE definition
#   of "this visitor's turns" — per-visitor, per-site, per-tenant isolation lives in
#   exactly one place. (2) ``_load_concierge_history`` shapes those rows into
#   ``[{"role","content"}]`` oldest-first (the shape ``load_history_for_scope``
#   returns), bounded by ``_HISTORY_TURN_CAP`` exchanges / ``_HISTORY_MESSAGE_CHARS``
#   per line / ``_HISTORY_TOTAL_CHARS`` overall — fitted newest-first so the budget
#   drops the OLDEST turns and the replay stays contiguous. Failure-soft: a read
#   error answers without memory instead of 500-ing the visitor. (3) The read
#   happens BEFORE ``create_run`` writes this turn's doc, so the current message
#   rides in ``content`` exactly once. (4) Gated on the SAME
#   ``concierge_store_transcripts`` toggle as the write — retention off means no
#   memory, which is the owner's privacy choice working rather than a gap to route
#   around.
# Updated: 2026-07-26 (site knowledge sync) — two owner endpoints over the site's
#   own knowledge: GET /paw-bar/admin/site/{id}/knowledge reports how many articles
#   the concierge can quote and how the last sync went, and POST
#   .../knowledge/sync re-reads the site's pages into pocket:<pocket_id> (the ONE
#   scope a concierge reads) without needing a re-publish. The sync also runs
#   automatically on publish and on agent provisioning; this is the manual handle.
#   The read gates on paw_bar.read, the sync on the new paw_bar.manage — both ADMIN,
#   but a mutation that spends compute does not ride a read gate. The sync is
#   awaited here (the owner clicked a button and wants the result) while the
#   automatic triggers are backgrounded.
# Updated: 2026-07-26 (concierge transcripts) — the visitor half of a conversation
#   is now written down, so an owner's transcript is a dialogue instead of the
#   agent talking to itself. (1) concierge_chat resolves the embed key through
#   ``resolve_site_key_with_site`` (same gates, hands back the Site the gate
#   already loaded) and sets ``RunSpec.persist_user_text`` — the visitor's message,
#   capped at ``_STORED_USER_TEXT_CHARS``, and ONLY when the site's
#   ``concierge_store_transcripts`` is on. The agent always receives the full
#   message; the toggle governs storage, not the answer. (2) ``_load_transcript``
#   emits the user turn (``ChatRunDoc.user_text``, stamped at run creation) before
#   the assistant turn (``partial_text``, stamped at completion); either may be
#   absent and the other still renders, so a retention-off site reads exactly as it
#   did before. (3) the conversations list falls back to the visitor's question
#   when a run produced no reply, instead of rendering a blank row. (4) the
#   settings GET/PATCH carry ``concierge_store_transcripts``. Turning the toggle
#   off stops collection on the next message; it does NOT purge stored lines.
# Updated: 2026-07-23 (feat/site-dedicated-agent) — auto-provision a DEDICATED
#   concierge agent per site. (1) create_widget: when the request carries NO
#   agent_id and the pocket resolves to a published Site, provision + bind one
#   dedicated agent after insert (via agent_provisioning.provision_widget_on_create)
#   and return the widget with agent_id set; a plain (non-site) widget stays unbound;
#   FAILURE-SOFT (a provision error logs + returns the widget unbound, never 500s
#   the create). A manual agent_id is honored and never replaced. (2)
#   update_site_concierge_settings: ANY PATCH that sets concierge_enabled=true
#   provisions the site's widget when it is still unbound (not only a false->true
#   transition — the E2 one-click "create dedicated agent" re-PATCHes enabled=true
#   on an already-enabled site as its provision hook), via
#   provision_on_concierge_enable; idempotent + a no-op on a bound widget; also
#   failure-soft. (3) _pawbar_frame_config now carries ``starters`` (the bound
#   agent's conversation starters, capped 4, empty default) — the owner preview
#   frame threads the bound agent's starters (via _bound_agent_starters); the public
#   frame passes []. (4) GET /paw-bar/admin/site/{id}/overview's widget block now
#   carries ``agent_name`` (the bound agent's display name, resolved via the agents
#   service) so the E2 dashboard card can show the concierge name + detect the
#   "<x> Concierge" dedicated pattern; empty when unbound or the agent no longer
#   resolves (a dangling agent_id degrades to absent, never 500s the overview). The
#   ASG-1 identity fields (welcome_message/conversation_starters) and agent free-form
#   tags are ABSENT on this branch, so their seeding degrades to a graceful no-op
#   (the unbound-chat 409 invariant is unchanged).
# Updated: 2026-07-17 (D5 owner preview frame) — added GET
#   /paw-bar/admin/site/{site_id}/preview-frame → the concierge bar frame HTML for
#   the OWNER to test inside the dashboard. SESSION-authed (paw_bar.read role gate)
#   sibling of the public /paw-bar/frame: REUSES ``_pawbar_bootstrap_html`` + a new
#   shared ``_pawbar_frame_config`` builder (the public frame now calls the same
#   builder — behavior unchanged, just no forked config dict). Two differences from
#   the public frame: (1) CSP ``frame-ancestors`` = the dashboard origin from the new
#   ``PAWBAR_DASHBOARD_ORIGIN`` env (default http://localhost:5173), sanitized to a
#   single host[:port] — never the Site allowlist, never ``*``/Origin/Referer; (2)
#   served REGARDLESS of ``concierge_enabled`` so a paused bar can be previewed
#   (chat/action still obey the kill switch, unchanged). The public visitor frame's
#   security (CSP from allowed_origins, kill-switch 403) is untouched.
# Updated: 2026-07-17 (D2 conversation transcript) — added GET
#   /paw-bar/admin/site/{site_id}/conversations/{customer_ref} → {customer_ref,
#   messages:[{role,content,created_at}], count}. Same role gate (paw_bar.read) +
#   site→widget→pocket resolution as the other reads; the transcript is the
#   concierge ``ChatRunDoc`` runs for (pocket, customer_ref), most-recent 200,
#   oldest-first; 400 on a bad customer_ref, 404 when the ref has no conversation
#   here. (The v1 caveat that every turn was role "assistant" no longer holds —
#   see the 2026-07-26 entry above; visitor lines are stored when the site's
#   retention toggle allows.)
# Updated: 2026-07-16 (D2 security review — role gate + empty-pocket guard) —
#   the four D2 reads now gate on ``require_action("paw_bar.read")`` (ADMIN — the
#   caller's WORKSPACE ROLE), NOT the coarse ``require_scope("admin")`` that
#   admitted any authenticated dashboard user (a member/viewer could read another
#   owner's visitor conversations). The role check binds to the SESSION workspace
#   (``workspace_dep=current_workspace_id``), the same one the reads scope data to,
#   so tenancy stays session-derived. Also ``_resolve_site_and_widget`` now returns
#   no widget on an empty ``Site.pocket_id`` (finding #2 — an empty pocket_id would
#   otherwise widen ``list_widgets`` and resolve a sibling's widget).
# Updated: 2026-07-16 (Paw Bar concierge dashboard reads, D2) — added four OWNER
#   aggregation reads for the per-site Concierge dashboard, all under
#   /paw-bar/admin/site/{site_id}/*, role-gated (``require_action("paw_bar.read")``)
#   + workspace-scoped: (1) GET /overview — {widget, enabled, greeting, counts} with
#   cheap COUNT/distinct counters; (2) GET /conversations — recent concierge
#   ``ChatRunDoc`` runs grouped by customer_ref (LISTABLE via the run model's
#   (workspace, context_type, scope_id, createdAt) index; bounded scan + optional
#   cursor); (3) GET /decisions — the site widget's paw_bar ``DecisionStatus`` rows
#   (``WHERE widget_id = ?``); (4) GET /handoffs — ``_paw_handoffs`` reserved Fabric
#   objects scoped to the widget (no producer yet → empty in v1). Tenancy runs at
#   TWO gates: the Site is loaded workspace-scoped (cross-tenant id → 404), then its
#   paw-bar widget is resolved from ``Site.pocket_id`` ALSO workspace-scoped; the
#   decisions/conversations/handoffs filters then bind to THAT widget/pocket — never
#   pocket-wide or workspace-wide — so a sibling site or a second widget in the same
#   workspace can never appear (the leak surface the security review checks).
#   Decisions read the singleton ``DecisionStatus`` table (the 1:1 mirror of the
#   Instinct proposals) rather than the Instinct store directly, because paw-bar
#   stamps the Instinct row's in-row ``workspace_id`` with the widget OWNER, not the
#   physical workspace, and the Instinct proposal's physical file is
#   ContextVar-dependent — the DecisionStatus table has neither hazard.
# Updated: 2026-07-16 (Paw Bar concierge settings + kill switch, D1 / SS-6) — the
#   owner's on/off toggle + greeting. (a) GET /paw-bar/frame now refuses (403
#   ``concierge_disabled``) when the resolved Site has ``concierge_enabled=False``,
#   mirroring the empty-allowlist 403; chat + action/cart get the SAME 403 via
#   ``resolve_site_key`` (the shared resolver), so all three public entry points fail
#   closed on the kill switch, re-read per request. (b) The frame's ``window.__PAWBAR__``
#   config now carries ``greeting`` (``Site.concierge_greeting``) for the glass app.
#   (c) New admin surface: GET + PATCH /paw-bar/admin/site/{site_id}/settings —
#   ``require_scope("admin")`` + workspace-scoped (cross-tenant id → 404), reads/writes
#   ONLY ``concierge_enabled`` + ``concierge_greeting`` on the Site doc (the natural
#   owner key — the fields live on the Site, not the widget).
# Updated: 2026-07-16 (C1 hardening) — (a) the shared front-gate now validates
#   customer_ref against a charset+length bound (400) as its cheapest check;
#   (b) GET /paw-bar/cart records a cart-read marker so read enumeration counts
#   toward the rate limiter like writes; (c) concierge_chat threads the widget's
#   catalog (capped at _MAX_PREAMBLE_CATALOG) onto surface_meta so the concierge
#   preamble can name real products, not just the action verbs.
# Updated: 2026-07-16 (Paw Bar action registry, C1) — the visitor commerce loop.
#   (1) POST /paw-bar/action {key,w,customer_ref,verb,args} and GET /paw-bar/cart
#   ?key&w&customer_ref — PUBLIC endpoints with the SAME armor as concierge chat,
#   factored into ``_front_gate_for_key``: resolve_site_key fail-closed → dual-mode
#   origin gate → within_rate_limit → widget↔workspace/pocket binding (403). Both
#   call the SHARED ``actions.execute_action`` / cart store — never a parallel path.
#   Args are structured (no free text), so no injection screen; the executor
#   enforces the schema strictly. (2) PATCH /paw-bar/widgets/{id} — admin + owner-
#   token, workspace-scoped (cross-tenant id → 404) partial update of agent_id +
#   name/allowed_domains/rate limits (spec stays on update_spec). The paw-enterprise
#   agent-binding UI drives it.
# Updated: 2026-07-15 (glass frame asset versioning) — the frame HTML now appends
#   ``?v=<newest bundle mtime>`` to the pawbar.js/css URLs (``_asset_version``).
#   The StaticFiles mount sends no Cache-Control, so browsers heuristically cache
#   the bundle and pin embedders to a STALE app after a deploy (bit the first live
#   demo). Versioned URLs bust every embedder's cache on deploy, no restart, no
#   hard-reload. ``pawbar_app_dir()`` is now the shared dir resolver (imported by
#   the cloud mount — same never-drift pattern as PAWBAR_APP_MOUNT).
# Updated: 2026-07-15 (Paw Bar glass frame, A1) — the iframe FRAME endpoint + the
#   CSP-based origin model. (1) GET /paw-bar/frame?key=<signed_key>[&w=&po=] serves
#   the glass app document from OUR origin: it authenticates the embed key
#   (``lookup_site_by_key`` — the SAME chain resolve_site_key runs), FAILS CLOSED on
#   an empty ``allowed_origins`` (403 refuse-to-render), and emits
#   ``Content-Security-Policy: frame-ancestors <Site.allowed_origins>`` so the
#   browser refuses to render the iframe inside any non-allowlisted parent — THIS
#   (not a per-request Origin header) becomes the embedder gate. The body seeds
#   ``window.__PAWBAR__`` (JSON-safe, ``<``-escaped) before loading pawbar.js/css
#   from a configurable StaticFiles mount (``PAWBAR_APP_MOUNT`` / ``PAWBAR_APP_DIR``,
#   wired in cloud/__init__.py). No ``X-Frame-Options`` — it is OBSOLETE beside
#   frame-ancestors and a conflicting XFO:DENY would block the frame. (2) The
#   /paw-bar/chat origin check is now DUAL-MODE: an inline/legacy widget request is
#   still gated against ``Site.allowed_origins`` (fail-closed, via resolve_site_key),
#   while an iframe-mode request (Origin == our ``PAWBAR_FRAME_ORIGIN``) is accepted
#   because the embedder was already gated by the frame CSP at render time. The old
#   step-2 ``_origin_allowed(widget, ...)`` footgun (empty widget.allowed_domains =
#   allow-all) NO LONGER gates chat — the chat path converges on the fail-closed
#   ``Site.allowed_origins`` allowlist. RESIDUAL (unchanged): CSP binds BROWSERS
#   only; the world-visible key + a raw curl POST was always possible — the real
#   controls remain the rate-limit + injection screen + zero-authority CONCIERGE
#   scope. ``_origin_allowed`` stays in use for the spec/ingest/decision endpoints.
# Updated: 2026-07-14 (Paw Bar concierge seam, T2) — added POST /paw-bar/chat, a
#   PUBLIC, anonymous, streaming (SSE) concierge chat endpoint. Front-gate:
#   _origin_allowed (403) → within_rate_limit (429) → injection-screen the
#   free-text message (400 on HIGH, via the new _screen_message_for_injection).
#   Auth: resolve_site_key (401/403 fail-closed) — the embed key is the ONLY
#   credential. Binds the widget to the RESOLVED key's workspace+pocket (403 on
#   mismatch — finding #2, no sibling-pocket reach), requires a bound agent (409),
#   then dispatches a CONCIERGE-scoped RunSpec over the SAME machinery the authed
#   chat uses (create_run + executor.submit + execute_run + transport) and relays
#   its frames as SSE. The tool lockdown + KB pocket-scoping live in the CONCIERGE
#   SurfaceProfile + scope, not here. Refactored the injection screen into the
#   shared _scan_text_is_safe primitive (event ingest + chat both reuse it).
# Updated: 2026-07-14 (concierge connector lockdown) — concierge_chat refuses
#   fail-closed (409) when the pocket exposes any connector (checked via
#   list_pocket_connectors), because _CONCIERGE_DENY cannot strip dynamic
#   per-workspace composio connector tool ids. Pilot posture; the GA fix is an
#   untrusted-mode in claude_sdk (see the guard's TODO(GA-blocker)).
# Updated: 2026-07-14 (Paw Bar concierge seam, T3) — CreateWidgetRequest accepts
#   an optional agent_id; create_widget stamps it onto the PawBarWidget so a
#   concierge widget is bound to the agent that answers its chats. Purely
#   additive — omitting it keeps the existing "" (unbound) behavior.
# Updated: 2026-07-11 (W4a spec revisions) — POST /paw-bar/widgets/{id}/spec/
#   rollback (admin + owner-token, workspace-scoped like update_spec) restores
#   the latest archived spec revision; 409 when no revision exists.
# Updated: 2026-07-11 (W4a tenancy seam) — (1) Admin CRUD (create / list /
#   update-spec / rotate-token / delete) now threads the caller's active
#   workspace via Depends(current_workspace_id): create stamps the row, the
#   rest scope lookups + mutations so a cross-tenant widget id 404s and never
#   mutates. (2) Public-path fix (the cross-tenant Fabric leak): ingest stays
#   token-only but derives the tenant from the widget ROW —
#   _apply_event_mapping now calls get_fabric_store(workspace_id=
#   widget.workspace_id or None) instead of the bare shared store; legacy
#   unstamped rows ('' → None) keep the old single-tenant behavior.
# Updated: 2026-07-08 — Renamed widget "Paw Print" → "Paw Bar" (routes /paw-print→/paw-bar,
#   header X-Paw-Print-Token→X-Paw-Bar-Token, tag PawPrint→PawBar, source_connector
#   "paw_print"→"paw_bar"). Hard-rename — widget has zero deployments. The separate
#   one-word audit feed (past-tense record) is a DIFFERENT feature, unaffected.
# Created: 2026-04-13 (Move 3 PR-B) — Spec serving (public, CORS-gated),
# widget CRUD (owner-authed via access_token), event ingest (rate-limited,
# domain-enforced, injection-screened, Fabric-mapped). The widget.js bundle
# built in PR-C consumes these endpoints.
# Updated: 2026-05-30 — Replaced the always-None Guardian no-op screen
# (getattr(guardian, "check_input") — GuardianAgent never exposed that
# method, so the check was a permanent accept-all) with the real
# InjectionScanner. The stringified event payload is now heuristically
# screened and dropped on a HIGH-or-higher threat. Renamed the helper to
# _screen_event_for_injection and the rejection reason to
# "injection_rejected".
# Updated: 2026-06-10 (W0b security fix) — Closed an unauthenticated
# access-token leak on the widget-management surface. (1) Widget CRUD
# (create / list / update-spec / delete) now requires a fully-authenticated
# dashboard caller via Depends(require_scope("admin")); previously these
# routes had NO route-level auth, and the /api/v1/* mount is auth-OPTIONAL at
# the middleware level, so an unauthenticated caller could reach them. (2) The
# list and read responses now serialize PawBarWidgetPublic, which omits
# access_token — the per-widget owner credential no longer leaves the server
# in a list/read payload. The token is still returned by the explicit,
# authenticated create + rotate-token paths so an owner can capture it once.
# The public spec-serving and event-ingest endpoints stay unauthenticated by
# design (origin/CORS-gated for the embedded widget bundle).
# Updated: 2026-06-11 (gap2 — close the customer decision loop) — An accepted,
# mapped customer event no longer dead-ends at a Fabric object: ingest now also
# raises an Instinct proposal via decision_loop.propose_customer_decision and
# parks a PENDING DecisionStatus row (best-effort — a loop failure never fails
# the ingest response). Added a public, CORS-gated poll endpoint
# (GET /paw-bar/events/{widget_id}/decision/{customer_ref}) so the rendered
# widget can read the owner's decision back out — the back-half of the loop. The
# approve/reject delivery hook lives in the instinct router (it owns the human
# decision); see decision_loop.deliver_customer_decision.

from __future__ import annotations

import json
import logging
import os
import re
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

from pocketpaw.api.deps import require_scope
from pocketpaw.paw_bar.models import (
    MAX_PAYLOAD_BYTES,
    PawBarEvent,
    PawBarEventMapping,
    PawBarSpec,
    PawBarWidget,
    PawBarWidgetPublic,
)
from pocketpaw_ee.cloud._core.deps import current_workspace_id, require_action

logger = logging.getLogger(__name__)

# Role gate for the D2 concierge dashboard reads. ``require_action`` enforces the
# caller's WORKSPACE ROLE against the ``paw_bar.read`` rule (ADMIN — owner/admin
# only, not member); replaces the coarse ``require_scope("admin")`` that admitted
# any authenticated dashboard user. ``workspace_dep=current_workspace_id`` binds
# the role check to the SESSION's active workspace (never a path/query value — the
# D2 routes carry ``{site_id}``, not ``{workspace_id}``, so the default
# path-sourced dep would read an attacker-suppliable query param), the SAME
# workspace the reads scope their data to.
_require_paw_bar_read = require_action("paw_bar.read", workspace_dep=current_workspace_id)

router = APIRouter(tags=["PawBar"])

_PLACEHOLDER_RE = re.compile(r"\{\{\s*([a-zA-Z0-9_.]+)\s*\}\}")

# Cap the catalog threaded into the concierge preamble so a large catalog can't
# bloat the prompt (C1). The full catalog is still enforced by the spec cap.
_MAX_PREAMBLE_CATALOG = 50


def _store():
    from pocketpaw_ee.api import get_paw_bar_store

    return get_paw_bar_store()


def _require_owner_token(widget: PawBarWidget, header_token: str | None) -> None:
    if not header_token or header_token != widget.access_token:
        raise HTTPException(status_code=401, detail="Invalid or missing access token")


def _origin_allowed(widget: PawBarWidget, origin: str | None) -> bool:
    """Match an inbound Origin header against the widget's allowed_domains.

    Empty `allowed_domains` disables the check — useful for local demos but
    must be set in production. The match is host-only so ports and paths don't
    matter: `https://brewco.com:443/menu` matches `brewco.com`.
    """
    if not widget.allowed_domains:
        return True
    if not origin:
        return False
    host = origin.strip().lower()
    if "://" in host:
        host = host.split("://", 1)[1]
    host = host.split("/", 1)[0]
    host = host.split(":", 1)[0]
    return host in widget.allowed_domains


# ---------------------------------------------------------------------------
# Glass frame (iframe) — CSP-based origin model (A1)
#
# The vanilla widget runs in the HOST page's origin, so a chat request's Origin
# header IS the embedder and the per-request Origin gate authenticates WHO
# embedded. Served in an iframe from OUR domain, every request carries OUR frame
# origin — identical for all embedders — so that gate degenerates. The frame
# endpoint moves the embedder gate to a CSP ``frame-ancestors`` header (the browser
# refuses to render our iframe inside a non-allowlisted parent), and the chat
# endpoint's origin check becomes dual-mode (see ``concierge_chat``).
# ---------------------------------------------------------------------------

# URL path the glass app bundle (pawbar.js + pawbar.css) is served from. The
# StaticFiles mount lives in ``ee/cloud/__init__.py``; both the mount and the frame
# HTML import THIS constant so the ``<script src>`` and the mount path never drift.
PAWBAR_APP_MOUNT = "/pawbar-app"


def pawbar_app_dir() -> Path:
    """Directory the glass app bundle is served from (PAWBAR_APP_DIR overridable).

    Single source of truth shared with the StaticFiles mount in
    ``ee/cloud/__init__.py`` (same never-drift pattern as ``PAWBAR_APP_MOUNT``).
    """
    return Path(os.environ.get("PAWBAR_APP_DIR", str(Path.home() / ".pocketpaw" / "pawbar-app")))


def _asset_version() -> str:
    """Cache-busting version stamp for the glass app assets.

    The StaticFiles mount serves pawbar.js/css with no ``Cache-Control``, so
    browsers fall back to heuristic freshness and can pin an embedder to a STALE
    bundle after a deploy (bit the first live demo: a sizing fix shipped but the
    browser kept replaying the old JS). The frame HTML appends ``?v=<newest
    mtime>`` to both asset URLs so every deploy mints new URLs and busts every
    embedder's cache with no server restart and no manual hard-reload. Two
    ``stat`` calls per frame render — negligible next to the DB key lookup.
    Returns "0" when the bundle isn't dropped in yet (assets 404 either way).
    """
    newest = 0
    for name in ("pawbar.js", "pawbar.css"):
        try:
            newest = max(newest, int((pawbar_app_dir() / name).stat().st_mtime))
        except OSError:
            continue
    return str(newest)


# ---------------------------------------------------------------------------
# The loader bundle (GET /paw-bar/widget.js)
#
# The one script a foreign page loads to grow a concierge. It reads its config off
# its own <script> tag and mounts the /paw-bar/frame iframe; everything
# tenant-specific lives in those attributes, so this file is the same bytes for
# every site and needs no auth. Before this existed the only advertised URL was an
# unprovisioned CDN placeholder, which is why a published site could carry a
# concierge and still show nothing.
# ---------------------------------------------------------------------------

# How long a browser may reuse the loader without re-asking. Short on purpose: the
# glass app's own assets are cache-busted by ``_asset_version`` in the frame HTML,
# but a <script src> baked into a customer's deployed page has no version stamp we
# control, so a long max-age would pin every embedder to whatever loader shipped on
# the day their site was published. Five minutes keeps the edge useful and keeps a
# fix at most one coffee away.
_WIDGET_JS_MAX_AGE = 300


def paw_bar_widget_file() -> Path:
    """Path of the loader bundle ``GET /paw-bar/widget.js`` serves.

    ``PAW_BAR_WIDGET_JS`` wins when set — that is the seam for serving a freshly
    built bundle (e.g. ``paw-bar/dist/…``) without a redeploy. Otherwise
    the copy vendored beside this module. The default is deliberately IN the
    package rather than an absolute path into a sibling checkout: the publish path
    now bakes this URL into customers' deployed HTML, so it has to resolve on every
    machine that runs the backend, not just a developer's.
    """
    override = os.environ.get("PAW_BAR_WIDGET_JS", "").strip()
    if override:
        return Path(override)
    return Path(__file__).resolve().parent / "static" / "paw-bar.js"


@router.get("/paw-bar/widget.js")
async def widget_js() -> Response:
    """Serve the glass-bar loader — PUBLIC, unauthenticated, tenant-blind.

    No key, no Site read, no per-caller variation: this is a world-visible static
    script, and the credential (the embed key) is presented later by the iframe it
    mounts, at ``/paw-bar/frame``. Read from disk per request rather than cached in
    memory so replacing the file takes effect without a restart — the file is a few
    KB and the OS page cache absorbs the repeat reads.

    A missing bundle is a clean 404 naming the env var that fixes it, not a
    FileNotFoundError escaping as an opaque 500: the operator seeing this is
    debugging why a live site shows no bar, and the message is the answer.
    """
    path = paw_bar_widget_file()
    try:
        body = path.read_bytes()
    except OSError:
        logger.warning("paw-bar: loader bundle unavailable at %s", path)
        raise HTTPException(
            status_code=404,
            detail=(
                "Paw Bar loader bundle not found. Set PAW_BAR_WIDGET_JS to the "
                "path of a built widget bundle, or restore the copy shipped at "
                "pocketpaw_ee/paw_bar/static/paw-bar.js."
            ),
        ) from None
    return Response(
        content=body,
        media_type="application/javascript; charset=utf-8",
        headers={"Cache-Control": f"public, max-age={_WIDGET_JS_MAX_AGE}"},
    )


# A frame-ancestors host-source is host[:port] with NO scheme, path, or whitespace.
# ``allowed_origins`` is owner-controlled data flowing into a response HEADER, so
# each entry is reduced to this safe shape (or dropped) — a stray space / ``;`` /
# newline would otherwise inject extra CSP directives or split the header.
_SAFE_ANCESTOR_RE = re.compile(r"^[a-z0-9.\-]+(:[0-9]{1,5})?$")


def _sanitize_ancestor(raw: Any) -> str | None:
    """Reduce one ``allowed_origins`` entry to a safe frame-ancestors host-source.

    Returns the bare ``host[:port]`` (scheme + path stripped) or ``None`` if it
    can't be safely represented. Emitting the bare host (no scheme) is deliberate:
    it matches the rest of the origin model (``origin_allowed`` is host-only) AND a
    schemeless frame-ancestors source adapts to the frame's OWN scheme — https-tight
    when the frame is served over https, http-permissive in local dev — instead of
    hard-pinning a scheme that would break one or the other.
    """
    if not isinstance(raw, str):
        return None
    v = raw.strip().lower()
    if not v:
        return None
    if "://" in v:
        v = v.split("://", 1)[1]
    v = v.split("/", 1)[0]  # drop any path
    # SECURITY (ordering is load-bearing): ``v`` was ``.strip()``-ed above, so it
    # carries no trailing newline. ``$`` in this pattern matches just BEFORE a
    # trailing ``\n``, so a regex-before-strip reorder would let ``"host\n"`` pass
    # and ``return v`` would emit that newline into the CSP header (classic header
    # injection / directive split). Keep .strip() ahead of this match.
    if not _SAFE_ANCESTOR_RE.match(v):
        return None
    # PORT (bug found 2026-07-30 by framing a real published site): a CSP
    # host-source with NO port matches only the scheme's DEFAULT port, so a bare
    # ``127.0.0.1`` refuses to be framed by ``http://127.0.0.1:4174`` and the bar
    # renders as an empty grey box. ``allowed_origins`` is normalized to bare HOSTS
    # (``_normalize_origin_hosts``), so in practice NO entry ever carries a port and
    # every site served on a non-default port was unframeable — every local, dev and
    # demo deploy, and any customer site not on 80/443.
    #
    # Emitting ``host:*`` (any port) is the fix, and it is a CONSISTENCY change
    # rather than a loosening: the origin gate this CSP mirrors
    # (``site_keys.origin_allowed``) compares HOSTS and ignores the port entirely,
    # so a request from any port on an allowed host is already accepted for chat.
    # The CSP was the stricter of the two. An entry that DOES carry an explicit port
    # is honored as written.
    if ":" not in v:
        return f"{v}:*"
    return v


def _frame_ancestors_csp(allowed_origins: list[str]) -> str | None:
    """Build the ``frame-ancestors`` CSP value from a Site's ``allowed_origins``.

    Returns ``None`` when NO entry survives sanitization (including an empty
    allowlist) — the caller FAILS CLOSED (refuses to render) rather than emit a
    source-less directive. Mirrors ``site_keys.origin_allowed``'s empty=deny model,
    NOT the router's ``_origin_allowed`` empty=allow-all footgun.
    """
    sources = [s for s in (_sanitize_ancestor(o) for o in (allowed_origins or [])) if s]
    if not sources:
        return None
    return "frame-ancestors " + " ".join(sources)


def _configured_frame_origin(request: Request) -> str:
    """OUR iframe's origin — the trusted parent for the dual-mode chat gate.

    Configurable via ``PAWBAR_FRAME_ORIGIN`` (set it in any multi-origin / proxied
    deploy where the frame is served from a fixed public origin). Defaults to the
    request's own ``scheme://host`` for single-origin / local deploys where the
    frame and the API share an origin.
    """
    configured = os.environ.get("PAWBAR_FRAME_ORIGIN", "").strip()
    if configured:
        return configured
    return f"{request.url.scheme}://{request.url.netloc}"


def _safe_parent_origin(po: str, allowed_origins: list[str]) -> str:
    """Validate the loader-supplied parent origin against the Site's allowlist.

    ``po`` rides in the iframe URL (set by the loader running in the embedder page),
    so it is attacker-influenceable and never trusted verbatim. Returns a clean
    ``scheme://host[:port]`` origin — usable as the glass app's postMessage
    ``targetOrigin`` — ONLY when its host is on the Site's allowlist; otherwise "".
    (The real embedder is allowlisted by definition, else the CSP blocks the frame.)
    """
    if not po:
        return ""
    v = po.strip().lower().rstrip("/")
    if "://" in v:
        scheme, rest = v.split("://", 1)
    else:
        scheme, rest = "https", v
    hostport = rest.split("/", 1)[0]
    hostonly = hostport.split(":", 1)[0]
    allowed_hostonly = {
        h.split(":", 1)[0] for h in (_sanitize_ancestor(o) for o in (allowed_origins or [])) if h
    }
    if hostonly not in allowed_hostonly:
        return ""
    if scheme not in ("http", "https") or not _SAFE_ANCESTOR_RE.match(hostport):
        return ""
    return f"{scheme}://{hostport}"


# Dark page background for the OWNER preview frame only (not the public embed), so
# the transparent glass bar sits on a dark surface matching the dashboard instead of
# a white canvas. A near-black neutral tuned to the paw-enterprise dark theme.
_PREVIEW_PAGE_BG = "#0d0e12"


def _pawbar_bootstrap_html(config: dict[str, Any], asset_mount: str, page_bg: str = "") -> str:
    """Render the glass frame document: seed ``window.__PAWBAR__`` then load the app.

    The config dict is ``json.dumps``'d with ``<`` escaped to ``\\u003c`` so no
    value (the world-visible key, the attacker-influenceable ``widgetId`` /
    ``parentOrigin`` query params) can break out of the inline ``<script>`` with a
    ``</script>`` sequence or inject markup. Assets load from ``asset_mount`` (the
    root-absolute StaticFiles mount), so the document is valid wherever the API
    router is mounted.

    ``page_bg`` is empty for the PUBLIC embed (the bar body stays transparent so it
    sits over the customer's real page) and set ONLY by the owner preview frame to a
    dark surface: a transparent-body iframe otherwise paints a white canvas backdrop,
    which clashes with the dark dashboard the preview is embedded in. The value is a
    hard-coded CSS color the server controls (never request-derived), so the inline
    ``<style>`` cannot be injected.
    """
    config_json = json.dumps(config).replace("<", "\\u003c")
    v = _asset_version()
    preview_style = (
        f"<style>html,body{{background:{page_bg};color-scheme:dark}}</style>\n" if page_bg else ""
    )
    return (
        "<!doctype html>\n"
        '<html lang="en">\n'
        "<head>\n"
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        "<title>Paw Bar</title>\n"
        f'<link rel="stylesheet" href="{asset_mount}/pawbar.css?v={v}">\n'
        f"{preview_style}"
        "</head>\n"
        "<body>\n"
        '<div id="pawbar-root"></div>\n'
        f"<script>window.__PAWBAR__ = {config_json};</script>\n"
        f'<script src="{asset_mount}/pawbar.js?v={v}"></script>\n'
        "</body>\n"
        "</html>\n"
    )


def _pawbar_frame_config(
    *,
    site_key: str,
    widget_id: str,
    api_base: str,
    parent_origin: str,
    greeting: str,
    starters: list[str] | None = None,
) -> dict[str, Any]:
    """Build the ``window.__PAWBAR__`` bootstrap config shared by the public frame
    and the owner preview frame (D5).

    Single source of truth for the config shape so the two framing paths never
    drift: both seed the SAME glass app with the SAME fields. The callers differ
    only in WHERE the values come from — the public frame reads ``siteKey`` +
    ``parentOrigin`` from the world-visible key + allowlist, the owner preview from
    the resolved Site + the dashboard origin — and in the CSP ancestor they set.

    ``starters`` are the bound agent's conversation starters (feat/site-dedicated-
    agent, E3): additive, capped at 4, defaulting to an empty list so a frame with
    no starters is unchanged. On this branch the Agent model carries no
    ``conversation_starters`` field (the ASG-1 identity fields are absent), so
    callers pass ``[]`` today — the wire is in place for when those fields land.
    """
    return {
        "siteKey": site_key,
        "widgetId": widget_id or "",
        "endpoint": api_base,
        "parentOrigin": parent_origin,
        "mode": "concierge",
        # D1 / SS-6 — the owner's opening line; the glass app renders it (D4) and
        # falls back to its own default when "".
        "greeting": greeting or "",
        # E3 — the bound agent's conversation starters (capped 4).
        "starters": (starters or [])[:4],
        "tokens": {},
    }


async def _bound_agent_starters(agent_id: str) -> list[str]:
    """Best-effort conversation starters for a widget's BOUND agent (E3).

    Returns the bound agent's ``conversation_starters`` (capped 4) for the frame
    config, or ``[]`` when the widget is unbound, the agent is gone, or the Agent
    model carries no ``conversation_starters`` field. NOTE: the ASG-1 identity
    fields are ABSENT on this branch, so ``getattr`` misses and this returns ``[]``
    today — the read is in place so starters flow the moment the field lands. Any
    lookup failure degrades to ``[]`` (the frame must still render).
    """
    if not agent_id:
        return []
    try:
        from pocketpaw_ee.cloud.agents import service as agents_service

        agent = await agents_service.get(agent_id)
    except Exception:  # noqa: BLE001 — a starter read must never break the frame
        return []
    starters = getattr(agent.config, "conversation_starters", None) or []
    return list(starters)[:4]


async def _bound_agent_name(agent_id: str) -> str:
    """Best-effort display name of a widget's BOUND agent (E2 dashboard card).

    Resolved through the SAME agents-service read path the provisioner uses, so the
    dashboard can show the concierge name and detect the "<x> Concierge" dedicated
    pattern. Returns "" when the widget is unbound OR the agent no longer resolves —
    a dangling agent_id degrades to absent rather than 500-ing the overview.
    """
    if not agent_id:
        return ""
    try:
        from pocketpaw_ee.cloud.agents import service as agents_service

        agent = await agents_service.get(agent_id)
    except Exception:  # noqa: BLE001 — a name read must never break the overview
        return ""
    return agent.name or ""


def _dashboard_origin() -> str:
    """The paw-enterprise dashboard origin allowed to frame the owner preview (D5).

    Read from ``PAWBAR_DASHBOARD_ORIGIN`` (set it to the deployed dashboard origin,
    e.g. ``https://app.example.com``). Defaults to the Vite dev server
    (``http://localhost:5173``) so local dev works with no config. This is the ONLY
    origin the preview frame's CSP ``frame-ancestors`` permits — deliberately NOT
    the Site's public ``allowed_origins`` (that gates the visitor frame) and never
    ``*`` or the request's own Origin/Referer.
    """
    return os.environ.get("PAWBAR_DASHBOARD_ORIGIN", "").strip() or "http://localhost:5173"


@router.get("/paw-bar/frame")
async def frame(
    request: Request,
    key: str = Query("", description="The public Site.signed_key baked into the loader"),
    w: str = Query("", description="Optional Paw Bar widget id"),
    po: str = Query("", description="Parent origin the loader passes for postMessage"),
) -> HTMLResponse:
    """Serve the glass Paw Bar app inside an iframe, gated by CSP frame-ancestors.

    The embedder gate is NOT a per-request Origin check (in an iframe every request
    carries OUR frame origin) — it is the ``Content-Security-Policy: frame-ancestors``
    header the browser enforces at render time: it refuses to display this iframe
    inside any parent whose origin isn't on the Site's ``allowed_origins``.

    Order (fail-closed):
      1. Authenticate the embed key — ``lookup_site_by_key`` (the SAME chain the
         chat path runs): blank / short / unknown / revoked → 401.
      2. Build the ``frame-ancestors`` value from ``Site.allowed_origins``. FAIL
         CLOSED on an empty / unusable allowlist → 403 (refuse to render), mirroring
         ``site_keys.origin_allowed`` — NOT the ``_origin_allowed`` allow-all footgun.
      3. Seed ``window.__PAWBAR__`` (JSON-safe) and load pawbar.js/css.

    No ``X-Frame-Options``: it is obsolete beside ``frame-ancestors`` and a
    conflicting ``XFO: DENY`` would stop the frame from rendering at all.

    Residual: ``frame-ancestors`` binds BROWSERS only. The key is world-visible and
    a raw ``curl`` POST to /paw-bar/chat was always possible and is unchanged here —
    the real controls stay the rate-limit + injection screen + the zero-authority
    CONCIERGE scope. CSP does not close the curl path.
    """
    from pocketpaw_ee.cloud.auth.site_keys import lookup_site_by_key

    # (1) Authenticate the embed key. A missing/blank ``key`` query param is a
    # too-short key → 401 (never a 422), so the refusal is uniform with the chat path.
    site = await lookup_site_by_key(key)

    # (1b) Kill switch (D1 / SS-6): the owner's ``concierge_enabled`` toggle. When
    # off, refuse to render (403) — the same fail-closed shape as the empty-allowlist
    # refusal below. Re-read on every request (``lookup_site_by_key`` does a fresh
    # find_one, nothing is cached), so toggling off silences the frame immediately.
    # Distinct from ``revoked`` (which cuts the KEY at 401 inside lookup_site_by_key).
    if not site.concierge_enabled:
        raise HTTPException(status_code=403, detail="concierge_disabled")

    # (2) The embedder gate: the CSP frame-ancestors header. Fail closed when no
    # allowlisted origin survives sanitization — refuse to render.
    csp = _frame_ancestors_csp(site.allowed_origins)
    if csp is None:
        raise HTTPException(status_code=403, detail="frame_ancestors_unset")

    # (3) Bootstrap config. ``endpoint`` is the API base derived from the request
    # path (mount-agnostic: /api/v1 in prod, "" when the router is mounted bare in
    # tests); the glass app POSTs ``{endpoint}/paw-bar/chat``. ``parentOrigin`` is
    # validated against the allowlist. ``siteKey`` in the page is fine — it is a
    # world-visible embed key by design.
    api_base = request.url.path.rsplit("/paw-bar/frame", 1)[0]
    # E3 — the bound agent's conversation starters ride the config. The public
    # frame is pre-auth (keyed on the world-visible embed key, no session) and does
    # not load the widget here, so it defaults to []; the bound-agent starters are
    # surfaced through the owner preview frame (below), where the widget is already
    # resolved workspace-scoped. On this branch the value is [] regardless (the
    # Agent ``conversation_starters`` field is absent — ASG-1 not merged here).
    config = _pawbar_frame_config(
        site_key=key,
        widget_id=w or "",
        api_base=api_base,
        parent_origin=_safe_parent_origin(po, site.allowed_origins),
        greeting=site.concierge_greeting or "",
        starters=[],
    )
    html = _pawbar_bootstrap_html(config, PAWBAR_APP_MOUNT)
    return HTMLResponse(
        content=html,
        headers={
            "Content-Security-Policy": csp,
            # The embed key is baked into the loader HTML per-embedder; the frame
            # doc itself must not be cached across keys/parents by a shared proxy.
            "Cache-Control": "no-store",
        },
    )


# ---------------------------------------------------------------------------
# Request / response schemas
# ---------------------------------------------------------------------------


class CreateWidgetRequest(BaseModel):
    pocket_id: str
    owner: str
    # T3 — bind the widget to the agent that answers its concierge chats. Optional
    # + defaults to "" (unbound), so existing create calls are unaffected.
    agent_id: str = ""
    name: str = ""
    spec: PawBarSpec
    allowed_domains: list[str] = Field(default_factory=list)
    rate_limit_per_min: int = 60
    per_customer_limit_per_min: int = 10
    event_mapping: dict[str, PawBarEventMapping] = Field(default_factory=dict)


class UpdateWidgetRequest(BaseModel):
    """Partial admin update of a widget's mutable, non-spec fields (C1).

    Every field is optional; only the ones the client SENDS are written (tracked
    via ``model_fields_set``). ``agent_id`` may be set to a new agent or ``null``
    to unbind (stored as ""). The spec is NOT editable here — that is
    ``update_spec`` (which archives a revision). The paw-enterprise agent-binding
    UI ships against exactly this shape.
    """

    agent_id: str | None = None
    name: str | None = None
    allowed_domains: list[str] | None = None
    rate_limit_per_min: int | None = None
    per_customer_limit_per_min: int | None = None


class WidgetListResponse(BaseModel):
    # PawBarWidgetPublic (not PawBarWidget) — list payloads must never
    # carry access_token (W0b).
    widgets: list[PawBarWidgetPublic]
    total: int


class EventIngestResponse(BaseModel):
    accepted: bool
    event: PawBarEvent | None = None
    fabric_object_id: str | None = None
    # gap2 — the Instinct proposal raised for this event (when the widget maps
    # the event type). The customer surface can poll the decision endpoint to
    # read the owner's eventual decision; None when no proposal was raised.
    instinct_action_id: str | None = None
    reason: str | None = None


class DecisionStatusResponse(BaseModel):
    """The customer-facing view of a decision (gap2).

    Deliberately omits internal-only fields (the Instinct action id, the
    workspace) — the customer surface only needs the state + the reply.
    ``found`` is False when no decision exists yet for this (widget, customer).
    """

    found: bool
    state: str | None = None
    reply: str | None = None
    decided_by: str | None = None
    updated_at: str | None = None


class EventsListResponse(BaseModel):
    events: list[PawBarEvent]
    total: int


# ---------------------------------------------------------------------------
# Widget management (CRUD)
#
# Auth model (W0b): these routes are mounted under /api/v1, which the
# dashboard AuthMiddleware treats as auth-OPTIONAL — it populates request.state
# but does NOT 401. So management routes MUST gate themselves at the route
# level. require_scope("admin") is fail-closed: it accepts a full-access
# dashboard session (master/session-cookie/localhost) or an admin-scoped
# API-key / OAuth token, and 403s everyone else (including unauthenticated
# callers). The per-widget access_token (X-Paw-Bar-Token) is a SECOND factor
# on read/mutate of a specific widget — it is not a substitute for being a
# signed-in dashboard user, which is why create/list need this guard.
# ---------------------------------------------------------------------------


@router.post(
    "/paw-bar/widgets",
    response_model=PawBarWidget,
    status_code=201,
    dependencies=[Depends(require_scope("admin"))],
)
async def create_widget(
    req: CreateWidgetRequest,
    workspace_id: str = Depends(current_workspace_id),
) -> PawBarWidget:
    # W4a — the row is stamped with the caller's ACTIVE workspace, never a
    # client-supplied value: tenancy is derived server-side from the session.
    widget = PawBarWidget(
        pocket_id=req.pocket_id,
        owner=req.owner,
        workspace_id=workspace_id,
        agent_id=req.agent_id,
        name=req.name,
        spec=req.spec,
        allowed_domains=req.allowed_domains,
        rate_limit_per_min=req.rate_limit_per_min,
        per_customer_limit_per_min=req.per_customer_limit_per_min,
        event_mapping=req.event_mapping,
    )
    created = await _store().create_widget(widget)
    # Auto-provision a DEDICATED concierge agent (feat/site-dedicated-agent). Only
    # when the caller bound NO agent_id and the pocket resolves to a published Site
    # — a plain (non-site) widget stays unbound. Failure-soft: a provisioning error
    # logs and returns the widget UNBOUND rather than 500-ing the create (chat then
    # 409s and the dashboard offers a manual create). A manual agent_id is honored
    # and never replaced (the trigger returns early on a bound widget).
    if not req.agent_id:
        from pocketpaw_ee.paw_bar.agent_provisioning import provision_widget_on_create

        created = await provision_widget_on_create(created, workspace_id)
    return created


@router.get(
    "/paw-bar/widgets",
    response_model=WidgetListResponse,
    dependencies=[Depends(require_scope("admin"))],
)
async def list_widgets(
    pocket_id: str | None = Query(None),
    owner: str | None = Query(None),
    limit: int = Query(100, ge=1, le=500),
    workspace_id: str = Depends(current_workspace_id),
) -> WidgetListResponse:
    widgets = await _store().list_widgets(
        pocket_id=pocket_id, owner=owner, limit=limit, workspace_id=workspace_id
    )
    # Project to the token-free model — a list payload must never carry the
    # per-widget access_token (W0b).
    public = [PawBarWidgetPublic.from_widget(w) for w in widgets]
    return WidgetListResponse(widgets=public, total=len(public))


@router.get("/paw-bar/widgets/{widget_id}", response_model=PawBarWidgetPublic)
async def get_widget(
    widget_id: str,
    x_paw_bar_token: str | None = Header(default=None, alias="X-Paw-Bar-Token"),
) -> PawBarWidgetPublic:
    widget = await _store().get_widget(widget_id)
    if widget is None:
        raise HTTPException(404, "Widget not found")
    _require_owner_token(widget, x_paw_bar_token)
    # Read responses omit access_token — the caller already holds it (they had
    # to present it to pass _require_owner_token), so echoing it back only
    # widens the blast radius if a read response is logged/cached (W0b).
    return PawBarWidgetPublic.from_widget(widget)


@router.patch(
    "/paw-bar/widgets/{widget_id}/spec",
    response_model=PawBarWidgetPublic,
    dependencies=[Depends(require_scope("admin"))],
)
async def update_spec(
    widget_id: str,
    spec: PawBarSpec,
    x_paw_bar_token: str | None = Header(default=None, alias="X-Paw-Bar-Token"),
    workspace_id: str = Depends(current_workspace_id),
) -> PawBarWidgetPublic:
    # W4a — the lookup is workspace-scoped: another tenant's widget id resolves
    # to None → 404, before the token even gets compared. Never mutates.
    widget = await _store().get_widget(widget_id, workspace_id=workspace_id)
    if widget is None:
        raise HTTPException(404, "Widget not found")
    _require_owner_token(widget, x_paw_bar_token)
    updated = await _store().update_spec(widget_id, spec, workspace_id=workspace_id)
    if updated is None:
        raise HTTPException(404, "Widget not found")
    return PawBarWidgetPublic.from_widget(updated)


@router.patch(
    "/paw-bar/widgets/{widget_id}",
    response_model=PawBarWidgetPublic,
    dependencies=[Depends(require_scope("admin"))],
)
async def update_widget(
    widget_id: str,
    req: UpdateWidgetRequest,
    x_paw_bar_token: str | None = Header(default=None, alias="X-Paw-Bar-Token"),
    workspace_id: str = Depends(current_workspace_id),
) -> PawBarWidgetPublic:
    """Partial-update a widget's mutable, non-spec fields (C1 — agent binding).

    Auth mirrors ``update_spec``: an admin dashboard session AND the per-widget
    owner token, with the lookup workspace-scoped so a cross-tenant widget id
    404s before anything is written. Only the fields the client SENT are applied
    (``model_fields_set``); ``agent_id: null`` unbinds (stored as ""). The spec is
    intentionally not editable here."""
    widget = await _store().get_widget(widget_id, workspace_id=workspace_id)
    if widget is None:
        raise HTTPException(404, "Widget not found")
    _require_owner_token(widget, x_paw_bar_token)

    # Only the sent fields become column writes. agent_id null → "" (unbind).
    fields: dict[str, Any] = {}
    for name in req.model_fields_set:
        value = getattr(req, name)
        if name == "agent_id":
            fields[name] = value or ""
        elif value is not None:
            fields[name] = value
    updated = await _store().update_fields(widget_id, fields, workspace_id=workspace_id)
    if updated is None:
        raise HTTPException(404, "Widget not found")
    return PawBarWidgetPublic.from_widget(updated)


@router.post(
    "/paw-bar/widgets/{widget_id}/spec/rollback",
    response_model=PawBarWidgetPublic,
    dependencies=[Depends(require_scope("admin"))],
)
async def rollback_spec(
    widget_id: str,
    x_paw_bar_token: str | None = Header(default=None, alias="X-Paw-Bar-Token"),
    workspace_id: str = Depends(current_workspace_id),
) -> PawBarWidgetPublic:
    """Restore the latest archived spec revision (W4a).

    Every ``update_spec`` archives the prior spec as a monotonic revision;
    this endpoint restores the most recent one. The restore is itself an
    update that archives the current spec, so a rollback is reversible.
    Auth mirrors ``update_spec``: admin session + per-widget owner token,
    with the lookup workspace-scoped (cross-tenant id → 404).
    """
    widget = await _store().get_widget(widget_id, workspace_id=workspace_id)
    if widget is None:
        raise HTTPException(404, "Widget not found")
    _require_owner_token(widget, x_paw_bar_token)
    restored = await _store().rollback_spec(widget_id, workspace_id=workspace_id)
    if restored is None:
        raise HTTPException(409, "No spec revision to roll back to")
    return PawBarWidgetPublic.from_widget(restored)


@router.post(
    "/paw-bar/widgets/{widget_id}/rotate-token",
    response_model=PawBarWidget,
    dependencies=[Depends(require_scope("admin"))],
)
async def rotate_token(
    widget_id: str,
    x_paw_bar_token: str | None = Header(default=None, alias="X-Paw-Bar-Token"),
    workspace_id: str = Depends(current_workspace_id),
) -> PawBarWidget:
    # Returns the FULL widget (with the new access_token) on purpose: this is
    # the explicit, authenticated reveal path so the owner can capture the
    # rotated secret. Still requires the old token AND an admin dashboard
    # session (W0b). W4a — lookup + rotate are workspace-scoped (cross-tenant
    # id → 404, nothing rotates).
    widget = await _store().get_widget(widget_id, workspace_id=workspace_id)
    if widget is None:
        raise HTTPException(404, "Widget not found")
    _require_owner_token(widget, x_paw_bar_token)
    rotated = await _store().rotate_token(widget_id, workspace_id=workspace_id)
    if rotated is None:
        raise HTTPException(404, "Widget not found")
    return rotated


@router.delete(
    "/paw-bar/widgets/{widget_id}",
    status_code=204,
    dependencies=[Depends(require_scope("admin"))],
)
async def delete_widget(
    widget_id: str,
    x_paw_bar_token: str | None = Header(default=None, alias="X-Paw-Bar-Token"),
    workspace_id: str = Depends(current_workspace_id),
) -> None:
    # W4a — scoped lookup + scoped DELETE: a cross-tenant widget id 404s and
    # the row is never touched.
    widget = await _store().get_widget(widget_id, workspace_id=workspace_id)
    if widget is None:
        raise HTTPException(404, "Widget not found")
    _require_owner_token(widget, x_paw_bar_token)
    await _store().delete_widget(widget_id, workspace_id=workspace_id)


@router.get("/paw-bar/widgets/{widget_id}/events", response_model=EventsListResponse)
async def list_events(
    widget_id: str,
    limit: int = Query(100, ge=1, le=500),
    x_paw_bar_token: str | None = Header(default=None, alias="X-Paw-Bar-Token"),
) -> EventsListResponse:
    widget = await _store().get_widget(widget_id)
    if widget is None:
        raise HTTPException(404, "Widget not found")
    _require_owner_token(widget, x_paw_bar_token)
    events = await _store().recent_events(widget_id, limit=limit)
    return EventsListResponse(events=events, total=len(events))


# ---------------------------------------------------------------------------
# Admin: per-Site concierge settings (D1 / SS-6)
#
# The kill switch + greeting live on the SITE doc (not the widget), so the owner
# read/update is keyed on ``site_id``, not a widget id. Auth mirrors the widget
# admin CRUD above: ``require_scope("admin")`` (a signed-in dashboard session) +
# the caller's ACTIVE workspace via ``current_workspace_id``. The lookup is
# workspace-scoped, so another tenant's site id resolves to 404 and never leaks or
# mutates. Reads/writes touch ONLY the concierge fields (the kill switch, the
# greeting, and the transcript-retention toggle) — the site's publish / billing /
# capture config is out of scope here. Co-located with the paw-bar
# enforcement (the frame/chat/action gates) rather than the heavier sites control
# plane so the owner surface and the switch it drives sit in one place.
# ---------------------------------------------------------------------------


class ConciergeSettingsUpdate(BaseModel):
    """Partial update of a Site's concierge settings (D1).

    Every field is optional; only the ones the client SENDS are written (tracked
    via ``model_fields_set``), so a PATCH that carries just ``concierge_enabled``
    leaves the greeting untouched and vice-versa.
    """

    concierge_enabled: bool | None = None
    concierge_greeting: str | None = None
    # Retention switch for the VISITOR half of a transcript. Off means the
    # concierge keeps working and keeps storing its own replies, but the visitor's
    # words are never written down. Turning it off does NOT purge what is already
    # stored — that is a delete operation, not a settings change.
    concierge_store_transcripts: bool | None = None


class ConciergeSettingsResponse(BaseModel):
    """The owner-facing view of a Site's concierge settings (D1)."""

    site_id: str
    concierge_enabled: bool
    concierge_greeting: str
    concierge_store_transcripts: bool


async def _load_site_scoped(site_id: str, workspace_id: str) -> Any:
    """Load a Site by id, scoped to the caller's workspace (cross-tenant → 404).

    Mirrors ``sites.service._load`` (the canonical tenant-scoped Site reader) but
    raises the router's ``HTTPException(404)`` instead of the service's domain
    ``NotFound`` — a malformed/foreign/absent id is all one 404 so nothing leaks
    across tenants. Kept local so the paw-bar router doesn't reach into the sites
    service's private helper or its exception-translation layer.
    """
    from bson import ObjectId
    from bson.errors import InvalidId

    from pocketpaw_ee.cloud.models.site import Site

    try:
        oid = ObjectId(site_id)
    except (InvalidId, TypeError):
        raise HTTPException(404, "Site not found")
    site = await Site.find_one({"_id": oid, "workspace": workspace_id})
    if site is None:
        raise HTTPException(404, "Site not found")
    return site


@router.get(
    "/paw-bar/admin/site/{site_id}/settings",
    response_model=ConciergeSettingsResponse,
    dependencies=[Depends(require_scope("admin"))],
)
async def get_site_concierge_settings(
    site_id: str,
    workspace_id: str = Depends(current_workspace_id),
) -> ConciergeSettingsResponse:
    """Read a Site's concierge settings so the dashboard can render the toggle +
    greeting field. Admin-authed, workspace-scoped (cross-tenant id → 404)."""
    site = await _load_site_scoped(site_id, workspace_id)
    return ConciergeSettingsResponse(
        site_id=str(site.id),
        concierge_enabled=site.concierge_enabled,
        concierge_greeting=site.concierge_greeting,
        concierge_store_transcripts=site.concierge_store_transcripts,
    )


@router.patch(
    "/paw-bar/admin/site/{site_id}/settings",
    response_model=ConciergeSettingsResponse,
    dependencies=[Depends(require_scope("admin"))],
)
async def update_site_concierge_settings(
    site_id: str,
    req: ConciergeSettingsUpdate,
    workspace_id: str = Depends(current_workspace_id),
) -> ConciergeSettingsResponse:
    """Toggle the kill switch and/or set the greeting on a Site (D1 / SS-6).

    Only the fields the client SENT are applied (``model_fields_set``). Admin-authed
    and workspace-scoped, so a cross-tenant site id 404s before anything is written.
    The change is read on the NEXT public request (the gates re-``find_one`` the Site
    every time), so toggling ``concierge_enabled`` off silences the bar immediately.
    """
    site = await _load_site_scoped(site_id, workspace_id)
    # Track whether this PATCH SETS the concierge on, so we can auto-provision the
    # dedicated agent for a site whose widget is still unbound (the owner enabling
    # the concierge is a natural provision point, alongside widget-create). We fire
    # on ANY PATCH that sets concierge_enabled=true, NOT only a false->true
    # transition: the E2 dashboard's one-click "create dedicated agent" re-PATCHes
    # {concierge_enabled: true} on an already-enabled site as its provision hook, so
    # a transition guard would leave that path no way in. It stays cheap + correct
    # because provision_on_concierge_enable only acts on an UNBOUND widget and
    # ensure_site_agent is idempotent, so a re-PATCH on a bound site is a no-op.
    enabling = "concierge_enabled" in req.model_fields_set and req.concierge_enabled is True
    for name in req.model_fields_set:
        value = getattr(req, name)
        if value is not None:
            setattr(site, name, value)
    await site.save()

    # Concierge-enable provisioning trigger (feat/site-dedicated-agent). Failure-
    # soft: a provisioning error logs and never fails this settings PATCH.
    if enabling:
        from pocketpaw_ee.paw_bar.agent_provisioning import provision_on_concierge_enable

        await provision_on_concierge_enable(site, workspace_id)

    return ConciergeSettingsResponse(
        site_id=str(site.id),
        concierge_enabled=site.concierge_enabled,
        concierge_greeting=site.concierge_greeting,
        concierge_store_transcripts=site.concierge_store_transcripts,
    )


# ---------------------------------------------------------------------------
# Admin: per-Site concierge KNOWLEDGE (site content → the pocket KB it reads)
#
# A concierge answers from ONE scope, ``pocket:<pocket_id>``, so if nothing put the
# site's own pages there the agent is live but knows nothing about the business it
# fronts. The sync runs automatically on publish and on agent provisioning; these
# two endpoints are the owner's window into it — what the concierge currently
# knows, and a way to re-run the sync without re-publishing.
#
# Gates: the read uses ``paw_bar.read`` and the sync ``paw_bar.manage`` (both
# ADMIN). The sync is a mutation that spends compute, so it does not ride the read.
# ---------------------------------------------------------------------------

_require_paw_bar_manage = require_action("paw_bar.manage", workspace_dep=current_workspace_id)


class ConciergeKnowledgeResponse(BaseModel):
    """What a site's concierge currently knows, and how the last sync went.

    ``status`` is "" for a clean sync; otherwise a stable machine code the dashboard
    can turn into a sentence: ``never_synced`` (nothing has run yet), ``no_content``
    (the pocket holds no ingestable pages — the case for a foreign site we do not
    host), ``pocket_unavailable`` or ``sync_failed``.
    """

    site_id: str
    article_count: int
    synced_at: str
    status: str
    ingested: int = 0
    removed: int = 0
    skipped: int = 0


def _knowledge_response(site: Any, report: Any = None) -> ConciergeKnowledgeResponse:
    synced_at = getattr(site, "kb_synced_at", None)
    status = getattr(site, "kb_sync_error", "") or ""
    if not synced_at and not status:
        status = "never_synced"
    return ConciergeKnowledgeResponse(
        site_id=str(site.id),
        article_count=len(getattr(site, "kb_article_ids", None) or []),
        synced_at=synced_at.isoformat() if synced_at else "",
        status=status,
        ingested=getattr(report, "ingested", 0) or 0,
        removed=getattr(report, "removed", 0) or 0,
        skipped=getattr(report, "skipped", 0) or 0,
    )


@router.get(
    "/paw-bar/admin/site/{site_id}/knowledge",
    response_model=ConciergeKnowledgeResponse,
    dependencies=[Depends(_require_paw_bar_read)],
)
async def get_site_knowledge(
    site_id: str,
    workspace_id: str = Depends(current_workspace_id),
) -> ConciergeKnowledgeResponse:
    """How much of this site the concierge can actually quote. Workspace-scoped."""
    site = await _load_site_scoped(site_id, workspace_id)
    return _knowledge_response(site)


@router.post(
    "/paw-bar/admin/site/{site_id}/knowledge/sync",
    response_model=ConciergeKnowledgeResponse,
    dependencies=[Depends(_require_paw_bar_manage)],
)
async def sync_site_knowledge_now(
    site_id: str,
    workspace_id: str = Depends(current_workspace_id),
) -> ConciergeKnowledgeResponse:
    """Re-read the site's pages into the KB its concierge answers from.

    Awaited rather than backgrounded, unlike the publish and provisioning triggers:
    the owner clicked a button and needs the result, not a promise. It is idempotent
    (re-ingesting a page versions its article rather than duplicating it) and prunes
    the articles pages that no longer exist left behind, so pressing it twice is
    harmless. Never raises on a sync failure — the failure is reported in ``status``
    so the dashboard can show it instead of a stack trace.
    """
    from pocketpaw_ee.sites.kb_ingest import safe_sync_site_knowledge

    site = await _load_site_scoped(site_id, workspace_id)
    report = await safe_sync_site_knowledge(site)
    return _knowledge_response(site, report)


# ---------------------------------------------------------------------------
# Admin: per-Site concierge aggregation reads (D2)
#
# Read-only aggregation over ONE site's paw-bar operational data, for the
# paw-enterprise Concierge dashboard. OWNER endpoints (admin session +
# ``current_workspace_id``), NOT the public visitor surface. Every read is
# scoped to the caller's ACTIVE workspace at TWO gates:
#   1. the Site is loaded workspace-scoped (``_load_site_scoped`` — cross-tenant
#      id → 404, never leaks existence), then
#   2. its paw-bar widget is resolved from ``Site.pocket_id`` ALSO workspace-scoped
#      (``list_widgets(pocket_id=…, workspace_id=…)``), so the widget in hand
#      always belongs to the caller's tenant.
# The decisions / conversations / handoffs reads then filter to THAT widget /
# pocket — never pocket-wide, never workspace-wide — so a sibling site or a
# second widget in the same workspace can never appear in this site's data. This
# widget/site-scoping is the leak surface the security review checks.
#
# Data sources (all reuse existing stores — nothing new is written here):
#   * decisions  — the paw_bar ``DecisionStatus`` table (get_paw_bar_store, a
#       SINGLETON store), filtered ``WHERE widget_id = ?``. This is the 1:1 mirror
#       of the Instinct proposals the decision loop raises (parked by
#       ``instinct_action_id``), but keyed on a real ``widget_id`` column and
#       served from ONE file regardless of deployment mode. It is chosen over
#       querying the Instinct store directly because (a) paw-bar stamps the
#       Instinct row's in-row ``workspace_id`` with the widget OWNER
#       (``decision_loop.resolve_workspace_id`` → ``widget.owner``), NOT the
#       physical dashboard workspace, so a workspace-scoped Instinct read would
#       hide every row, and (b) the Instinct proposal's physical file is
#       ContextVar-dependent (per-workspace on the run path, shared on the ingest
#       path) — the singleton DecisionStatus table has neither hazard, giving an
#       airtight ``WHERE widget_id = ?`` isolation seam.
#   * conversations — concierge runs are persisted as ``ChatRunDoc`` (context_type
#       "concierge", scope_id = the site's pocket). LISTABLE: the model's compound
#       (workspace, context_type, scope_id, createdAt DESC) index backs an
#       efficient per-site query; a Site is 1:1 with (workspace, pocket_id), so a
#       pocket-scoped read IS site-scoped. Runs are grouped by ``user_id``
#       (customer_ref) into one conversation each. No full-collection scan — the
#       fetch window is bounded.
#   * handoffs — the ``_paw_handoffs`` reserved Fabric object (SS-6). NO producer
#       exists yet, so v1 defines the shape (contact / question / transcript_ref /
#       created_at) and queries Fabric for objects of that type carrying this
#       widget's id — an empty list until the capture path ships (deferred). The
#       widget-id property filter is the same cross-site isolation guarantee.
# Overview counts are cheap (COUNT / distinct) — never load a full list.
# ---------------------------------------------------------------------------


# The reserved Fabric object type for a human-handoff request (SS-6). No producer
# yet; v1 only READS it. The shape below is the contract a future capture path
# must write (each field is a Fabric object property; ``widget_id`` is the scope
# key this read filters on).
_PAW_HANDOFFS_TYPE = "_paw_handoffs"

# The concierge run marker on ``ChatRunDoc`` — a concierge dispatch stamps
# ``context_type="concierge"`` and ``scope_id=<pocket_id>`` (see ``concierge_chat``).
_CONCIERGE_CONTEXT_TYPE = "concierge"

# Preview length for a conversation's last message — enough for the dashboard row
# without shipping the whole transcript.
_CONVERSATION_PREVIEW_CHARS = 140

# Upper bound on how many raw runs a single conversations page scans before
# grouping — keeps the read bounded (never a full-collection scan) while leaving
# room to dedupe several runs per customer down to ``limit`` conversations.
_CONVERSATION_SCAN_CAP = 200

# Max turns returned in one conversation transcript (the most recent N, presented
# oldest-first). Bounds the drill-in read; a long-running visitor conversation
# never ships the whole history in one response.
_TRANSCRIPT_CAP = 200

# Max characters of a visitor's own message persisted on the run doc for the
# transcript. The agent still receives the FULL message — this caps only what we
# write down, so a pasted wall of text (or a deliberate storage-stuffing attempt)
# can't grow the run collection without bound. Generous enough that a real
# question is never clipped.
_STORED_USER_TEXT_CHARS = 4000

# ---------------------------------------------------------------------------
# Concierge conversation memory — the bounds on rehydrated history
#
# Every concierge turn replays the visitor's PRIOR turns into the run so the
# agent remembers what was already said. Unbounded that would be both a cost
# problem (the whole conversation is re-sent, and re-billed, on every turn) and
# an abuse vector (a visitor could grow the prompt indefinitely by pasting).
# Three bounds, each closing a different hole:
# ---------------------------------------------------------------------------

# How many prior EXCHANGES (a run = the visitor's line + the agent's reply) are
# replayed. The most recent N, presented oldest-first. A site conversation is
# short by nature — a dozen exchanges covers a full support or sales back-and-
# forth, and it keeps the per-turn prompt cost flat instead of growing linearly.
_HISTORY_TURN_CAP = 12

# Max characters of any SINGLE replayed line. A long agent reply would otherwise
# eat the whole budget below and evict every other turn; clipping it means the
# newest exchange ALWAYS fits (2 x this < the total below), so memory can never
# degrade to nothing because of one verbose turn. The clip affects the replayed
# copy only — the stored transcript the owner reads is untouched.
_HISTORY_MESSAGE_CHARS = 2000

# Total characters across all replayed lines (~3k tokens). This is the real
# ceiling on what a visitor can force us to re-send every turn. Turns are fitted
# newest-first and the budget cuts off the OLDEST ones, so what survives is
# always a contiguous run of the most recent conversation, never a gappy one.
_HISTORY_TOTAL_CHARS = 12000


class AdminWidgetView(BaseModel):
    """The site's paw-bar widget as the owner dashboard needs it (D2 overview)."""

    id: str
    spec: PawBarSpec
    agent_id: str = ""
    # The bound agent's display name (feat/site-dedicated-agent, E2). Resolved from
    # the agents service when ``agent_id`` is set so the dashboard card can show the
    # concierge name and detect the "<x> Concierge" dedicated pattern. Empty when the
    # widget is unbound OR the agent no longer resolves (a dangling agent_id degrades
    # to "" rather than 500-ing the overview).
    agent_name: str = ""


class OverviewCounts(BaseModel):
    """Cheap per-site counters for the dashboard header (D2)."""

    conversations: int = 0
    pending_decisions: int = 0
    handoffs: int = 0


class SiteOverviewResponse(BaseModel):
    """GET /paw-bar/admin/site/{id}/overview payload (D2).

    ``widget`` is ``None`` when the site has no paw-bar widget yet (a published
    site whose concierge widget was never created) — the toggle + greeting still
    render off the Site, and every count is 0.
    """

    widget: AdminWidgetView | None = None
    enabled: bool
    greeting: str
    # The third owner setting, alongside ``enabled`` and ``greeting``: whether the
    # visitor's own messages are stored. Carried here so the dashboard renders all
    # three from the one call it already makes rather than a second round trip for
    # a single boolean.
    store_transcripts: bool = True
    counts: OverviewCounts


class ConversationItem(BaseModel):
    customer_ref: str
    last_message_at: str
    preview: str


class ConversationsResponse(BaseModel):
    """GET /paw-bar/admin/site/{id}/conversations payload (D2).

    ``unsupported`` stays False on this deployment: concierge runs ARE listable
    (the ChatRunDoc compound index backs the per-site query). The field is part of
    the frozen contract so the frontend degrades gracefully if a future backend
    can't serve the list. ``cursor`` is the ISO ``createdAt`` to page older
    conversations from; ``None`` when the scan reached the end.
    """

    items: list[ConversationItem] = Field(default_factory=list)
    cursor: str | None = None
    unsupported: bool = False


class TranscriptMessage(BaseModel):
    """One message in a conversation transcript (D2 drill-in).

    ``role`` is "user" or "assistant" per the frozen contract. Both roles are now
    real: the agent reply comes from ``ChatRunDoc.partial_text`` and the visitor's
    own line from ``ChatRunDoc.user_text``. A site whose owner turned
    ``concierge_store_transcripts`` off stores no visitor lines, so its transcripts
    are assistant-only — the same shape this DTO always had, just missing one role.
    """

    role: str
    content: str
    created_at: str


class ConversationTranscriptResponse(BaseModel):
    customer_ref: str
    messages: list[TranscriptMessage] = Field(default_factory=list)
    count: int


class DecisionItem(BaseModel):
    id: str
    verb_or_kind: str
    summary: str
    status: str
    created_at: str


class DecisionsResponse(BaseModel):
    items: list[DecisionItem] = Field(default_factory=list)


class HandoffItem(BaseModel):
    contact: str
    question: str
    transcript_ref: str
    created_at: str


class HandoffsResponse(BaseModel):
    items: list[HandoffItem] = Field(default_factory=list)


async def _resolve_site_and_widget(
    site_id: str, workspace_id: str
) -> tuple[Any, PawBarWidget | None]:
    """Resolve a site id → (Site, its paw-bar widget) both workspace-scoped (D2).

    Step 1 loads the Site scoped to the caller's workspace (cross-tenant / bad id
    → 404, via the shared ``_load_site_scoped``). Step 2 resolves the site's
    paw-bar widget from ``Site.pocket_id``, ALSO workspace-scoped, so the returned
    widget always belongs to the caller's tenant. Returns ``widget=None`` when the
    site has no paw-bar widget yet (the concierge was never wired) — the callers
    degrade to an empty view rather than 404, because the SITE exists and its
    owner-facing settings are still meaningful.
    """
    site = await _load_site_scoped(site_id, workspace_id)
    # Belt-and-suspenders (security review finding #2): never resolve a widget on
    # an EMPTY pocket_id. ``list_widgets`` drops the pocket filter on a falsy
    # pocket_id, so a bad write that ever left ``Site.pocket_id`` blank would
    # otherwise match the workspace's FIRST widget — a sibling site's concierge.
    # A site with no pocket has no concierge widget by definition; return None.
    if not site.pocket_id:
        return site, None
    widgets = await _store().list_widgets(
        pocket_id=site.pocket_id, workspace_id=workspace_id, limit=1
    )
    widget = widgets[0] if widgets else None
    return site, widget


def _decision_verb_or_kind(event_type: str) -> str:
    """Map a parked decision's ``event_type`` to the contract's ``verb_or_kind``.

    A gated concierge action parks ``event_type="paw_bar_action:<verb>"`` (see
    ``decision_loop.propose_customer_action``) → return ``<verb>``. Any other
    event is an ingest customer reply → return ``"customer_reply"``.
    """
    if event_type.startswith("paw_bar_action:"):
        return event_type.split(":", 1)[1] or "action"
    return "customer_reply"


def _decision_summary(decision: Any) -> str:
    """One-line owner-facing summary of a parked decision (D2 decisions list).

    Once a human has answered (delivered / declined), the operator's reply is the
    most useful summary; while pending, describe the request from its
    ``verb_or_kind``. Kept terse — the dashboard row is a glance, not the detail
    view.
    """
    reply = str(getattr(decision, "reply", "") or "").strip()
    if reply:
        return reply
    kind = _decision_verb_or_kind(str(getattr(decision, "event_type", "") or ""))
    if kind == "customer_reply":
        return "Customer request awaiting a reply"
    return f"Visitor '{kind}' action awaiting a decision"


@router.get(
    "/paw-bar/admin/site/{site_id}/overview",
    response_model=SiteOverviewResponse,
    dependencies=[Depends(_require_paw_bar_read)],
)
async def get_site_overview(
    site_id: str,
    workspace_id: str = Depends(current_workspace_id),
) -> SiteOverviewResponse:
    """Aggregate a site's concierge widget, kill-switch state, and cheap counts.

    Admin-authed, workspace-scoped (cross-tenant site id → 404). Counts are cheap
    (COUNT / distinct) and each is scoped to THIS site's widget / pocket — never
    pocket-wide or workspace-wide. Every count degrades to 0 on a missing widget
    or an unavailable backing store rather than failing the whole overview.
    """
    site, widget = await _resolve_site_and_widget(site_id, workspace_id)

    counts = OverviewCounts()
    widget_view: AdminWidgetView | None = None
    if widget is not None:
        widget_view = AdminWidgetView(
            id=widget.id,
            spec=widget.spec,
            agent_id=widget.agent_id,
            agent_name=await _bound_agent_name(widget.agent_id),
        )
        counts.pending_decisions = await _store().count_pending_decisions(widget.id)
        counts.handoffs = await _count_handoffs(widget.id, workspace_id)
    # Conversations are pocket-scoped (a Site is 1:1 with its pocket), so the
    # count stands even when the widget row is absent.
    counts.conversations = await _count_conversations(site.pocket_id, workspace_id)

    return SiteOverviewResponse(
        widget=widget_view,
        enabled=site.concierge_enabled,
        greeting=site.concierge_greeting,
        store_transcripts=site.concierge_store_transcripts,
        counts=counts,
    )


@router.get(
    "/paw-bar/admin/site/{site_id}/conversations",
    response_model=ConversationsResponse,
    dependencies=[Depends(_require_paw_bar_read)],
)
async def get_site_conversations(
    site_id: str,
    limit: int = Query(20, ge=1, le=100),
    cursor: str | None = Query(None),
    workspace_id: str = Depends(current_workspace_id),
) -> ConversationsResponse:
    """Recent concierge conversations for a site, newest first (D2).

    Concierge runs persist as ``ChatRunDoc`` (context_type "concierge",
    scope_id = the site's pocket). The compound (workspace, context_type,
    scope_id, createdAt) index backs an efficient per-site query, so this is
    LISTABLE (``unsupported`` stays False). Runs are grouped by customer_ref into
    one conversation each (most-recent run wins for the preview + timestamp). The
    scan window is bounded (never a full-collection read). Cross-site isolation:
    scope_id is the site's OWN pocket, so a sibling site's runs never match.
    """
    site = await _load_site_scoped(site_id, workspace_id)
    return await _list_conversations(site.pocket_id, workspace_id, limit=limit, cursor=cursor)


@router.get(
    "/paw-bar/admin/site/{site_id}/conversations/{customer_ref}",
    response_model=ConversationTranscriptResponse,
    dependencies=[Depends(_require_paw_bar_read)],
)
async def get_site_conversation_transcript(
    site_id: str,
    customer_ref: str,
    workspace_id: str = Depends(current_workspace_id),
) -> ConversationTranscriptResponse:
    """One visitor's concierge transcript on a site, oldest-first (D2 drill-in).

    Admin/owner-authed (``paw_bar.read``), workspace-scoped. Resolves site → widget
    → pocket exactly like the sibling reads, then returns the messages of the
    concierge conversation for ``customer_ref`` on THIS site's pocket
    (``ChatRunDoc`` with context_type "concierge", scope_id = the pocket, user_id =
    customer_ref). Capped at the most recent ``_TRANSCRIPT_CAP`` (200) turns,
    presented oldest-first. 404 when the ref has no concierge conversation here.

    ROLE COVERAGE: both halves of the conversation are here — the visitor's line
    from ``ChatRunDoc.user_text`` and the agent's from ``partial_text``. The
    visitor half is stored only while the site's ``concierge_store_transcripts``
    toggle is on (it is personal data, so the owner controls it); an owner who
    turns it off keeps getting assistant-only transcripts from that point on, and
    lines already stored are NOT retroactively purged.
    """
    if not _CUSTOMER_REF_RE.match(customer_ref or ""):
        raise HTTPException(400, "invalid_customer_ref")
    site, widget = await _resolve_site_and_widget(site_id, workspace_id)
    # No concierge widget on this site → no conversation exists to read.
    if widget is None:
        raise HTTPException(404, "conversation_not_found")
    messages = await _load_transcript(site.pocket_id, customer_ref, workspace_id)
    if messages is None:
        # No concierge run for this (pocket, customer_ref) — the ref has no
        # conversation on this site's widget.
        raise HTTPException(404, "conversation_not_found")
    return ConversationTranscriptResponse(
        customer_ref=customer_ref, messages=messages, count=len(messages)
    )


@router.get(
    "/paw-bar/admin/site/{site_id}/decisions",
    response_model=DecisionsResponse,
    dependencies=[Depends(_require_paw_bar_read)],
)
async def get_site_decisions(
    site_id: str,
    limit: int = Query(50, ge=1, le=200),
    workspace_id: str = Depends(current_workspace_id),
) -> DecisionsResponse:
    """Recent decisions raised on a site's concierge widget, newest first (D2).

    Reads the paw_bar ``DecisionStatus`` rows filtered ``WHERE widget_id = ?`` —
    the airtight cross-site isolation seam (the widget was resolved
    workspace-scoped, so its id belongs to this tenant, and a sibling widget's id
    never matches). Each row maps to {id (the Instinct action id, the Tray join
    key), verb_or_kind, summary, status, created_at}. No widget → empty list.
    """
    _site, widget = await _resolve_site_and_widget(site_id, workspace_id)
    if widget is None:
        return DecisionsResponse(items=[])
    decisions = await _store().list_decisions_for_widget(widget.id, limit=limit)
    items = [
        DecisionItem(
            # Prefer the Instinct action id so the dashboard can deep-link the
            # decision into The Tray; fall back to the row id for a pre-loop row.
            id=d.instinct_action_id or d.id,
            verb_or_kind=_decision_verb_or_kind(d.event_type),
            summary=_decision_summary(d),
            status=d.state.value,
            created_at=d.created_at.isoformat(),
        )
        for d in decisions
    ]
    return DecisionsResponse(items=items)


@router.get(
    "/paw-bar/admin/site/{site_id}/handoffs",
    response_model=HandoffsResponse,
    dependencies=[Depends(_require_paw_bar_read)],
)
async def get_site_handoffs(
    site_id: str,
    limit: int = Query(50, ge=1, le=200),
    workspace_id: str = Depends(current_workspace_id),
) -> HandoffsResponse:
    """Human-handoff requests captured for a site's concierge widget (D2).

    Reads ``_paw_handoffs`` Fabric objects scoped to this widget + workspace. The
    capture path does not exist yet (SS-6, deferred), so this returns an empty but
    well-shaped list in v1. When a producer ships, each object's properties map to
    {contact, question, transcript_ref, created_at}. Cross-site isolation is the
    ``widget_id`` property filter (a sibling widget's handoffs never match).
    """
    _site, widget = await _resolve_site_and_widget(site_id, workspace_id)
    if widget is None:
        return HandoffsResponse(items=[])
    objects = await _query_handoff_objects(widget.id, workspace_id, limit=limit)
    items = [
        HandoffItem(
            contact=str(o.properties.get("contact", "") or ""),
            question=str(o.properties.get("question", "") or ""),
            transcript_ref=str(o.properties.get("transcript_ref", "") or ""),
            created_at=o.created_at.isoformat() if getattr(o, "created_at", None) else "",
        )
        for o in objects
    ]
    return HandoffsResponse(items=items)


@router.get(
    "/paw-bar/admin/site/{site_id}/preview-frame",
    response_class=HTMLResponse,
    dependencies=[Depends(_require_paw_bar_read)],
)
async def get_site_preview_frame(
    site_id: str,
    request: Request,
    workspace_id: str = Depends(current_workspace_id),
) -> HTMLResponse:
    """Serve the concierge bar frame for the OWNER to preview inside the dashboard (D5).

    This is the SESSION-authed sibling of the public GET /paw-bar/frame: same glass
    app, same ``_pawbar_bootstrap_html`` + config builder, but gated by the admin
    role (``paw_bar.read``) instead of the world-visible embed key, and framed by
    the DASHBOARD origin instead of the Site's public ``allowed_origins``. It exists
    so an owner can test their bar in the dashboard without exposing a permissive
    ancestor to the public.

    Differences from the public frame (everything else is identical):
      * Auth: admin/owner role (route-level ``_require_paw_bar_read``), workspace-
        scoped site resolution (cross-tenant / bad id → 404, no widget → 404).
      * CSP ``frame-ancestors`` = the configured dashboard origin
        (``PAWBAR_DASHBOARD_ORIGIN``), sanitized to a single host[:port] — never the
        Site allowlist, never ``*``, never the request's own Origin/Referer.
      * Kill switch: served REGARDLESS of ``concierge_enabled`` so the owner can
        preview a PAUSED bar. Chat/action are unchanged and still obey the kill
        switch, so a paused bar renders in preview but won't answer — correct and
        consistent. The preview iframe is same-origin with the backend, so its
        chat/action calls already satisfy the existing dual-mode Origin gate.
    """
    site, widget = await _resolve_site_and_widget(site_id, workspace_id)
    if widget is None:
        raise HTTPException(status_code=404, detail="no_concierge_widget")

    # The ONLY embedder allowed to frame the preview is the dashboard origin.
    # Reuse the SAME sanitizer the public frame uses on allowed_origins, so the
    # ancestor is reduced to a safe host[:port] with no header injection. Fail
    # closed (500 — a misconfiguration, not a client error) if the configured
    # origin can't be represented; NEVER emit a source-less or wildcard directive.
    dash = _dashboard_origin()
    csp = _frame_ancestors_csp([dash])
    if csp is None:
        raise HTTPException(status_code=500, detail="dashboard_origin_invalid")

    api_base = request.url.path.split("/paw-bar/", 1)[0]
    # E3 — thread the BOUND agent's conversation starters (capped 4). The widget was
    # resolved workspace-scoped above, so reading its agent's starters here is safe
    # (no cross-tenant reach). [] on this branch until the ASG-1 identity fields land.
    config = _pawbar_frame_config(
        site_key=site.signed_key,
        widget_id=widget.id,
        api_base=api_base,
        # The dashboard is the trusted parent; validate it against itself so the
        # glass app's postMessage targetOrigin is a clean scheme://host[:port].
        parent_origin=_safe_parent_origin(dash, [dash]),
        greeting=site.concierge_greeting or "",
        starters=await _bound_agent_starters(widget.agent_id),
    )
    # Preview-only dark page so the transparent bar reads as sitting on the dark
    # dashboard, not a white canvas. The public /paw-bar/frame passes no page_bg
    # (stays transparent over the customer's real site).
    html = _pawbar_bootstrap_html(config, PAWBAR_APP_MOUNT, page_bg=_PREVIEW_PAGE_BG)
    return HTMLResponse(
        content=html,
        headers={"Content-Security-Policy": csp, "Cache-Control": "no-store"},
    )


# --- D2 aggregation data-source helpers -------------------------------------


async def _list_conversations(
    pocket_id: str, workspace_id: str, *, limit: int, cursor: str | None
) -> ConversationsResponse:
    """Group concierge ``ChatRunDoc`` runs into per-customer conversations.

    Fetches a BOUNDED window of the site's concierge runs (index-backed, newest
    first, optionally older than ``cursor``), then dedupes by customer_ref keeping
    the most-recent run per customer. Returns up to ``limit`` conversations plus a
    cursor (the oldest scanned run's timestamp) when the window filled — the
    signal that older conversations remain. Best-effort: a store error degrades to
    an empty, well-shaped payload rather than failing the dashboard.
    """
    from datetime import datetime

    from pocketpaw_ee.cloud.models.chat_run import ChatRunDoc

    try:
        conditions: list[Any] = [
            ChatRunDoc.workspace == workspace_id,
            ChatRunDoc.context_type == _CONCIERGE_CONTEXT_TYPE,
            ChatRunDoc.scope_id == pocket_id,
        ]
        if cursor:
            try:
                conditions.append(ChatRunDoc.createdAt < datetime.fromisoformat(cursor))
            except ValueError:
                pass  # A malformed cursor is ignored — start from the newest run.
        runs = (
            await ChatRunDoc.find(*conditions)
            .sort(-ChatRunDoc.createdAt)  # type: ignore[operator]
            .limit(_CONVERSATION_SCAN_CAP)
            .to_list()
        )
    except Exception:  # noqa: BLE001 — a read failure must not 500 the dashboard
        logger.warning("conversations read failed for pocket %s", pocket_id, exc_info=True)
        return ConversationsResponse(items=[], cursor=None, unsupported=False)

    items: list[ConversationItem] = []
    seen: set[str] = set()
    for run in runs:
        if run.user_id in seen:
            continue
        seen.add(run.user_id)
        when = run.ended_at or run.createdAt
        # Prefer the agent's reply as the row preview (unchanged). Fall back to the
        # visitor's own question when there is no reply — a run that failed or was
        # cut off used to render a blank row, which told the owner nothing; the
        # question at least says what the visitor wanted.
        preview = (run.partial_text or "") or (getattr(run, "user_text", "") or "")
        items.append(
            ConversationItem(
                customer_ref=run.user_id,
                last_message_at=when.isoformat() if when else "",
                preview=preview[:_CONVERSATION_PREVIEW_CHARS],
            )
        )
        if len(items) >= limit:
            break

    # A cursor is offered only when the scan hit its cap (older runs may remain);
    # it is the oldest run we looked at, so the next page continues strictly older.
    next_cursor = (
        runs[-1].createdAt.isoformat() if len(runs) >= _CONVERSATION_SCAN_CAP and runs else None
    )
    return ConversationsResponse(items=items, cursor=next_cursor, unsupported=False)


async def _concierge_runs_for_visitor(
    pocket_id: str, customer_ref: str, workspace_id: str, *, limit: int
) -> list[Any]:
    """One visitor's concierge runs on one site, most-recent first.

    THE per-visitor isolation seam, deliberately in ONE place: both the owner's
    transcript read and the agent's conversation-memory rehydration go through
    this query, so there is no second, drifting definition of "this visitor's
    turns". All four predicates are load-bearing:

      * ``workspace``     — tenant isolation; another tenant's runs never match.
      * ``context_type``  — concierge runs only; an authed pocket/session run on
                            the same pocket is a different conversation.
      * ``scope_id``      — the site's OWN pocket; a sibling site never matches.
      * ``user_id``       — the anonymous customer handle; a sibling VISITOR of
                            the same widget never matches.

    Callers pass ``workspace_id`` / ``pocket_id`` from the authenticated
    authority (the resolved site key or the session's workspace), never from the
    request body. Index-backed and always bounded by ``limit``.
    """
    from pocketpaw_ee.cloud.models.chat_run import ChatRunDoc

    return (
        await ChatRunDoc.find(
            ChatRunDoc.workspace == workspace_id,
            ChatRunDoc.context_type == _CONCIERGE_CONTEXT_TYPE,
            ChatRunDoc.scope_id == pocket_id,
            ChatRunDoc.user_id == customer_ref,
        )
        .sort(-ChatRunDoc.createdAt)  # type: ignore[operator]
        .limit(limit)
        .to_list()
    )


async def _load_concierge_history(
    pocket_id: str, customer_ref: str, workspace_id: str
) -> list[dict[str, str]]:
    """Rehydrate one visitor's prior turns as ``RunSpec.history``.

    The concierge visitor is anonymous and has no ``Message`` rows, so the authed
    surfaces' ``load_history_for_scope`` has nothing to read and every turn was
    answered cold: the agent could not recall a name, an order number, or its own
    previous answer. The stored run docs ARE the transcript, so they are also the
    memory — same rows, same query (``_concierge_runs_for_visitor``), just shaped
    for the model instead of for the dashboard.

    Shape matches ``load_history_for_scope``: ``[{"role", "content"}]``, roles
    "user" / "assistant", oldest-first.

    Bounded by ``_HISTORY_TURN_CAP`` exchanges, ``_HISTORY_MESSAGE_CHARS`` per
    line, and ``_HISTORY_TOTAL_CHARS`` overall. Turns are fitted newest-first and
    the budget cuts the oldest off, so the replay is always the most recent
    CONTIGUOUS stretch of the conversation — an older turn never jumps a newer
    one just because it happens to be shorter.

    The CURRENT turn is not in here: the caller reads before ``create_run``
    writes this turn's doc, so the visitor's message rides in ``RunSpec.content``
    exactly once.

    Failure-soft: any read error degrades to no memory and logs. A visitor's chat
    must not 500 because the run collection hiccuped.
    """
    # An empty handle is not a visitor — every anonymous caller that omitted the
    # ref would otherwise share one bucket and read each other's conversation.
    if not customer_ref:
        return []

    try:
        runs = await _concierge_runs_for_visitor(
            pocket_id, customer_ref, workspace_id, limit=_HISTORY_TURN_CAP
        )

        history: list[dict[str, str]] = []
        budget = _HISTORY_TOTAL_CHARS
        for run in runs:  # newest-first — the newest turns win the char budget
            turn: list[dict[str, str]] = []
            for role, raw in (
                ("user", getattr(run, "user_text", "") or ""),
                ("assistant", getattr(run, "partial_text", "") or ""),
            ):
                line = raw[:_HISTORY_MESSAGE_CHARS]
                if line:
                    turn.append({"role": role, "content": line})
            if not turn:
                # A run that stored neither side (retention off and no reply yet)
                # contributes nothing, and costs nothing.
                continue
            cost = sum(len(m["content"]) for m in turn)
            if cost > budget:
                # Out of budget. Everything left in ``runs`` is OLDER, so stop
                # rather than skip — that is what keeps the replay contiguous.
                break
            budget -= cost
            history = turn + history  # prepend: the result reads oldest-first
        return history
    except Exception:  # noqa: BLE001 — memory is best-effort, the reply is not
        logger.warning(
            "concierge history load failed for pocket %s; answering without memory",
            pocket_id,
            exc_info=True,
        )
        return []


async def _load_transcript(
    pocket_id: str, customer_ref: str, workspace_id: str
) -> list[TranscriptMessage] | None:
    """Build one visitor's concierge transcript, oldest-first (D2 drill-in).

    Fetches this (pocket, customer_ref)'s concierge ``ChatRunDoc`` runs (index-
    backed, most-recent ``_TRANSCRIPT_CAP``), then presents them oldest-first. Each
    run contributes up to TWO messages, in conversation order: the visitor's own
    line (``user_text``) as "user", then the agent reply (``partial_text``) as
    "assistant". Either can be missing and the other still renders — a site with
    ``concierge_store_transcripts`` off has no user lines (assistant-only, exactly
    the old shape), and a run that failed before producing text has no assistant
    line. Both empty and the run contributes nothing.

    Returns ``None`` when the ref has NO concierge run here (the caller 404s) —
    distinct from an empty list, which means runs exist but none carried any text.
    """
    runs = await _concierge_runs_for_visitor(
        pocket_id, customer_ref, workspace_id, limit=_TRANSCRIPT_CAP
    )
    if not runs:
        return None

    messages: list[TranscriptMessage] = []
    for run in reversed(runs):  # oldest-first
        # The visitor's line is stamped at the moment the run was created; the
        # reply is stamped when it finished. Using each one's own timestamp keeps
        # the pair honest about how long the answer took.
        asked = run.createdAt
        answered = run.ended_at or run.createdAt
        user_text = getattr(run, "user_text", "") or ""
        if user_text:
            messages.append(
                TranscriptMessage(
                    role="user",
                    content=user_text,
                    created_at=asked.isoformat() if asked else "",
                )
            )
        reply = run.partial_text or ""
        if reply:
            messages.append(
                TranscriptMessage(
                    role="assistant",
                    content=reply,
                    created_at=answered.isoformat() if answered else "",
                )
            )
    return messages


async def _count_conversations(pocket_id: str, workspace_id: str) -> int:
    """Count DISTINCT concierge customers for a site (D2 overview).

    Scans a BOUNDED window of the site's concierge runs (index-backed, capped at
    ``_CONVERSATION_SCAN_CAP`` — never a full-collection read) and counts distinct
    customer_refs. Bounded rather than exact so a very busy site can't turn the
    overview into an unbounded scan; the badge is a "recent activity" signal, not
    an audited total. Best-effort: any read error degrades to 0.
    """
    try:
        from pocketpaw_ee.cloud.models.chat_run import ChatRunDoc

        runs = (
            await ChatRunDoc.find(
                ChatRunDoc.workspace == workspace_id,
                ChatRunDoc.context_type == _CONCIERGE_CONTEXT_TYPE,
                ChatRunDoc.scope_id == pocket_id,
            )
            .sort(-ChatRunDoc.createdAt)  # type: ignore[operator]
            .limit(_CONVERSATION_SCAN_CAP)
            .to_list()
        )
        return len({run.user_id for run in runs})
    except Exception:  # noqa: BLE001 — an overview count is best-effort
        logger.debug("conversations count failed for pocket %s", pocket_id, exc_info=True)
        return 0


async def _query_handoff_objects(widget_id: str, workspace_id: str, *, limit: int) -> list[Any]:
    """Query ``_paw_handoffs`` Fabric objects for one widget (D2 handoffs).

    Scoped to the widget (a ``widget_id`` object property) AND the workspace. No
    producer exists yet, so this returns [] in v1. Best-effort: a Fabric error
    degrades to an empty list.
    """
    try:
        from pocketpaw.fabric.models import FabricQuery
        from pocketpaw_ee.api import get_fabric_store

        fabric = get_fabric_store(workspace_id=workspace_id)
        result = await fabric.query(
            FabricQuery(
                type_name=_PAW_HANDOFFS_TYPE,
                filters={"widget_id": widget_id},
                limit=limit,
            ),
            workspace_id=workspace_id,
        )
        return list(result.objects)
    except Exception:  # noqa: BLE001 — a read failure must not 500 the dashboard
        logger.warning("handoffs read failed for widget %s", widget_id, exc_info=True)
        return []


async def _count_handoffs(widget_id: str, workspace_id: str) -> int:
    """Cheap COUNT of a widget's ``_paw_handoffs`` objects (D2 overview).

    Reuses the scoped Fabric query but reads only its ``total`` (a COUNT(*)) — the
    row payload isn't materialized. 0 in v1 (no producer). Best-effort → 0.
    """
    try:
        from pocketpaw.fabric.models import FabricQuery
        from pocketpaw_ee.api import get_fabric_store

        fabric = get_fabric_store(workspace_id=workspace_id)
        result = await fabric.query(
            FabricQuery(type_name=_PAW_HANDOFFS_TYPE, filters={"widget_id": widget_id}, limit=1),
            workspace_id=workspace_id,
        )
        return result.total
    except Exception:  # noqa: BLE001 — an overview count is best-effort
        logger.debug("handoffs count failed for widget %s", widget_id, exc_info=True)
        return 0


# ---------------------------------------------------------------------------
# Public spec serving (CORS-enforced)
# ---------------------------------------------------------------------------


@router.get("/paw-bar/spec/{widget_id}")
async def get_spec(
    widget_id: str,
    request: Request,
) -> JSONResponse:
    """Public spec endpoint consumed by the widget.js bundle.

    CORS is enforced per-widget: the response carries
    `Access-Control-Allow-Origin` set to the inbound Origin only when it
    matches the widget's allowlist. Any other origin gets a 403 — browsers
    would block the fetch anyway, but failing explicitly makes misconfigs
    loud instead of silent.
    """
    widget = await _store().get_widget(widget_id)
    if widget is None:
        raise HTTPException(404, "Widget not found")

    origin = request.headers.get("origin")
    if not _origin_allowed(widget, origin):
        raise HTTPException(403, "Origin not allowed for this widget")

    headers: dict[str, str] = {}
    if origin:
        headers["Access-Control-Allow-Origin"] = origin
        headers["Vary"] = "Origin"
    return JSONResponse(widget.spec.model_dump(), headers=headers)


# ---------------------------------------------------------------------------
# Event ingest
# ---------------------------------------------------------------------------


class IngestPayload(BaseModel):
    type: str
    payload: dict[str, Any] = Field(default_factory=dict)
    customer_ref: str


@router.post("/paw-bar/events/{widget_id}", response_model=EventIngestResponse)
async def ingest_event(
    widget_id: str,
    body: IngestPayload,
    request: Request,
) -> EventIngestResponse:
    """Inbound customer event.

    Enforces (in order):
    1. Widget exists.
    2. Origin is on the widget's allowlist.
    3. Payload size is under MAX_PAYLOAD_BYTES.
    4. Rate limits (overall + per customer_ref).
    5. Injection screening: the stringified payload is run through the
       heuristic InjectionScanner and dropped on a HIGH-or-higher threat
       (degrades cleanly to accept when the security stack is absent).
    After that, the event is persisted and — if the widget has a matching
    `event_mapping` — a Fabric object is created.

    gap2 — when the event maps to a Fabric object, ingest ALSO raises an
    Instinct proposal carrying the event context (best-effort) so a human can
    decide and the decision is delivered back via the poll endpoint. This is the
    open-the-loop half; the human decides on the existing Instinct surface and
    deliver_customer_decision closes it.
    """
    store = _store()
    widget = await store.get_widget(widget_id)
    if widget is None:
        raise HTTPException(404, "Widget not found")

    origin = request.headers.get("origin")
    if not _origin_allowed(widget, origin):
        raise HTTPException(403, "Origin not allowed for this widget")

    event = PawBarEvent(
        widget_id=widget_id,
        type=body.type,
        payload=body.payload,
        customer_ref=body.customer_ref,
    )

    if event.payload_size() > MAX_PAYLOAD_BYTES:
        raise HTTPException(413, "Payload exceeds 4KB cap")

    ok = await store.within_rate_limit(
        widget_id,
        overall_per_min=widget.rate_limit_per_min,
        per_customer_per_min=widget.per_customer_limit_per_min,
        customer_ref=event.customer_ref,
    )
    if not ok:
        raise HTTPException(429, "Rate limit exceeded")

    if not await _screen_event_for_injection(event):
        return EventIngestResponse(accepted=False, reason="injection_rejected")

    await store.record_event(event)
    fabric_object_id = await _apply_event_mapping(widget, event)

    # gap2 — open the customer decision loop. Only events the widget actually
    # maps (a real, recognized customer request, not arbitrary telemetry) raise
    # a proposal, so we don't flood The Tray with noise. Best-effort: a loop
    # failure never fails this ingest response — the event + Fabric object have
    # already persisted.
    instinct_action_id: str | None = None
    if widget.event_mapping.get(event.type) is not None:
        instinct_action_id = await _open_decision_loop(widget, event, store)

    return EventIngestResponse(
        accepted=True,
        event=event,
        fabric_object_id=fabric_object_id,
        instinct_action_id=instinct_action_id,
    )


# ---------------------------------------------------------------------------
# Customer decision poll (public, CORS-enforced) — the back-half of the loop
# ---------------------------------------------------------------------------


@router.get("/paw-bar/events/{widget_id}/decision/{customer_ref}")
async def get_decision(
    widget_id: str,
    customer_ref: str,
    request: Request,
) -> JSONResponse:
    """Public endpoint the rendered widget polls to read the owner's decision.

    The widget posted an event (which may have raised an Instinct proposal);
    this returns the latest decision for ``(widget_id, customer_ref)``:
    ``pending`` while a human hasn't decided, then ``delivered`` (with the reply)
    on approval or ``declined`` on rejection.

    Auth model matches the public spec/ingest endpoints: no owner credential —
    the row is scoped to the customer's own ``customer_ref`` on a specific
    widget, which is all the embedded widget knows. CORS is enforced per-widget
    exactly as on the spec endpoint so only allowlisted origins can read it.
    """
    store = _store()
    widget = await store.get_widget(widget_id)
    if widget is None:
        raise HTTPException(404, "Widget not found")

    origin = request.headers.get("origin")
    if not _origin_allowed(widget, origin):
        raise HTTPException(403, "Origin not allowed for this widget")

    decision = await store.get_latest_decision(widget_id, customer_ref)
    headers: dict[str, str] = {}
    if origin:
        headers["Access-Control-Allow-Origin"] = origin
        headers["Vary"] = "Origin"

    if decision is None:
        body = DecisionStatusResponse(found=False)
    else:
        body = DecisionStatusResponse(
            found=True,
            state=decision.state.value,
            reply=decision.reply,
            decided_by=decision.decided_by,
            updated_at=decision.updated_at.isoformat(),
        )
    return JSONResponse(body.model_dump(), headers=headers)


# ---------------------------------------------------------------------------
# Public concierge chat (SSE) — T2
#
# A PUBLIC, anonymous, streaming chat endpoint. The visitor's embed key
# (Site.signed_key) is the only credential — there is NO signed-in user. Every
# authority comes from the RESOLVED Site scope (resolve_site_key), and the run is
# bound to the Site's pocket + the widget's agent. It drives the SAME run
# machinery the authenticated dashboard chat uses (create_run + executor.submit
# + execute_run + transport) via a CONCIERGE-scoped RunSpec — no new SSE loop, no
# new executor, no new transport. The grounding guard (deny web/code/write tools,
# lock KB to pocket:<id>) is enforced by the CONCIERGE SurfaceProfile + scope, not
# here; this handler owns the front-gate + auth + dispatch.
# ---------------------------------------------------------------------------


class ConciergeChatRequest(BaseModel):
    widget_id: str
    # The public, origin-bound embed key (Site.signed_key) baked into the widget.
    signed_key: str
    # The anonymous, widget-minted customer handle — a session / rate-limit key,
    # NEVER an authenticated principal.
    customer_ref: str
    message: str


def _sse(event: str, data: dict[str, Any], *, entry_id: str | None = None) -> bytes:
    """Encode one SSE frame — the SAME wire shape ``agent_router._sse`` writes so
    the frontend's EventSource parser (and Last-Event-Id resume) is unchanged.
    Mirrored here rather than imported so the public router doesn't reach into a
    private helper of the authed chat module."""
    head = f"id: {entry_id}\n" if entry_id else ""
    return f"{head}event: {event}\ndata: {json.dumps(data)}\n\n".encode()


@router.post("/paw-bar/chat")
async def concierge_chat(body: ConciergeChatRequest, request: Request) -> StreamingResponse:
    """Stream a concierge reply for a public visitor's message.

    Order (fail-closed, cheap gates first):
      1. Widget exists (404).
      2. Resolve our frame origin (dual-mode origin model — no rejection here; the
         authoritative, fail-closed origin gate is folded into step 5).
      3. Rate limit, overall + per-customer (429).
      4. Injection screen the free-text message; drop on HIGH (400).
      5. Authenticate the embed key + dual-mode origin gate (``resolve_site_key`` —
         401 bad key / 403 disallowed origin, fail-closed).
      6. Bind the widget to the RESOLVED key: the widget must belong to the key's
         workspace AND pocket (403) — a key for pocket A must not drive a widget
         for a sibling pocket B (finding #2).
      7. The widget must have a concierge agent bound (409).
      7b. The pocket must expose NO connectors (409) — public-safe lockdown until
          the claude_sdk untrusted-mode GA fix (a static deny can't strip dynamic
          composio connector ids). Fail-closed on a lookup error too.
      8. Dispatch a CONCIERGE-scoped run over the shared machinery and stream its
         frames back as SSE.
    """
    origin = request.headers.get("origin")
    store = _store()

    # (1) Widget lookup — UNSCOPED: we don't have the workspace until the key is
    # resolved. The workspace/pocket binding is enforced at step 6.
    widget = await store.get_widget(body.widget_id)
    if widget is None:
        raise HTTPException(404, "Widget not found")

    # (2) Origin model — DUAL-MODE (A1). The chat origin gate now converges on the
    # fail-closed ``Site.allowed_origins`` allowlist, enforced at step 5 by
    # ``resolve_site_key``. It is NOT gated on ``widget.allowed_domains`` anymore —
    # that check's empty=allow-all footgun would (a) silently allow any origin for
    # an unconfigured widget and (b) reject OUR frame origin for a configured one,
    # breaking the iframe path. ``frame_origin`` is our configured iframe origin: a
    # request whose Origin equals it was already gated by the frame CSP at render
    # time (iframe mode); any other Origin must be an allowlisted embedder (inline
    # mode). The authoritative, fail-closed decision is made in ``resolve_site_key``.
    frame_origin = _configured_frame_origin(request)

    # (3) Rate limit (reuse the ingest limiter). Counts prior events for this
    # (widget, customer); a recorded chat marker below feeds subsequent checks.
    ok = await store.within_rate_limit(
        body.widget_id,
        overall_per_min=widget.rate_limit_per_min,
        per_customer_per_min=widget.per_customer_limit_per_min,
        customer_ref=body.customer_ref,
    )
    if not ok:
        raise HTTPException(429, "Rate limit exceeded")

    # (4) Injection-screen the untrusted free-text message; drop on HIGH.
    if not await _screen_message_for_injection(body.message, body.widget_id):
        raise HTTPException(400, "message_rejected")

    # (5) Authenticate the embed key + apply the dual-mode origin gate — fail-closed
    # (401 bad/unknown/revoked key, 403 disallowed/missing origin). This is THE
    # credential; there is no user. ``frame_origin`` makes the origin gate accept an
    # iframe-mode request (Origin == our frame, already gated by the frame CSP) while
    # still requiring an inline-mode request's Origin to be on ``Site.allowed_origins``.
    # ``_with_site`` hands back the Site the gate already loaded, so step (8) can
    # read the owner's transcript-retention toggle without a second query.
    from pocketpaw_ee.cloud.auth.site_keys import resolve_site_key_with_site

    ctx, site = await resolve_site_key_with_site(
        body.signed_key, origin, body.customer_ref, frame_origin=frame_origin
    )

    # (6) Bind the widget to the RESOLVED key (finding #2). A legacy '' widget
    # workspace matches any; a non-empty mismatch is refused. The pocket MUST
    # match the key's pocket — the run is bound to ``ctx.pocket_id`` (the
    # authenticated authority), so a widget for a sibling pocket is rejected.
    if widget.workspace_id and widget.workspace_id != ctx.workspace_id:
        raise HTTPException(403, "widget_workspace_mismatch")
    if widget.pocket_id != ctx.pocket_id:
        raise HTTPException(403, "widget_pocket_mismatch")

    # (7) The widget must be bound to a concierge agent (T3 sets agent_id).
    if not widget.agent_id:
        raise HTTPException(409, "widget has no concierge agent")

    # (7b) Fail-closed connector lockdown (pilot posture, captain call 2026-07-14).
    # ``_CONCIERGE_DENY`` strips web/code/write/pocket-write, but composio CONNECTOR
    # tool ids are dynamic/per-workspace and survive the always-allowed ``composio``
    # server, so a static deny can't reach them. Until the GA fix lands, a PUBLIC
    # concierge pocket must expose NO connectors: ``list_pocket_connectors`` reports
    # exactly the connectors this pocket's agent can use (pocket-scoped OR
    # workspace-wide), so if it returns anything, refuse rather than let a
    # prompt-injected visitor reach it. Fail CLOSED — a lookup error refuses too.
    # TODO(GA-blocker): replace this refuse-guard with an untrusted/public lockdown
    # mode in claude_sdk — a ``ScopeKind.CONCIERGE`` run skips the universal grant
    # (POCKET_CREATION_GRANT/WIDGET/ATLAS) AND the ``ALWAYS_ALLOWED_MCP_SERVERS``
    # bypass, so connectors are stripped for real and a concierge pocket CAN safely
    # have connectors. Touches shared tool-gating -> full-suite + flag-mode validation.
    from pocketpaw_ee.cloud.connectors.service import list_pocket_connectors

    try:
        _bound_connectors = await list_pocket_connectors(ctx.workspace_id, ctx.pocket_id or "")
    except Exception:
        logger.warning("concierge connector check failed; refusing fail-closed", exc_info=True)
        raise HTTPException(409, "concierge_connector_check_failed")
    if _bound_connectors:
        raise HTTPException(409, "concierge_pocket_has_connectors")

    # Record a minimal chat marker so the rate limiter counts concierge traffic
    # (the message body is NOT stored here — the assistant reply persists via the
    # run). Best-effort: a store hiccup must not fail the reply.
    try:
        await store.record_event(
            PawBarEvent(
                widget_id=body.widget_id,
                type="concierge_message",
                payload={},
                customer_ref=body.customer_ref,
            )
        )
    except Exception:
        logger.debug("concierge chat marker record failed (non-fatal)", exc_info=True)

    # (8) Dispatch a CONCIERGE run over the SAME machinery the authed chat uses.
    from pocketpaw_ee.cloud.chat.runs import service as run_service
    from pocketpaw_ee.cloud.chat.runs.domain import RunSpec
    from pocketpaw_ee.cloud.chat.runs.executor import get_executor
    from pocketpaw_ee.cloud.chat.runs.transport import get_stream_transport

    run_id = uuid.uuid4().hex
    client_message_id = uuid.uuid4().hex
    # C1 — the widget's declared actions ride surface_meta (JSON-shaped) so the
    # CONCIERGE run allow-lists + surfaces EXACTLY this widget's per-verb tools and
    # binds them onto the per-stream ContextVar the pawbar_actions MCP server builds
    # from. Stamped server-side from the widget spec (never client-supplied). Empty
    # when the widget declares no actions → the concierge stays deny-all as before.
    pawbar_actions = [
        {"verb": a.verb, "policy": a.policy, "args": dict(a.args), "label": a.label}
        for a in (widget.spec.actions or [])
    ]
    # C1 — the catalog also rides surface_meta (capped) so the concierge preamble
    # can name real products, prices, and ids: without it the agent knows the
    # action verbs but not WHAT it sells and declines ("I don't have a list"). Only
    # threaded when actions are declared; the preamble renders a compact block.
    pawbar_catalog = (
        [
            {
                "id": c.id,
                "name": c.name,
                "price_cents": c.price_cents,
                "currency": c.currency,
            }
            for c in (widget.spec.catalog or [])[:_MAX_PREAMBLE_CATALOG]
        ]
        if pawbar_actions
        else []
    )
    # The run is bound to the KEY's pocket (ctx.pocket_id — the authenticated
    # authority), the KEY's workspace, and the widget's agent. ``user_id`` is the
    # anonymous customer handle (session / rate-limit key, never a principal).
    # ``surface="concierge"`` makes execute_run resolve the CONCIERGE
    # SurfaceProfile (tool lockdown); ``context_type="concierge"`` makes it
    # resolve the CONCIERGE scope (KB locked to pocket:<id>).
    #
    # ``persist_user_text`` is the visitor's own line, written onto the run doc so
    # the owner's transcript is a conversation rather than a monologue. The visitor
    # is anonymous and has no Message row, so ``user_message_id`` stays "" and this
    # is the only place that text can live. Gated on the site owner's
    # ``concierge_store_transcripts`` (re-read every turn, so turning it off stops
    # collection on the next message) and length-capped — the agent still gets the
    # full message either way, this governs only what is stored.
    stored_user_text = (
        body.message[:_STORED_USER_TEXT_CHARS] if site.concierge_store_transcripts else ""
    )
    # ``history`` is THIS visitor's prior turns on THIS site (see
    # ``_load_concierge_history``). Read BEFORE ``create_run`` below writes this
    # turn's doc, so the current message rides in ``content`` and appears exactly
    # once. Scoped to (workspace, concierge, pocket, customer_ref) — a sibling
    # visitor's, a sibling site's, and another tenant's turns can never appear.
    #
    # Gated on the SAME retention toggle as the write: an owner who turned
    # transcript storage off gets no memory, because there is nothing stored to
    # remember from and because replaying the agent's half alone would feed it a
    # conversation with the questions missing. That degradation is the owner's
    # privacy choice working, not a bug to route around.
    prior_history = (
        await _load_concierge_history(ctx.pocket_id or "", body.customer_ref, ctx.workspace_id)
        if site.concierge_store_transcripts
        else []
    )
    spec = RunSpec(
        run_id=run_id,
        workspace_id=ctx.workspace_id,
        context_type="concierge",
        scope_id=ctx.pocket_id or "",
        session_key=f"cloud:concierge:{ctx.pocket_id}:{body.customer_ref}:{widget.agent_id}",
        group=None,
        user_id=body.customer_ref,
        agent_id=widget.agent_id,
        client_message_id=client_message_id,
        user_message_id="",
        persist_user_text=stored_user_text,
        content=body.message,
        history=prior_history,
        intent=None,
        attachments=[],
        mentions=[],
        surface="concierge",
        surface_meta={
            "pocket_id": ctx.pocket_id,
            "route_path": "/paw-bar",
            "widget_id": widget.id,
            "pawbar_actions": pawbar_actions,
            "pawbar_catalog": pawbar_catalog,
        },
    )
    run = await run_service.create_run(spec)
    if run.run_id != spec.run_id:
        spec = spec.model_copy(update={"run_id": run.run_id})
    run_id = run.run_id
    await get_executor().submit(spec)

    transport = get_stream_transport()

    async def gen() -> AsyncIterator[bytes]:
        # Mirror agent_router.post_agent_chat's tail: announce the run, then relay
        # the transport frames the executor writes, verbatim, until a terminal one.
        yield _sse(
            "message.persisted",
            {"run_id": run_id, "client_message_id": client_message_id},
        )
        cursor = "0"
        while True:
            saw_terminal = False
            async for ev in transport.read_events(run_id, after=cursor, block_ms=2000):
                cursor = ev.entry_id
                yield _sse(ev.event, ev.data, entry_id=ev.entry_id)
                if ev.is_terminal:
                    saw_terminal = True
            if saw_terminal:
                return
            if await transport.is_cancelled(run_id):
                yield _sse("interrupted", {"reason": "cancelled"})
                return
            yield b": ping\n\n"

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ---------------------------------------------------------------------------
# Public action / cart endpoints (C1) — the visitor commerce loop
#
# Same armor class as concierge chat, factored into ``_front_gate_for_key`` so the
# two endpoints share ONE fail-closed gate (no parallel path). The args are
# structured (verb + typed args, not free text), so there is no injection screen —
# the executor validates every arg against the widget's declared schema. Neither
# endpoint runs the agent, so the agent-bound + connector-lockdown steps that
# concierge chat carries don't apply here.
# ---------------------------------------------------------------------------

# The visitor handle is client-minted (the glass app uses a 128-bit crypto-random
# hex ref). Bound its charset + length server-side so a malformed / oversized ref
# is refused with the same fail-closed shape as the other gate checks. This is a
# cheap bound, NOT the root fix — a server-issued/HMAC-signed handle is a tracked
# follow-up (the 256-bit client ref makes enumeration impractical today).
_CUSTOMER_REF_RE = re.compile(r"^[A-Za-z0-9_-]{8,128}$")


async def _front_gate_for_key(
    *,
    widget_id: str,
    signed_key: str,
    customer_ref: str,
    origin: str | None,
    request: Request,
) -> tuple[PawBarWidget, Any]:
    """The shared public front-gate: resolve the widget + authenticate the key.

    Mirrors ``concierge_chat`` steps 1-6 (fail-closed, cheap gates first):
      0. ``customer_ref`` matches the charset + length bound (400) — cheapest gate.
      1. Widget exists (404) — UNSCOPED (workspace unknown until the key resolves).
      2. Rate limit, overall + per-customer (429).
      3. Authenticate the embed key + dual-mode origin gate (``resolve_site_key`` —
         401 bad/unknown/revoked key, 403 disallowed/missing origin, fail-closed).
      4. Bind the widget to the RESOLVED key: it must belong to the key's workspace
         AND pocket (403) — a key for pocket A must not drive a widget for pocket B.
    Returns ``(widget, ctx)`` where ``ctx.workspace_id`` is the authenticated
    tenant used to scope any gated Instinct proposal."""
    if not _CUSTOMER_REF_RE.match(customer_ref or ""):
        raise HTTPException(400, "invalid_customer_ref")
    store = _store()
    widget = await store.get_widget(widget_id)
    if widget is None:
        raise HTTPException(404, "Widget not found")

    ok = await store.within_rate_limit(
        widget_id,
        overall_per_min=widget.rate_limit_per_min,
        per_customer_per_min=widget.per_customer_limit_per_min,
        customer_ref=customer_ref,
    )
    if not ok:
        raise HTTPException(429, "Rate limit exceeded")

    frame_origin = _configured_frame_origin(request)
    from pocketpaw_ee.cloud.auth.site_keys import resolve_site_key

    ctx = await resolve_site_key(signed_key, origin, customer_ref, frame_origin=frame_origin)

    # Bind the widget to the resolved key (finding #2 — no sibling-pocket reach).
    if widget.workspace_id and widget.workspace_id != ctx.workspace_id:
        raise HTTPException(403, "widget_workspace_mismatch")
    if widget.pocket_id != ctx.pocket_id:
        raise HTTPException(403, "widget_pocket_mismatch")
    return widget, ctx


class PawBarActionRequest(BaseModel):
    # The public embed key + the widget id (named ``key`` / ``w`` to match the
    # frame endpoint's query params and the glass app's action fetcher).
    key: str
    w: str
    customer_ref: str
    verb: str
    args: dict[str, Any] = Field(default_factory=dict)


@router.post("/paw-bar/action")
async def post_action(body: PawBarActionRequest, request: Request) -> JSONResponse:
    """Execute one declared action for a public visitor → {ok, result, cart}.

    Front-gated by ``_front_gate_for_key`` (auth + origin + rate + binding), then
    routed through the SHARED ``execute_action`` — the same code path the concierge
    agent's per-verb tools use. SS-2: a gated verb never executes; it raises an
    Instinct proposal and returns a pending result the visitor polls on the
    decision endpoint. The executor's status hint becomes the HTTP status on
    failure (422 bad verb/args, 409 empty cart / unavailable)."""
    origin = request.headers.get("origin")
    widget, ctx = await _front_gate_for_key(
        widget_id=body.w,
        signed_key=body.key,
        customer_ref=body.customer_ref,
        origin=origin,
        request=request,
    )
    from pocketpaw_ee.paw_bar.actions import execute_action

    outcome = await execute_action(
        widget,
        ctx.workspace_id,
        body.customer_ref,
        body.verb,
        body.args,
        store=_store(),
    )
    if not outcome.ok:
        raise HTTPException(outcome.http_status, outcome.error)
    return JSONResponse({"ok": True, "result": outcome.result, "cart": outcome.cart})


@router.get("/paw-bar/cart")
async def get_cart(
    request: Request,
    key: str = Query("", description="The public Site.signed_key"),
    w: str = Query("", description="The Paw Bar widget id"),
    customer_ref: str = Query("", description="The anonymous visitor handle"),
) -> JSONResponse:
    """Return the visitor's cart → {items, total_cents, currency, checkout_url}.

    Same front-gate as POST /paw-bar/action. Reads the cart via the shared
    ``cart_wire`` serializer (so the shape never drifts from the executor's cart
    results); no cart yet returns an empty cart with the rendered checkout_url."""
    from pocketpaw_ee.paw_bar.actions import cart_wire

    origin = request.headers.get("origin")
    widget, _ctx = await _front_gate_for_key(
        widget_id=w,
        signed_key=key,
        customer_ref=customer_ref,
        origin=origin,
        request=request,
    )
    store = _store()
    # Record a cart-read marker so reads count toward the shared rate limiter —
    # otherwise a read-only enumeration loop is unbounded (the front-gate only
    # CHECKS the limit, nothing was recording for it). Best-effort; a store hiccup
    # must not fail the read.
    try:
        await store.record_event(
            PawBarEvent(widget_id=w, type="pawbar_cart_read", payload={}, customer_ref=customer_ref)
        )
    except Exception:
        logger.debug("cart-read marker record failed (non-fatal)", exc_info=True)
    cart = await store.get_cart(w, customer_ref)
    return JSONResponse(cart_wire(widget, customer_ref, cart))


# ---------------------------------------------------------------------------
# Async decision delivery (2026-07-30) — the visitor who leaves the page
#
# The decision loop's on-page half is the poll endpoint above; this is the
# async half. A visitor whose request is still PENDING can leave an email
# before closing the tab; when the owner decides, the delivery hook emails
# them the SAME customer-facing reply the poll would have shown. Same armor
# class as /paw-bar/chat via the shared ``_front_gate_for_key``. The email is
# row-only PII (see models.DecisionStatus.contact_email) and is NEVER echoed
# back by any public read — the decision poll response omits it by shape.
# ---------------------------------------------------------------------------

# Simple RFC-ish shape check — one local part, one @, a dotted domain. The
# 254-char cap is the SMTP path limit; anything longer is refused outright.
_CONTACT_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_MAX_CONTACT_EMAIL_CHARS = 254


class DecisionContactRequest(BaseModel):
    widget_id: str
    # The public, origin-bound embed key (Site.signed_key) — same credential
    # model as ConciergeChatRequest; there is no signed-in user.
    signed_key: str
    customer_ref: str
    email: str


@router.post("/paw-bar/decision-contact")
async def post_decision_contact(body: DecisionContactRequest, request: Request) -> JSONResponse:
    """Attach a contact email to this visitor's PENDING decision rows.

    Gates mirror ``concierge_chat`` via the shared ``_front_gate_for_key``
    (fail-closed, cheap gates first): widget exists (404) → rate limit (429) →
    embed-key auth + dual-mode origin gate (401/403) → widget∩key binding
    (403). Then the email is validated (422) and stamped onto the visitor's
    pending rows only — a decided row was already answered on-page. Returns
    ``{ok: true, attached: N}``; the address itself is never echoed back here
    or on any other public read.
    """
    origin = request.headers.get("origin")
    widget, _ctx = await _front_gate_for_key(
        widget_id=body.widget_id,
        signed_key=body.signed_key,
        customer_ref=body.customer_ref,
        origin=origin,
        request=request,
    )

    email = body.email.strip()
    if len(email) > _MAX_CONTACT_EMAIL_CHARS or not _CONTACT_EMAIL_RE.match(email):
        raise HTTPException(422, "invalid_email")

    store = _store()
    # Record a marker so contact posts count toward the shared rate limiter
    # (the front gate only CHECKS the limit). Payload stays EMPTY — the email
    # must never land in the event log (PII invariant). Best-effort.
    try:
        await store.record_event(
            PawBarEvent(
                widget_id=widget.id,
                type="pawbar_decision_contact",
                payload={},
                customer_ref=body.customer_ref,
            )
        )
    except Exception:
        logger.debug("decision-contact marker record failed (non-fatal)", exc_info=True)

    # Scope the write with the same workspace value the decision rows were
    # stamped with at propose time (decision_loop.resolve_workspace_id — the
    # widget's real workspace, or the owner label for a legacy row).
    from pocketpaw_ee.paw_bar.decision_loop import resolve_workspace_id

    attached = await store.attach_contact_email(
        widget.id,
        body.customer_ref,
        email,
        workspace_id=resolve_workspace_id(widget) or None,
    )
    return JSONResponse({"ok": True, "attached": attached})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _scan_text_is_safe(text: str, *, source: str) -> bool:
    """Shared InjectionScanner gate — the single screening primitive both the
    structured-event ingest and the free-text concierge-chat paths reuse.

    Runs the heuristic :class:`InjectionScanner` (regex-based, no API key
    required) over ``text`` and returns ``False`` — DROP — when the scan reports
    a ``HIGH`` threat. The HIGH threshold is deliberate: ``MEDIUM`` covers softer
    persona/roleplay phrasing that legitimate input ("act as my travel guide")
    could trip, so screening only the unambiguous HIGH patterns (instruction
    overrides, delimiter attacks, jailbreaks, exfiltration) avoids false-dropping
    real input.

    Degrades cleanly: if the security module can't be imported or the scan
    raises, the input is ACCEPTED (availability over a hard fail on a public
    endpoint). This is the same logic that replaced the old permanent-no-op
    ``getattr(guardian, "check_input")`` Guardian screen.
    """
    try:
        from pocketpaw.security.injection_scanner import (
            ThreatLevel,
            get_injection_scanner,
        )
    except Exception:
        return True

    try:
        scan = get_injection_scanner().scan(text, source=source)
    except Exception:
        logger.debug("Injection scan raised; accepting by default")
        return True

    if scan.threat_level == ThreatLevel.HIGH:
        logger.warning(
            "Dropping paw-bar input from %s — injection threat %s (patterns: %s)",
            source,
            scan.threat_level.value,
            ", ".join(scan.matched_patterns),
        )
        return False
    return True


async def _screen_event_for_injection(event: PawBarEvent) -> bool:
    """Screen the stringified event payload for prompt-injection content.

    Thin wrapper over :func:`_scan_text_is_safe` (the shared scanner gate) on the
    JSON-serialized payload. Behavior is unchanged from before the shared helper
    existed: drop on HIGH, accept otherwise, degrade to accept on any failure.
    """
    payload = json.dumps(event.payload, default=str)
    return await _scan_text_is_safe(payload, source=f"paw_bar:{event.widget_id}")


async def _screen_message_for_injection(message: str, widget_id: str) -> bool:
    """Screen a free-text concierge-chat message for prompt-injection content (T2).

    The concierge chat path is public + unauthenticated-by-user, and the message
    is untrusted free text (not a ≤4KB structured event), so it runs through the
    SAME :func:`_scan_text_is_safe` gate the event ingest uses — dropped on a HIGH
    threat. This is one layer of the concierge guard; the hard controls are the
    tool-denying surface profile and the pocket-locked KB scope.
    """
    return await _scan_text_is_safe(message, source=f"paw_bar_chat:{widget_id}")


async def _open_decision_loop(
    widget: PawBarWidget,
    event: PawBarEvent,
    store: Any,
) -> str | None:
    """Raise an Instinct proposal for a mapped customer event (gap2).

    Thin wrapper over ``decision_loop.propose_customer_decision`` — keeps the
    import lazy (the OSS paw_bar store never reaches into the EE decision-loop
    module) and the failure best-effort: any error is swallowed by the called
    function, and a defensive guard here ensures even an import failure can't
    break the ingest response. Returns the proposed Instinct action id, or None.
    """
    try:
        from pocketpaw_ee.paw_bar.decision_loop import propose_customer_decision

        return await propose_customer_decision(
            widget=widget,
            event=event,
            paw_bar_store=store,
        )
    except Exception:
        logger.warning(
            "decision-loop proposal failed for widget %s (non-fatal)",
            widget.id,
            exc_info=True,
        )
        return None


async def _apply_event_mapping(widget: PawBarWidget, event: PawBarEvent) -> str | None:
    """Turn a PawBarEvent into a Fabric object when a mapping exists."""
    mapping = widget.event_mapping.get(event.type)
    if mapping is None:
        return None

    try:
        from pocketpaw.fabric.models import FabricObject
        from pocketpaw_ee.api import get_fabric_store
    except ImportError:
        return None

    # W4a tenancy — the public ingest path is token-only (no session), so the
    # tenant is derived from the widget ROW: the workspace_id stamped at
    # create time by the admin route. That is a REAL workspace id (unlike the
    # logical, possibly colon-qualified ``owner`` — ``user:maya`` — which fails
    # the store factory's path allowlist and must never be used as a store
    # key). ``or None`` preserves legacy/single-tenant behavior: an unstamped
    # ('' ) row keeps writing to the shared default store exactly as before.
    fabric = get_fabric_store(workspace_id=widget.workspace_id or None)
    if fabric is None:
        return None

    context = {"payload": event.payload, "customer_ref": event.customer_ref}
    properties = {k: _interpolate(v, context) for k, v in mapping.fields.items()}
    try:
        obj = FabricObject(
            type_name=mapping.creates,
            properties=properties,
            source_connector="paw_bar",
            source_id=widget.id,
        )
        created = await fabric.create_object(obj)
        return getattr(created, "id", None)
    except Exception:
        logger.exception("Failed to create Fabric object from paw-bar event")
        return None


def _interpolate(template: str, context: dict[str, Any]) -> Any:
    """Resolve `{{ a.b }}` placeholders against the context dict.

    If the entire template is a single placeholder (`{{ payload.item }}`), the
    raw value is returned (preserving non-string types). Mixed strings fall back
    to stringified substitution.
    """
    full_match = re.fullmatch(r"\{\{\s*([a-zA-Z0-9_.]+)\s*\}\}", template)
    if full_match:
        return _lookup(full_match.group(1), context)

    def _replace(m: re.Match[str]) -> str:
        val = _lookup(m.group(1), context)
        return "" if val is None else str(val)

    return _PLACEHOLDER_RE.sub(_replace, template)


def _lookup(path: str, context: dict[str, Any]) -> Any:
    cur: Any = context
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return None
    return cur
