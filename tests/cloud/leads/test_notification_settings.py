# tests/cloud/leads/test_notification_settings.py
# Per-site owner notifications (Site.lead_notifications) end to end over the
# shared mongo_db fixture and an httpx MockTransport:
#   * default: the owner's account email gets the FULL lead (escaped, reply_to
#     the visitor) and push still fires; webhook data carries the lead
#   * add recipient -> confirm email -> public confirm link (idempotent; bad /
#     expired / superseded tokens refused); unconfirmed addresses never get mail
#   * per-event sinks, the signed site webhook, and the workspace fallback
#   * routes: admin OK, member 403, another workspace's site 404

from __future__ import annotations

import json
import time
import uuid

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pocketpaw_ee.cloud.leads import notification_settings as ns
from pocketpaw_ee.cloud.leads import service as leads_service
from pocketpaw_ee.cloud.leads.bridges import notifications as leads_bridge
from pocketpaw_ee.cloud.leads.notifications_router import router
from pocketpaw_ee.cloud.models.lead import Lead, LeadSource
from pocketpaw_ee.cloud.models.notification import Notification as _NotificationDoc
from pocketpaw_ee.cloud.models.notification_outbox import NotificationOutboxItem
from pocketpaw_ee.cloud.models.site import Site
from pocketpaw_ee.cloud.models.user import User, WorkspaceMembership
from pocketpaw_ee.cloud.models.workspace import Workspace
from pocketpaw_ee.cloud.notifications import email as email_mod
from pocketpaw_ee.cloud.notifications import outbox, webhook_signing
from pocketpaw_ee.cloud.notifications import service as notifications_service

from tests.cloud.conftest import override_workspace_role

pytestmark = pytest.mark.usefixtures("mongo_db", "public_dns", "email_on")

OWNER_EMAIL = "owner@acme.test"
SITE_HOOK = "https://hooks.example.com/site"
WS_HOOK = "https://hooks.example.com/workspace"


@pytest.fixture
def public_dns(monkeypatch):
    from pocketpaw_ee.cloud.audit import webhooks as audit_webhooks

    async def _resolve(_hostname):
        return ["93.184.216.34"]

    monkeypatch.setattr(audit_webhooks, "_resolve_addresses", _resolve)


@pytest.fixture
def email_on(monkeypatch):
    config = email_mod.EmailConfig("acct", "tok", "notify@paw.example", "Paw")
    monkeypatch.setattr(email_mod, "load_config", lambda: config)


@pytest.fixture
def net(monkeypatch):
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
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
    return requests


def _emails(requests) -> list[dict]:
    return [json.loads(r.content) for r in requests if "api.cloudflare.com" in str(r.url)]


def _hooks(requests, url) -> list[httpx.Request]:
    return [r for r in requests if str(r.url) == url]


async def _tenant(email: str = OWNER_EMAIL) -> tuple[str, str]:
    """A workspace + its owner user. Returns (workspace_id, owner_id)."""
    user = User(
        email=email,
        hashed_password="x",
        is_active=True,
        is_verified=True,
        full_name="Owner",
        workspaces=[],
    )
    await user.insert()
    ws = Workspace(name="Acme", slug=f"acme-{uuid.uuid4().hex[:8]}", owner=str(user.id))
    await ws.insert()
    user.workspaces = [WorkspaceMembership(workspace=str(ws.id), role="owner")]
    await user.save()
    return str(ws.id), str(user.id)


async def _site(ws: str, name: str = "Bright Smile") -> Site:
    site = Site(workspace=ws, pocket_id=f"pk-{uuid.uuid4().hex[:6]}", owner="u", name=name)
    await site.insert()
    site.script_name = str(site.id)
    await site.save()
    return site


async def _lead(ws: str, site: Site, **props) -> str:
    doc = Lead(
        workspace=ws,
        site_id=site.script_name,
        form_type="lead",
        properties=props,
        source=LeadSource(form_type="lead", site_id=site.script_name),
    )
    await doc.insert()
    return str(doc.id)


async def _capture(ws: str, site: Site, lead_id: str) -> None:
    await leads_bridge._on_lead_captured(
        {
            "workspace_id": ws,
            "lead_id": lead_id,
            "site_id": site.script_name,
            "site_name": site.name,
            "form_type": "lead",
        }
    )


# ---------------------------------------------------------------------------
# Default routing + lead payload
# ---------------------------------------------------------------------------


async def test_default_emails_owner_the_full_lead_and_pushes(net) -> None:
    ws, owner = await _tenant()
    site = await _site(ws)
    lead_id = await _lead(
        ws,
        site,
        full_name="<b>Priya</b>",
        email="priya@x.com",
        phone="555 010 1234",
        message="20 jackets please",
        team="Ops",
    )
    await _capture(ws, site, lead_id)

    # push: the owner's bell row (lock-screen body stays generic)
    notes = await _NotificationDoc.find({"recipient": owner}).to_list()
    assert len(notes) == 1 and "priya" not in notes[0].body.lower()

    await outbox.process_due()
    mails = _emails(net)
    assert [m["to"] for m in mails] == [[OWNER_EMAIL]]
    mail = mails[0]
    assert mail["reply_to"] == "priya@x.com"
    assert "&lt;b&gt;Priya&lt;/b&gt;" in mail["html"] and "<b>Priya</b>" not in mail["html"]
    for value in ("priya@x.com", "555 010 1234", "20 jackets please", "Ops", "Bright Smile"):
        assert value in mail["text"]
    assert f"/sites/{site.id}?view=leads&lead={lead_id}" in mail["text"]


async def test_lead_payload_carries_lead_data(net) -> None:
    ws, _ = await _tenant()
    site = await _site(ws)
    lead_id = await _lead(ws, site, name="Priya", email="priya@x.com", message="hi")
    data = await leads_service.lead_payload(ws, lead_id)
    assert data["name"] == "Priya" and data["email"] == "priya@x.com" and data["message"] == "hi"
    assert data["site_name"] == "Bright Smile" and data["form_type"] == "lead"
    assert data["source"]["kind"] == "form"
    assert data["properties"]["name"] == "Priya"


# ---------------------------------------------------------------------------
# Confirm flow
# ---------------------------------------------------------------------------


async def test_unconfirmed_recipient_gets_only_the_confirm_email(net) -> None:
    ws, _ = await _tenant()
    site = await _site(ws)
    out = await ns.add_recipient(ws, str(site.id), "team@acme.test")
    assert out["emails"] == [
        {
            "email": "team@acme.test",
            "status": "pending",
            "added_at": out["emails"][0]["added_at"],
            "confirmed_at": None,
        }
    ]
    lead_id = await _lead(ws, site, email="v@x.com")
    await _capture(ws, site, lead_id)
    await outbox.process_due()

    mails = _emails(net)
    to_team = [m for m in mails if m["to"] == ["team@acme.test"]]
    assert len(to_team) == 1 and "Confirm" in to_team[0]["subject"]
    assert [m["to"] for m in mails if m not in to_team] == [[OWNER_EMAIL]]


async def test_confirm_link_then_recipient_gets_lead_mail(net) -> None:
    ws, _ = await _tenant()
    site = await _site(ws)
    await ns.add_recipient(ws, str(site.id), "team@acme.test")
    await outbox.process_due()
    confirm_mail = _emails(net)[0]
    url = next(w for w in confirm_mail["text"].split() if "/lead-notifications/confirm/" in w)
    token = url.rsplit("/", 1)[1]

    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        first = await client.get(f"/api/v1/lead-notifications/confirm/{token}")
        second = await client.get(f"/api/v1/lead-notifications/confirm/{token}")
        bad = await client.get("/api/v1/lead-notifications/confirm/not-a-token")
    assert first.status_code == 200 and "Email confirmed" in first.text
    assert second.status_code == 200  # idempotent
    assert bad.status_code == 400
    assert "Bright Smile" in first.text

    state = await ns.get_settings(ws, str(site.id))
    assert state["emails"][0]["status"] == "confirmed"

    net.clear()
    lead_id = await _lead(ws, site, email="v@x.com")
    await _capture(ws, site, lead_id)
    await outbox.process_due()
    assert sorted(m["to"][0] for m in _emails(net)) == ["owner@acme.test", "team@acme.test"]


async def test_expired_or_superseded_token_is_refused(net) -> None:
    from pocketpaw_ee.cloud.auth.sso import crypto

    ws, _ = await _tenant()
    site = await _site(ws)
    await ns.add_recipient(ws, str(site.id), "team@acme.test")
    settings = (await Site.get(site.id)).lead_notifications
    nonce = settings.emails[0].confirm_nonce
    claims = json.dumps({"s": str(site.id), "w": ws, "e": "team@acme.test", "n": nonce})
    old = (
        crypto._get_fernet()
        .encrypt_at_time(claims.encode(), int(time.time()) - ns.CONFIRM_TTL_SECONDS - 10)
        .decode()
    )
    assert (await ns.confirm(old))[0] == "invalid"

    fresh = ns._confirm_token(await Site.get(site.id), "team@acme.test", nonce)
    # Re-adding mints a new nonce: the earlier link stops working.
    await ns.add_recipient(ws, str(site.id), "team@acme.test")
    assert (await ns.confirm(fresh))[0] == "invalid"


async def test_recipient_cap_and_validation() -> None:
    from pocketpaw_ee.cloud._core.errors import ValidationError

    ws, _ = await _tenant()
    site = await _site(ws)
    for i in range(5):
        await ns.add_recipient(ws, str(site.id), f"r{i}@acme.test")
    with pytest.raises(ValidationError):
        await ns.add_recipient(ws, str(site.id), "r5@acme.test")
    with pytest.raises(ValidationError):
        await ns.add_recipient(ws, str(site.id), "not an email")


async def test_removed_recipient_is_not_mailed_even_if_already_queued(net) -> None:
    ws, _ = await _tenant()
    site = await _site(ws)
    await ns.add_recipient(ws, str(site.id), "team@acme.test")
    s = (await Site.get(site.id)).lead_notifications
    token = ns._confirm_token(await Site.get(site.id), "team@acme.test", s.emails[0].confirm_nonce)
    assert (await ns.confirm(token))[0] == "confirmed"
    await NotificationOutboxItem.find_all().delete()

    lead_id = await _lead(ws, site, email="v@x.com")
    await _capture(ws, site, lead_id)  # queues mail to owner + team
    await ns.remove_recipient(ws, str(site.id), "team@acme.test")
    await outbox.process_due()
    assert [m["to"] for m in _emails(net)] == [[OWNER_EMAIL]]


# ---------------------------------------------------------------------------
# Sinks, site webhook, workspace fallback
# ---------------------------------------------------------------------------


async def test_event_sinks_turn_email_and_push_off(net) -> None:
    ws, owner = await _tenant()
    site = await _site(ws)
    await ns.update_settings(ws, str(site.id), events={"lead_captured": []})
    await _capture(ws, site, await _lead(ws, site, email="v@x.com"))
    await outbox.process_due()
    assert _emails(net) == []
    assert await _NotificationDoc.find({"recipient": owner}).count() == 0


async def test_site_webhook_is_signed_and_carries_the_lead(net) -> None:
    ws, _ = await _tenant()
    site = await _site(ws)
    saved = await ns.update_settings(
        ws,
        str(site.id),
        webhook_url=SITE_HOOK,
        events={"lead_captured": ["webhook"]},
    )
    secret = saved["webhook_secret"]
    assert secret and (await ns.get_settings(ws, str(site.id)))["webhook_secret"] is None
    # The workspace webhook is the fallback only; with a site webhook it is skipped.
    await notifications_service.set_delivery_config(ws, webhook_url=WS_HOOK, enabled=True)

    lead_id = await _lead(
        ws, site, full_name="Priya", email="priya@x.com", phone="5550101234", message="hello"
    )
    await _capture(ws, site, lead_id)
    await outbox.process_due()

    hooks = _hooks(net, SITE_HOOK)
    assert len(hooks) == 1 and _hooks(net, WS_HOOK) == []
    req = hooks[0]
    assert webhook_signing.verify_signature(
        secret, req.headers["X-Paw-Timestamp"], req.content, req.headers["X-Paw-Signature"]
    )
    event = json.loads(req.content)
    assert event["type"] == "lead.captured"
    data = event["data"]
    assert (data["id"], data["name"], data["email"], data["phone"], data["message"]) == (
        lead_id,
        "Priya",
        "priya@x.com",
        "5550101234",
        "hello",
    )
    assert data["site_name"] == "Bright Smile" and data["form_type"] == "lead"
    assert data["source"]["kind"] == "form"


async def test_workspace_config_is_the_fallback(net) -> None:
    ws, _ = await _tenant()
    site = await _site(ws)
    saved = await notifications_service.set_delivery_config(
        ws,
        webhook_url=WS_HOOK,
        slack_webhook_url="https://hooks.slack.com/services/a/b/c",
        enabled=True,
    )
    lead_id = await _lead(ws, site, email="priya@x.com")
    await _capture(ws, site, lead_id)
    await outbox.process_due()

    ws_hooks = _hooks(net, WS_HOOK)
    assert len(ws_hooks) == 1  # once per lead, not once per admin
    event = json.loads(ws_hooks[0].content)
    assert event["type"] == "lead.captured" and event["data"]["email"] == "priya@x.com"
    assert webhook_signing.verify_signature(
        saved["webhook_secret"],
        ws_hooks[0].headers["X-Paw-Timestamp"],
        ws_hooks[0].content,
        ws_hooks[0].headers["X-Paw-Signature"],
    )
    slack = _hooks(net, "https://hooks.slack.com/services/a/b/c")
    assert len(slack) == 1 and "priya" not in slack[0].content.decode().lower()


async def test_send_test_queues_mail_and_webhook(net) -> None:
    ws, _ = await _tenant()
    site = await _site(ws)
    await ns.update_settings(ws, str(site.id), webhook_url=SITE_HOOK)
    out = await ns.send_test(ws, str(site.id))
    assert out == {"emails": [OWNER_EMAIL], "webhook": True}
    await outbox.process_due()
    assert len(_emails(net)) == 1 and len(_hooks(net, SITE_HOOK)) == 1


# ---------------------------------------------------------------------------
# Routes: RBAC + tenancy
# ---------------------------------------------------------------------------


def _app(role: str, ws: str) -> FastAPI:
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    override_workspace_role(app, role=role, workspace_id=ws, user_id="u1")
    return app


async def test_routes_admin_ok_member_forbidden_cross_tenant_404(net) -> None:
    ws, _ = await _tenant()
    other_ws, _ = await _tenant("other@acme.test")
    site = await _site(ws)
    foreign = await _site(other_ws)

    async with AsyncClient(
        transport=ASGITransport(app=_app("admin", ws)), base_url="http://t"
    ) as admin:
        got = await admin.get(f"/api/v1/sites/{site.id}/lead-notifications")
        assert got.status_code == 200
        assert got.json()["owner_email"] == OWNER_EMAIL
        assert got.json()["events"]["lead_captured"] == ["email", "push"]
        put = await admin.put(
            f"/api/v1/sites/{site.id}/lead-notifications", json={"events": {"handoff": ["push"]}}
        )
        assert put.status_code == 200 and put.json()["events"]["handoff"] == ["push"]
        add = await admin.post(
            f"/api/v1/sites/{site.id}/lead-notifications/recipients",
            json={"email": "team@acme.test"},
        )
        assert add.status_code == 200
        rm = await admin.delete(
            f"/api/v1/sites/{site.id}/lead-notifications/recipients/team@acme.test"
        )
        assert rm.status_code == 200 and rm.json()["emails"] == []
        test = await admin.post(f"/api/v1/sites/{site.id}/lead-notifications/test")
        assert test.status_code == 200
        bad = await admin.put(
            f"/api/v1/sites/{site.id}/lead-notifications",
            json={"webhook_url": "https://127.0.0.1/x"},
        )
        assert bad.status_code == 403
        cross = await admin.get(f"/api/v1/sites/{foreign.id}/lead-notifications")
        assert cross.status_code == 404
        cross_put = await admin.post(
            f"/api/v1/sites/{foreign.id}/lead-notifications/recipients",
            json={"email": "x@acme.test"},
        )
        assert cross_put.status_code == 404

    async with AsyncClient(
        transport=ASGITransport(app=_app("member", ws)), base_url="http://t"
    ) as member:
        for method, path, body in [
            ("GET", "", None),
            ("PUT", "", {"include_owner": False}),
            ("POST", "/recipients", {"email": "x@acme.test"}),
            ("DELETE", "/recipients/x@acme.test", None),
            ("POST", "/test", None),
        ]:
            resp = await member.request(
                method, f"/api/v1/sites/{site.id}/lead-notifications{path}", json=body
            )
            assert resp.status_code == 403, (method, path)


# ---------------------------------------------------------------------------
# Concierge handoff -> per-site routing
# ---------------------------------------------------------------------------


async def test_handoff_routes_through_site_settings(net, monkeypatch) -> None:
    from pocketpaw_ee.paw_bar import notify

    ws, owner = await _tenant()
    site = await _site(ws)
    await ns.update_settings(
        ws,
        str(site.id),
        webhook_url=SITE_HOOK,
        events={"handoff": ["email", "push", "webhook"]},
    )

    async def _site_for(_widget_id, _ws):
        return str(site.id), site.name

    monkeypatch.setattr(notify, "resolve_widget_site", _site_for)
    ok = await notify.notify_workspace_owner(
        workspace_id=ws,
        kind=notify.NOTIFY_NEEDS_HUMAN,
        title="A visitor asked for a person",
        body="<i>call me</i>",
        widget_id="w1",
        customer_ref="c1",
        agent_id="ag1",
    )
    assert ok
    assert await _NotificationDoc.find({"recipient": owner}).count() == 1
    await outbox.process_due()
    mail = _emails(net)[0]
    assert mail["to"] == [OWNER_EMAIL]
    assert "&lt;i&gt;call me&lt;/i&gt;" in mail["html"]
    assert "/agents/ag1?tab=conversations" in mail["text"]
    event = json.loads(_hooks(net, SITE_HOOK)[0].content)
    assert event["type"] == "concierge.handoff"
    assert event["data"]["customer_ref"] == "c1" and event["data"]["question"]
