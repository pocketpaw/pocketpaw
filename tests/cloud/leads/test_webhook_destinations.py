# tests/cloud/leads/test_webhook_destinations.py
# Per-site lead-notification webhook destinations end to end (mongomock + an
# httpx MockTransport, the real dispatch and outbox):
#   * legacy single webhook reads as destination ``legacy``, an already queued
#     "site:<id>" row still delivers, and the first write moves it into the list
#   * cap of 5, duplicate URL, SSRF check on save
#   * routing: each destination gets its platform's shape (chat unsigned, json
#     signed), ``events`` narrows, ``lead.updated`` reaches json only
#   * health is per destination: one switched off doesn't stop the others
#   * routes: add / patch / delete / rotate / test / preview / platforms, member
#     403, another workspace's site 404; preview makes no network call

from __future__ import annotations

import json

import pytest
from httpx import ASGITransport, AsyncClient
from pocketpaw_ee.cloud._core.errors import Forbidden, ValidationError
from pocketpaw_ee.cloud.leads import notification_settings as ns
from pocketpaw_ee.cloud.leads import webhook_destinations as wd
from pocketpaw_ee.cloud.models.lead_notifications import SiteNotificationSettings
from pocketpaw_ee.cloud.models.notification_outbox import NotificationOutboxItem
from pocketpaw_ee.cloud.notifications import outbox, webhook_signing

from tests.cloud.leads.test_notification_settings import (  # noqa: F401 — fixtures
    _app,
    _capture,
    _hooks,
    _lead,
    _site,
    _tenant,
    _updated,
    email_on,
    net,
    public_dns,
)

pytestmark = pytest.mark.usefixtures("mongo_db", "public_dns", "email_on")

SLACK = "https://hooks.slack.com/services/T1/B1/xyz"
DISCORD = "https://discord.com/api/webhooks/1/abc"
JSON_HOOK = "https://hooks.zapier.com/hooks/catch/1/abc/"


@pytest.fixture
def sent(net):  # noqa: F811 — the imported fixture
    """The requests the MockTransport saw (the shared ``net`` fixture)."""
    return net


async def _raw(site) -> dict:
    return await SiteNotificationSettings.get_pymongo_collection().find_one(
        {"workspace": site.workspace, "site_id": str(site.id)}
    )


# ---------------------------------------------------------------------------
# Legacy read-through
# ---------------------------------------------------------------------------


async def test_legacy_webhook_reads_as_destination_and_migrates_on_write(sent) -> None:
    from pocketpaw_ee.cloud.auth.sso import crypto

    ws, _ = await _tenant()
    site = await _site(ws)
    # A row saved before destinations existed.
    await SiteNotificationSettings.get_pymongo_collection().insert_one(
        {
            "workspace": ws,
            "site_id": str(site.id),
            "include_owner": True,
            "emails": [],
            "webhook_url": JSON_HOOK,
            "webhook_secret_enc": crypto.encrypt("old-secret"),
            "webhook_secret_prev_enc": "",
            "webhook_failure_count": 3,
            "webhook_disabled_at": None,
            "events": {"lead_captured": ["webhook"]},
        }
    )
    state = await ns.get_settings(ws, str(site.id))
    assert [h["id"] for h in state["webhooks"]] == ["legacy"]
    legacy = state["webhooks"][0]
    assert legacy["url"] == JSON_HOOK and legacy["platform"] == "json"
    assert legacy["failure_count"] == 3 and legacy["has_secret"]
    # Older clients still read the flat fields.
    assert state["webhook_url"] == JSON_HOOK and state["webhook_failure_count"] == 3

    # A row queued before the upgrade carries the old ref and still delivers.
    await outbox.enqueue(
        workspace=ws,
        kind="lead_captured",
        sink="webhook",
        target=JSON_HOOK,
        payload={"event_type": "lead.captured", "data": {"x": 1}},
        webhook_ref=f"site:{site.id}",
    )
    await outbox.process_due()
    req = _hooks(sent, JSON_HOOK)[0]
    assert webhook_signing.verify_signature(
        "old-secret", req.headers["X-Paw-Timestamp"], req.content, req.headers["X-Paw-Signature"]
    )
    raw = await _raw(site)
    # That success was a write: the legacy webhook now lives in the list.
    assert raw["webhook_url"] is None
    assert [h["id"] for h in raw["webhooks"]] == ["legacy"]
    assert raw["webhooks"][0]["failure_count"] == 0

    added = await wd.add_webhook(ws, str(site.id), url=SLACK, label="Sales")
    raw = await _raw(site)
    assert [h["id"] for h in raw["webhooks"]] == ["legacy", added["webhook"]["id"]]
    # The secret survived the move: an old-ref row still verifies.
    sent.clear()
    await outbox.enqueue(
        workspace=ws,
        kind="lead_captured",
        sink="webhook",
        target=JSON_HOOK,
        payload={"event_type": "lead.captured", "data": {}},
        webhook_ref=f"site:{site.id}",
    )
    await outbox.process_due()
    req = _hooks(sent, JSON_HOOK)[0]
    assert webhook_signing.verify_signature(
        "old-secret", req.headers["X-Paw-Timestamp"], req.content, req.headers["X-Paw-Signature"]
    )


async def test_migration_keeps_a_concurrent_counter_bump() -> None:
    import asyncio

    ws, _ = await _tenant()
    site = await _site(ws)
    await SiteNotificationSettings.get_pymongo_collection().insert_one(
        {
            "workspace": ws,
            "site_id": str(site.id),
            "webhook_url": JSON_HOOK,
            "webhook_secret_enc": "enc",
            "webhook_failure_count": 0,
        }
    )
    await asyncio.gather(*(ns.record_webhook_result(ws, str(site.id), ok=False) for _ in range(4)))
    raw = await _raw(site)
    assert raw["webhook_url"] is None
    assert len(raw["webhooks"]) == 1 and raw["webhooks"][0]["failure_count"] == 4


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


async def test_cap_duplicates_and_ssrf() -> None:
    ws, _ = await _tenant()
    site = await _site(ws)
    for i in range(5):
        await wd.add_webhook(ws, str(site.id), url=f"https://hooks.example.com/{i}")
    with pytest.raises(ValidationError) as exc:
        await wd.add_webhook(ws, str(site.id), url="https://hooks.example.com/6")
    assert exc.value.code == "lead_notifications.too_many_webhooks"
    await wd.remove_webhook(ws, str(site.id), (await _raw(site))["webhooks"][0]["id"])
    with pytest.raises(ValidationError) as exc:
        await wd.add_webhook(ws, str(site.id), url="https://hooks.example.com/1")
    assert exc.value.code == "lead_notifications.duplicate_webhook"
    with pytest.raises(Forbidden):
        await wd.add_webhook(ws, str(site.id), url="https://169.254.169.254/latest")
    with pytest.raises(Forbidden):
        await wd.add_webhook(ws, str(site.id), url="http://hooks.example.com/plain")
    with pytest.raises(ValidationError):
        await wd.add_webhook(ws, str(site.id), url=SLACK, template={"fields": ["ssn"]})
    with pytest.raises(ValidationError):
        await wd.add_webhook(ws, str(site.id), url=SLACK, platform_override="fax")
    assert len((await _raw(site))["webhooks"]) == 4


async def test_concurrent_adds_respect_the_cap() -> None:
    import asyncio

    ws, _ = await _tenant()
    site = await _site(ws)
    results = await asyncio.gather(
        *(wd.add_webhook(ws, str(site.id), url=f"https://h.example.com/{i}") for i in range(8)),
        return_exceptions=True,
    )
    assert len((await _raw(site))["webhooks"]) == 5
    assert sum(isinstance(r, ValidationError) for r in results) == 3


# ---------------------------------------------------------------------------
# Routing + outbox
# ---------------------------------------------------------------------------


async def test_each_destination_gets_its_platform_shape(sent) -> None:
    ws, _ = await _tenant()
    site = await _site(ws)
    sid = str(site.id)
    await ns.update_settings(ws, sid, events={"lead_captured": ["webhook"]})
    slack = await wd.add_webhook(ws, sid, url=SLACK, template={"title": "Hot lead"})
    assert slack["secret"] is None and slack["webhook"]["platform"] == "slack"
    disc = await wd.add_webhook(ws, sid, url=DISCORD, events=["handoff"])
    js = await wd.add_webhook(ws, sid, url=JSON_HOOK)
    assert js["secret"] and js["webhook"]["signed"]

    lead_id = await _lead(ws, site, full_name="Priya", email="priya@x.com", message="@everyone hi")
    await _capture(ws, site, lead_id)
    refs = {r.webhook_ref for r in await NotificationOutboxItem.find({"sink": "webhook"}).to_list()}
    assert refs == {f"site:{sid}:{slack['webhook']['id']}", f"site:{sid}:{js['webhook']['id']}"}
    await outbox.process_due()

    s_req = _hooks(sent, SLACK)[0]
    assert "X-Paw-Signature" not in s_req.headers
    body = json.loads(s_req.content)
    assert body["blocks"][0]["text"]["text"] == "Hot lead"
    assert "Priya" in s_req.content.decode() and f"lead={lead_id}" in s_req.content.decode()
    assert _hooks(sent, DISCORD) == []  # only takes handoffs
    j_req = _hooks(sent, JSON_HOOK)[0]
    assert webhook_signing.verify_signature(
        js["secret"],
        j_req.headers["X-Paw-Timestamp"],
        j_req.content,
        j_req.headers["X-Paw-Signature"],
    )
    event = json.loads(j_req.content)
    assert set(event) == {"id", "type", "created_at", "data"}
    assert event["data"]["id"] == lead_id
    assert disc["webhook"]["events"] == ["handoff"]


async def test_lead_updated_reaches_json_destinations_only(sent) -> None:
    ws, _ = await _tenant()
    site = await _site(ws)
    sid = str(site.id)
    await ns.update_settings(ws, sid, events={"lead_captured": ["webhook"]})
    await wd.add_webhook(ws, sid, url=SLACK)
    await wd.add_webhook(ws, sid, url=JSON_HOOK)
    lead_id = await _lead(ws, site, email="priya@x.com")
    await _updated(ws, site, lead_id)
    await outbox.process_due()
    assert _hooks(sent, SLACK) == []
    assert json.loads(_hooks(sent, JSON_HOOK)[0].content)["type"] == "lead.updated"


async def test_chat_destination_skips_a_queued_update_after_override_change(sent) -> None:
    ws, _ = await _tenant()
    site = await _site(ws)
    sid = str(site.id)
    await ns.update_settings(ws, sid, events={"lead_captured": ["webhook"]})
    added = await wd.add_webhook(ws, sid, url=JSON_HOOK)
    hid = added["webhook"]["id"]
    assert await ns.dispatch_lead_updated(
        workspace_id=ws, site_ref=sid, lead_id=await _lead(ws, site, email="a@x.com")
    )
    await wd.update_webhook(ws, sid, hid, platform_override="slack")
    await outbox.process_due()
    assert _hooks(sent, JSON_HOOK) == []
    row = await NotificationOutboxItem.find_one({"sink": "webhook"})
    assert row.status == "dead" and "skip" in row.last_error
    assert (await ns.get_settings(ws, sid))["webhooks"][0]["failure_count"] == 0


async def test_health_is_per_destination(sent) -> None:
    ws, _ = await _tenant()
    site = await _site(ws)
    sid = str(site.id)
    await ns.update_settings(ws, sid, events={"lead_captured": ["webhook"]})
    a = (await wd.add_webhook(ws, sid, url=SLACK))["webhook"]["id"]
    await wd.add_webhook(ws, sid, url=JSON_HOOK)
    for _ in range(wd.WEBHOOK_DISABLE_THRESHOLD):
        await ns.record_webhook_result(ws, sid, ok=False, webhook_id=a)
    state = await ns.get_settings(ws, sid)
    statuses = {h["url"]: (h["status"], h["failure_count"]) for h in state["webhooks"]}
    assert statuses == {SLACK: ("disabled", 10), JSON_HOOK: ("active", 0)}

    await _capture(ws, site, await _lead(ws, site, email="v@x.com"))
    await outbox.process_due()
    assert _hooks(sent, SLACK) == [] and len(_hooks(sent, JSON_HOOK)) == 1

    with pytest.raises(ValidationError):
        await wd.send_webhook_test(ws, sid, a)
    rearmed = await wd.update_webhook(ws, sid, a, rearm=True)
    assert rearmed["webhook"]["status"] == "active" and rearmed["webhook"]["failure_count"] == 0


async def test_patch_url_and_override_mint_a_secret_shown_for_json_only() -> None:
    ws, _ = await _tenant()
    site = await _site(ws)
    sid = str(site.id)
    hid = (await wd.add_webhook(ws, sid, url=SLACK))["webhook"]["id"]
    labelled = await wd.update_webhook(ws, sid, hid, label="Team channel")
    assert labelled["webhook"]["label"] == "Team channel" and labelled["secret"] is None
    to_json = await wd.update_webhook(ws, sid, hid, platform_override="json")
    assert to_json["secret"] and to_json["webhook"]["platform"] == "json"
    assert to_json["webhook"]["detected_platform"] == "slack"
    back = await wd.update_webhook(ws, sid, hid, platform_override=None)
    assert back["webhook"]["platform"] == "slack" and back["secret"] is None
    moved = await wd.update_webhook(ws, sid, hid, url=JSON_HOOK)
    assert moved["secret"] and moved["webhook"]["url"] == JSON_HOOK
    with pytest.raises(Forbidden):
        await wd.update_webhook(ws, sid, hid, url="https://localhost/x")


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


async def test_routes_admin_member_and_cross_tenant(sent) -> None:
    ws, _ = await _tenant()
    other_ws, _ = await _tenant("other@acme.test")
    site = await _site(ws)
    foreign = await _site(other_ws)
    base = f"/api/v1/sites/{site.id}/lead-notifications"

    async with AsyncClient(
        transport=ASGITransport(app=_app("admin", ws)), base_url="http://t"
    ) as admin:
        platforms = await admin.get("/api/v1/lead-notifications/platforms")
        assert platforms.status_code == 200
        catalogue = platforms.json()
        assert [f["id"] for f in catalogue["fields"]] == [
            "name",
            "email",
            "phone",
            "message",
            "company",
            "source",
            "page",
            "extras",
        ]
        assert {p["id"] for p in catalogue["platforms"]} == {
            "slack",
            "discord",
            "teams",
            "google_chat",
            "json",
        }
        add = await admin.post(
            f"{base}/webhooks",
            json={"url": JSON_HOOK, "label": "Zapier", "template": {"fields": ["name"]}},
        )
        assert add.status_code == 200
        hook = add.json()["webhook"]
        assert add.json()["secret"] and hook["template"]["fields"] == ["name"]
        assert add.json()["webhooks"][0]["id"] == hook["id"]
        hid = hook["id"]
        patch = await admin.patch(
            f"{base}/webhooks/{hid}", json={"events": ["lead_captured"], "label": "CRM"}
        )
        assert patch.status_code == 200 and patch.json()["webhook"]["label"] == "CRM"
        rotated = await admin.post(f"{base}/webhooks/{hid}/secret")
        assert rotated.status_code == 200 and rotated.json()["secret"] != add.json()["secret"]
        test = await admin.post(f"{base}/webhooks/{hid}/test")
        assert test.status_code == 200 and test.json()["queued"] is True

        sent.clear()
        preview = await admin.post(
            f"{base}/preview",
            json={"url": DISCORD, "event": "handoff", "template": {"title": "Help!"}},
        )
        assert preview.status_code == 200
        assert preview.json()["platform"] == "discord"
        assert preview.json()["body"]["embeds"][0]["title"] == "Help!"
        assert sent == []  # rendered locally, nothing sent
        bad = await admin.post(f"{base}/preview", json={"platform": "fax"})
        assert bad.status_code in (400, 422)
        unsafe = await admin.post(f"{base}/webhooks", json={"url": "https://10.0.0.1/x"})
        assert unsafe.status_code == 403
        missing = await admin.patch(f"{base}/webhooks/nope", json={"label": "x"})
        assert missing.status_code == 404
        cross = await admin.post(
            f"/api/v1/sites/{foreign.id}/lead-notifications/webhooks", json={"url": SLACK}
        )
        assert cross.status_code == 404
        gone = await admin.delete(f"{base}/webhooks/{hid}")
        assert gone.status_code == 200 and gone.json()["webhooks"] == []

    async with AsyncClient(
        transport=ASGITransport(app=_app("member", ws)), base_url="http://t"
    ) as member:
        for method, path, body in [
            ("GET", "/api/v1/lead-notifications/platforms", None),
            ("POST", f"{base}/webhooks", {"url": SLACK}),
            ("PATCH", f"{base}/webhooks/x", {"label": "y"}),
            ("DELETE", f"{base}/webhooks/x", None),
            ("POST", f"{base}/webhooks/x/secret", None),
            ("POST", f"{base}/webhooks/x/test", None),
            ("POST", f"{base}/preview", {"platform": "slack"}),
        ]:
            resp = await member.request(method, path, json=body)
            assert resp.status_code == 403, (method, path)
