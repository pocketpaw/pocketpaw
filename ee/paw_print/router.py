# ee/paw_print/router.py — HTTP surface for the Paw Print widget layer.
# Created: 2026-04-13 (Move 3 PR-B) — Spec serving (public, CORS-gated),
# widget CRUD (owner-authed via access_token), event ingest (rate-limited,
# domain-enforced, Guardian-screened, Fabric-mapped). The widget.js bundle
# built in PR-C consumes these endpoints.

from __future__ import annotations

import json
import logging
import re
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ee.paw_print.models import (
    MAX_PAYLOAD_BYTES,
    PawPrintEvent,
    PawPrintEventMapping,
    PawPrintSpec,
    PawPrintWidget,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["PawPrint"])

_PLACEHOLDER_RE = re.compile(r"\{\{\s*([a-zA-Z0-9_.]+)\s*\}\}")


def _store():
    from ee.api import get_paw_print_store

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
    widgets: list[PawPrintWidget]
    total: int


class EventIngestResponse(BaseModel):
    accepted: bool
    event: PawPrintEvent | None = None
    fabric_object_id: str | None = None
    reason: str | None = None


class EventsListResponse(BaseModel):
    events: list[PawPrintEvent]
    total: int


# ---------------------------------------------------------------------------
# Owner-authed CRUD
# ---------------------------------------------------------------------------


@router.post("/paw-print/widgets", response_model=PawPrintWidget, status_code=201)
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


@router.get("/paw-print/widgets", response_model=WidgetListResponse)
async def list_widgets(
    pocket_id: str | None = Query(None),
    owner: str | None = Query(None),
    limit: int = Query(100, ge=1, le=500),
) -> WidgetListResponse:
    widgets = await _store().list_widgets(pocket_id=pocket_id, owner=owner, limit=limit)
    return WidgetListResponse(widgets=widgets, total=len(widgets))


@router.get("/paw-print/widgets/{widget_id}", response_model=PawPrintWidget)
async def get_widget(
    widget_id: str,
    x_paw_print_token: str | None = Header(default=None, alias="X-Paw-Print-Token"),
) -> PawPrintWidget:
    widget = await _store().get_widget(widget_id)
    if widget is None:
        raise HTTPException(404, "Widget not found")
    _require_owner_token(widget, x_paw_print_token)
    return widget


@router.patch("/paw-print/widgets/{widget_id}/spec", response_model=PawPrintWidget)
async def update_spec(
    widget_id: str,
    spec: PawPrintSpec,
    x_paw_print_token: str | None = Header(default=None, alias="X-Paw-Print-Token"),
) -> PawPrintWidget:
    widget = await _store().get_widget(widget_id)
    if widget is None:
        raise HTTPException(404, "Widget not found")
    _require_owner_token(widget, x_paw_print_token)
    updated = await _store().update_spec(widget_id, spec)
    if updated is None:
        raise HTTPException(404, "Widget not found")
    return updated


@router.post("/paw-print/widgets/{widget_id}/rotate-token", response_model=PawPrintWidget)
async def rotate_token(
    widget_id: str,
    x_paw_print_token: str | None = Header(default=None, alias="X-Paw-Print-Token"),
) -> PawPrintWidget:
    widget = await _store().get_widget(widget_id)
    if widget is None:
        raise HTTPException(404, "Widget not found")
    _require_owner_token(widget, x_paw_print_token)
    rotated = await _store().rotate_token(widget_id)
    if rotated is None:
        raise HTTPException(404, "Widget not found")
    return rotated


@router.delete("/paw-print/widgets/{widget_id}", status_code=204)
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
    5. Guardian screens the payload (input sanitization layer — degrades
       cleanly when ee/ lacks the guardian backend).
    After that, the event is persisted and — if the widget has a matching
    `event_mapping` — a Fabric object is created.
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

    if not await _pass_through_guardian(event):
        return EventIngestResponse(accepted=False, reason="guardian_rejected")

    await store.record_event(event)
    fabric_object_id = await _apply_event_mapping(widget, event)

    return EventIngestResponse(
        accepted=True,
        event=event,
        fabric_object_id=fabric_object_id,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _pass_through_guardian(event: PawPrintEvent) -> bool:
    """Best-effort Guardian screen — tolerant when the security stack is absent."""
    try:
        from pocketpaw.security.guardian import GuardianProtocol, get_guardian
    except Exception:
        return True

    try:
        guardian: GuardianProtocol = get_guardian()
    except Exception:
        return True

    payload = json.dumps(event.payload, default=str)
    check = getattr(guardian, "check_input", None)
    if check is None:
        return True
    try:
        verdict = await check(payload)
    except Exception:
        logger.debug("Guardian check raised; accepting event by default")
        return True
    if isinstance(verdict, bool):
        return verdict
    # Guardian may return a richer dataclass; accept when no `blocked` attr.
    return not getattr(verdict, "blocked", False)


async def _apply_event_mapping(widget: PawPrintWidget, event: PawPrintEvent) -> str | None:
    """Turn a PawPrintEvent into a Fabric object when a mapping exists."""
    mapping = widget.event_mapping.get(event.type)
    if mapping is None:
        return None

    try:
        from ee.api import get_fabric_store
        from ee.fabric.models import FabricObject
    except ImportError:
        return None

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
