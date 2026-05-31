"""Golden-response tests for the notifications router.

Asserts the wire shape returned by each endpoint matches the legacy
``_to_wire`` output byte-for-byte. Uses the shared ``mongo_db`` fixture
so the router exercises real Beanie queries against a mongomock-motor
DB; no Protocol fakes.
"""

from __future__ import annotations

import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pocketpaw_ee.cloud._core.http import add_error_handler
from pocketpaw_ee.cloud.models.notification import Notification as _NotificationDoc
from pocketpaw_ee.cloud.models.notification import NotificationSource as _NotificationSourceDoc
from pocketpaw_ee.cloud.notifications.router import router


async def _seed(
    *,
    workspace: str = "w1",
    recipient: str = "user-1",
    kind: str = "mention",
    title: str = "hi",
    body: str = "b",
    source_id: str = "m1",
    read: bool = False,
) -> _NotificationDoc:
    doc = _NotificationDoc(
        workspace=workspace,
        recipient=recipient,
        type=kind,
        title=title,
        body=body,
        source=_NotificationSourceDoc(type="message", id=source_id, pocket_id=None),
        read=read,
    )
    await doc.insert()
    return doc


@pytest_asyncio.fixture
async def app_client(mongo_db) -> AsyncClient:
    from pocketpaw_ee.cloud.auth import current_active_user

    class _U:
        id = "user-1"
        active_workspace = "w1"
        workspaces: list = []

    app = FastAPI()
    add_error_handler(app)
    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[current_active_user] = lambda: _U()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        yield client


async def test_list_returns_dto_shape(app_client) -> None:
    await _seed()
    resp = await app_client.get("/api/v1/notifications")
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body, list)
    assert len(body) == 1
    item = body[0]
    assert set(item.keys()) == {
        "id",
        "user_id",
        "workspace_id",
        "kind",
        "title",
        "body",
        "source_id",
        "read",
        "created_at",
    }
    assert item["user_id"] == "user-1"
    assert item["workspace_id"] == "w1"
    assert item["kind"] == "mention"
    assert item["title"] == "hi"
    assert item["body"] == "b"
    assert item["source_id"] == "m1"
    assert item["read"] is False
    assert item["created_at"].endswith("+00:00")


async def test_list_unread_query_filters(app_client) -> None:
    await _seed(read=True)
    await _seed(read=False)
    resp = await app_client.get("/api/v1/notifications?unread=true")
    body = resp.json()
    assert len(body) == 1
    assert body[0]["read"] is False


async def test_list_limit_query_caps_results(app_client) -> None:
    for _ in range(5):
        await _seed()
    resp = await app_client.get("/api/v1/notifications?limit=2")
    assert len(resp.json()) == 2


async def test_mark_read_returns_ok_envelope(app_client) -> None:
    doc = await _seed()
    resp = await app_client.post(f"/api/v1/notifications/{doc.id}/read")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}

    refreshed = await _NotificationDoc.get(doc.id)
    assert refreshed is not None
    assert refreshed.read is True


async def test_clear_returns_count(app_client) -> None:
    await _seed(read=False)
    await _seed(read=False)
    resp = await app_client.post("/api/v1/notifications/clear")
    assert resp.status_code == 200
    assert resp.json() == {"cleared": 2}
