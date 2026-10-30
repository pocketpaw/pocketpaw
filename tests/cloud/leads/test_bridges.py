# tests/cloud/leads/test_bridges.py — the lead.captured → notification bridge.
#
# Created 2026-08-06 (feat/coupling-lead-captured, T-6). Built to the shape of
# tests/cloud/meetings/test_bridges.py, with one deliberate difference: the
# notification side is NOT mocked. The bridge is exercised against the real
# notifications service and the real workspace admin resolver over the shared
# mongo_db fixture, and the assertions read the persisted Notification docs.
# Mocking notifications_service.create here would prove only that the bridge
# calls a function — the things that can actually be wrong (which users get the
# notification, whether the source deep-links to the right site, whether a lead
# in one tenant can reach another) all live below that seam.
#
# Covered:
#   • a captured lead notifies the workspace owner + admins, and nobody else
#   • the notification kind is lead_captured and its source deep-links the site
#   • a malformed event payload mints nothing
#   • cross-tenant: a lead in workspace A never notifies workspace B
#   • end-to-end through the REAL bus: capture() → lead.captured → notification
#   • the body names the site the owner recognises, never the raw script id
#
# Updated 2026-08-06 (review fix): fixtures use a REALISTIC site id. script_name
# is a 24-char hex ObjectId, and the friendly "site_1" this file used is exactly
# why a body rendering the raw id looked fine in the tests and looked like a hash
# in the bell. The registered_bridge fixture also snapshots and restores the
# topic's subscriber list, so these tests are hermetic no matter who else
# (mount_cloud in test_integration.py) subscribed earlier in the session.

from __future__ import annotations

import pytest
from pocketpaw_ee.cloud.leads import service as leads_service
from pocketpaw_ee.cloud.leads.bridges import notifications as leads_bridge
from pocketpaw_ee.cloud.models.notification import Notification as _NotificationDoc
from pocketpaw_ee.cloud.models.site import Site
from pocketpaw_ee.cloud.models.user import User as _UserDoc
from pocketpaw_ee.cloud.models.user import WorkspaceMembership
from pocketpaw_ee.cloud.shared.events import event_bus


async def _user(email: str, workspace: str, role: str) -> str:
    doc = _UserDoc(
        email=email,
        hashed_password="x",
        is_active=True,
        is_verified=True,
        full_name=email,
        workspaces=[WorkspaceMembership(workspace=workspace, role=role)],
    )
    await doc.insert()
    return str(doc.id)


# A real script_name is a 24-char hex ObjectId, not a slug. See the header.
SITE_ID = "6d4a1f2b3c8e9a0f1b2c3d4e"
SITE_NAME = "Bright Smile Dental"


async def _site(ws: str = "ws1", site_id: str = SITE_ID, name: str = SITE_NAME) -> Site:
    site = Site(
        workspace=ws,
        pocket_id="pk1",
        owner="u1",
        name=name,
        script_name=site_id,
        allowed_origins=["brightsmiledental.com"],
        signed_key="pp_tok_x",
        event_mapping={
            "AppointmentRequest": {
                "creates": "AppointmentRequest",
                "fields": {"name": "{{ payload.full_name }}"},
            }
        },
    )
    await site.insert()
    return site


async def _notifications(recipient: str | None = None) -> list[_NotificationDoc]:
    query = {"recipient": recipient} if recipient else {}
    return await _NotificationDoc.find(query).to_list()


@pytest.fixture
def registered_bridge():
    """Register the production subscriber on the real singleton bus.

    Snapshots the topic's subscriber list and clears it first, so the test sees
    EXACTLY one handler even when something earlier in the session already
    subscribed one (``mount_cloud`` does, and tests/cloud/test_integration.py
    calls it repeatedly) — a duplicate would double every notification and turn
    the count assertions into order-dependent flakes. The snapshot is restored on
    teardown so nothing leaks either way.
    """
    saved = list(event_bus._handlers["lead.captured"])
    event_bus._handlers["lead.captured"].clear()
    leads_bridge.register_lead_notification_listeners()
    yield
    event_bus._handlers["lead.captured"] = saved


@pytest.mark.asyncio
async def test_captured_lead_notifies_owner_and_admins(mongo_db):
    """The owner and every admin get a notification; a plain member does not —
    the recipient set is workspace_service.list_admin_ids, not "everyone"."""
    owner = await _user("owner@x.c", "ws1", "owner")
    admin = await _user("admin@x.c", "ws1", "admin")
    member = await _user("member@x.c", "ws1", "member")

    await leads_bridge._on_lead_captured(
        {
            "workspace_id": "ws1",
            "lead_id": "ld-1",
            "site_id": SITE_ID,
            "form_type": "AppointmentRequest",
        }
    )

    recipients = {n.recipient for n in await _notifications()}
    assert recipients == {owner, admin}
    assert member not in recipients


@pytest.mark.asyncio
async def test_notification_kind_and_source_deep_link_the_site(mongo_db):
    """kind is lead_captured; the source names the LEAD as the entity and
    carries the SITE on room_id, which is what the frontend resolver turns into
    /sites/<site>?view=leads."""
    owner = await _user("owner@x.c", "ws1", "owner")

    await leads_bridge._on_lead_captured(
        {
            "workspace_id": "ws1",
            "lead_id": "ld-42",
            "site_id": SITE_ID,
            "form_type": "AppointmentRequest",
        }
    )

    notes = await _notifications(owner)
    assert len(notes) == 1
    note = notes[0]
    assert note.type == "lead_captured"
    assert note.workspace == "ws1"
    assert note.source is not None
    assert note.source.type == "lead"
    assert note.source.id == "ld-42"
    assert note.source.room_id == SITE_ID
    assert "AppointmentRequest" in note.body


@pytest.mark.asyncio
async def test_body_names_the_site_not_its_script_id(mongo_db):
    """The owner reads this in a bell, so it has to name the site they know.
    site_id is the deploy script name — a 24-char hex id — and rendering it made
    the notification read "… on 6d4a1f2b3c8e9a0f1b2c3d4e"."""
    owner = await _user("owner@x.c", "ws1", "owner")

    await leads_bridge._on_lead_captured(
        {
            "workspace_id": "ws1",
            "lead_id": "ld-1",
            "site_id": SITE_ID,
            "site_name": SITE_NAME,
            "form_type": "AppointmentRequest",
        }
    )

    body = (await _notifications(owner))[0].body
    assert SITE_NAME in body
    assert SITE_ID not in body
    assert "AppointmentRequest" in body


@pytest.mark.asyncio
@pytest.mark.parametrize("site_name", [None, "", "   "])
async def test_body_falls_back_to_the_id_for_an_unnamed_site(mongo_db, site_name):
    """A site that was never named has no better label — the id is at least
    something the owner can match against the URL the click lands on. Missing,
    empty, and whitespace-only all take the fallback rather than rendering
    "on ." or "on None"."""
    owner = await _user("owner@x.c", "ws1", "owner")

    payload = {
        "workspace_id": "ws1",
        "lead_id": "ld-1",
        "site_id": SITE_ID,
        "form_type": "AppointmentRequest",
    }
    if site_name is not None:
        payload["site_name"] = site_name

    await leads_bridge._on_lead_captured(payload)
    body = (await _notifications(owner))[0].body
    assert SITE_ID in body
    assert "None" not in body


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {"lead_id": "ld-1", "site_id": SITE_ID},  # no workspace → no tenancy
        {"workspace_id": "ws1", "site_id": SITE_ID},  # no lead → points nowhere
        {"workspace_id": "ws1", "lead_id": "ld-1"},  # no site → no surface
        {},
    ],
)
async def test_malformed_payload_mints_nothing(mongo_db, payload):
    """An incomplete event is dropped rather than turned into a dead
    notification the owner can only click into a broken link."""
    await _user("owner@x.c", "ws1", "owner")

    await leads_bridge._on_lead_captured(payload)

    assert await _notifications() == []


@pytest.mark.asyncio
async def test_lead_in_one_workspace_never_notifies_another(mongo_db):
    """Cross-tenant safety: the admin resolver filters on the membership's
    workspace, so a lead captured in ws1 cannot reach ws2's owner — including
    the case where the same site_id string exists in both tenants."""
    owner_a = await _user("a@x.c", "ws1", "owner")
    owner_b = await _user("b@x.c", "ws2", "owner")

    await leads_bridge._on_lead_captured(
        {
            "workspace_id": "ws1",
            "lead_id": "ld-1",
            "site_id": "site_shared_name",
            "form_type": "AppointmentRequest",
        }
    )

    assert len(await _notifications(owner_a)) == 1
    assert await _notifications(owner_b) == []


@pytest.mark.asyncio
async def test_capture_to_notification_end_to_end(mongo_db, registered_bridge):
    """The whole T-6 chain over the real in-process bus: a form submission on a
    published site persists a Lead, emits lead.captured, and the registered
    bridge turns it into a notification pointing back at that site."""
    owner = await _user("owner@x.c", "ws1", "owner")
    site = await _site()

    lead = await leads_service.capture(
        site=site,
        form_type="AppointmentRequest",
        payload={"full_name": "Sam", "company_website": ""},
        submitter_ref="ip_hash_1",
        rate_key="rk_e2e",
    )

    assert lead is not None
    notes = await _notifications(owner)
    assert len(notes) == 1
    assert notes[0].type == "lead_captured"
    assert notes[0].source.id == lead.id
    assert notes[0].source.room_id == SITE_ID


@pytest.mark.asyncio
async def test_dropped_capture_notifies_nobody(mongo_db, registered_bridge):
    """A honeypot submission never reaches the insert, so the subscribed bridge
    stays silent — spam must not page the workspace owner."""
    await _user("owner@x.c", "ws1", "owner")
    site = await _site()

    lead = await leads_service.capture(
        site=site,
        form_type="AppointmentRequest",
        payload={"full_name": "Bot", "company_website": "spam"},
        submitter_ref="ip_bot",
        rate_key="rk_bot",
    )

    assert lead is None
    assert await _notifications() == []


@pytest.mark.asyncio
async def test_workspace_with_no_admins_is_a_silent_no_op(mongo_db):
    """No resolvable recipient must not raise — the bus dispatcher would log it
    and sibling handlers would still run, but a lead capture with nobody to
    notify is a normal state, not an error."""
    await leads_bridge._on_lead_captured(
        {
            "workspace_id": "ws-empty",
            "lead_id": "ld-1",
            "site_id": SITE_ID,
            "form_type": "AppointmentRequest",
        }
    )

    assert await _notifications() == []
