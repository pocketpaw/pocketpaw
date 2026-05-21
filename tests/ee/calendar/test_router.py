# tests/ee/calendar/test_router.py — FastAPI router tests.
# Updated: 2026-05-19 (fix/calendar-security-hardening, #1142 H3 + H-NEW-1).
#
# Changes:
# - Added test_list_events_malformed_starts_after_returns_422 — covers H3.
#   Previously a non-ISO ``starts_after`` propagated a ``ValueError`` and
#   became HTTP 500; with the FastAPI-native ``datetime`` query type the
#   response is now 422 with the standard validation envelope.
# - Added test_list_events_missing_starts_before_returns_422 — required
#   query params are still required.
# - H-NEW-1: _make_event_response now populates created_by_user_id since
#   EventResponse exposes it for the UI.
#
# Mount the router on a throwaway FastAPI app, install a global CloudError
# handler (so we get the same envelope the real cloud app emits), and
# override the auth deps so requests carry a deterministic workspace + user.
# The service itself is mocked at module boundary — we're exercising the
# wiring, not the persistence.

from __future__ import annotations

import importlib
from datetime import datetime
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.testclient import TestClient

from ee.calendar.domain import Attendee, AttendeeResponse, ConflictSeverity
from ee.calendar.dto import (
    ConflictReport,
    EventListResponse,
    EventResponse,
    FreeBusyResponse,
)
from ee.cloud.shared.deps import current_user_id, current_workspace_id
from ee.cloud.shared.errors import CloudError, NotFound

# Use importlib so we get the submodule, not the `router` attr re-exported
# by ee.calendar.__init__. Beware: ``import ee.calendar.router`` binds to
# the re-exported APIRouter, not the module. Fixtures need the module so
# they can monkeypatch the ``svc_*`` aliases.
router_module = importlib.import_module("ee.calendar.router")
router = router_module.router


def _make_event_response(**overrides: Any) -> EventResponse:
    base = dict(
        id="evt-1",
        workspace_id="ws-test",
        calendar_id="cal-1",
        title="Sample",
        description="",
        starts_at=datetime(2026, 5, 19, 9, 0),
        ends_at=datetime(2026, 5, 19, 10, 0),
        timezone="UTC",
        # H-NEW-1: EventResponse now exposes this so the UI can render
        # "created by X" and gate Edit/Delete affordances.
        created_by_user_id="user-test",
        location=None,
        attendees=[],
        recurrence=None,
        fabric_object_id=None,
        source_connector=None,
        source_external_id=None,
        created_at=datetime(2026, 5, 1),
        updated_at=datetime(2026, 5, 1),
    )
    base.update(overrides)
    return EventResponse(**base)


@pytest.fixture
def client(monkeypatch) -> TestClient:
    """Build a minimal FastAPI app mounting only the calendar router.

    We install the same CloudError handler the real cloud app uses so that
    a NotFound from the service produces a 404 with the standard envelope
    rather than a 500 with a traceback.
    """
    app = FastAPI()

    async def _cloud_error_handler(request: Request, exc: CloudError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content=exc.to_dict())

    app.add_exception_handler(CloudError, _cloud_error_handler)
    app.include_router(router)

    # Override auth deps so we don't need real JWTs.
    async def _ws() -> str:
        return "ws-test"

    async def _user() -> str:
        return "user-test"

    app.dependency_overrides[current_workspace_id] = _ws
    app.dependency_overrides[current_user_id] = _user

    return TestClient(app)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_create_event_endpoint(client, monkeypatch):
    mock_create = AsyncMock(return_value=_make_event_response(title="Booked"))
    monkeypatch.setattr(router_module, "svc_create_event", mock_create)

    response = client.post(
        "/api/v1/calendar/events",
        json={
            "calendar_id": "cal-1",
            "title": "Booked",
            "starts_at": "2026-05-19T09:00:00",
            "ends_at": "2026-05-19T10:00:00",
            "timezone": "UTC",
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["title"] == "Booked"
    assert body["workspace_id"] == "ws-test"
    mock_create.assert_awaited_once()


def test_create_event_invalid_dto_returns_422(client, monkeypatch):
    """Pydantic rejection happens before the service is invoked."""
    sentinel = AsyncMock()
    monkeypatch.setattr(router_module, "svc_create_event", sentinel)

    response = client.post(
        "/api/v1/calendar/events",
        json={
            "calendar_id": "cal-1",
            # missing title
            "starts_at": "2026-05-19T09:00:00",
            "ends_at": "2026-05-19T10:00:00",
            "timezone": "UTC",
        },
    )
    assert response.status_code == 422
    sentinel.assert_not_awaited()


def test_list_events_endpoint(client, monkeypatch):
    mock_list = AsyncMock(
        return_value=EventListResponse(
            events=[
                _make_event_response(id="evt-1"),
                _make_event_response(id="evt-2", title="Lunch"),
            ],
            total=2,
        ),
    )
    monkeypatch.setattr(router_module, "svc_list_events", mock_list)

    response = client.get(
        "/api/v1/calendar/events",
        params={
            "starts_after": "2026-05-19T00:00:00",
            "starts_before": "2026-05-20T00:00:00",
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 2
    assert {e["id"] for e in body["events"]} == {"evt-1", "evt-2"}


def test_not_found_returns_404_with_cloud_error_shape(client, monkeypatch):
    """CloudError → standard envelope. We verify the shape, not just the code."""
    mock_get = AsyncMock(side_effect=NotFound("event", "missing"))
    monkeypatch.setattr(router_module, "svc_get_event", mock_get)

    response = client.get("/api/v1/calendar/events/missing")
    assert response.status_code == 404
    body = response.json()
    assert body == {"error": {"code": "event.not_found", "message": "event 'missing' not found"}}


def test_get_event_endpoint(client, monkeypatch):
    mock_get = AsyncMock(
        return_value=_make_event_response(
            id="evt-1",
            attendees=[Attendee(email="alice@example.com", response=AttendeeResponse.ACCEPTED)],
        ),
    )
    monkeypatch.setattr(router_module, "svc_get_event", mock_get)

    response = client.get("/api/v1/calendar/events/evt-1")
    assert response.status_code == 200
    body = response.json()
    assert body["id"] == "evt-1"
    assert body["attendees"][0]["email"] == "alice@example.com"


def test_freebusy_endpoint(client, monkeypatch):
    mock_fb = AsyncMock(return_value=FreeBusyResponse(freebusy=[]))
    monkeypatch.setattr(router_module, "svc_get_freebusy", mock_fb)

    response = client.post(
        "/api/v1/calendar/freebusy",
        json={
            "attendee_emails": ["a@example.com"],
            "starts_at": "2026-05-19T00:00:00",
            "ends_at": "2026-05-19T23:59:00",
        },
    )
    assert response.status_code == 200, response.text


def test_conflicts_endpoint(client, monkeypatch):
    mock_conflicts = AsyncMock(
        return_value=ConflictReport(
            event_id="evt-1",
            conflicting_events=[],
            severity=ConflictSeverity.LOW,
        ),
    )
    monkeypatch.setattr(router_module, "svc_detect_conflicts", mock_conflicts)

    response = client.get("/api/v1/calendar/events/evt-1/conflicts")
    assert response.status_code == 200
    body = response.json()
    assert body["event_id"] == "evt-1"
    assert body["severity"] == "low"
    assert body["conflicting_events"] == []


# ---------------------------------------------------------------------------
# H3 — datetime query parameter validation.
# ---------------------------------------------------------------------------


def test_list_events_malformed_starts_after_returns_422(client, monkeypatch):
    """Malformed datetime → 422 (not 500). H3 of the #1132 audit.

    Before the fix, the router called ``datetime.fromisoformat`` on the
    raw string and let the ValueError bubble into the global handler as
    HTTP 500. With the FastAPI-native ``datetime`` query type, Pydantic
    rejects the input cleanly.
    """
    sentinel = AsyncMock()
    monkeypatch.setattr(router_module, "svc_list_events", sentinel)

    response = client.get(
        "/api/v1/calendar/events",
        params={
            "starts_after": "not-a-date",
            "starts_before": "2026-05-20T00:00:00",
        },
    )
    assert response.status_code == 422, response.text
    sentinel.assert_not_awaited()


def test_list_events_malformed_starts_before_returns_422(client, monkeypatch):
    """Same as above but on the other query param."""
    sentinel = AsyncMock()
    monkeypatch.setattr(router_module, "svc_list_events", sentinel)

    response = client.get(
        "/api/v1/calendar/events",
        params={
            "starts_after": "2026-05-19T00:00:00",
            "starts_before": "not-a-date-either",
        },
    )
    assert response.status_code == 422, response.text
    sentinel.assert_not_awaited()


def test_list_events_missing_starts_before_returns_422(client, monkeypatch):
    """Both query params are required — omitting one yields 422."""
    sentinel = AsyncMock()
    monkeypatch.setattr(router_module, "svc_list_events", sentinel)

    response = client.get(
        "/api/v1/calendar/events",
        params={"starts_after": "2026-05-19T00:00:00"},
    )
    assert response.status_code == 422
    sentinel.assert_not_awaited()
