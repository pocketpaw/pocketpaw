# test_cycles_snapshot_scheduler.py — manual-trigger endpoint +
# in-process scheduler tests.
# Created: 2026-05-16 — Mission Control backend completion. Covers the
#   POST /cycles/{id}/snapshot endpoint (manual trigger), the
#   list_active_workspace_ids helper used by the scheduler loop, and the
#   idempotency rule that the second call within the same UTC day is a
#   no-op (already covered at the service layer; this re-asserts via the
#   HTTP boundary).
"""Tests for the manual snapshot endpoint and the scheduler helpers.

The actual ``asyncio`` loop in ``ee.cloud.cycles.scheduler`` isn't
exercised directly — exercising a wall-clock 24h loop in pytest would
either freeze the run or require mocking ``datetime.now`` everywhere.
Instead we verify (a) the manual-trigger endpoint works and is
idempotent, and (b) ``list_active_workspace_ids`` returns the right
set of tenants, which is what the loop iterates.
"""

from __future__ import annotations

import sys
import types
from datetime import UTC, date, datetime
from typing import Any

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from ee.cloud._core.context import RequestContext, ScopeKind, request_context
from ee.cloud._core.http import add_error_handler
from ee.cloud.cycles import service as cycles_service
from ee.cloud.cycles.dto import CreateCycleRequest
from ee.cloud.cycles.router import router as cycles_router
from ee.cloud.license import require_license

pytestmark = pytest.mark.usefixtures("mongo_db")


class _FakeTask:
    def __init__(self, status: str, task_id: str = "t1") -> None:
        self.status = status
        self.id = task_id


def _install_fake_tasks(tasks_to_return: list[_FakeTask] | None = None) -> None:
    """Install a stub tasks package so the cycles service's lazy-import
    picks up controllable test doubles. Mirrors the helper in
    ``test_cycles_daily_snapshot.py`` so the snapshot path is
    deterministic without depending on the real Tasks Beanie writes.
    """
    mod_tasks = types.ModuleType("ee.cloud.tasks")
    mod_service = types.ModuleType("ee.cloud.tasks.service")
    mod_dto = types.ModuleType("ee.cloud.tasks.dto")

    class _ListReq:
        def __init__(self, cycle_id: str | None = None, **_: object) -> None:
            self.cycle_id = cycle_id

    mod_dto.ListTasksRequest = _ListReq  # type: ignore[attr-defined]

    async def _list(_ctx, _body):  # type: ignore[no-untyped-def]
        return list(tasks_to_return or [])

    mod_service.agent_list_tasks = _list  # type: ignore[attr-defined]
    mod_tasks.service = mod_service  # type: ignore[attr-defined]
    mod_tasks.dto = mod_dto  # type: ignore[attr-defined]

    sys.modules["ee.cloud.tasks"] = mod_tasks
    sys.modules["ee.cloud.tasks.service"] = mod_service
    sys.modules["ee.cloud.tasks.dto"] = mod_dto


def _uninstall_fake_tasks() -> None:
    for name in ("ee.cloud.tasks", "ee.cloud.tasks.service", "ee.cloud.tasks.dto"):
        sys.modules.pop(name, None)


@pytest.fixture
def fake_tasks():
    bucket: list[_FakeTask] = []
    _install_fake_tasks(bucket)
    try:
        yield bucket
    finally:
        _uninstall_fake_tasks()


def _ctx(workspace: str = "w1", user: str = "u1") -> RequestContext:
    return RequestContext(
        user_id=user,
        workspace_id=workspace,
        request_id="snap",
        scope=ScopeKind.WORKSPACE,
        started_at=datetime.now(UTC),
    )


def _build_app(workspace_id: str | None = "w1", user_id: str = "u1") -> FastAPI:
    app = FastAPI()
    add_error_handler(app)
    app.include_router(cycles_router)

    async def _ctx_dep() -> RequestContext:
        return _ctx(workspace_id, user_id)

    app.dependency_overrides[request_context] = _ctx_dep
    app.dependency_overrides[require_license] = lambda: None
    return app


@pytest_asyncio.fixture
async def http_client(mongo_db: Any) -> AsyncClient:
    app = _build_app(workspace_id="w1")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        yield client


# ---------------------------------------------------------------------------
# Manual snapshot endpoint
# ---------------------------------------------------------------------------


async def test_manual_snapshot_appends_point(fake_tasks, http_client: AsyncClient) -> None:
    fake_tasks.extend([_FakeTask("proposed"), _FakeTask("in_progress"), _FakeTask("done")])

    r = await http_client.post(
        "/cycles",
        json={
            "name": "live cycle",
            "start": "2026-05-01",
            "end": "2026-05-31",
            "status": "active",
        },
    )
    assert r.status_code == 200, r.text
    cid = r.json()["id"]

    snap = await http_client.post(f"/cycles/{cid}/snapshot")
    assert snap.status_code == 200, snap.text
    body = snap.json()
    # Manual trigger should append today's point with current counters.
    assert body is not None
    assert body["scope"] == 3
    assert body["started"] == 2  # in_progress + done
    assert body["completed"] == 1


async def test_manual_snapshot_idempotent_within_day(fake_tasks, http_client: AsyncClient) -> None:
    fake_tasks.append(_FakeTask("in_progress"))
    r = await http_client.post(
        "/cycles",
        json={
            "name": "live cycle",
            "start": "2026-05-01",
            "end": "2026-05-31",
            "status": "active",
        },
    )
    cid = r.json()["id"]

    first = await http_client.post(f"/cycles/{cid}/snapshot")
    second = await http_client.post(f"/cycles/{cid}/snapshot")
    assert first.status_code == 200
    assert first.json() is not None
    assert second.status_code == 200
    # Same calendar day → second call returns null body, no new point appended.
    assert second.json() is None


async def test_manual_snapshot_404_for_other_workspace(fake_tasks, mongo_db: Any) -> None:
    """Forcing a snapshot for a cycle in another workspace returns 404,
    not a leaked snapshot."""
    fake_tasks.append(_FakeTask("proposed"))

    # Create in w1 via service path.
    out = await cycles_service.agent_create_cycle(
        _ctx(workspace="w1"),
        CreateCycleRequest(
            name="w1-cycle",
            start=date(2026, 5, 1),
            end=date(2026, 5, 31),
            status="active",
        ),
    )

    # Hit the snapshot endpoint as w2.
    app = _build_app(workspace_id="w2")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        r = await client.post(f"/cycles/{out.id}/snapshot")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Workspace discovery helper used by the scheduler loop
# ---------------------------------------------------------------------------


async def test_list_active_workspace_ids_returns_workspaces_with_active_cycles() -> None:
    """The scheduler loop iterates this set; verify it gives only the
    tenants that actually have an active cycle right now."""

    await cycles_service.agent_create_cycle(
        _ctx(workspace="ws-with-active"),
        CreateCycleRequest(
            name="live",
            start=date(2026, 5, 1),
            end=date(2026, 5, 31),
            status="active",
        ),
    )
    await cycles_service.agent_create_cycle(
        _ctx(workspace="ws-upcoming-only"),
        CreateCycleRequest(
            name="not-live",
            start=date(2026, 7, 1),
            end=date(2026, 7, 31),
            status="upcoming",
        ),
    )

    ids = await cycles_service.list_active_workspace_ids()
    assert "ws-with-active" in ids
    assert "ws-upcoming-only" not in ids


# ---------------------------------------------------------------------------
# Scheduler start/stop wiring (no clock dependence)
# ---------------------------------------------------------------------------


async def test_scheduler_start_and_stop_are_idempotent() -> None:
    """start → start → stop → stop should not raise. Verifies the
    app-state plumbing without exercising the 24h sleep loop itself."""

    from ee.cloud.cycles import scheduler

    app = FastAPI()
    await scheduler.start_in_process_scheduler(app)
    task = getattr(app.state, "_cycle_snapshot_scheduler_task", None)
    assert task is not None
    assert not task.done()

    # Second start is a no-op (same task instance).
    await scheduler.start_in_process_scheduler(app)
    assert getattr(app.state, "_cycle_snapshot_scheduler_task", None) is task

    await scheduler.stop_in_process_scheduler(app)
    # After stop, the slot is cleared.
    assert getattr(app.state, "_cycle_snapshot_scheduler_task", None) is None

    # Second stop is also a no-op.
    await scheduler.stop_in_process_scheduler(app)
