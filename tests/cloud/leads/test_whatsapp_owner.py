# tests/cloud/leads/test_whatsapp_owner.py
# WhatsApp alerts to the site owner's own numbers, end to end (mongomock, the
# real routes, dispatch and outbox; HTTP through an httpx MockTransport, so Meta
# is never called):
#   * numbers: E.164 validation, consent required, cap of 3, duplicates, delete
#     with an encoded "+", member 403
#   * the first number switches ``whatsapp_owner`` on for every event; the
#     settings API accepts that sink and still rejects partner ``whatsapp``
#   * dispatch: one row per number; mock mode marks rows sent with provider
#     "mock" and makes no request; live mode posts the template to the Graph API
#   * Meta 131047 is dead; a number removed after queueing is dead
#   * the per-site hourly cap (dispatch skips, the test route answers 429)

from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from pocketpaw_ee.cloud.leads import notification_settings as ns
from pocketpaw_ee.cloud.leads import whatsapp_numbers as wa
from pocketpaw_ee.cloud.models.notification_outbox import NotificationOutboxItem
from pocketpaw_ee.cloud.notifications import outbox

from tests.cloud.leads.test_notification_settings import (  # noqa: F401 — fixtures
    _app,
    _capture,
    _emails,
    _lead,
    _site,
    _tenant,
    email_on,
    net,
    public_dns,
)

pytestmark = pytest.mark.usefixtures("mongo_db", "public_dns", "email_on", "wa_mock")

OWNER_EMAIL = "owner@acme.test"
NUM_A = "+14155550123"
NUM_B = "+919876543210"
_WA_ENV = (
    "POCKETPAW_WA_NOTIFY_ACCESS_TOKEN",
    "POCKETPAW_WA_NOTIFY_PHONE_NUMBER_ID",
    "POCKETPAW_WA_NOTIFY_SEND_AS",
    "POCKETPAW_WA_NOTIFY_TEMPLATE",
    "POCKETPAW_WA_NOTIFY_TEMPLATE_LANG",
    "POCKETPAW_WA_NOTIFY_API_VERSION",
)


@pytest.fixture
def wa_mock(monkeypatch):
    from pocketpaw.config import get_settings

    for key in _WA_ENV:
        monkeypatch.delenv(key, raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def wa_live(monkeypatch, wa_mock):
    from pocketpaw.config import get_settings

    monkeypatch.setenv("POCKETPAW_WA_NOTIFY_ACCESS_TOKEN", "EAAG-test-token")
    monkeypatch.setenv("POCKETPAW_WA_NOTIFY_PHONE_NUMBER_ID", "1098765")
    monkeypatch.setenv("POCKETPAW_WA_NOTIFY_TEMPLATE", "paw_new_lead")
    get_settings.cache_clear()


@pytest.fixture
def meta_answer(monkeypatch):
    """Answer Graph API calls with ``state['response']`` (default 200) and
    everything else (Cloudflare email) like the shared ``net`` fixture."""
    state: dict = {"response": None, "requests": []}

    def handler(request: httpx.Request) -> httpx.Response:
        state["requests"].append(request)
        if request.url.host == "graph.facebook.com":
            return state["response"] or httpx.Response(200, json={"messages": [{"id": "w"}]})
        if "api.cloudflare.com" in str(request.url):
            to = json.loads(request.content)["to"]
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "errors": [],
                    "result": {"delivered": to, "queued": [], "permanent_bounces": []},
                },
            )
        return httpx.Response(200)

    real = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    return state


def _graph(requests) -> list[httpx.Request]:
    return [r for r in requests if r.url.host == "graph.facebook.com"]


async def _rows() -> list[NotificationOutboxItem]:
    return await NotificationOutboxItem.find({"sink": "whatsapp_owner"}).to_list()


def _base(site) -> str:
    return f"/api/v1/sites/{site.id}/lead-notifications"


async def _admin(ws):
    return AsyncClient(transport=ASGITransport(app=_app("admin", ws)), base_url="http://t")


# ---------------------------------------------------------------------------
# Numbers
# ---------------------------------------------------------------------------


async def test_add_number_validates_e164_and_requires_consent(net) -> None:  # noqa: F811
    ws, _ = await _tenant()
    site = await _site(ws)
    async with await _admin(ws) as admin:
        no_consent = await admin.post(f"{_base(site)}/whatsapp/numbers", json={"e164": NUM_A})
        assert no_consent.status_code == 422
        assert no_consent.json()["error"]["code"] == "lead_notifications.whatsapp_consent_required"
        for bad in ("14155550123", "+0123456789", "+1234567", "+1234567890123456", "+1 415 abc"):
            resp = await admin.post(
                f"{_base(site)}/whatsapp/numbers", json={"e164": bad, "consent": True}
            )
            assert resp.status_code == 422, bad
            assert resp.json()["error"]["code"] == "lead_notifications.invalid_phone"
        ok = await admin.post(
            f"{_base(site)}/whatsapp/numbers", json={"e164": "+1 (415) 555-0123", "consent": True}
        )
        assert ok.status_code == 200
        state = ok.json()["whatsapp"]
        assert [n["e164"] for n in state["numbers"]] == [NUM_A]
        assert state["numbers"][0]["consent_at"] is not None
        assert state["mode"] == "mock" and state["send_as"] == "template"
        assert state["max_numbers"] == 3


async def test_cap_duplicates_and_delete(net) -> None:  # noqa: F811
    ws, _ = await _tenant()
    site = await _site(ws)
    async with await _admin(ws) as admin:
        for number in (NUM_A, NUM_B, "+447700900123"):
            resp = await admin.post(
                f"{_base(site)}/whatsapp/numbers", json={"e164": number, "consent": True}
            )
            assert resp.status_code == 200
        dup = await admin.post(
            f"{_base(site)}/whatsapp/numbers", json={"e164": NUM_A, "consent": True}
        )
        assert dup.json()["error"]["code"] == "lead_notifications.duplicate_number"
        over = await admin.post(
            f"{_base(site)}/whatsapp/numbers", json={"e164": "+61400000000", "consent": True}
        )
        assert over.status_code == 422
        assert over.json()["error"]["code"] == "lead_notifications.too_many_numbers"

        rm = await admin.delete(f"{_base(site)}/whatsapp/numbers/%2B14155550123")
        assert rm.status_code == 200
        assert [n["e164"] for n in rm.json()["whatsapp"]["numbers"]] == [NUM_B, "+447700900123"]
        gone = await admin.delete(f"{_base(site)}/whatsapp/numbers/%2B14155550123")
        assert gone.status_code == 404


async def test_member_cannot_touch_whatsapp_routes(net) -> None:  # noqa: F811
    ws, _ = await _tenant()
    site = await _site(ws)
    async with AsyncClient(
        transport=ASGITransport(app=_app("member", ws)), base_url="http://t"
    ) as member:
        for method, path, body in [
            ("POST", "/whatsapp/numbers", {"e164": NUM_A, "consent": True}),
            ("DELETE", "/whatsapp/numbers/%2B14155550123", None),
            ("POST", "/whatsapp/test", None),
        ]:
            resp = await member.request(method, f"{_base(site)}{path}", json=body)
            assert resp.status_code == 403, (method, path)


async def test_first_number_switches_whatsapp_on_for_every_event(net) -> None:  # noqa: F811
    ws, _ = await _tenant()
    site = await _site(ws)
    await ns.update_settings(ws, str(site.id), events={"handoff": ["push"]})
    state = await wa.add_number(ws, str(site.id), NUM_A, consent=True)
    assert state["events"] == {
        "lead_captured": ["email", "push", "whatsapp_owner"],
        "handoff": ["push", "whatsapp_owner"],
        "booking": ["email", "push", "whatsapp_owner"],
    }
    # The owner turns it off for handoffs; a second number doesn't undo that.
    await ns.update_settings(ws, str(site.id), events={"handoff": ["push"]})
    state = await wa.add_number(ws, str(site.id), NUM_B, consent=True)
    assert state["events"]["handoff"] == ["push"]


async def test_settings_api_accepts_whatsapp_owner_and_rejects_partner_whatsapp(net) -> None:  # noqa: F811
    from pocketpaw_ee.cloud._core.errors import ValidationError

    ws, _ = await _tenant()
    site = await _site(ws)
    state = await ns.update_settings(
        ws, str(site.id), events={"booking": ["email", "whatsapp_owner"]}
    )
    assert state["events"]["booking"] == ["email", "whatsapp_owner"]
    with pytest.raises(ValidationError) as exc:
        await ns.update_settings(ws, str(site.id), events={"booking": ["whatsapp"]})
    assert exc.value.code == "lead_notifications.unknown_sink"
    # Defaults are unchanged for a site that never configured anything.
    fresh = await _site(ws, name="Other")
    assert (await ns.get_settings(ws, str(fresh.id)))["events"]["lead_captured"] == [
        "email",
        "push",
    ]


# ---------------------------------------------------------------------------
# Dispatch + send
# ---------------------------------------------------------------------------


async def test_lead_queues_one_row_per_number_and_mock_marks_them_sent(meta_answer) -> None:
    ws, _ = await _tenant()
    site = await _site(ws)
    await wa.add_number(ws, str(site.id), NUM_A, consent=True)
    await wa.add_number(ws, str(site.id), NUM_B, consent=True)
    lead_id = await _lead(ws, site, full_name="Priya", phone="555 0101", message="Hi")
    await _capture(ws, site, lead_id)

    rows = await _rows()
    assert sorted(r.target for r in rows) == sorted([NUM_A, NUM_B])
    assert all(
        r.payload == {"event": "lead_captured", "site_ref": str(site.id), "lead_id": lead_id}
        for r in rows
    )
    await outbox.process_due()
    rows = await _rows()
    assert {(r.status, r.provider) for r in rows} == {("sent", "mock")}
    assert _graph(meta_answer["requests"]) == []
    # Email is untouched by the extra sink.
    assert [m["to"] for m in _emails(meta_answer["requests"])] == [[OWNER_EMAIL]]


async def test_live_mode_posts_the_template_to_the_graph_api(meta_answer, wa_live) -> None:
    ws, _ = await _tenant()
    site = await _site(ws)
    await wa.add_number(ws, str(site.id), NUM_A, consent=True)
    lead_id = await _lead(ws, site, full_name="Priya", phone="555 0101", message="Need a quote")
    await _capture(ws, site, lead_id)
    await outbox.process_due()

    [req] = _graph(meta_answer["requests"])
    assert req.url.path == "/v26.0/1098765/messages"
    assert req.headers["authorization"] == "Bearer EAAG-test-token"
    body = json.loads(req.content)
    assert body["to"] == "14155550123" and body["template"]["name"] == "paw_new_lead"
    [param] = body["template"]["components"][0]["parameters"]
    assert param["text"] == (
        "New enquiry for Bright Smile via Paw Sites by PocketPaw: Priya — Need a quote "
        "Contact: 555 0101"
    )
    [row] = await _rows()
    assert row.status == "sent" and row.provider == "meta"
    assert (await ns.get_settings(ws, str(site.id)))["whatsapp"]["mode"] == "live"


async def test_meta_outside_window_is_dead_with_a_clear_error(meta_answer, wa_live) -> None:
    meta_answer["response"] = httpx.Response(
        400, json={"error": {"code": 131047, "message": "Re-engagement message"}}
    )
    ws, _ = await _tenant()
    site = await _site(ws)
    await wa.add_number(ws, str(site.id), NUM_A, consent=True)
    await _capture(ws, site, await _lead(ws, site, full_name="P", message="x"))
    await outbox.process_due()
    [row] = await _rows()
    assert row.status == "dead" and "131047" in row.last_error and "24h" in row.last_error


async def test_number_removed_after_queueing_is_dead(meta_answer) -> None:
    ws, _ = await _tenant()
    site = await _site(ws)
    await wa.add_number(ws, str(site.id), NUM_A, consent=True)
    await _capture(ws, site, await _lead(ws, site, full_name="P", message="x"))
    await wa.remove_number(ws, str(site.id), NUM_A)
    await outbox.process_due()
    [row] = await _rows()
    assert row.status == "dead" and row.last_error == "number removed"


async def test_handoff_without_a_lead_sends_the_title_and_body(meta_answer, wa_live) -> None:
    ws, admin_id = await _tenant()
    site = await _site(ws)
    await wa.add_number(ws, str(site.id), NUM_A, consent=True)
    counts = await ns.dispatch_site_event(
        workspace_id=ws,
        site_ref=str(site.id),
        event="handoff",
        kind="paw_bar_needs_human",
        title="A visitor wants a person",
        body="Can someone call me?",
        source=None,
        push_recipients=[],
    )
    assert counts["whatsapp_owner"] == 1
    await outbox.process_due()
    [req] = _graph(meta_answer["requests"])
    [param] = json.loads(req.content)["template"]["components"][0]["parameters"]
    assert param["text"] == (
        "A visitor wants a person on Bright Smile via Paw Sites by PocketPaw: Can someone call me?"
    )


async def test_test_route_queues_one_per_number(meta_answer) -> None:
    ws, _ = await _tenant()
    site = await _site(ws)
    async with await _admin(ws) as admin:
        none = await admin.post(f"{_base(site)}/whatsapp/test")
        assert none.status_code == 422
        assert none.json()["error"]["code"] == "lead_notifications.whatsapp_no_numbers"
        await wa.add_number(ws, str(site.id), NUM_A, consent=True)
        resp = await admin.post(f"{_base(site)}/whatsapp/test")
        assert resp.status_code == 200
        assert resp.json() == {"queued": 1, "numbers": [NUM_A], "mode": "mock"}
    await outbox.process_due()
    [row] = await _rows()
    assert row.kind == wa.TEST_KIND and row.status == "sent" and row.provider == "mock"


async def _fill_hour(ws: str, site, count: int) -> None:
    now = datetime.now(UTC)
    await NotificationOutboxItem.get_pymongo_collection().insert_many(
        [
            {
                "workspace": ws,
                "kind": "lead_captured",
                "sink": "whatsapp_owner",
                "target": NUM_A,
                "payload": {"site_ref": str(site.id)},
                "status": "sent",
                "attempts": 1,
                "next_at": now,
                "created_at": now,
                "finished_at": now,
            }
            for _ in range(count)
        ]
    )


async def test_hourly_cap_per_site(meta_answer, caplog) -> None:
    ws, _ = await _tenant()
    site = await _site(ws)
    other = await _site(ws, name="Other")
    await wa.add_number(ws, str(site.id), NUM_A, consent=True)
    await wa.add_number(ws, str(site.id), NUM_B, consent=True)
    await wa.add_number(ws, str(other.id), NUM_A, consent=True)
    await _fill_hour(ws, site, wa.HOURLY_CAP - 1)

    counts = await ns.dispatch_site_event(
        workspace_id=ws,
        site_ref=str(site.id),
        event="lead_captured",
        kind="lead_captured",
        title="New lead",
        body="x",
        source=None,
        push_recipients=[],
        lead_id=await _lead(ws, site, full_name="P"),
    )
    assert counts["whatsapp_owner"] == 1
    assert "hourly cap" in caplog.text

    async with await _admin(ws) as admin:
        full = await admin.post(f"{_base(site)}/whatsapp/test")
        assert full.status_code == 429
        assert full.json()["error"]["code"] == "lead_notifications.whatsapp_rate_limited"
        # Another site's budget is its own.
        elsewhere = await admin.post(f"/api/v1/sites/{other.id}/lead-notifications/whatsapp/test")
        assert elsewhere.status_code == 200
