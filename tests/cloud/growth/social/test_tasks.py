# tests/cloud/growth/social/test_tasks.py — the Growth › Social "By hand" tasks
# (``/api/v1/growth/social/tasks``). HTTP tests use the social router harness
# (fixed RequestContext per workspace, w1/w2 clients, mongomock-backed Beanie);
# the date-dependent cases call the service with an injected ``now``.
#
# Covers: CRUD round trip and created order; weekly ``due_today`` only on its
# weekday; done toggle on then off; ``done_on`` trimmed to the newest 60; another
# workspace's task or profile is a 404; the 21st task is a 422; bad link scheme,
# blank or long title and weekly-without-weekday are 422s; PATCH to daily clears
# the weekday, to weekly without one (or daily with one) is a 422.

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pocketpaw_ee.cloud._core.context import RequestContext, ScopeKind, request_context
from pocketpaw_ee.cloud._core.deps import current_workspace_id
from pocketpaw_ee.cloud._core.http import add_error_handler
from pocketpaw_ee.cloud.auth import current_active_user
from pocketpaw_ee.cloud.growth.social import service as social_service
from pocketpaw_ee.cloud.growth.social.dto import (
    CreateSocialTaskRequest,
    MarkSocialTaskDoneRequest,
)
from pocketpaw_ee.cloud.growth.social.router import router as social_router
from pocketpaw_ee.cloud.license import require_license

BASE = "/api/v1/growth/social"
TASKS = f"{BASE}/tasks"
MONDAY = datetime(2026, 11, 16, 9, 0, tzinfo=UTC)
TUESDAY = MONDAY + timedelta(days=1)


class _FakeMembership:
    def __init__(self, workspace: str) -> None:
        self.workspace = workspace
        self.role = "admin"


class _FakeUser:
    def __init__(self, workspace_id: str) -> None:
        self.id = "u1"
        self.active_workspace = workspace_id
        self.workspaces = [_FakeMembership(workspace_id)]


def _ctx(workspace_id: str) -> RequestContext:
    return RequestContext(
        user_id="u1",
        workspace_id=workspace_id,
        request_id="test",
        scope=ScopeKind.WORKSPACE,
        started_at=datetime.now(UTC),
    )


def _build_app(workspace_id: str) -> FastAPI:
    app = FastAPI()
    add_error_handler(app)
    app.include_router(social_router, prefix="/api/v1")

    async def _request_ctx() -> RequestContext:
        return _ctx(workspace_id)

    user = _FakeUser(workspace_id)
    app.dependency_overrides[request_context] = _request_ctx
    app.dependency_overrides[require_license] = lambda: None
    app.dependency_overrides[current_active_user] = lambda: user
    app.dependency_overrides[current_workspace_id] = lambda: user.active_workspace
    return app


@pytest_asyncio.fixture
async def w1(mongo_db: Any):
    async with AsyncClient(transport=ASGITransport(app=_build_app("w1")), base_url="http://t") as c:
        yield c


@pytest_asyncio.fixture
async def w2(mongo_db: Any):
    async with AsyncClient(transport=ASGITransport(app=_build_app("w2")), base_url="http://t") as c:
        yield c


async def _profile(client: AsyncClient) -> str:
    resp = await client.post(f"{BASE}/profiles")
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


async def _task(client: AsyncClient, profile_id: str, **body: Any) -> dict[str, Any]:
    payload = {"profile_id": profile_id, "title": "2 Reddit comments", "repeat": "daily", **body}
    resp = await client.post(TASKS, json=payload)
    assert resp.status_code == 200, resp.text
    return resp.json()


@pytest.mark.asyncio
async def test_crud_round_trip(w1):
    pid = await _profile(w1)
    first = await _task(w1, pid, title="  2 Reddit comments  ", link="https://reddit.com/r/x")
    assert first == {
        "id": first["id"],
        "profile_id": pid,
        "title": "2 Reddit comments",
        "link": "https://reddit.com/r/x",
        "repeat": "daily",
        "weekday": None,
        "due_today": True,
        "done_today": False,
    }
    second = await _task(w1, pid, title="Fill tracking", repeat="weekly", weekday=6)
    assert second["weekday"] == 6

    listed = (await w1.get(TASKS, params={"profile_id": pid})).json()["items"]
    assert [t["id"] for t in listed] == [first["id"], second["id"]]

    resp = await w1.patch(f"{TASKS}/{first['id']}", json={"title": "3 Reddit comments"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["title"] == "3 Reddit comments"
    assert resp.json()["link"] == "https://reddit.com/r/x"  # omitted = left alone

    resp = await w1.patch(f"{TASKS}/{first['id']}", json={"link": None})
    assert resp.json()["link"] is None

    assert (await w1.delete(f"{TASKS}/{first['id']}")).status_code == 204
    listed = (await w1.get(TASKS, params={"profile_id": pid})).json()["items"]
    assert [t["id"] for t in listed] == [second["id"]]
    assert (await w1.delete(f"{TASKS}/{first['id']}")).status_code == 404


@pytest.mark.asyncio
async def test_done_toggles_today_on_then_off(w1):
    task = await _task(w1, await _profile(w1))
    on = await w1.post(f"{TASKS}/{task['id']}/done", json={"done": True})
    assert on.status_code == 200, on.text
    assert on.json()["done_today"] is True
    again = await w1.post(f"{TASKS}/{task['id']}/done", json={"done": True})
    assert again.json()["done_today"] is True
    off = await w1.post(f"{TASKS}/{task['id']}/done", json={"done": False})
    assert off.json()["done_today"] is False
    listed = (await w1.get(TASKS, params={"profile_id": task["profile_id"]})).json()["items"]
    assert listed[0]["done_today"] is False


@pytest.mark.asyncio
async def test_weekly_is_due_only_on_its_weekday(mongo_db):
    ctx = _ctx("w1")
    pid = (await social_service.create_profile(ctx)).id
    body = CreateSocialTaskRequest(
        profile_id=pid, title="Fill tracking", repeat="weekly", weekday=0
    )
    made = await social_service.create_task(ctx, body, now=MONDAY)
    assert made.due_today is True
    assert (await social_service.list_tasks(ctx, pid, now=MONDAY)).items[0].due_today is True
    assert (await social_service.list_tasks(ctx, pid, now=TUESDAY)).items[0].due_today is False

    daily = CreateSocialTaskRequest(profile_id=pid, title="Reply", repeat="daily")
    await social_service.create_task(ctx, daily, now=TUESDAY)
    items = (await social_service.list_tasks(ctx, pid, now=TUESDAY)).items
    assert [t.due_today for t in items] == [False, True]


@pytest.mark.asyncio
async def test_done_is_per_utc_day_and_keeps_the_newest_60(mongo_db):
    ctx = _ctx("w1")
    pid = (await social_service.create_profile(ctx)).id
    body = CreateSocialTaskRequest(profile_id=pid, title="Reply", repeat="daily")
    task = await social_service.create_task(ctx, body, now=MONDAY)
    done = MarkSocialTaskDoneRequest(done=True)
    for i in range(61):
        await social_service.mark_task_done(ctx, task.id, done, now=MONDAY + timedelta(days=i))

    last = MONDAY + timedelta(days=60)
    assert (await social_service.list_tasks(ctx, pid, now=last)).items[0].done_today is True
    # Day 0 was trimmed; day 1 is the oldest kept.
    assert (await social_service.list_tasks(ctx, pid, now=MONDAY)).items[0].done_today is False
    day1 = MONDAY + timedelta(days=1)
    assert (await social_service.list_tasks(ctx, pid, now=day1)).items[0].done_today is True

    stored = await social_service._fetch_task_in_workspace("w1", task.id)
    assert len(stored.done_on) == 60
    assert stored.done_on[0] == date(2026, 11, 17)
    assert stored.done_on[-1] == last.date()


@pytest.mark.asyncio
async def test_another_workspaces_task_or_profile_is_404(w1, w2):
    pid = await _profile(w1)
    task = await _task(w1, pid)
    tid = task["id"]
    assert (await w2.get(TASKS, params={"profile_id": pid})).status_code == 404
    create = {"profile_id": pid, "title": "x", "repeat": "daily"}
    assert (await w2.post(TASKS, json=create)).status_code == 404
    assert (await w2.patch(f"{TASKS}/{tid}", json={"title": "y"})).status_code == 404
    assert (await w2.post(f"{TASKS}/{tid}/done", json={"done": True})).status_code == 404
    assert (await w2.delete(f"{TASKS}/{tid}")).status_code == 404
    assert (await w1.patch(f"{TASKS}/not-an-id", json={"title": "y"})).status_code == 404
    # w1 still has its task, untouched.
    listed = (await w1.get(TASKS, params={"profile_id": pid})).json()["items"]
    assert [(t["id"], t["title"], t["done_today"]) for t in listed] == [(tid, task["title"], False)]


@pytest.mark.asyncio
async def test_the_21st_task_is_422(w1):
    pid = await _profile(w1)
    for i in range(20):
        await _task(w1, pid, title=f"Task {i}")
    resp = await w1.post(TASKS, json={"profile_id": pid, "title": "One more", "repeat": "daily"})
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "social.task_limit"
    # The cap is per brand.
    await _task(w1, await _profile(w1))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        {"link": "javascript:alert(1)"},
        {"link": "ftp://example.com/file"},
        {"title": "   "},
        {"title": "x" * 81},
        {"repeat": "weekly"},
        {"repeat": "daily", "weekday": 2},
        {"repeat": "weekly", "weekday": 7},
        {"repeat": "monthly"},
    ],
)
async def test_bad_creates_are_422(w1, body):
    pid = await _profile(w1)
    payload = {"profile_id": pid, "title": "Task", "repeat": "daily", **body}
    assert (await w1.post(TASKS, json=payload)).status_code == 422


@pytest.mark.asyncio
async def test_patch_to_daily_clears_weekday_and_to_weekly_needs_one(w1):
    pid = await _profile(w1)
    weekly = await _task(w1, pid, repeat="weekly", weekday=3)
    resp = await w1.patch(f"{TASKS}/{weekly['id']}", json={"repeat": "daily"})
    assert resp.status_code == 200, resp.text
    assert (resp.json()["repeat"], resp.json()["weekday"]) == ("daily", None)

    resp = await w1.patch(f"{TASKS}/{weekly['id']}", json={"repeat": "weekly"})
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "social.task_weekday_required"

    resp = await w1.patch(f"{TASKS}/{weekly['id']}", json={"repeat": "weekly", "weekday": 6})
    assert (resp.json()["repeat"], resp.json()["weekday"]) == ("weekly", 6)
    resp = await w1.patch(f"{TASKS}/{weekly['id']}", json={"weekday": None})
    assert resp.status_code == 422

    resp = await w1.patch(f"{TASKS}/{weekly['id']}", json={"repeat": "daily", "weekday": 2})
    assert resp.json()["error"]["code"] == "social.task_weekday_daily"
    daily = await _task(w1, pid)
    resp = await w1.patch(f"{TASKS}/{daily['id']}", json={"weekday": 2})
    assert resp.status_code == 422

    for bad in ({}, {"link": "mailto:a@b.c"}, {"title": ""}):
        assert (await w1.patch(f"{TASKS}/{weekly['id']}", json=bad)).status_code == 422
