# tests/cloud/leads/test_conversation_ref.py
# Created: 2026-08-08 (feat/coupling-t11-conversation-ref).
#
# T-11 — the concierge conversation behind a captured lead.
#
# A visitor chats with the site's concierge, then fills the contact form. The
# owner used to get a lead with no way to read what the person said. The
# paw-bar loader now stamps a hidden ``paw_conversation_ref`` on the lead form
# and this is the server side that stores it.
#
# WHY A NEW FIELD, NOT ``submitter_ref`` (the refuted design):
# ``submitter_ref`` is SERVER-FORCED — ``"anon"`` on the JSON path,
# ``"form:<page>"`` on the native path. Joining a transcript on it would map
# essentially every lead on a site to the key ``"anon"`` and surface one
# visitor's private conversation behind a different visitor's lead. That is
# worse than today's no-link state, so the join gets its own field and
# ``test_submitter_ref_is_still_not_an_identity`` keeps it that way.

from __future__ import annotations

import pytest

pytest.importorskip("pocketpaw_ee")

from pocketpaw_ee.cloud.leads import service as leads_service  # noqa: E402
from pocketpaw_ee.cloud.models.lead import Lead as _LeadDoc  # noqa: E402
from pocketpaw_ee.cloud.models.site import Site as _SiteDoc  # noqa: E402

WS = "w1"
REF = "a" * 64


async def _site(**over) -> _SiteDoc:
    doc = _SiteDoc(
        workspace=WS,
        script_name="site-abc",
        signed_key="sk_test",
        pocket_id="p1",
        owner="u1",
        event_mapping={"lead": {"creates": "Lead", "fields": {"email": "{{ payload.email }}"}}},
        **over,
    )
    await doc.insert()
    return doc


async def _capture(site, *, conversation_ref: str = "", submitter_ref: str = "anon"):
    return await leads_service.capture(
        site=site,
        form_type="lead",
        payload={"email": "sam@example.com"},
        submitter_ref=submitter_ref,
        conversation_ref=conversation_ref,
        rate_key="host-1",
    )


# ---------------------------------------------------------------------------
# 1. the join
# ---------------------------------------------------------------------------


async def test_a_lead_stores_the_conversation_ref(mongo_db):
    """THE SLICE. The ref survives capture onto the Lead, so the owner's
    transcript link has something to point at.

    MUTATION THAT BREAKS THIS: stop passing ``conversation_ref`` into
    ``_LeadSourceDoc`` — the stored ref comes back empty."""
    site = await _site()

    lead = await _capture(site, conversation_ref=REF)

    assert lead is not None
    doc = await _LeadDoc.get(lead.id)
    assert doc is not None
    assert doc.source.conversation_ref == REF


async def test_a_lead_with_no_conversation_stores_an_empty_ref(mongo_db):
    """The common case — most visitors never open the concierge. Empty, never
    a placeholder, so the FE can branch on truthiness alone."""
    site = await _site()

    lead = await _capture(site)

    doc = await _LeadDoc.get(lead.id)
    assert doc.source.conversation_ref == ""


# ---------------------------------------------------------------------------
# 2. the refuted design must stay refuted
# ---------------------------------------------------------------------------


async def test_submitter_ref_is_still_not_an_identity(mongo_db):
    """The privacy guard. ``submitter_ref`` is server-forced, so two DIFFERENT
    visitors on the same site share its value — which is exactly why the
    conversation join must not use it.

    This test exists to make the refutation permanent: if someone later
    "simplifies" T-11 by joining on ``submitter_ref``, the identical values
    below show what that would surface.

    MUTATION THAT BREAKS THIS: have the router pass ``submitter_ref`` through
    as the conversation ref — the two leads' refs stop being distinguishable
    while their conversations differ."""
    site = await _site()

    first = await _capture(site, conversation_ref="a" * 64, submitter_ref="anon")
    second = await _capture(site, conversation_ref="b" * 64, submitter_ref="anon")

    d1 = await _LeadDoc.get(first.id)
    d2 = await _LeadDoc.get(second.id)

    # Same submitter_ref — it is a label, not a person.
    assert d1.source.submitter_ref == d2.source.submitter_ref == "anon"
    # Different conversations — which is the whole point of the separate field.
    assert d1.source.conversation_ref != d2.source.conversation_ref


# ---------------------------------------------------------------------------
# 3. the screen
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    ["", "   ", "short", "has spaces", "<script>alert(1)</script>", "x" * 200, "a/b", "a.b"],
)
async def test_a_malformed_ref_is_dropped_and_the_lead_still_lands(mongo_db, bad):
    """A bad ref costs the transcript link, never the lead. The lead is the
    thing with revenue attached; refusing it over a broken join key would trade
    a missing link for a missing customer.

    MUTATION THAT BREAKS THIS: make ``_clean_conversation_ref`` return its
    input unscreened — the malformed values land on the doc."""
    site = await _site()

    lead = await _capture(site, conversation_ref=bad)

    assert lead is not None, "the lead was dropped over a bad conversation ref"
    doc = await _LeadDoc.get(lead.id)
    assert doc.source.conversation_ref == ""


async def test_a_well_shaped_ref_survives_the_screen(mongo_db):
    """The screen must not be so tight it rejects the real thing — the paw-bar
    ref is a 64-char sha256 hex."""
    site = await _site()

    lead = await _capture(site, conversation_ref=REF)

    doc = await _LeadDoc.get(lead.id)
    assert doc.source.conversation_ref == REF


def test_conversation_ref_shape_matches_paw_bar():
    """CROSS-REPO PIN. The ref this service accepts is minted and validated in
    paw-bar. A pattern that drifts either way ships leads whose transcript link
    404s — the failure is silent on both sides, which is why it is pinned here
    rather than described in a comment.

    MUTATION THAT BREAKS THIS: loosen or tighten ``_CONVERSATION_REF_RE``."""
    from pocketpaw_ee.paw_bar.router import _CUSTOMER_REF_RE

    assert leads_service._CONVERSATION_REF_RE.pattern == _CUSTOMER_REF_RE.pattern


# ---------------------------------------------------------------------------
# 4. tenancy
# ---------------------------------------------------------------------------


async def test_the_ref_rides_the_lead_into_its_own_workspace(mongo_db):
    """The ref is a pointer into a workspace-scoped transcript. It must land on
    the site's own workspace row and nowhere else."""
    site = await _site()

    lead = await _capture(site, conversation_ref=REF)

    doc = await _LeadDoc.get(lead.id)
    assert doc.workspace == WS
    assert await _LeadDoc.find({"workspace": "w2"}).to_list() == []


async def test_a_lead_with_no_conversation_never_borrows_submitter_ref(mongo_db):
    """THE PRIVACY GATE, and the one that actually bites.

    The previous test passes a real conversation ref for both leads, so a
    ``conversation_ref or submitter_ref`` fallback would never fire and the
    mutation escaped. This is the case that exercises it: NO conversation, and
    a submitter_ref well-shaped enough to survive the screen.

    Two visitors who submit the same ``submitter_ref`` (it is caller-supplied
    on the JSON path, so nothing stops that) must both end up with NO
    conversation link. Under the fallback they would both point at the same
    ref, and the site owner clicking either lead would open a transcript that
    belongs to neither of them — or worse, to whichever visitor's conversation
    happened to be keyed there.

    MUTATION THAT BREAKS THIS: ``conversation_ref or submitter_ref`` in
    ``capture``'s ``_LeadSourceDoc`` — both refs come back non-empty."""
    site = await _site()
    shared = "visitorlabel123456"  # well-shaped: the screen will NOT reject it
    assert leads_service._CONVERSATION_REF_RE.match(shared), "fixture must survive the screen"

    first = await _capture(site, conversation_ref="", submitter_ref=shared)
    second = await _capture(site, conversation_ref="", submitter_ref=shared)

    d1 = await _LeadDoc.get(first.id)
    d2 = await _LeadDoc.get(second.id)

    assert d1.source.submitter_ref == d2.source.submitter_ref == shared
    assert d1.source.conversation_ref == "", "a lead borrowed submitter_ref as its conversation"
    assert d2.source.conversation_ref == ""


async def test_the_conversation_ref_survives_onto_the_wire(mongo_db):
    """The field-by-field DTO mapper drops anything it does not name, so a ref
    that reaches Mongo can still be invisible to the FE. Pinned end to end.

    MUTATION THAT BREAKS THIS: remove ``conversation_ref`` from
    ``lead_to_dto`` — the wire row comes back empty while the doc is correct,
    which is exactly the failure that looks like a frontend bug."""
    from pocketpaw_ee.cloud.leads.dto import lead_to_dto

    site = await _site()
    await _capture(site, conversation_ref=REF)

    leads = await leads_service.list_for_site(WS, site.script_name, limit=10)
    assert leads, "the lead did not come back from the list read"
    assert lead_to_dto(leads[0]).conversation_ref == REF
