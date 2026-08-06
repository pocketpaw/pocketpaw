# tests/cloud/growth/test_lead_bridge.py — the lead.captured → Prospect bridge.
#
# Created 2026-08-06 (feat/coupling-lead-to-prospect, T-7). Built to the shape
# of tests/cloud/leads/test_bridges.py, and with the same deliberate choice:
# NOTHING below the bridge is mocked. Leads are created by the real capture
# path, prospects are read back as persisted docs, and the assertions are about
# rows in the database. Mocking growth_service here would prove the bridge calls
# a function; the things that can actually be wrong — which rows collapse into
# which, whether an inbound lead resets a live pipeline row, whether a lead in
# one tenant can reach another — all live below that seam.
#
# The load-bearing case is the CONSUMER-EMAIL COLLAPSE. Growth dedupes on
# company domain, site visitors submit personal addresses, and keying those on
# the mail host would file every unrelated gmail lead a workspace ever gets onto
# one prospect called "gmail.com". Two gmail leads → two prospects is the test
# this file exists for; two colleagues at one real company → one prospect is the
# test that proves the guard did not just disable deduping.

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

import pytest
from pocketpaw_ee.cloud._core.context import RequestContext, ScopeKind
from pocketpaw_ee.cloud.growth import service as growth_service
from pocketpaw_ee.cloud.growth.bridges import leads as growth_bridge
from pocketpaw_ee.cloud.growth.dto import CreateProspectRequest
from pocketpaw_ee.cloud.leads import service as leads_service
from pocketpaw_ee.cloud.models.prospect import Prospect as _ProspectDoc
from pocketpaw_ee.cloud.models.site import Site
from pocketpaw_ee.cloud.shared.events import event_bus

# A real script_name is a 24-char hex ObjectId, not a slug (T-6 review fix).
SITE_ID = "6d4a1f2b3c8e9a0f1b2c3d4e"
SITE_NAME = "Bright Smile Dental"
FORM_TYPE = "Contact"


async def _site(ws: str = "ws1", site_id: str = SITE_ID, name: str = SITE_NAME) -> Site:
    """A published site whose form collects the fields a B2B enquiry carries.

    ``honeypot_field`` is left at its default (``company_website``), so the
    company's site is collected under ``company_url`` — which is how a real
    site has to do it, since anything landing in the honeypot is dropped as a
    bot before a Lead exists.
    """
    site = Site(
        workspace=ws,
        pocket_id="pk1",
        owner="u1",
        name=name,
        script_name=site_id,
        allowed_origins=["brightsmiledental.com"],
        signed_key="pp_tok_x",
        event_mapping={
            FORM_TYPE: {
                "creates": "Lead",
                "fields": {
                    "full_name": "{{ payload.full_name }}",
                    "email": "{{ payload.email }}",
                    "company": "{{ payload.company }}",
                    "website": "{{ payload.company_url }}",
                    "phone": "{{ payload.phone }}",
                },
            }
        },
    )
    await site.insert()
    return site


async def _capture(site: Site, *, rate_key: str = "rk", **payload: Any):
    """Put one submission through the real capture path."""
    return await leads_service.capture(
        site=site,
        form_type=FORM_TYPE,
        payload=payload,
        submitter_ref=f"ref_{rate_key}",
        rate_key=rate_key,
    )


def _event(lead, *, workspace_id: str = "ws1") -> dict[str, Any]:
    """The lead.captured payload the leads service emits for ``lead``."""
    return {
        "workspace_id": workspace_id,
        "lead_id": lead.id,
        "site_id": SITE_ID,
        "site_name": SITE_NAME,
        "form_type": FORM_TYPE,
    }


async def _prospects(workspace: str = "ws1") -> list[_ProspectDoc]:
    return await _ProspectDoc.find({"workspace": workspace}).to_list()


def _ctx(workspace_id: str, user_id: str = "u1") -> RequestContext:
    """A viewer context for the read paths /growth serves."""
    return RequestContext(
        user_id=user_id,
        workspace_id=workspace_id,
        request_id="test",
        scope=ScopeKind.WORKSPACE,
        started_at=datetime.now(UTC),
    )


@pytest.fixture(autouse=True)
def _isolated_bus():
    """Snapshot, clear and restore the topic's subscribers around every test.

    ``mount_cloud`` subscribes the production handlers and tests/cloud/
    test_integration.py mounts repeatedly, so without this a ``capture()``
    here would fan out into whatever an earlier test left behind. Restored on
    teardown so this file leaks nothing either.
    """
    saved = list(event_bus._handlers["lead.captured"])
    event_bus._handlers["lead.captured"].clear()
    yield
    event_bus._handlers["lead.captured"] = saved


@pytest.fixture
def registered_bridge(_isolated_bus):
    """The production subscriber on the real singleton bus, and nothing else."""
    growth_bridge.register_growth_lead_listeners()


# ---------------------------------------------------------------------------
# (a) a captured lead becomes a prospect
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_captured_lead_becomes_a_prospect(mongo_db, registered_bridge):
    """The whole T-7 chain over the real bus: a form submission on a published
    site lands in /growth as a prospect that points back at the submission.

    Mutation: change ``source="site_lead"`` in the bridge to any other source,
    or drop ``lead_id`` from the request — both fail here.
    """
    site = await _site()

    lead = await _capture(
        site,
        full_name="Sam Founder",
        email="sam@acme-dental.com",
        company="Acme Dental",
        rate_key="rk1",
    )

    assert lead is not None
    rows = await _prospects()
    assert len(rows) == 1
    row = rows[0]
    assert row.source == "site_lead"
    assert row.lead_id == lead.id
    assert row.domain == "acme-dental.com"
    assert row.name == "Sam Founder"
    assert row.company == "Acme Dental"
    assert row.emails == ["sam@acme-dental.com"]
    # An inbound row starts where every other prospect starts — research
    # triages it, the bridge does not get to claim it is qualified.
    assert row.tier == "unqualified"
    assert row.status == "new"


@pytest.mark.asyncio
async def test_prospect_response_carries_the_lead_link(mongo_db):
    """``lead_id`` survives the doc → domain → response mapping, so /growth can
    actually render the link back to the submission. Read through the same
    service call the view uses, not off the doc — the mapping is where a
    provenance field goes missing without anything failing.

    Mutation: drop ``lead_id`` from ``_to_domain`` or from ``_to_response``.
    """
    site = await _site()
    lead = await _capture(site, full_name="Sam", email="sam@acme-dental.com", rate_key="rk1")

    await growth_bridge._on_lead_captured(_event(lead))

    row = (await _prospects())[0]
    response = await growth_service.get(_ctx("ws1"), str(row.id))
    assert response.lead_id == lead.id
    assert response.source == "site_lead"


# ---------------------------------------------------------------------------
# (b) real company domains still dedup
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_two_leads_at_one_real_company_dedup_to_one_prospect(mongo_db, registered_bridge):
    """Two colleagues enquiring from the same company are ONE company to sell
    to — the pre-T-7 behaviour, which the collapse guard must not disable.

    Mutation: make the key always the email address — this fails.
    """
    site = await _site()

    first = await _capture(
        site, full_name="Sam Founder", email="sam@acme-dental.com", rate_key="rk1"
    )
    second = await _capture(site, full_name="Jo Ops", email="jo@acme-dental.com", rate_key="rk2")

    assert first is not None and second is not None
    rows = await _prospects()
    assert len(rows) == 1
    assert rows[0].domain == "acme-dental.com"
    # Both ways to reach the company are kept; the second does not replace the
    # first.
    assert rows[0].emails == ["sam@acme-dental.com", "jo@acme-dental.com"]
    # Provenance answers "where did this row come from", so it stays with the
    # submission that created it.
    assert rows[0].lead_id == first.id


# ---------------------------------------------------------------------------
# (c) the collapse guard — consumer addresses key per person
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_two_consumer_email_leads_produce_two_prospects(mongo_db, registered_bridge):
    """THE guard. Alice and Bob share nothing but a mail provider; keying them
    on ``gmail.com`` would file them as one prospect and let the second
    overwrite the first's name.

    Mutation: empty ``CONSUMER_EMAIL_DOMAINS``, or make
    ``is_consumer_email_domain`` return False — this fails with 1 prospect.
    """
    site = await _site()

    await _capture(site, full_name="Alice A", email="alice@gmail.com", rate_key="rk1")
    await _capture(site, full_name="Bob B", email="bob@gmail.com", rate_key="rk2")

    rows = sorted(await _prospects(), key=lambda r: r.domain)
    assert len(rows) == 2
    assert [r.domain for r in rows] == ["alice@gmail.com", "bob@gmail.com"]
    assert {r.name for r in rows} == {"Alice A", "Bob B"}
    assert all(r.source == "site_lead" for r in rows)


@pytest.mark.asyncio
async def test_the_same_person_twice_is_still_one_prospect(mongo_db, registered_bridge):
    """Per-email keying is still DEDUPING — a visitor who submits twice (or
    fills a second form on the same site) is one prospect, not two.

    Mutation: key on the lead id instead of the address.
    """
    site = await _site()

    await _capture(site, full_name="Alice A", email="alice@gmail.com", rate_key="rk1")
    await _capture(site, full_name="Alice A", email="ALICE@Gmail.com", rate_key="rk2")

    rows = await _prospects()
    assert len(rows) == 1
    assert rows[0].domain == "alice@gmail.com"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "address",
    ["alice@outlook.com", "alice@yahoo.co.uk", "alice@icloud.com", "alice@proton.me"],
)
async def test_other_consumer_providers_key_per_person(mongo_db, registered_bridge, address):
    """Gmail is not the only one. A provider missing from the set collapses
    exactly the same way, so the set is checked past its most obvious member."""
    site = await _site()

    await _capture(site, full_name="Alice", email=address, rate_key="rk1")
    await _capture(site, full_name="Bob", email=address.replace("alice@", "bob@"), rate_key="rk2")

    assert len(await _prospects()) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "address",
    ["a/b@gmail.com", "a:b@gmail.com", "www.smith@gmail.com", "/x@gmail.com"],
)
async def test_an_email_key_is_never_url_surgered(mongo_db, registered_bridge, caplog, address):
    """The dedupe key rides through the DTO's domain normaliser, and a WHOLE
    ADDRESS must come out the other side intact. Before the ``@`` guard the
    normaliser treated it as a URL: ``a/b@gmail.com`` truncated at the slash to
    ``a``, ``www.smith@gmail.com`` lost its ``www.`` prefix, and ``/x@gmail.com``
    normalised to empty — a ValidationError whose pydantic message carried the
    visitor's raw address into the error log.

    Rare local parts (Gmail forbids them), but other mailboxes allow them and
    the bridge's email check is deliberately loose. Asserts the stored key IS
    the address and that nothing reached the error log.

    Mutation: remove the ``@`` guard in ``normalise_domain`` — this fails.
    """
    site = await _site()

    with caplog.at_level(logging.ERROR, logger=growth_bridge.__name__):
        lead = await _capture(site, full_name="Weird Localpart", email=address, rate_key="rk1")

    assert lead is not None
    rows = await _prospects()
    assert len(rows) == 1
    assert rows[0].domain == address.lower()
    assert [r.message for r in caplog.records if r.levelno >= logging.ERROR] == []


@pytest.mark.asyncio
async def test_url_surgery_cannot_merge_unrelated_visitors(mongo_db, registered_bridge):
    """The collapse class the guard closes: ``a/b@gmail.com`` and
    ``a:b@gmail.com`` both truncated to the key ``a`` before the fix, merging
    two strangers onto one prospect. Same for ``www.smith@gmail.com``
    swallowing the real ``smith@gmail.com``. Four visitors, four rows."""
    site = await _site()

    await _capture(site, full_name="Visitor One", email="a/b@gmail.com", rate_key="rk1")
    await _capture(site, full_name="Visitor Two", email="a:b@gmail.com", rate_key="rk2")
    await _capture(site, full_name="Real Smith", email="smith@gmail.com", rate_key="rk3")
    await _capture(site, full_name="Www Smith", email="www.smith@gmail.com", rate_key="rk4")

    rows = await _prospects()
    assert sorted(r.domain for r in rows) == [
        "a/b@gmail.com",
        "a:b@gmail.com",
        "smith@gmail.com",
        "www.smith@gmail.com",
    ]


@pytest.mark.asyncio
async def test_a_company_website_beats_a_personal_address(mongo_db, registered_bridge):
    """Someone enquiring for acme.com from their personal mailbox is still
    acme.com. The form answered the dedupe question directly, so the company
    domain wins over the mail host — and a colleague doing the same collapses
    onto the same row."""
    site = await _site()

    await _capture(
        site,
        full_name="Alice A",
        email="alice@gmail.com",
        company_url="https://www.Acme.com/contact",
        rate_key="rk1",
    )
    await _capture(
        site,
        full_name="Bob B",
        email="bob@gmail.com",
        company_url="acme.com",
        rate_key="rk2",
    )

    rows = await _prospects()
    assert len(rows) == 1
    assert rows[0].domain == "acme.com"


@pytest.mark.asyncio
async def test_a_junk_website_answer_is_not_a_dedupe_key(mongo_db, registered_bridge):
    """ "n/a" is what a website field collects most of the time. Treating it as
    a domain would merge every lead that typed the same non-answer, which is
    the collapse bug again wearing a different hat."""
    site = await _site()

    await _capture(
        site, full_name="Alice", email="alice@gmail.com", company_url="n/a", rate_key="rk1"
    )
    await _capture(site, full_name="Bob", email="bob@gmail.com", company_url="none", rate_key="rk2")

    rows = sorted(await _prospects(), key=lambda r: r.domain)
    assert [r.domain for r in rows] == ["alice@gmail.com", "bob@gmail.com"]


@pytest.mark.asyncio
async def test_a_lead_with_no_identity_files_no_prospect(mongo_db, registered_bridge, caplog):
    """A phone-only submission has no stable key. Filing it under a synthetic
    per-submission id would create a fresh row every time the same visitor came
    back — so the pipeline row is skipped and the LEAD is untouched: still
    persisted, still in the Leads view, still notified.

    It is skipped QUIETLY. A form with no email is a normal state, not a
    failure, and without the early return the empty key reaches the DTO, trips
    its ``min_length`` and lands in the bridge's rescue — which would stamp a
    stack trace into the error log for every phone-only lead a site collects.
    That is the half of this guard a row count alone cannot see.

    Mutation: delete the ``if not key`` early return — the prospect count still
    passes and this assertion fails.
    """
    site = await _site()

    with caplog.at_level(logging.ERROR, logger=growth_bridge.__name__):
        lead = await _capture(site, full_name="Anon", phone="+15551234567", rate_key="rk1")

    assert lead is not None
    assert await _prospects() == []
    assert [r.message for r in caplog.records if r.levelno >= logging.ERROR] == []


# ---------------------------------------------------------------------------
# An inbound lead must never walk a live prospect backwards
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_inbound_lead_never_resets_a_live_prospect(mongo_db, registered_bridge):
    """The best thing that can happen to a prospect you are already working is
    that they write to you. It must not be the thing that resets them.

    ``upsert_by_domain`` overwrites every mutable field, so routing the bridge
    through it would walk a researched, in-sequence row back to
    new/unqualified and replace its research brief with a form fill. This is
    why the bridge has its own seam.

    Mutation: point ``_upsert`` at ``upsert_by_domain`` — this fails.
    """
    await growth_service.upsert_by_domain(
        "ws1",
        CreateProspectRequest(
            name="Sam Founder",
            company="Acme Dental",
            domain="acme-dental.com",
            source="discovery",
            tier="a",
            status="in_sequence",
            research_brief="Six chairs, still books by phone.",
            emails=["sam@acme-dental.com"],
        ),
    )
    site = await _site()

    await _capture(site, full_name="Jo Ops", email="jo@acme-dental.com", rate_key="rk1")

    rows = await _prospects()
    assert len(rows) == 1
    row = rows[0]
    assert row.tier == "a"
    assert row.status == "in_sequence"
    assert row.research_brief == "Six chairs, still books by phone."
    assert row.name == "Sam Founder"
    # Provenance at first capture is kept, exactly as upsert_by_domain does.
    assert row.source == "discovery"
    # …and the enquiry still added what it knew.
    assert row.emails == ["sam@acme-dental.com", "jo@acme-dental.com"]


@pytest.mark.asyncio
async def test_an_inbound_lead_fills_in_what_a_bare_import_left_blank(mongo_db, registered_bridge):
    """The other half of enrich-only: a pasted list of bare domains leaves
    ``name``/``company`` at ``""`` (NOT YET KNOWN, per the doc class), and the
    form fill is exactly the thing that finally knows them. The fill must land —
    add-only means "never overwrite", not "never write".

    Mutation: neuter the ``doc.name = body.name`` fill in
    ``upsert_from_site_lead`` — this fails.
    """
    await growth_service.upsert_by_domain(
        "ws1",
        CreateProspectRequest(domain="acme-dental.com", source="directory"),
    )
    site = await _site()

    await _capture(
        site,
        full_name="Sam Founder",
        email="sam@acme-dental.com",
        company="Acme Dental",
        rate_key="rk1",
    )

    rows = await _prospects()
    assert len(rows) == 1
    row = rows[0]
    assert row.name == "Sam Founder"
    assert row.company == "Acme Dental"
    assert row.emails == ["sam@acme-dental.com"]
    assert row.lead_id is not None
    # Provenance at first capture is still the import's.
    assert row.source == "directory"


# ---------------------------------------------------------------------------
# (d) cross-tenant
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_lead_in_one_workspace_never_files_in_another(mongo_db, registered_bridge):
    """The capture path's workspace is the only one that gets the prospect."""
    site = await _site(ws="ws1")

    await _capture(site, full_name="Sam", email="sam@acme-dental.com", rate_key="rk1")

    assert len(await _prospects("ws1")) == 1
    assert await _prospects("ws2") == []


@pytest.mark.asyncio
async def test_an_event_naming_a_foreign_tenant_files_nothing(mongo_db):
    """The tenancy boundary is the lead READ, not a filter afterwards. An event
    claiming workspace B while naming workspace A's lead resolves to nothing,
    so no prospect is written in either tenant — B never learns the lead exists
    and A is not written to by a claim it did not make.

    Mutation: drop ``workspace`` from the filter in
    ``leads_service.get_in_workspace`` — this fails with a prospect in ws2.
    """
    site = await _site(ws="ws1")
    lead = await _capture(site, full_name="Sam", email="sam@acme-dental.com", rate_key="rk1")

    await growth_bridge._on_lead_captured(_event(lead, workspace_id="ws2"))

    assert await _prospects("ws1") == []
    assert await _prospects("ws2") == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {"lead_id": "ld-1"},  # no workspace → no tenant to file into
        {"workspace_id": "ws1"},  # no lead → nothing to read
        {},
    ],
)
async def test_malformed_payload_files_nothing(mongo_db, payload):
    """An incomplete event is dropped rather than guessed at."""
    await growth_bridge._on_lead_captured(payload)

    assert await _prospects() == []


@pytest.mark.asyncio
async def test_an_unknown_lead_id_files_nothing(mongo_db):
    """A well-formed event naming a lead that isn't there — a deleted row, a
    replayed event — must not mint a prospect out of the event alone."""
    await growth_bridge._on_lead_captured(
        {
            "workspace_id": "ws1",
            "lead_id": "6d4a1f2b3c8e9a0f1b2c3d4e",
            "site_id": SITE_ID,
            "form_type": FORM_TYPE,
        }
    )

    assert await _prospects() == []


# ---------------------------------------------------------------------------
# (e) a growth failure never costs the lead
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_growth_failure_is_contained_inside_the_bridge(mongo_db, monkeypatch):
    """The handler is called DIRECTLY here, with no bus underneath it, so the
    bridge's own try/except is the only thing that can contain the failure.

    ``EventBus.emit`` also logs and swallows, which is why this is asserted
    without it: through ``emit`` the mutation would be invisible, and a
    subscriber that relies on the dispatcher's rescue is one direct call or
    replay job away from taking lead capture down with it.

    Mutation: remove the try/except from ``_upsert`` — this errors.
    """
    site = await _site()
    lead = await _capture(site, full_name="Sam", email="sam@acme-dental.com", rate_key="rk1")

    async def _boom(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("growth is down")

    monkeypatch.setattr(growth_service, "upsert_from_site_lead", _boom)

    await growth_bridge._on_lead_captured(_event(lead))  # must not raise

    assert await _prospects() == []


@pytest.mark.asyncio
async def test_a_growth_failure_costs_neither_the_lead_nor_the_notification(mongo_db, monkeypatch):
    """The product claim, over the real bus with BOTH subscribers wired and
    growth registered FIRST — the ordering where a propagating failure would
    swallow the notification too.

    A lead is revenue; a pipeline row is convenience. The capture returns the
    lead, the workspace still gets rung, and only the prospect is missing.
    """
    from pocketpaw_ee.cloud.leads.bridges import notifications as leads_notifications
    from pocketpaw_ee.cloud.models.notification import Notification as _NotificationDoc
    from pocketpaw_ee.cloud.models.user import User as _UserDoc
    from pocketpaw_ee.cloud.models.user import WorkspaceMembership

    owner = _UserDoc(
        email="owner@x.c",
        hashed_password="x",
        is_active=True,
        is_verified=True,
        full_name="Owner",
        workspaces=[WorkspaceMembership(workspace="ws1", role="owner")],
    )
    await owner.insert()

    growth_bridge.register_growth_lead_listeners()
    leads_notifications.register_lead_notification_listeners()

    async def _boom(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("growth is down")

    monkeypatch.setattr(growth_service, "upsert_from_site_lead", _boom)

    site = await _site()
    lead = await _capture(site, full_name="Sam", email="sam@acme-dental.com", rate_key="rk1")

    assert lead is not None  # the lead is persisted and returned to the visitor
    notes = await _NotificationDoc.find({"recipient": str(owner.id)}).to_list()
    assert len(notes) == 1
    assert notes[0].type == "lead_captured"
    assert await _prospects() == []
