# Integration tests for /api/v1/meetings.
# Verifies routes register, tenancy filters apply, CloudError mapping
# produces the right JSON envelope, and create/cancel round-trip through
# Mongo via an injected fake provider adapter.
# See docs/plans/2026-05-19-meetings-integration-design.md.

from __future__ import annotations

from types import SimpleNamespace

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pocketpaw_ee.cloud.auth import current_active_user
from pocketpaw_ee.cloud.license import require_license


@pytest_asyncio.fixture
async def meetings_client(monkeypatch, mongo_db):  # noqa: ARG001 — mongo_db forces Beanie init
    """FastAPI app with /api/v1/meetings mounted + RBAC + auth stubbed."""
    from pocketpaw_ee.cloud._core.http import add_error_handler
    from pocketpaw_ee.cloud.meetings.router import router as meetings_router

    # Pass RBAC checks.
    from pocketpaw_ee.guards import deps as guards_deps

    monkeypatch.setattr(guards_deps, "check_workspace_action", lambda *a, **k: None)

    fake_user = SimpleNamespace(
        id="user-1",
        active_workspace="ws-alpha",
        workspaces=[SimpleNamespace(workspace="ws-alpha", role="owner")],
    )

    async def fake_current_active_user():
        return fake_user

    app = FastAPI()
    add_error_handler(app)
    app.dependency_overrides[require_license] = lambda: None
    app.dependency_overrides[current_active_user] = fake_current_active_user
    app.include_router(meetings_router, prefix="/api/v1")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test.local") as client:
        yield client


# ---------------------------------------------------------------------------
# Routes register / scaffold sanity
# ---------------------------------------------------------------------------


async def test_routes_register(meetings_client: AsyncClient) -> None:
    """Smoke: hitting an unknown meeting id returns a CloudError envelope."""
    resp = await meetings_client.get("/api/v1/meetings/does-not-exist")
    assert resp.status_code == 404
    body = resp.json()
    # CloudError envelope shape: {"error": {"code": ..., "message": ...}}
    assert body["error"]["code"] == "meeting.not_found"


async def test_list_meetings_empty(meetings_client: AsyncClient) -> None:
    """A workspace with no meetings returns an empty list, not 404."""
    resp = await meetings_client.get("/api/v1/meetings")
    assert resp.status_code == 200
    assert resp.json() == []


async def test_create_meeting_without_provider_env_errors(
    meetings_client: AsyncClient, monkeypatch
) -> None:
    """With the provider's env vars unset, create_meeting fails clearly."""
    for var in ("ZOOM_ACCOUNT_ID", "ZOOM_CLIENT_ID", "ZOOM_CLIENT_SECRET"):
        monkeypatch.delenv(var, raising=False)
    resp = await meetings_client.post(
        "/api/v1/meetings",
        json={"provider": "zoom", "title": "test", "duration_minutes": 30},
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "meeting.zoom_not_configured"


async def test_create_meeting_rejects_empty_title(meetings_client: AsyncClient) -> None:
    """Whitespace-only title is rejected before reaching the adapter."""
    resp = await meetings_client.post(
        "/api/v1/meetings",
        json={"provider": "zoom", "title": "   ", "duration_minutes": 30},
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "meeting.empty_title"


# ---------------------------------------------------------------------------
# End-to-end create/cancel via an injected fake adapter
# ---------------------------------------------------------------------------


class _FakeAdapter:
    """Captures execute() calls and returns canned ActionResults."""

    def __init__(self):
        from pocketpaw.connectors.protocol import ActionResult

        self.calls: list[tuple[str, dict]] = []
        self.create_result = ActionResult(
            success=True,
            data={
                "id": 9876543210,
                "join_url": "https://zoom.us/j/9876543210",
                "host_email": "host@example.com",
            },
            records_affected=1,
        )
        self.cancel_result = ActionResult(success=True, records_affected=1)

    async def execute(self, action, params):
        self.calls.append((action, params))
        if action == "meeting_create":
            return self.create_result
        if action == "meeting_cancel":
            return self.cancel_result
        from pocketpaw.connectors.protocol import ActionResult

        return ActionResult(success=False, error="unknown action")


@pytest_asyncio.fixture
async def with_fake_adapter(meetings_client):  # noqa: ARG001
    """Swap the adapter factory globally for the duration of the test."""
    from pocketpaw_ee.cloud.meetings import service as meetings_service

    fake = _FakeAdapter()

    async def _factory(workspace_id, provider):
        return fake

    prev = meetings_service._set_adapter_factory(_factory)
    yield meetings_client, fake
    meetings_service._set_adapter_factory(prev)


async def test_create_meeting_end_to_end(with_fake_adapter) -> None:
    """POST /meetings calls adapter, persists Meeting row, emits event, returns DTO."""
    client, fake = with_fake_adapter
    resp = await client.post(
        "/api/v1/meetings",
        json={"provider": "zoom", "title": "Standup", "duration_minutes": 15},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["provider"] == "zoom"
    assert body["title"] == "Standup"
    assert body["join_url"] == "https://zoom.us/j/9876543210"
    assert body["status"] == "scheduled"

    # Adapter received the right params.
    assert fake.calls[0][0] == "meeting_create"
    assert fake.calls[0][1]["topic"] == "Standup"
    assert fake.calls[0][1]["duration_minutes"] == 15
    # No start_time → instant meeting (start_time absent from params).
    assert "start_time" not in fake.calls[0][1]

    # Round-trip: meeting is now listable.
    list_resp = await client.get("/api/v1/meetings")
    assert list_resp.status_code == 200
    titles = [m["title"] for m in list_resp.json()]
    assert "Standup" in titles


async def test_create_meeting_with_scheduled_start(with_fake_adapter) -> None:
    """scheduled_start surfaces as RFC 3339 to the adapter."""
    client, fake = with_fake_adapter
    resp = await client.post(
        "/api/v1/meetings",
        json={
            "provider": "zoom",
            "title": "Planning",
            "scheduled_start": "2026-06-01T14:30:00Z",
            "duration_minutes": 60,
        },
    )
    assert resp.status_code == 200, resp.text
    assert fake.calls[0][1]["start_time"] == "2026-06-01T14:30:00Z"


async def test_create_meeting_surfaces_provider_error(with_fake_adapter) -> None:
    """If adapter returns success=False we map it to 422 ``meeting.provider_error``."""
    from pocketpaw.connectors.protocol import ActionResult

    client, fake = with_fake_adapter
    fake.create_result = ActionResult(success=False, error="Zoom rate limited")

    resp = await client.post(
        "/api/v1/meetings",
        json={"provider": "zoom", "title": "Standup"},
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["error"]["code"] == "meeting.provider_error"
    assert "Zoom rate limited" in body["error"]["message"]


async def test_cancel_meeting_end_to_end(with_fake_adapter) -> None:
    """DELETE /meetings/{id} calls adapter, marks doc cancelled, emits event."""
    client, fake = with_fake_adapter
    create_resp = await client.post(
        "/api/v1/meetings",
        json={"provider": "zoom", "title": "ToKill"},
    )
    meeting_id = create_resp.json()["id"]

    fake.calls.clear()
    cancel_resp = await client.delete(f"/api/v1/meetings/{meeting_id}")
    assert cancel_resp.status_code == 200
    assert cancel_resp.json()["status"] == "cancelled"

    # Adapter received the provider's meeting id, not our Mongo id.
    assert fake.calls[0][0] == "meeting_cancel"
    assert fake.calls[0][1]["meeting_id"] == "9876543210"


# ---------------------------------------------------------------------------
# Cross-provider aggregation (search + list_recent)
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("mongo_db")
async def test_search_meetings_substring_match() -> None:
    """search_meetings matches titles + participant names, with tenancy."""
    from pocketpaw_ee.cloud.meetings import service as ms
    from pocketpaw_ee.cloud.models.meeting import Meeting as _MD

    await _MD(
        workspace="ws-alpha",
        provider="zoom",
        provider_meeting_id="m1",
        title="Acme Q3 sync",
        join_url="https://x/1",
        participants=[{"name": "Alice", "email": "alice@acme.com"}],
    ).insert()
    await _MD(
        workspace="ws-alpha",
        provider="zoom",
        provider_meeting_id="m2",
        title="Internal review",
        join_url="https://x/2",
    ).insert()
    # Other workspace must not leak.
    await _MD(
        workspace="ws-beta",
        provider="zoom",
        provider_meeting_id="m3",
        title="Acme separate",
        join_url="https://x/3",
    ).insert()

    rows = await ms.search_meetings("ws-alpha", query="Acme")
    titles = [r.title for r in rows]
    assert "Acme Q3 sync" in titles
    assert "Acme separate" not in titles  # tenancy
    assert "Internal review" not in titles


@pytest.mark.usefixtures("mongo_db")
async def test_list_recent_meetings_orders_newest_first() -> None:
    from datetime import UTC, datetime, timedelta

    from pocketpaw_ee.cloud.meetings import service as ms
    from pocketpaw_ee.cloud.models.meeting import Meeting as _MD

    now = datetime.now(UTC)
    await _MD(
        workspace="ws-1",
        provider="zoom",
        provider_meeting_id="old",
        title="Old",
        join_url="x",
        scheduled_start=now - timedelta(days=5),
    ).insert()
    await _MD(
        workspace="ws-1",
        provider="zoom",
        provider_meeting_id="new",
        title="New",
        join_url="x",
        scheduled_start=now,
    ).insert()

    rows = await ms.list_recent_meetings("ws-1", limit=5)
    assert [r.title for r in rows] == ["New", "Old"]


@pytest.mark.usefixtures("mongo_db")
async def test_list_meetings_filters_by_workspace() -> None:
    """A Meeting belonging to another workspace must NOT appear in our list."""
    from pocketpaw_ee.cloud.meetings import service as meetings_service
    from pocketpaw_ee.cloud.meetings.dto import ListMeetingsRequest
    from pocketpaw_ee.cloud.models.meeting import Meeting as _MeetingDoc

    await _MeetingDoc(
        workspace="ws-alpha",
        provider="zoom",
        provider_meeting_id="meet-1",
        title="Mine",
        join_url="https://zoom.us/j/1",
    ).insert()
    await _MeetingDoc(
        workspace="ws-beta",
        provider="zoom",
        provider_meeting_id="meet-2",
        title="Theirs",
        join_url="https://zoom.us/j/2",
    ).insert()

    rows = await meetings_service.list_meetings("ws-alpha", ListMeetingsRequest())
    titles = [r.title for r in rows]
    assert titles == ["Mine"]
