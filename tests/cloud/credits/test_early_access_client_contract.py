# tests/cloud/credits/test_early_access_client_contract.py: the fields the
# early-access client reads.
#
# * GET /entitlements carries ``payments_open`` and ``free_model_allowlist``
#   straight from settings, so the client can hide every upgrade surface and
#   filter its model pickers without a second call.
# * POST /api/v1/early-access/notify stamps ``User.notify_plans_at`` once; a
#   second call returns the same timestamp. No session is a 401.
# * GET /auth/me carries ``early_access_granted_at`` and ``notify_plans_at``.
#
# Real routes, real in-memory Mongo, real ``request_context`` (only the session
# lookup is overridden). Settings are swapped with ``model_copy``.
from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

pytestmark = pytest.mark.asyncio

WS = "ws_client_contract"


def _settings(monkeypatch, **flags) -> None:
    from pocketpaw_ee.cloud.entitlements import dto

    from pocketpaw.config import get_settings

    settings = get_settings().model_copy(update=flags)
    monkeypatch.setattr(dto, "get_settings", lambda: settings)


def _entitlements_client() -> AsyncClient:
    from pocketpaw_ee.cloud.entitlements.router import router
    from pocketpaw_ee.cloud.license import require_license
    from pocketpaw_ee.cloud.shared.deps import current_workspace_id

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_license] = lambda: None
    app.dependency_overrides[current_workspace_id] = lambda: WS
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://t")


def _early_access_client(user) -> AsyncClient:
    from pocketpaw_ee.cloud._core.http import add_error_handler
    from pocketpaw_ee.cloud.auth import current_optional_user
    from pocketpaw_ee.cloud.early_access.router import router

    app = FastAPI()
    add_error_handler(app)
    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[current_optional_user] = lambda: user
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://t")


async def _user(**fields):
    from pocketpaw_ee.cloud.models.user import User

    doc = User(email="ada@paw.test", hashed_password="x", **fields)
    await doc.insert()
    return doc


async def test_entitlements_default_to_payments_closed_and_the_settings_allowlist(
    mongo_db,  # noqa: ARG001
    monkeypatch,
):
    _settings(monkeypatch, payments_open=False, free_model_allowlist=["claude-haiku-4-5"])

    async with _entitlements_client() as client:
        body = (await client.get("/entitlements")).json()

    assert body["payments_open"] is False
    assert body["free_model_allowlist"] == ["claude-haiku-4-5"]


async def test_entitlements_follow_the_settings(mongo_db, monkeypatch):  # noqa: ARG001
    _settings(monkeypatch, payments_open=True, free_model_allowlist=["a", "b"])

    async with _entitlements_client() as client:
        body = (await client.get("/entitlements")).json()

    assert body["payments_open"] is True
    assert body["free_model_allowlist"] == ["a", "b"]


async def test_notify_stamps_once_and_repeats_the_same_time(mongo_db):  # noqa: ARG001
    from pocketpaw_ee.cloud.models.user import User

    user = await _user()
    async with _early_access_client(user) as client:
        first = await client.post("/api/v1/early-access/notify")
        second = await client.post("/api/v1/early-access/notify")

    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    stored = (await User.get(user.id)).notify_plans_at
    assert stored is not None
    # Zoned on the wire: a zone-less ISO string parses as browser-local time.
    assert first.json()["notified_at"].endswith(("Z", "+00:00"))
    got = datetime.fromisoformat(first.json()["notified_at"])
    assert abs(got.replace(tzinfo=None) - stored.replace(tzinfo=None)).total_seconds() < 0.001


async def test_notify_keeps_an_earlier_stamp(mongo_db):  # noqa: ARG001
    earlier = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
    user = await _user(notify_plans_at=earlier)

    async with _early_access_client(user) as client:
        resp = await client.post("/api/v1/early-access/notify")

    assert resp.status_code == 200
    got = datetime.fromisoformat(resp.json()["notified_at"])
    assert got.replace(tzinfo=None) == earlier.replace(tzinfo=None)


async def test_notify_without_a_session_is_401(mongo_db):  # noqa: ARG001
    async with _early_access_client(None) as client:
        resp = await client.post("/api/v1/early-access/notify")

    assert resp.status_code == 401


async def test_auth_me_carries_grant_and_notify_stamps(mongo_db):  # noqa: ARG001
    from pocketpaw_ee.cloud._core.context import RequestContext, ScopeKind
    from pocketpaw_ee.cloud.auth import service
    from pocketpaw_ee.cloud.auth.dto import auth_user_to_profile_out

    granted = datetime(2026, 10, 15, 9, 0, tzinfo=UTC)
    notified = datetime(2026, 10, 16, 9, 0, tzinfo=UTC)
    user = await _user(early_access_granted_at=granted, notify_plans_at=notified)
    ctx = RequestContext(
        user_id=str(user.id),
        workspace_id=None,
        request_id="r",
        scope=ScopeKind.NONE,
        started_at=datetime.now(UTC),
        scopes=None,
    )

    out = auth_user_to_profile_out(await service.get_profile(ctx))

    # Zoned on the wire, like notified_at.
    assert out.early_access_granted_at == "2026-10-15T09:00:00+00:00"
    assert out.notify_plans_at == "2026-10-16T09:00:00+00:00"


async def test_auth_me_stamps_are_null_for_a_new_user(mongo_db):  # noqa: ARG001
    from pocketpaw_ee.cloud._core.context import RequestContext, ScopeKind
    from pocketpaw_ee.cloud.auth import service
    from pocketpaw_ee.cloud.auth.dto import auth_user_to_profile_out

    user = await _user()
    ctx = RequestContext(
        user_id=str(user.id),
        workspace_id=None,
        request_id="r",
        scope=ScopeKind.NONE,
        started_at=datetime.now(UTC),
        scopes=None,
    )

    dump = auth_user_to_profile_out(await service.get_profile(ctx)).model_dump()

    assert dump["early_access_granted_at"] is None
    assert dump["notify_plans_at"] is None
