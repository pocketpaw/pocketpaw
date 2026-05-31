# tests/ee/calendar/test_router_mount.py — Calendar router mount smoke test.
# Updated: 2026-05-19 (fix/calendar-security-hardening, #1142 H-NEW-1).
#
# Changes:
# - _make_event_response now sets created_by_user_id; EventResponse
#   requires it after the H-NEW-1 fix.
#
# Verifies that mount_cloud() wires the calendar router into the cloud app
# so /api/v1/calendar/* endpoints are reachable. The companion file
# test_router.py already exercises the route handlers in isolation; this
# file's job is to prove the wiring: that mount_cloud() registers the
# right paths and that requests to those paths don't 404.
#
# We deliberately mock the service-layer entry points so the test never
# touches Mongo. A 422 (validation failure) or 401 (auth failure) on a
# route is sufficient evidence that the route IS mounted — only a 404 on
# the path would indicate the mount didn't happen.

from __future__ import annotations

import importlib
from datetime import datetime
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from pocketpaw_ee.calendar.dto import EventListResponse, EventResponse
from pocketpaw_ee.cloud import mount_cloud
from pocketpaw_ee.cloud.shared.deps import current_user_id, current_workspace_id
from starlette.testclient import TestClient

# Use importlib to grab the module, not the re-exported APIRouter. Mirrors
# the same trick in tests/ee/calendar/test_router.py.
_router_module = importlib.import_module("pocketpaw_ee.calendar.router")


def _get_route_paths(app: FastAPI) -> list[str]:
    """Extract all route paths from a FastAPI app."""
    paths = []
    for route in app.routes:
        if hasattr(route, "path"):
            paths.append(route.path)
    return paths


def _make_event_response() -> EventResponse:
    return EventResponse(
        id="evt-1",
        workspace_id="ws-test",
        calendar_id="cal-1",
        title="Smoke",
        description="",
        starts_at=datetime(2026, 5, 19, 9, 0),
        ends_at=datetime(2026, 5, 19, 10, 0),
        timezone="UTC",
        # H-NEW-1: required on EventResponse now.
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


@pytest.fixture
def app() -> FastAPI:
    """Build a cloud app with all routers mounted, then override calendar deps."""
    instance = FastAPI()
    mount_cloud(instance)

    async def _ws() -> str:
        return "ws-test"

    async def _user() -> str:
        return "user-test"

    instance.dependency_overrides[current_workspace_id] = _ws
    instance.dependency_overrides[current_user_id] = _user
    return instance


def test_calendar_routes_mounted_on_cloud_app(app: FastAPI) -> None:
    """mount_cloud() should register the calendar router's paths."""
    paths = _get_route_paths(app)
    calendar_paths = [p for p in paths if "/calendar" in p]
    assert calendar_paths, (
        "Expected /api/v1/calendar/* paths after mount_cloud(); got none. "
        "Calendar router import / include_router probably failed."
    )
    # Spot-check the canonical event paths.
    assert "/api/v1/calendar/events" in paths
    assert "/api/v1/calendar/events/{event_id}" in paths
    assert "/api/v1/calendar/freebusy" in paths


def test_post_events_routes_through_mount(app: FastAPI, monkeypatch) -> None:
    """POST /api/v1/calendar/events resolves through the cloud app — not 404."""
    mock_create = AsyncMock(return_value=_make_event_response())
    monkeypatch.setattr(_router_module, "svc_create_event", mock_create)

    client = TestClient(app)
    response = client.post(
        "/api/v1/calendar/events",
        json={
            "calendar_id": "cal-1",
            "title": "Smoke",
            "starts_at": "2026-05-19T09:00:00",
            "ends_at": "2026-05-19T10:00:00",
            "timezone": "UTC",
        },
    )
    # Route exists and handler ran (201). A 404 here would mean the mount
    # didn't happen.
    assert response.status_code != 404, f"Calendar POST not mounted: {response.text}"
    assert response.status_code == 201, response.text
    mock_create.assert_awaited_once()


def test_get_events_routes_through_mount(app: FastAPI, monkeypatch) -> None:
    """GET /api/v1/calendar/events resolves through the cloud app — not 404."""
    mock_list = AsyncMock(return_value=EventListResponse(events=[], total=0))
    monkeypatch.setattr(_router_module, "svc_list_events", mock_list)

    client = TestClient(app)
    response = client.get(
        "/api/v1/calendar/events",
        params={
            "starts_after": "2026-05-19T00:00:00",
            "starts_before": "2026-05-20T00:00:00",
        },
    )
    assert response.status_code != 404, f"Calendar GET not mounted: {response.text}"
    assert response.status_code == 200, response.text
    assert response.json() == {"events": [], "total": 0}
