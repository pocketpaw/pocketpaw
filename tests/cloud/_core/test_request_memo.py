"""The Workspace plan/overrides reads are memoised per GET/HEAD request, and only then.

``require_plan_feature`` reads the plan once and ``resolve_entitlements`` reads
plan + overrides, as two separate calls on purpose (tests patch
``get_workspace_plan`` alone). ``request_memo`` sits above those calls, so on
one GET the plan is read once and the overrides once, however many times the
resolver runs. What must hold:

  * a write request re-reads every time (it may read, change, re-read);
  * nothing outside a request is memoised, nor a task that outlives it;
  * a failed read is not cached;
  * the memo never crosses from one request to the next.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from pocketpaw_ee.cloud._core import ee_auth_bridge
from pocketpaw_ee.cloud._core.deps import current_workspace_id, require_plan_feature
from pocketpaw_ee.cloud._core.ee_auth_bridge import EEAuthBridgeMiddleware, request_memo
from pocketpaw_ee.cloud.entitlements.service import resolve_entitlements
from pocketpaw_ee.cloud.workspace import service as workspace_service

_WS = "ws-memo"


@pytest.fixture
def reads(monkeypatch):
    plan = AsyncMock(return_value="free")
    overrides = AsyncMock(return_value=None)
    monkeypatch.setattr(workspace_service, "get_workspace_plan", plan)
    monkeypatch.setattr(workspace_service, "get_workspace_overrides", overrides)
    return plan, overrides


def _client() -> TestClient:
    app = FastAPI()
    app.add_middleware(EEAuthBridgeMiddleware)
    app.dependency_overrides[current_workspace_id] = lambda: _WS

    async def _handler() -> dict:
        await resolve_entitlements(_WS)
        await resolve_entitlements(_WS)
        return {"ok": True}

    gate = [Depends(require_plan_feature("pockets"))]
    app.add_api_route("/api/v1/gated", _handler, methods=["GET", "POST"], dependencies=gate)

    return TestClient(app)


def test_a_get_reads_the_plan_once_and_the_overrides_once(reads) -> None:
    plan, overrides = reads
    r = _client().get("/api/v1/gated")
    assert r.status_code == 200, r.text
    assert plan.await_count == 1
    assert overrides.await_count == 1


def test_the_memo_does_not_cross_requests(reads) -> None:
    plan, overrides = reads
    client = _client()
    client.get("/api/v1/gated")
    client.get("/api/v1/gated")
    assert plan.await_count == 2
    assert overrides.await_count == 2


def test_a_write_request_is_never_memoised(reads) -> None:
    plan, overrides = reads
    r = _client().post("/api/v1/gated")
    assert r.status_code == 200, r.text
    assert plan.await_count == 3
    assert overrides.await_count == 2


async def test_nothing_outside_a_request_is_memoised(reads) -> None:
    plan, _ = reads
    await resolve_entitlements(_WS)
    await resolve_entitlements(_WS)
    assert plan.await_count == 2


async def test_a_closed_scope_is_not_read() -> None:
    scope = ee_auth_bridge._RequestScope(memo={})
    reset = ee_auth_bridge._request_scope.set(scope)
    try:
        fetch = AsyncMock(return_value="go")
        assert await request_memo("k", fetch) == "go"
        assert await request_memo("k", fetch) == "go"
        assert fetch.await_count == 1
        scope.close()
        assert await request_memo("k", fetch) == "go"
        assert fetch.await_count == 2
    finally:
        ee_auth_bridge._request_scope.reset(reset)


async def test_a_failed_read_is_not_cached_and_none_is() -> None:
    scope = ee_auth_bridge._RequestScope(memo={})
    reset = ee_auth_bridge._request_scope.set(scope)
    try:
        flaky = AsyncMock(side_effect=[RuntimeError("mongo flap"), "pro"])
        with pytest.raises(RuntimeError):
            await request_memo("plan", flaky)
        assert await request_memo("plan", flaky) == "pro"
        assert await request_memo("plan", flaky) == "pro"
        assert flaky.await_count == 2

        missing = AsyncMock(return_value=None)
        assert await request_memo("gone", missing) is None
        assert await request_memo("gone", missing) is None
        assert missing.await_count == 1
    finally:
        ee_auth_bridge._request_scope.reset(reset)
