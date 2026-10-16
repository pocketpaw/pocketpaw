# tests/cloud/platform/test_platform_early_access_stats.py: the operator launch
# read, ``GET /api/v1/platform/stats/early-access``.
#
# Real routes on in-memory Mongo (mongomock-motor); only the session lookup is
# overridden. Seeds back-dated users, ledger debits, notify stamps, waitlist rows,
# balances, the early-access meter and the launch controls, then asserts the
# per-day series, cap share, today's meter and the controls. A failed meter read
# degrades to ``today: null`` plus an error line. A user without
# ``platform.stats.read`` is refused.
from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest
from bson import ObjectId
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

pytestmark = pytest.mark.asyncio

M = 1_000_000  # micro-credits per credit


def _oid_at(when: datetime) -> ObjectId:
    """An ObjectId minted at ``when``, unique (random tail)."""
    return ObjectId(ObjectId.from_datetime(when).binary[:4] + os.urandom(8))


def _client(user) -> AsyncClient:
    from pocketpaw_ee.cloud.auth import current_active_user
    from pocketpaw_ee.cloud.platform.stats import router

    app = FastAPI()
    app.include_router(router, prefix="/api/v1/platform")
    app.dependency_overrides[current_active_user] = lambda: user
    return AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://t",
        cookies={"paw_auth": "a-real-session-token"},
    )


async def _user(*, created: datetime, **fields):
    from beanie import PydanticObjectId
    from pocketpaw_ee.cloud.models.user import User

    doc = User(
        id=PydanticObjectId(_oid_at(created)),
        email=f"u{os.urandom(4).hex()}@paw.test",
        hashed_password="x",
        **fields,
    )
    await doc.insert()
    return doc


async def _ledger(*, workspace: str, when: datetime, micro: int, cause: str, applied=True):
    from pocketpaw_ee.cloud.models.credit import CreditLedgerEntry

    entry = CreditLedgerEntry(
        workspace=workspace,
        kind="spend" if micro < 0 else "grant",
        amount_delta_micro=micro,
        applied=applied,
        conditional=False,
        cause=cause,
        ref={},
        idempotency_key=f"k-{os.urandom(6).hex()}",
    )
    await entry.insert()
    await CreditLedgerEntry.get_pymongo_collection().update_one(
        {"_id": entry.id}, {"$set": {"createdAt": when}}
    )


async def _workspace(plan: str = "free") -> str:
    from pocketpaw_ee.cloud.models.workspace import Workspace

    ws = Workspace(name="w", slug=f"w-{os.urandom(4).hex()}", owner="o", plan=plan)
    await ws.insert()
    return str(ws.id)


async def _balance(workspace: str, micro: int) -> None:
    from pocketpaw_ee.cloud.models.credit import CreditBalance

    await CreditBalance(workspace=workspace, balance_micro=micro).insert()


async def test_series_cap_share_meter_and_controls(mongo_db):  # noqa: ARG001
    from pocketpaw_ee.cloud.launch_controls import service as controls_service
    from pocketpaw_ee.cloud.metering import service as metering
    from pocketpaw_ee.cloud.metering.domain import DailyMeter
    from pocketpaw_ee.cloud.models.platform_audit import PlatformAuditEvent
    from pocketpaw_ee.cloud.models.waitlist import WaitlistEntry

    now = datetime.now(UTC)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    d1 = today - timedelta(days=1)
    d2 = today - timedelta(days=2)
    old = today - timedelta(days=20)

    operator = await _user(created=old, platform_role="support")

    # Signups: two yesterday, one two days ago, one guest (not a signup), one
    # outside the window.
    await _user(created=d1 + timedelta(hours=1))
    await _user(created=d1 + timedelta(hours=2))
    await _user(created=d2 + timedelta(hours=3))
    await _user(created=d1 + timedelta(hours=4), is_guest=True)
    await _user(created=old + timedelta(hours=1))

    # Credits burned on unpaid workspaces only.
    unpaid = await _workspace()
    topped_up = await _workspace()
    pro = await _workspace(plan="pro")
    await _ledger(
        workspace=unpaid, when=d1 + timedelta(hours=1), micro=-150 * M, cause="compute_spend"
    )
    await _ledger(
        workspace=unpaid, when=d1 + timedelta(hours=5), micro=-50 * M, cause="litellm_spend"
    )
    await _ledger(
        workspace=unpaid,
        when=d1 + timedelta(hours=6),
        micro=-999 * M,
        cause="compute_spend",
        applied=False,
    )
    await _ledger(
        workspace=unpaid,
        when=today + timedelta(minutes=1),
        micro=-(7 * M + 1),
        cause="compute_spend",
    )
    await _ledger(workspace=unpaid, when=old, micro=-500 * M, cause="compute_spend")
    await _ledger(workspace=topped_up, when=old, micro=1000 * M, cause="top_up")
    await _ledger(workspace=topped_up, when=d1, micro=-300 * M, cause="compute_spend")
    await _ledger(workspace=pro, when=d1, micro=-400 * M, cause="compute_spend")
    # No plan field and a retired tier both resolve to free, so their burn counts.
    from pocketpaw_ee.cloud.models.workspace import Workspace

    planless = await _workspace()
    await Workspace.get_pymongo_collection().update_one(
        {"_id": ObjectId(planless)}, {"$unset": {"plan": ""}}
    )
    legacy = await _workspace(plan="team")
    await _ledger(workspace=planless, when=d1, micro=-20 * M, cause="compute_spend")
    await _ledger(workspace=legacy, when=d1, micro=-30 * M, cause="compute_spend")

    # Notify-me clicks and waitlist joins.
    await _user(created=old, notify_plans_at=d2 + timedelta(hours=1))
    await _user(created=old, notify_plans_at=d2 + timedelta(hours=2))
    await _user(created=old, notify_plans_at=old)
    await WaitlistEntry(email="a@x.test", created_at=d2 + timedelta(hours=1)).insert()
    await WaitlistEntry(email="b@x.test", created_at=today + timedelta(minutes=2)).insert()

    # Cap share: four granted users, two at or below zero.
    for balance in (0, -5 * M, 10 * M, 1):
        ws = await _workspace()
        await _balance(ws, balance)
        await _user(created=old, early_access_granted_at=old, early_access_workspace_id=ws)

    # Today's meter and the controls.
    assert await metering.try_spend(
        subject_type="workspace",
        subject_id=metering.PLATFORM_SUBJECT,
        meter=DailyMeter.EARLY_ACCESS_CREDITS,
        amount=300,
        cap=25_000,
    )
    await controls_service.update_launch_controls(
        {"signups_paused": True, "daily_free_ceiling_credits": 5_000}, operator_id="ops"
    )

    async with _client(operator) as client:
        resp = await client.get("/api/v1/platform/stats/early-access", params={"days": 3})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    by_date = {d["date"]: d for d in body["days"]}
    assert list(by_date) == [d.strftime("%Y-%m-%d") for d in (d2, d1, today)]

    def row(d: datetime) -> dict:
        return by_date[d.strftime("%Y-%m-%d")]

    assert row(d2) == {
        "date": d2.strftime("%Y-%m-%d"),
        "signups": 1,
        "credits_burned": 0,
        "notify": 2,
        "waitlist": 1,
    }
    assert row(d1)["signups"] == 2
    assert row(d1)["credits_burned"] == 250
    assert row(d1)["notify"] == 0
    assert row(today)["credits_burned"] == 8  # 7 credits and one micro, rounded up
    assert row(today)["waitlist"] == 1
    assert row(today)["signups"] == 0

    assert body["cap_share"] == 0.5
    assert body["today"] == {"meter": 300, "ceiling": 5_000}
    assert body["controls"] == {
        "signups_paused": True,
        "free_usage_paused": False,
        "daily_free_ceiling_credits": 5_000,
    }

    assert await PlatformAuditEvent.find_all().count() == 1


async def test_empty_deployment_is_zeros(mongo_db):  # noqa: ARG001
    operator = await _user(created=datetime.now(UTC) - timedelta(days=30), platform_role="support")

    async with _client(operator) as client:
        resp = await client.get("/api/v1/platform/stats/early-access")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["days"]) == 14
    assert all(
        d["signups"] == d["credits_burned"] == d["notify"] == d["waitlist"] == 0
        for d in body["days"]
    )
    assert body["cap_share"] == 0.0
    assert body["today"] == {"meter": 0, "ceiling": 25_000}


async def test_meter_failure_degrades_to_null_today(mongo_db, monkeypatch):  # noqa: ARG001
    from pocketpaw_ee.cloud.metering import service as metering

    async def boom() -> int:
        raise RuntimeError("meter down")

    monkeypatch.setattr(metering, "early_access_credits_used_today", boom)
    operator = await _user(created=datetime.now(UTC) - timedelta(days=30), platform_role="support")

    async with _client(operator) as client:
        resp = await client.get("/api/v1/platform/stats/early-access")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["today"] is None
    assert [e for e in body["errors"] if e.startswith("today:")] == ["today: meter down"]


async def test_refused_without_platform_stats_read(mongo_db):  # noqa: ARG001
    user = await _user(created=datetime.now(UTC), platform_role=None)

    async with _client(user) as client:
        resp = await client.get("/api/v1/platform/stats/early-access")

    assert resp.status_code == 403
    assert resp.json()["detail"] == "platform.not_operator"


async def test_days_is_bounded(mongo_db):  # noqa: ARG001
    operator = await _user(created=datetime.now(UTC), platform_role="support")

    async with _client(operator) as client:
        resp = await client.get("/api/v1/platform/stats/early-access", params={"days": 91})

    assert resp.status_code == 422
