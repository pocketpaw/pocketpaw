# ee/paw_bar/router.py — HTTP surface for the Paw Bar widget layer.
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
import re
import uuid
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
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
from pocketpaw_ee.cloud._core.deps import current_workspace_id

logger = logging.getLogger(__name__)

router = APIRouter(tags=["PawBar"])

_PLACEHOLDER_RE = re.compile(r"\{\{\s*([a-zA-Z0-9_.]+)\s*\}\}")


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
    return await _store().create_widget(widget)


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
      2. Origin on the widget's allowlist (403) — the front-gate.
      3. Rate limit, overall + per-customer (429).
      4. Injection screen the free-text message; drop on HIGH (400).
      5. Authenticate the embed key (``resolve_site_key`` — 401/403 fail-closed).
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

    # (2) Origin front-gate.
    if not _origin_allowed(widget, origin):
        raise HTTPException(403, "Origin not allowed for this widget")

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

    # (5) Authenticate the embed key — fail-closed (401 bad/unknown/revoked key,
    # 403 disallowed/missing origin). This is THE credential; there is no user.
    from pocketpaw_ee.cloud.auth.site_keys import resolve_site_key

    ctx = await resolve_site_key(body.signed_key, origin, body.customer_ref)

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
    # The run is bound to the KEY's pocket (ctx.pocket_id — the authenticated
    # authority), the KEY's workspace, and the widget's agent. ``user_id`` is the
    # anonymous customer handle (session / rate-limit key, never a principal).
    # ``surface="concierge"`` makes execute_run resolve the CONCIERGE
    # SurfaceProfile (tool lockdown); ``context_type="concierge"`` makes it
    # resolve the CONCIERGE scope (KB locked to pocket:<id>). Stateless MVP:
    # ``history=[]`` (no cross-visitor bleed) and no persisted user message.
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
        content=body.message,
        history=[],
        intent=None,
        attachments=[],
        mentions=[],
        surface="concierge",
        surface_meta={"pocket_id": ctx.pocket_id, "route_path": "/paw-bar"},
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
