# tests/cloud/leads/test_lead_email_owner_sender.py
# A site's lead emails go out through the owner's own Resend account once the
# site has a connected ``resend`` binding with a sender (the real bridge, dispatch
# and outbox over mongomock; Resend and Cloudflare answer through the MockTransport):
#   * connected: Resend gets the mail from the owner's sender, with the vault's
#     token and the outbox row's idempotency key; Cloudflare gets nothing
#   * no binding, a dead grant, or another workspace's binding: Cloudflare sends
#   * Resend refusing the sender (403 unverified domain): Cloudflare sends it and
#     the binding turns setup_required; a Resend 5xx is retried, not re-routed

from __future__ import annotations

import json

import httpx
import pytest
from cryptography.fernet import Fernet
from pocketpaw_ee.cloud.models.integration import IntegrationConnection, SiteIntegration
from pocketpaw_ee.cloud.models.notification_outbox import NotificationOutboxItem
from pocketpaw_ee.cloud.notifications import outbox

from tests.cloud.leads.test_notification_settings import (  # noqa: F401 - fixtures
    _capture,
    _emails,
    _lead,
    _site,
    _tenant,
    email_on,
    public_dns,
)

pytestmark = pytest.mark.usefixtures("mongo_db", "public_dns", "email_on")

SENDER = "leads@shop.example"


@pytest.fixture
def resend_net(monkeypatch):
    monkeypatch.setenv("CLOUD_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("POCKETPAW_INTEGRATIONS_RESEND_CLIENT_ID", "client-1")
    state = {"status": 200, "requests": []}

    def handler(request: httpx.Request) -> httpx.Response:
        state["requests"].append(request)
        if request.url.host == "api.resend.com":
            if state["status"] != 200:
                return httpx.Response(state["status"], json={"name": "validation_error"})
            return httpx.Response(200, json={"id": "em_1"})
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


def _resend(state) -> list[httpx.Request]:
    return [r for r in state["requests"] if r.url.host == "api.resend.com"]


async def _connect_resend(ws: str, owner: str, site, *, workspace: str | None = None):
    from pocketpaw_ee.integrations import vault
    from pocketpaw_ee.integrations.providers.base import TokenGrant

    conn = await vault.store_grant(
        workspace_id=workspace or ws,
        provider="resend",
        grant=TokenGrant(
            access_token="at-live", refresh_token="rt-1", expires_in=900, scopes=["emails:send"]
        ),
        user_id=owner,
    )
    binding = SiteIntegration(
        workspace=workspace or ws,
        pocket_id=site.pocket_id,
        provider="resend",
        connection_id=str(conn.id),
        settings={"from_address": SENDER, "from_name": "Bright Smile"},
        created_by=owner,
    )
    await binding.insert()
    return conn, binding


async def test_lead_mail_goes_through_the_owners_resend(resend_net) -> None:
    ws, owner = await _tenant()
    site = await _site(ws)
    await _connect_resend(ws, owner, site)
    await _capture(ws, site, await _lead(ws, site, full_name="Priya", email="p@x.com"))
    await outbox.process_due()

    sent = _resend(resend_net)
    assert len(sent) == 1
    req = sent[0]
    assert req.url.path == "/emails"
    assert req.headers["authorization"] == "Bearer at-live"
    row = await NotificationOutboxItem.find_one(NotificationOutboxItem.sink == "email")
    assert req.headers["idempotency-key"] == f"paw-outbox-{row.id}"
    body = json.loads(req.content)
    assert body["from"] == f'"Bright Smile" <{SENDER}>'
    assert body["to"] == ["owner@acme.test"]
    assert body["reply_to"] == "p@x.com"
    assert _emails(resend_net["requests"]) == []
    assert row.status == "sent"


async def test_no_binding_uses_the_platform_sender(resend_net) -> None:
    ws, _ = await _tenant()
    site = await _site(ws)
    await _capture(ws, site, await _lead(ws, site, email="p@x.com"))
    await outbox.process_due()
    assert _resend(resend_net) == []
    assert len(_emails(resend_net["requests"])) == 1


async def test_another_workspaces_binding_is_never_used(resend_net) -> None:
    ws, owner = await _tenant()
    other_ws, other_owner = await _tenant("other@acme.test")
    site = await _site(ws)
    # A binding row for this pocket id, but in another workspace.
    await _connect_resend(other_ws, other_owner, site, workspace=other_ws)
    await _capture(ws, site, await _lead(ws, site, email="p@x.com"))
    await outbox.process_due()
    assert _resend(resend_net) == []
    assert len(_emails(resend_net["requests"])) == 1


async def test_a_dead_grant_falls_back_to_the_platform(resend_net) -> None:
    ws, owner = await _tenant()
    site = await _site(ws)
    conn, _ = await _connect_resend(ws, owner, site)
    await IntegrationConnection.get_pymongo_collection().update_one(
        {"_id": conn.id}, {"$set": {"status": "needs_reconnect"}}
    )
    await _capture(ws, site, await _lead(ws, site, email="p@x.com"))
    await outbox.process_due()
    assert _resend(resend_net) == []
    assert len(_emails(resend_net["requests"])) == 1


async def test_a_refused_sender_falls_back_and_marks_the_site(resend_net) -> None:
    ws, owner = await _tenant()
    site = await _site(ws)
    _, binding = await _connect_resend(ws, owner, site)
    resend_net["status"] = 403
    await _capture(ws, site, await _lead(ws, site, email="p@x.com"))
    await outbox.process_due()
    assert len(_resend(resend_net)) == 1
    assert len(_emails(resend_net["requests"])) == 1
    fresh = await SiteIntegration.get(binding.id)
    assert fresh.setup_error == "sender_rejected"


async def test_a_resend_outage_is_retried_not_rerouted(resend_net) -> None:
    ws, owner = await _tenant()
    site = await _site(ws)
    await _connect_resend(ws, owner, site)
    resend_net["status"] = 503
    await _capture(ws, site, await _lead(ws, site, email="p@x.com"))
    await outbox.process_due()
    assert len(_resend(resend_net)) == 1
    assert _emails(resend_net["requests"]) == []
    row = await NotificationOutboxItem.find_one(NotificationOutboxItem.sink == "email")
    assert row.status == "pending" and row.attempts == 1
