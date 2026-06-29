# ee/paw_print/router.py — HTTP surface for the Paw Print widget layer.
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
# list and read responses now serialize PawPrintWidgetPublic, which omits
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
# (GET /paw-print/events/{widget_id}/decision/{customer_ref}) so the rendered
# widget can read the owner's decision back out — the back-half of the loop. The
# approve/reject delivery hook lives in the instinct router (it owns the human
# decision); see decision_loop.deliver_customer_decision.

from __future__ import annotations

import json
import logging
import re
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from pocketpaw.api.deps import require_scope
from pocketpaw.paw_print.models import (
    MAX_PAYLOAD_BYTES,
    PawPrintEvent,
    PawPrintEventMapping,
    PawPrintSpec,
    PawPrintWidget,
    PawPrintWidgetPublic,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["PawPrint"])

_PLACEHOLDER_RE = re.compile(r"\{\{\s*([a-zA-Z0-9_.]+)\s*\}\}")


def _store():
    from pocketpaw_ee.api import get_paw_print_store

    return get_paw_print_store()


def _require_owner_token(widget: PawPrintWidget, header_token: str | None) -> None:
    if not header_token or header_token != widget.access_token:
        raise HTTPException(status_code=401, detail="Invalid or missing access token")


def _origin_allowed(widget: PawPrintWidget, origin: str | None) -> bool:
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
    name: str = ""
    spec: PawPrintSpec
    allowed_domains: list[str] = Field(default_factory=list)
    rate_limit_per_min: int = 60
    per_customer_limit_per_min: int = 10
    event_mapping: dict[str, PawPrintEventMapping] = Field(default_factory=dict)


class WidgetListResponse(BaseModel):
    # PawPrintWidgetPublic (not PawPrintWidget) — list payloads must never
    # carry access_token (W0b).
    widgets: list[PawPrintWidgetPublic]
    total: int


class EventIngestResponse(BaseModel):
    accepted: bool
    event: PawPrintEvent | None = None
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
    events: list[PawPrintEvent]
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
# callers). The per-widget access_token (X-Paw-Print-Token) is a SECOND factor
# on read/mutate of a specific widget — it is not a substitute for being a
# signed-in dashboard user, which is why create/list need this guard.
# ---------------------------------------------------------------------------


@router.post(
    "/paw-print/widgets",
    response_model=PawPrintWidget,
    status_code=201,
    dependencies=[Depends(require_scope("admin"))],
)
async def create_widget(req: CreateWidgetRequest) -> PawPrintWidget:
    widget = PawPrintWidget(
        pocket_id=req.pocket_id,
        owner=req.owner,
        name=req.name,
        spec=req.spec,
        allowed_domains=req.allowed_domains,
        rate_limit_per_min=req.rate_limit_per_min,
        per_customer_limit_per_min=req.per_customer_limit_per_min,
        event_mapping=req.event_mapping,
    )
    return await _store().create_widget(widget)


@router.get(
    "/paw-print/widgets",
    response_model=WidgetListResponse,
    dependencies=[Depends(require_scope("admin"))],
)
async def list_widgets(
    pocket_id: str | None = Query(None),
    owner: str | None = Query(None),
    limit: int = Query(100, ge=1, le=500),
) -> WidgetListResponse:
    widgets = await _store().list_widgets(pocket_id=pocket_id, owner=owner, limit=limit)
    # Project to the token-free model — a list payload must never carry the
    # per-widget access_token (W0b).
    public = [PawPrintWidgetPublic.from_widget(w) for w in widgets]
    return WidgetListResponse(widgets=public, total=len(public))


@router.get("/paw-print/widgets/{widget_id}", response_model=PawPrintWidgetPublic)
async def get_widget(
    widget_id: str,
    x_paw_print_token: str | None = Header(default=None, alias="X-Paw-Print-Token"),
) -> PawPrintWidgetPublic:
    widget = await _store().get_widget(widget_id)
    if widget is None:
        raise HTTPException(404, "Widget not found")
    _require_owner_token(widget, x_paw_print_token)
    # Read responses omit access_token — the caller already holds it (they had
    # to present it to pass _require_owner_token), so echoing it back only
    # widens the blast radius if a read response is logged/cached (W0b).
    return PawPrintWidgetPublic.from_widget(widget)


@router.patch(
    "/paw-print/widgets/{widget_id}/spec",
    response_model=PawPrintWidgetPublic,
    dependencies=[Depends(require_scope("admin"))],
)
async def update_spec(
    widget_id: str,
    spec: PawPrintSpec,
    x_paw_print_token: str | None = Header(default=None, alias="X-Paw-Print-Token"),
) -> PawPrintWidgetPublic:
    widget = await _store().get_widget(widget_id)
    if widget is None:
        raise HTTPException(404, "Widget not found")
    _require_owner_token(widget, x_paw_print_token)
    updated = await _store().update_spec(widget_id, spec)
    if updated is None:
        raise HTTPException(404, "Widget not found")
    return PawPrintWidgetPublic.from_widget(updated)


@router.post(
    "/paw-print/widgets/{widget_id}/rotate-token",
    response_model=PawPrintWidget,
    dependencies=[Depends(require_scope("admin"))],
)
async def rotate_token(
    widget_id: str,
    x_paw_print_token: str | None = Header(default=None, alias="X-Paw-Print-Token"),
) -> PawPrintWidget:
    # Returns the FULL widget (with the new access_token) on purpose: this is
    # the explicit, authenticated reveal path so the owner can capture the
    # rotated secret. Still requires the old token AND an admin dashboard
    # session (W0b).
    widget = await _store().get_widget(widget_id)
    if widget is None:
        raise HTTPException(404, "Widget not found")
    _require_owner_token(widget, x_paw_print_token)
    rotated = await _store().rotate_token(widget_id)
    if rotated is None:
        raise HTTPException(404, "Widget not found")
    return rotated


@router.delete(
    "/paw-print/widgets/{widget_id}",
    status_code=204,
    dependencies=[Depends(require_scope("admin"))],
)
async def delete_widget(
    widget_id: str,
    x_paw_print_token: str | None = Header(default=None, alias="X-Paw-Print-Token"),
) -> None:
    widget = await _store().get_widget(widget_id)
    if widget is None:
        raise HTTPException(404, "Widget not found")
    _require_owner_token(widget, x_paw_print_token)
    await _store().delete_widget(widget_id)


@router.get("/paw-print/widgets/{widget_id}/events", response_model=EventsListResponse)
async def list_events(
    widget_id: str,
    limit: int = Query(100, ge=1, le=500),
    x_paw_print_token: str | None = Header(default=None, alias="X-Paw-Print-Token"),
) -> EventsListResponse:
    widget = await _store().get_widget(widget_id)
    if widget is None:
        raise HTTPException(404, "Widget not found")
    _require_owner_token(widget, x_paw_print_token)
    events = await _store().recent_events(widget_id, limit=limit)
    return EventsListResponse(events=events, total=len(events))


# ---------------------------------------------------------------------------
# Public spec serving (CORS-enforced)
# ---------------------------------------------------------------------------


@router.get("/paw-print/spec/{widget_id}")
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


@router.post("/paw-print/events/{widget_id}", response_model=EventIngestResponse)
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

    event = PawPrintEvent(
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


@router.get("/paw-print/events/{widget_id}/decision/{customer_ref}")
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
# Helpers
# ---------------------------------------------------------------------------


async def _screen_event_for_injection(event: PawPrintEvent) -> bool:
    """Screen the stringified event payload for prompt-injection content.

    Runs the heuristic :class:`InjectionScanner` (regex-based, no API key
    required) over the JSON-serialized payload and returns ``False`` — drop
    the event — when the scan reports a ``HIGH`` (or higher) threat. The
    HIGH threshold is deliberate: ``MEDIUM`` covers softer persona/roleplay
    phrasing that legitimate widget input ("act as my travel guide") could
    trip, so screening only the unambiguous HIGH patterns (instruction
    overrides, delimiter attacks, jailbreaks, exfiltration) avoids
    false-dropping real customer events.

    Degrades cleanly: if the security module can't be imported or the scan
    raises, the event is accepted (availability over a hard fail on a public
    ingest endpoint). This replaced the previous ``getattr(guardian,
    "check_input")`` call, which was a permanent no-op — ``GuardianAgent``
    only ever exposed ``check_command``, so the attribute was always ``None``
    and every event was accepted unscreened.
    """
    try:
        from pocketpaw.security.injection_scanner import (
            ThreatLevel,
            get_injection_scanner,
        )
    except Exception:
        return True

    payload = json.dumps(event.payload, default=str)
    try:
        scan = get_injection_scanner().scan(payload, source=f"paw_print:{event.widget_id}")
    except Exception:
        logger.debug("Injection scan raised; accepting event by default")
        return True

    if scan.threat_level == ThreatLevel.HIGH:
        logger.warning(
            "Dropping paw-print event for widget %s — injection threat %s (patterns: %s)",
            event.widget_id,
            scan.threat_level.value,
            ", ".join(scan.matched_patterns),
        )
        return False
    return True


async def _open_decision_loop(
    widget: PawPrintWidget,
    event: PawPrintEvent,
    store: Any,
) -> str | None:
    """Raise an Instinct proposal for a mapped customer event (gap2).

    Thin wrapper over ``decision_loop.propose_customer_decision`` — keeps the
    import lazy (the OSS paw_print store never reaches into the EE decision-loop
    module) and the failure best-effort: any error is swallowed by the called
    function, and a defensive guard here ensures even an import failure can't
    break the ingest response. Returns the proposed Instinct action id, or None.
    """
    try:
        from pocketpaw_ee.paw_print.decision_loop import propose_customer_decision

        return await propose_customer_decision(
            widget=widget,
            event=event,
            paw_print_store=store,
        )
    except Exception:
        logger.warning(
            "decision-loop proposal failed for widget %s (non-fatal)",
            widget.id,
            exc_info=True,
        )
        return None


async def _apply_event_mapping(widget: PawPrintWidget, event: PawPrintEvent) -> str | None:
    """Turn a PawPrintEvent into a Fabric object when a mapping exists."""
    mapping = widget.event_mapping.get(event.type)
    if mapping is None:
        return None

    try:
        from pocketpaw.fabric.models import FabricObject
        from pocketpaw_ee.api import get_fabric_store
    except ImportError:
        return None

    # ISO note: paw-print's tenant key is the widget OWNER, a logical, possibly
    # colon-qualified string (``user:maya``) — NOT a physical-store-path
    # workspace id (a ``:`` fails the path-traversal allowlist). The Fabric
    # write is scoped by the object's ``source_*`` provenance + the store's own
    # in-row guard, not by a per-owner store file, so this keeps the BARE store
    # rather than threading the owner into the factory (which would ValueError).
    fabric = get_fabric_store()
    if fabric is None:
        return None

    context = {"payload": event.payload, "customer_ref": event.customer_ref}
    properties = {k: _interpolate(v, context) for k, v in mapping.fields.items()}
    try:
        obj = FabricObject(
            type_name=mapping.creates,
            properties=properties,
            source_connector="paw_print",
            source_id=widget.id,
        )
        created = await fabric.create_object(obj)
        return getattr(created, "id", None)
    except Exception:
        logger.exception("Failed to create Fabric object from paw-print event")
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
