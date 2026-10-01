# tests/cloud/growth/test_delivery_queue.py — per-channel delivery queues, the
# workspace mock-delivery setting, and the in-process mock provider: settings
# read/write + RBAC, queue selection / recipient / latest delivery, mock_deliver
# (sent, blocked), the executor's mock branch (no enqueue), deliver-approved,
# the WhatsApp cap ignoring mock rows, and a mock-sent draft reaching the
# follow-up sweep. Reuses the test_gate.py harness on a REAL Workspace doc,
# since the setting lives on it.

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from pocketpaw_ee.cloud.growth import mock_delivery
from pocketpaw_ee.cloud.growth import service as growth_service
from pocketpaw_ee.cloud.models.workspace import Workspace

from tests.cloud.growth import test_gate as _gate
from tests.cloud.growth.test_gate import _build_growth_app, _build_instinct_app, _FakeUser

# The gate suite's fixtures, re-exported by assignment (an import made every
# test that takes one an F811 redefinition).
gate_store = _gate.gate_store
pool = _gate.pool
authorized = _gate.authorized
_clear_locks = _gate._clear_locks
_enterprise_plan = _gate._enterprise_plan


@pytest.fixture(autouse=True)
def _instant_delivery(monkeypatch):
    monkeypatch.setenv("GROWTH_MOCK_DELIVERY_SECONDS", "0")
    yield
    mock_delivery._IN_FLIGHT.clear()


@pytest_asyncio.fixture
async def ws_id(mongo_db: Any) -> str:
    ws = Workspace(name="Acme", slug="acme-growth", owner="u1")
    await ws.insert()
    return str(ws.id)


async def _client(ws_id: str, role: str = "admin") -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=_build_growth_app(ws_id, role=role)), base_url="http://t"
    )


@pytest_asyncio.fixture
async def admin(ws_id: str) -> AsyncClient:
    async with await _client(ws_id) as client:
        yield client


@pytest_asyncio.fixture
async def tray(ws_id: str) -> AsyncClient:
    transport = ASGITransport(app=_build_instinct_app(_FakeUser("approver-1", ws_id)))
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        yield client


async def _prospect(client: AsyncClient, **overrides: Any) -> dict:
    payload = {
        "name": "Sam Founder",
        "company": "Acme Dental",
        "domain": "acme-dental.com",
        "source": "manual",
        "emails": ["not-an-address", "sam@acme-dental.com"],
        "whatsapp_number": "+15550001111",
        "linkedin_url": "https://linkedin.com/in/sam",
        "opted_in": True,
    }
    payload.update(overrides)
    resp = await client.post("/api/v1/growth/prospects", json=payload)
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _draft(client: AsyncClient, prospect_id: str, channel: str = "email") -> dict:
    body: dict[str, Any] = {"channel": channel, "body": f"Hello on {channel}"}
    if channel == "email":
        body["subject"] = "Quick idea"
    resp = await client.post(f"/api/v1/growth/prospects/{prospect_id}/drafts", json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()


_PATH = {
    "proposed": ("proposed",),
    "approved": ("proposed", "approved"),
    "sent": ("proposed", "approved", "sent"),
    "rejected": ("rejected",),
}


async def _walk(ws_id: str, draft_id: str, status: str) -> None:
    for step in _PATH.get(status, ()):
        await growth_service.gate_transition(ws_id, draft_id, step)


async def _status(client: AsyncClient, draft_id: str) -> str:
    resp = await client.get("/api/v1/growth/drafts")
    return next(d["status"] for d in resp.json() if d["id"] == draft_id)


async def _logs(ws_id: str, draft_id: str) -> list[Any]:
    from pocketpaw_ee.cloud.models.message_log import MessageLog

    return await MessageLog.find({"workspace": ws_id, "draft_id": draft_id}).to_list()


async def _attempt(ws_id: str, draft: dict, status: str, provider: str = "mock") -> str:
    return await growth_service.record_delivery_attempt(
        ws_id,
        draft_id=draft["id"],
        prospect_id=draft["prospect_id"],
        channel=draft["channel"],
        provider=provider,
        to_address="x",
        status=status,
    )


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_settings_default_off_and_patch_persists(admin, ws_id):
    assert (await admin.get("/api/v1/growth/settings")).json() == {"mock_delivery": False}

    resp = await admin.patch("/api/v1/growth/settings", json={"mock_delivery": True})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"mock_delivery": True}
    assert (await admin.get("/api/v1/growth/settings")).json() == {"mock_delivery": True}
    doc = await Workspace.get(ws_id)
    assert doc.settings.growth_mock_delivery is True
    assert doc.settings.allow_invites is True  # siblings untouched


@pytest.mark.asyncio
async def test_member_cannot_patch_settings(ws_id):
    async with await _client(ws_id, role="member") as member:
        assert (await member.get("/api/v1/growth/settings")).status_code == 200
        resp = await member.patch("/api/v1/growth/settings", json={"mock_delivery": True})
    assert resp.status_code == 403
    assert (await Workspace.get(ws_id)).settings.growth_mock_delivery is False


# ---------------------------------------------------------------------------
# Queue
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("channel", "to"),
    [
        ("email", "sam@acme-dental.com"),
        ("whatsapp", "+15550001111"),
        ("linkedin", "https://linkedin.com/in/sam"),
    ],
)
async def test_queue_selects_channel_and_live_statuses(admin, ws_id, channel, to):
    prospect = await _prospect(admin)
    kept = {}
    for status in ("draft", "proposed", "approved", "sent", "rejected"):
        draft = await _draft(admin, prospect["id"], channel)
        await _walk(ws_id, draft["id"], status)
        kept[draft["id"]] = status
    other = "linkedin" if channel != "linkedin" else "email"
    other_draft = await _draft(admin, prospect["id"], other)
    await _walk(ws_id, other_draft["id"], "approved")

    resp = await admin.get(f"/api/v1/growth/queue/{channel}")
    assert resp.status_code == 200, resp.text
    items = resp.json()

    assert [i["draft"]["status"] for i in items] == ["sent", "approved", "proposed"]
    assert all(i["draft"]["channel"] == channel for i in items)
    head = items[0]
    assert head["to"] == to
    assert head["opted_in"] is True
    assert head["prospect_domain"] == "acme-dental.com"
    assert head["tier"] == "unqualified"
    assert head["delivery"] is None


@pytest.mark.asyncio
async def test_queue_attaches_the_latest_delivery(admin, ws_id):
    prospect = await _prospect(admin)
    draft = await _draft(admin, prospect["id"])
    await _walk(ws_id, draft["id"], "approved")
    await _attempt(ws_id, draft, "failed", provider="mailtrap")
    log_id = await _attempt(ws_id, draft, "sending")
    await growth_service.finish_delivery_attempt(log_id, workspace_id=ws_id, status="sent")

    (item,) = (await admin.get("/api/v1/growth/queue/email")).json()
    delivery = item["delivery"]
    assert delivery["outcome"] == "sent"
    assert delivery["provider"] == "mock"
    assert delivery["mock"] is True
    assert delivery["sent_at"] and delivery["at"]


@pytest.mark.asyncio
async def test_queue_rejects_an_unknown_channel(admin):
    assert (await admin.get("/api/v1/growth/queue/sms")).status_code == 422


# ---------------------------------------------------------------------------
# mock_deliver
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mock_deliver_sends_email(admin, ws_id):
    prospect = await _prospect(admin)
    draft = await _draft(admin, prospect["id"])
    await _walk(ws_id, draft["id"], "approved")

    await mock_delivery.mock_deliver(ws_id, draft["id"], "email")

    (row,) = await _logs(ws_id, draft["id"])
    assert (row.outcome, row.provider, row.to_address) == ("sent", "mock", "sam@acme-dental.com")
    assert row.sent_at is not None
    assert row.provider_message_id.startswith("mock-")
    assert await _status(admin, draft["id"]) == "sent"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("channel", "prospect_overrides", "reason"),
    [
        ("whatsapp", {"opted_in": False}, "not_opted_in"),
        ("email", {"emails": []}, "no_address"),
    ],
)
async def test_mock_deliver_blocks_an_ineligible_draft(
    admin, ws_id, channel, prospect_overrides, reason
):
    prospect = await _prospect(admin, **prospect_overrides)
    draft = await _draft(admin, prospect["id"], channel)
    await _walk(ws_id, draft["id"], "approved")

    await mock_delivery.mock_deliver(ws_id, draft["id"], channel)

    (row,) = await _logs(ws_id, draft["id"])
    assert (row.outcome, row.provider, row.blocked_reason) == ("blocked", "mock", reason)
    assert row.error
    assert await _status(admin, draft["id"]) == "approved"


# ---------------------------------------------------------------------------
# Executor
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("mock_on", [True, False])
async def test_approve_uses_mock_delivery_only_when_on(
    admin, tray, ws_id, gate_store, pool, authorized, mock_on
):
    if mock_on:
        await admin.patch("/api/v1/growth/settings", json={"mock_delivery": True})
    prospect = await _prospect(admin)
    draft = await _draft(admin, prospect["id"])
    proposal = (await admin.post(f"/api/v1/growth/drafts/{draft['id']}/propose")).json()

    resp = await tray.post(f"/instinct/actions/{proposal['proposal_id']}/approve")
    assert resp.status_code == 200, resp.text
    await mock_delivery.drain_mock_deliveries()

    action = await gate_store.get_action(proposal["proposal_id"])
    assert str(getattr(action.status, "value", action.status)) == "executed"
    if mock_on:
        assert pool.calls == []
        assert await _status(admin, draft["id"]) == "sent"
        assert action.parameters["_growth_send"]["outcome"]["detail"].startswith(
            "growth.mock_delivery started"
        )
    else:
        assert [c[0] for c in pool.calls] == ["growth.dispatch"]
        assert await _status(admin, draft["id"]) == "approved"
        assert await _logs(ws_id, draft["id"]) == []


# ---------------------------------------------------------------------------
# deliver-approved
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deliver_approved_refuses_when_off_and_for_linkedin(admin):
    resp = await admin.post("/api/v1/growth/queue/email/deliver-approved")
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "growth.mock_delivery_off"

    await admin.patch("/api/v1/growth/settings", json={"mock_delivery": True})
    resp = await admin.post("/api/v1/growth/queue/linkedin/deliver-approved")
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "queue.not_deliverable"


@pytest.mark.asyncio
async def test_deliver_approved_starts_only_idle_approved_drafts(admin, ws_id):
    await admin.patch("/api/v1/growth/settings", json={"mock_delivery": True})
    prospect = await _prospect(admin)
    idle, in_flight, stranded, proposed = [await _draft(admin, prospect["id"]) for _ in range(4)]
    for draft in (idle, in_flight, stranded):
        await _walk(ws_id, draft["id"], "approved")
    await _walk(ws_id, proposed["id"], "proposed")
    await _attempt(ws_id, idle, "failed")
    await _attempt(ws_id, in_flight, "sending")
    await _attempt(ws_id, stranded, "sending")
    (row,) = await _logs(ws_id, stranded["id"])
    row.createdAt = datetime.now(UTC) - timedelta(minutes=5)
    await row.save()

    resp = await admin.post("/api/v1/growth/queue/email/deliver-approved")
    assert resp.status_code == 200, resp.text
    assert sorted(resp.json()["started"]) == sorted([idle["id"], stranded["id"]])
    await mock_delivery.drain_mock_deliveries()
    assert await _status(admin, idle["id"]) == "sent"
    assert await _status(admin, stranded["id"]) == "sent"
    assert await _status(admin, in_flight["id"]) == "approved"


# ---------------------------------------------------------------------------
# Real-provider budget and the follow-up loop
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_whatsapp_cap_ignores_mock_rows(admin, ws_id):
    prospect = await _prospect(admin)
    draft = await _draft(admin, prospect["id"], "whatsapp")
    for _ in range(3):
        await _attempt(ws_id, draft, "sent", provider="mock")
    await _attempt(ws_id, draft, "sent", provider="msg91")

    since = datetime.now(UTC) - timedelta(hours=1)
    assert await growth_service.count_whatsapp_attempts_since(ws_id, since) == 1


@pytest.mark.asyncio
async def test_mock_sent_draft_reaches_the_followup_sweep(admin, ws_id):
    prospect = await _prospect(admin)
    draft = await _draft(admin, prospect["id"])
    await _walk(ws_id, draft["id"], "approved")
    await mock_delivery.mock_deliver(ws_id, draft["id"], "email")

    rows = await growth_service.list_sent_drafts_for_followup()
    (row,) = [r for r in rows if r["id"] == draft["id"]]
    (log,) = await _logs(ws_id, draft["id"])
    assert row["sent_at"] == log.sent_at.replace(tzinfo=log.sent_at.tzinfo or UTC)
