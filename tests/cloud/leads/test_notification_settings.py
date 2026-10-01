# tests/cloud/leads/test_notification_settings.py
# Per-site owner notifications (site_notification_settings) end to end over the
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
    # Pinned connections: the logical URL is the Host header plus the path.
    return [
        r
        for r in requests
        if f"{r.url.scheme}://{r.headers['host']}{r.url.raw_path.decode()}" == url
    ]


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


async def _backdate_confirm(site: Site) -> None:
    """Move every confirm-email rate marker an hour into the past."""
    from datetime import UTC, datetime, timedelta

    from pocketpaw_ee.cloud.models.notification_outbox import NotificationRateMarker

    await NotificationRateMarker.get_pymongo_collection().update_many(
        {}, {"$set": {"at": datetime.now(UTC) - timedelta(hours=1)}}
    )


def _confirm_token_from(mail: dict) -> str:
    url = next(w for w in mail["text"].split() if "/lead-notifications/confirm/" in w)
    return url.rsplit("/", 1)[1]


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
        page = await client.get(f"/api/v1/lead-notifications/confirm/{token}")
        # GET is what a link scanner does: it renders a button and confirms nothing.
        assert page.status_code == 200 and '<form method="post">' in page.text
        assert page.headers["referrer-policy"] == "no-referrer"
        assert page.headers["cache-control"] == "no-store"
        assert (await ns.get_settings(ws, str(site.id)))["emails"][0]["status"] == "pending"
        first = await client.post(f"/api/v1/lead-notifications/confirm/{token}")
        second = await client.post(f"/api/v1/lead-notifications/confirm/{token}")
        bad = await client.post("/api/v1/lead-notifications/confirm/not-a-token")
        bad_get = await client.get("/api/v1/lead-notifications/confirm/not-a-token")
    assert first.status_code == 200 and "Email confirmed" in first.text
    assert second.status_code == 200  # idempotent
    assert bad.status_code == 400 and bad_get.status_code == 400
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
    settings = await ns.settings_for(site)
    nonce = settings.emails[0].confirm_nonce
    claims = json.dumps({"s": str(site.id), "w": ws, "e": "team@acme.test", "n": nonce})
    old = (
        crypto._get_fernet()
        .encrypt_at_time(claims.encode(), int(time.time()) - ns.CONFIRM_TTL_SECONDS - 10)
        .decode()
    )
    assert (await ns.confirm(old))[0] == "invalid"

    fresh = ns._confirm_token(await Site.get(site.id), "team@acme.test", nonce)
    # Re-adding (after the 30-minute resend limit) mints a new nonce: the
    # earlier link stops working.
    await _backdate_confirm(site)
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
    s = await ns.settings_for(site)
    token = ns._confirm_token(site, "team@acme.test", s.emails[0].confirm_nonce)
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
    # New event types are envelope-only: no legacy flat keys.
    assert set(event) == {"id", "type", "created_at", "data"}
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


# ---------------------------------------------------------------------------
# Security-review fixes
# ---------------------------------------------------------------------------


async def _confirmed(ws: str, site: Site, email: str) -> None:
    await ns.add_recipient(ws, str(site.id), email)
    s = await ns.settings_for(site)
    nonce = next(r.confirm_nonce for r in s.emails if r.email == email)
    assert (await ns.confirm(ns._confirm_token(site, email, nonce)))[0] == "confirmed"


async def test_s1_whole_site_save_cannot_clobber_settings(net) -> None:
    ws, _ = await _tenant()
    site = await _site(ws)
    stale = await Site.get(site.id)  # loaded before the settings change
    await _confirmed(ws, site, "team@acme.test")
    stale.name = "Renamed"
    await stale.save()  # what sites/service.py does
    state = await ns.get_settings(ws, str(site.id))
    assert [e["status"] for e in state["emails"]] == ["confirmed"]


async def test_s1_concurrent_adds_respect_the_cap_and_never_duplicate(net) -> None:
    import asyncio

    from pocketpaw_ee.cloud._core.errors import ValidationError

    ws, _ = await _tenant()
    site = await _site(ws)
    addresses = [f"r{i}@acme.test" for i in range(7)] + ["r0@acme.test"]
    results = await asyncio.gather(
        *(ns.add_recipient(ws, str(site.id), a) for a in addresses), return_exceptions=True
    )
    stored = [r.email for r in (await ns.settings_for(site)).emails]
    assert len(stored) == 5 and len(set(stored)) == 5
    assert any(isinstance(r, ValidationError) for r in results)


async def test_s1_concurrent_confirm_bounce_and_failures_all_stick(net) -> None:
    import asyncio

    ws, _ = await _tenant()
    site = await _site(ws)
    await ns.update_settings(ws, str(site.id), webhook_url=SITE_HOOK)
    await _confirmed(ws, site, "a@acme.test")
    await ns.add_recipient(ws, str(site.id), "b@acme.test")
    s = await ns.settings_for(site)
    b_nonce = next(r.confirm_nonce for r in s.emails if r.email == "b@acme.test")
    await asyncio.gather(
        ns.confirm(ns._confirm_token(site, "b@acme.test", b_nonce)),
        ns.record_bounce(ws, str(site.id), "a@acme.test"),
        *(ns.record_webhook_result(ws, str(site.id), ok=False) for _ in range(10)),
    )
    s = await ns.settings_for(site)
    states = {r.email: (r.confirmed_at is not None, r.bounced_at is not None) for r in s.emails}
    assert states == {"a@acme.test": (True, True), "b@acme.test": (True, False)}
    assert s.webhook_failure_count == 10 and s.webhook_disabled_at is not None


async def _unverify(owner_id: str) -> None:
    from pocketpaw_ee.cloud.models.user import User

    user = await User.get(owner_id)
    user.is_verified = False
    await user.save()


async def test_unverified_owner_confirms_through_the_link_then_gets_mail(net) -> None:
    ws, owner = await _tenant()
    await _unverify(owner)
    site = await _site(ws)

    state = await ns.get_settings(ws, str(site.id))
    assert state["owner_email"] == OWNER_EMAIL
    assert state["owner_email_status"] == "pending_confirm"
    assert (await ns.send_test(ws, str(site.id)))["emails"] == []

    # The first lead sends the owner the confirm link, not the lead.
    await _capture(ws, site, await _lead(ws, site, email="v@x.com"))
    await outbox.process_due()
    mails = _emails(net)
    assert [m["to"] for m in mails] == [[OWNER_EMAIL]]
    assert "Confirm" in mails[0]["subject"]
    # A second lead the same day doesn't re-send it.
    await _capture(ws, site, await _lead(ws, site, email="w@x.com"))
    await outbox.process_due()
    assert len(_emails(net)) == 1

    assert (await ns.confirm(_confirm_token_from(mails[0])))[0] == "confirmed"
    assert (await ns.get_settings(ws, str(site.id)))["owner_email_status"] == "confirmed"
    net.clear()
    await _capture(ws, site, await _lead(ws, site, full_name="Priya", email="p@x.com"))
    await outbox.process_due()
    mails = _emails(net)
    assert [m["to"] for m in mails] == [[OWNER_EMAIL]]
    assert "New lead" in mails[0]["subject"]


async def test_unverified_owner_can_resend_through_the_recipients_route(net) -> None:
    ws, owner = await _tenant()
    await _unverify(owner)
    site = await _site(ws)
    path = f"/api/v1/sites/{site.id}/lead-notifications/recipients"
    async with AsyncClient(
        transport=ASGITransport(app=_app("admin", ws)), base_url="http://t"
    ) as admin:
        first = await admin.post(path, json={"email": OWNER_EMAIL.upper()})
        assert first.status_code == 200
        assert first.json()["emails"] == []  # the owner isn't an extra recipient
        assert first.json()["owner_email_status"] == "pending_confirm"
        again = await admin.post(path, json={"email": OWNER_EMAIL})
        assert again.status_code == 429  # same rate limit as any confirm
    await outbox.process_due()
    assert [m["to"] for m in _emails(net)] == [[OWNER_EMAIL]]


async def test_verified_owner_needs_no_confirm(net) -> None:
    ws, _ = await _tenant()
    site = await _site(ws)
    assert (await ns.get_settings(ws, str(site.id)))["owner_email_status"] == "verified"
    await _capture(ws, site, await _lead(ws, site, email="v@x.com"))
    await outbox.process_due()
    assert ["New lead" in m["subject"] for m in _emails(net)] == [True]


async def test_s3_rate_limit_survives_remove_and_re_add(net) -> None:
    ws, _ = await _tenant()
    site = await _site(ws)
    await ns.add_recipient(ws, str(site.id), "a@acme.test")
    await ns.remove_recipient(ws, str(site.id), "a@acme.test")
    from pocketpaw_ee.cloud._core.errors import RateLimited

    with pytest.raises(RateLimited):
        await ns.add_recipient(ws, str(site.id), "a@acme.test")


def test_s3_daily_cap_count_has_an_index() -> None:
    keys = [list(i.document["key"]) for i in NotificationOutboxItem.Settings.indexes]
    assert ["workspace", "kind", "created_at"] in keys


async def test_confirm_post_with_auth_cookie_and_no_csrf_header_confirms(net) -> None:
    from pocketpaw_ee.cloud._core.csrf import CSRFMiddleware

    ws, _ = await _tenant()
    site = await _site(ws)
    await ns.add_recipient(ws, str(site.id), "team@acme.test")
    await outbox.process_due()
    token = _confirm_token_from(_emails(net)[0])

    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.add_middleware(CSRFMiddleware)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://t", cookies={"paw_auth": "x"}
    ) as client:
        resp = await client.post(f"/api/v1/lead-notifications/confirm/{token}")
        # A look-alike route is still protected.
        other = await client.post(f"/api/v1/sites/{site.id}/lead-notifications/test")
    assert resp.status_code == 200 and "Email confirmed" in resp.text
    assert other.status_code == 403


async def test_s3_confirm_resend_and_daily_cap_return_429(net, monkeypatch) -> None:
    ws, _ = await _tenant()
    site = await _site(ws)
    async with AsyncClient(
        transport=ASGITransport(app=_app("admin", ws)), base_url="http://t"
    ) as admin:
        path = f"/api/v1/sites/{site.id}/lead-notifications/recipients"
        assert (await admin.post(path, json={"email": "a@acme.test"})).status_code == 200
        again = await admin.post(path, json={"email": "a@acme.test"})
        assert again.status_code == 429
        assert again.json()["error"]["code"] == "lead_notifications.confirm_rate_limited"
        monkeypatch.setattr(ns, "CONFIRM_DAILY_CAP", 1)
        capped = await admin.post(path, json={"email": "b@acme.test"})
        assert capped.status_code == 429
        assert capped.json()["error"]["code"] == "lead_notifications.confirm_daily_cap"


async def test_s6_workspace_webhook_lead_event_keeps_flat_kind(net) -> None:
    ws, _ = await _tenant()
    site = await _site(ws)
    await notifications_service.set_delivery_config(ws, webhook_url=WS_HOOK, enabled=True)
    await _capture(ws, site, await _lead(ws, site, email="priya@x.com"))
    await outbox.process_due()
    event = json.loads(_hooks(net, WS_HOOK)[0].content)
    assert event["type"] == "lead.captured" and event["data"]["email"] == "priya@x.com"
    assert event["kind"] == "lead_captured" and event["title"] == "New lead"
    assert event["workspace_id"] == ws and event["recipient_id"] is None
    assert "Bright Smile" in event["body"]


async def test_s7_same_url_save_rearms_and_rotate_route(net) -> None:
    ws, _ = await _tenant()
    site = await _site(ws)
    first = await ns.update_settings(ws, str(site.id), webhook_url=SITE_HOOK)
    for _ in range(10):
        await ns.record_webhook_result(ws, str(site.id), ok=False)
    assert (await ns.get_settings(ws, str(site.id)))["webhook_disabled_at"] is not None
    again = await ns.update_settings(ws, str(site.id), webhook_url=SITE_HOOK)
    assert again["webhook_disabled_at"] is None and again["webhook_failure_count"] == 0
    assert again["webhook_secret"] is None

    path = f"/api/v1/sites/{site.id}/lead-notifications/webhook-secret"
    async with AsyncClient(
        transport=ASGITransport(app=_app("member", ws)), base_url="http://t"
    ) as member:
        assert (await member.post(path)).status_code == 403
    async with AsyncClient(
        transport=ASGITransport(app=_app("admin", ws)), base_url="http://t"
    ) as admin:
        rotated = await admin.post(path)
    assert rotated.status_code == 200
    new = rotated.json()["webhook_secret"]
    assert new and new != first["webhook_secret"]
    _url, secrets_now = await ns.webhook_target(ws, str(site.id))
    assert secrets_now == [new, first["webhook_secret"]]  # grace window


async def test_n5_add_recipient_refused_without_public_url_in_production(monkeypatch) -> None:
    from pocketpaw_ee.cloud._core.errors import ValidationError

    ws, _ = await _tenant()
    site = await _site(ws)
    monkeypatch.setenv("POCKETPAW_ENV", "production")
    monkeypatch.delenv("POCKETPAW_PUBLIC_BASE_URL", raising=False)
    with pytest.raises(ValidationError) as exc:
        await ns.add_recipient(ws, str(site.id), "a@acme.test")
    assert exc.value.code == "lead_notifications.public_url_unset"


async def test_n6_site_lookup_failure_still_rings_the_bell(net, monkeypatch) -> None:
    ws, owner = await _tenant()
    site = await _site(ws)

    async def _boom(*_a, **_k):
        raise RuntimeError("mongo down")

    monkeypatch.setattr(ns, "find_site", _boom)
    await _capture(ws, site, await _lead(ws, site, email="v@x.com"))
    assert await _NotificationDoc.find({"recipient": owner}).count() == 1
