# tests/cloud/sites/test_transfer_authz.py — the transfer's tenancy boundary,
# driven through the real service functions against a real (mock) Mongo.
#
# Created 2026-09-12 (sites lifecycle wave 3, feat/sites-transfer).
#
# WHY THIS EXISTS ALONGSIDE tests/ee/sites/test_transfer.py. That file tests the
# ordering and the refusal PREDICATES in isolation, with doubles. This one exists
# because the predicates being right proves nothing about whether they are reached:
# a transfer is the one sites operation that deliberately crosses a tenancy line, and
# the ways that goes wrong are all in the wiring —
#
#   * the incoming-offers read is the only sites query NOT anchored on the caller's
#     workspace. If its filter were wrong it would list other tenants' sites, and a
#     test with one workspace in the database could never see that;
#   * accepting addresses a row that belongs to SOMEBODY ELSE, so it cannot use the
#     tenant-scoped loader every other sites read uses. That is exactly the shape of
#     code that ends up with no tenant filter at all;
#   * the membership check has to read the accepting user's OWN record. Asserting it
#     against a constructed list would test the function and not the wiring.
#
# So every test here puts at least two workspaces and two users in the database.

from __future__ import annotations

import itertools

import pytest
from pocketpaw_ee.cloud._core.errors import CloudError
from pocketpaw_ee.cloud.models.lead import Lead, LeadSource
from pocketpaw_ee.cloud.models.pocket import Pocket
from pocketpaw_ee.cloud.models.site import Site
from pocketpaw_ee.cloud.models.user import User, WorkspaceMembership
from pocketpaw_ee.cloud.models.workspace import Workspace
from pocketpaw_ee.sites import public_assets
from pocketpaw_ee.sites import service as sites_service

pytestmark = pytest.mark.usefixtures("mongo_db")


_SLUGS = itertools.count()


async def _workspace(slug: str, owner: str, *, transfers_allowed: bool = True) -> str:
    # ``slug`` is uniquely indexed, and several tests build two tenant pairs, so the
    # name is suffixed rather than reused.
    ws = Workspace(name=slug, slug=f"{slug}-{next(_SLUGS)}", owner=owner)
    ws.settings.site_transfers_allowed = transfers_allowed
    await ws.insert()
    return str(ws.id)


async def _user(workspaces: list[str], email: str) -> str:
    user = User(
        email=email,
        name=email.split("@")[0],
        # Required on the model; never read by anything under test here.
        hashed_password="x",
        workspaces=[WorkspaceMembership(workspace=w, role="member") for w in workspaces],
    )
    await user.insert()
    return str(user.id)


async def _site(workspace: str, owner: str, *, name: str = "Acme") -> Site:
    pocket = Pocket(workspace=workspace, owner=owner, name=name)
    await pocket.insert()
    oid = sites_service._live_object_id(workspace, str(pocket.id))
    site = Site(
        id=oid,
        workspace=workspace,
        pocket_id=str(pocket.id),
        owner=owner,
        name=name,
        script_name=str(oid),
        url=f"https://{oid}.paw.dev",
        deployed=True,
    )
    await site.insert()
    return site


async def _two_tenants():
    """A source and a destination, with a distinct owner on each side."""
    sender = await _user([], "sender@acme.test")
    recipient = await _user([], "recipient@client.test")
    source = await _workspace("acme", sender)
    dest = await _workspace("client", recipient)
    # Re-read memberships now that the workspace ids exist.
    for uid, ws in ((sender, source), (recipient, dest)):
        user = await User.get(uid)
        user.workspaces = [WorkspaceMembership(workspace=ws, role="member")]
        await user.save()
    return sender, recipient, source, dest


async def _offered():
    sender, recipient, source, dest = await _two_tenants()
    site = await _site(source, sender)
    await sites_service.offer_site_transfer(
        workspace_id=source,
        user_id=sender,
        site_id=str(site.id),
        destination_workspace_id=dest,
    )
    return sender, recipient, source, dest, site


# ---------------------------------------------------------------------------
# The send half
# ---------------------------------------------------------------------------


async def test_offer_is_tenant_scoped_to_the_sender():
    """A workspace cannot offer a site it does not own, even by id.

    The offer loads through ``_load``, which filters on workspace — so a third party
    naming the site id gets a not-found rather than the power to give it away.
    """
    sender, _recipient, source, dest = await _two_tenants()
    site = await _site(source, sender)
    outsider_ws = await _workspace("outsider", "u-outsider")
    with pytest.raises(CloudError) as exc:
        await sites_service.offer_site_transfer(
            workspace_id=outsider_ws,
            user_id="u-outsider",
            site_id=str(site.id),
            destination_workspace_id=dest,
        )
    assert exc.value.status_code == 404


async def test_offer_refuses_a_destination_that_does_not_exist():
    """An offer into a typo would park the site in ``offered`` forever, with nobody
    able to accept it and nobody told why."""
    sender, _r, source, _dest = await _two_tenants()
    site = await _site(source, sender)
    with pytest.raises(CloudError) as exc:
        await sites_service.offer_site_transfer(
            workspace_id=source,
            user_id=sender,
            site_id=str(site.id),
            destination_workspace_id="64b7f1e2c3d4e5f6a7b8c9d0",
        )
    assert exc.value.status_code == 404


async def test_offer_refuses_when_the_workspace_forbids_transfers_out():
    sender = await _user([], "s@acme.test")
    source = await _workspace("locked", sender, transfers_allowed=False)
    dest = await _workspace("dest", "u2")
    user = await User.get(sender)
    user.workspaces = [WorkspaceMembership(workspace=source, role="member")]
    await user.save()
    site = await _site(source, sender)
    with pytest.raises(CloudError) as exc:
        await sites_service.offer_site_transfer(
            workspace_id=source,
            user_id=sender,
            site_id=str(site.id),
            destination_workspace_id=dest,
        )
    assert exc.value.status_code == 403


async def test_offer_moves_nothing():
    """THE TWO-PHASE PROPERTY. An offer is a request, not a transfer.

    Mutation that must break this: have ``offer_site_transfer`` set ``workspace`` to
    the destination alongside ``transfer_to_workspace``.
    """
    sender, _r, source, dest, site = await _offered()
    fresh = await Site.get(site.id)
    assert fresh.workspace == source
    assert fresh.owner == sender
    assert fresh.transfer_status == "offered"
    assert fresh.transfer_to_workspace == dest


# ---------------------------------------------------------------------------
# The receive half — the one a single-call transfer would not gate at all
# ---------------------------------------------------------------------------


async def test_incoming_lists_only_what_was_offered_to_you():
    """The one sites read not anchored on the caller's workspace.

    Two live offers exist in the database, addressed to two different tenants. Each
    must see exactly its own. A single-tenant fixture would pass against a filter
    that was missing entirely, which is why there are two here.

    Mutation that must break this: drop ``transfer_to_workspace`` from the query in
    ``list_incoming_site_transfers``.
    """
    _s1, _r1, _src1, dest1, site1 = await _offered()
    _s2, _r2, _src2, dest2, site2 = await _offered()

    to_one = await sites_service.list_incoming_site_transfers(workspace_id=dest1)
    to_two = await sites_service.list_incoming_site_transfers(workspace_id=dest2)
    assert [r["siteId"] for r in to_one] == [str(site1.id)]
    assert [r["siteId"] for r in to_two] == [str(site2.id)]


async def test_incoming_does_not_leak_the_signed_key_or_capture_config():
    """The destination is reading a row that still belongs to somebody else.

    ``signed_key`` is a live credential for lead ingest on a site this workspace does
    not own yet. It must not be in the payload that helps them decide.
    """
    _s, _r, _src, dest, _site = await _offered()
    rows = await sites_service.list_incoming_site_transfers(workspace_id=dest)
    assert rows, "expected the offer to be listed"
    blob = repr(rows[0]).lower()
    assert "signed" not in blob
    assert "site_key_" not in blob
    assert set(rows[0]) == {
        "siteId",
        "name",
        "url",
        "fromWorkspaceId",
        "offeredBy",
        "offeredAt",
        "status",
        "toWorkspaceId",
    }


async def test_a_non_member_cannot_accept_into_the_destination():
    """THE RECEIVING GUARD, and the reason this test needs a real user row.

    The caller names the destination workspace in the request context — which a
    caller can do freely. What stops them is their own User record not listing it.

    Mutation that must break this: have ``_member_workspace_ids`` return
    ``(workspace_id,)`` instead of reading the user.
    """
    _sender, _recipient, _source, dest, site = await _offered()
    outsider = await _user(["some-other-workspace"], "outsider@elsewhere.test")
    with pytest.raises(CloudError) as exc:
        await sites_service.accept_site_transfer(
            workspace_id=dest,
            user_id=outsider,
            site_id=str(site.id),
        )
    assert exc.value.status_code == 403


async def test_a_third_workspace_cannot_accept_an_offer_addressed_elsewhere():
    """Not-found rather than forbidden: telling a stranger the site exists is the
    leak, and it is one a 403 would commit."""
    _sender, _recipient, _source, _dest, site = await _offered()
    third = await _workspace("third", "u-third")
    interloper = await _user([third], "third@party.test")
    with pytest.raises(CloudError) as exc:
        await sites_service.accept_site_transfer(
            workspace_id=third,
            user_id=interloper,
            site_id=str(site.id),
        )
    assert exc.value.status_code == 404


# ---------------------------------------------------------------------------
# What a completed transfer actually does
# ---------------------------------------------------------------------------


async def test_accept_rekeys_the_site_and_the_pocket_together():
    """The pair moves as one. A window with the pocket and the site in different
    tenants re-opens the orphan wave 2 closed: the guard that stops a pocket delete
    stranding a live site finds the site through the POCKET's workspace."""
    _sender, recipient, _source, dest, site = await _offered()
    await sites_service.accept_site_transfer(
        workspace_id=dest, user_id=recipient, site_id=str(site.id)
    )
    fresh = await Site.get(site.id)
    pocket = await Pocket.get(fresh.pocket_id)
    assert fresh.workspace == dest
    assert pocket.workspace == dest
    assert fresh.owner == recipient
    assert pocket.owner == recipient


async def test_the_old_tenants_people_lose_access_to_the_pocket():
    """``owner`` and ``shared_with`` name users of the workspace the pocket just
    left, and a pocket read is an ``$or`` over exactly those two plus visibility."""
    sender, recipient, source, dest, site = await _offered()
    pocket = await Pocket.get(site.pocket_id)
    pocket.shared_with = ["colleague-at-source"]
    await pocket.save()

    await sites_service.accept_site_transfer(
        workspace_id=dest, user_id=recipient, site_id=str(site.id)
    )
    moved = await Pocket.get(site.pocket_id)
    assert moved.owner != sender
    assert moved.shared_with == []


async def test_the_site_keeps_its_id_its_script_and_its_url():
    """THE INVARIANT THE DESIGN IS BUILT AROUND — asserted end to end.

    ``_id`` is the Worker's script name and the subdomain the site serves at.
    """
    _sender, recipient, _source, dest, site = await _offered()
    before = (site.id, site.script_name, site.url)
    await sites_service.accept_site_transfer(
        workspace_id=dest, user_id=recipient, site_id=str(site.id)
    )
    fresh = await Site.get(site.id)
    assert (fresh.id, fresh.script_name, fresh.url) == before


async def test_a_republish_after_transfer_does_not_fork_the_site():
    """THE BUG THIS WAVE EXISTS TO NOT SHIP.

    ``_live_object_id`` derives the id from (workspace, pocket) — so after a
    transfer the derivation no longer matches the row, and an unguarded publish
    would insert a SECOND Site doc, upload a SECOND Worker, and serve it at a
    SECOND address while every custom domain still resolved to the first.

    Mutation that must break this: make ``_resolve_live_site_oid`` return
    ``_live_object_id(workspace_id, pocket_id)`` unconditionally.
    """
    _sender, recipient, source, dest, site = await _offered()
    await sites_service.accept_site_transfer(
        workspace_id=dest, user_id=recipient, site_id=str(site.id)
    )
    fresh = await Site.get(site.id)

    derived_in_destination = sites_service._live_object_id(dest, fresh.pocket_id)
    assert derived_in_destination != site.id, "fixture is not exercising the fork"

    resolved = await sites_service._resolve_live_site_oid(dest, fresh.pocket_id)
    assert resolved == site.id

    # And the derivation is still what an untransferred pocket gets, so the PERF-1
    # dedupe invariant is untouched for every other site.
    other = await _site(source, "u-someone", name="Other")
    assert await sites_service._resolve_live_site_oid(
        source, other.pocket_id
    ) == sites_service._live_object_id(source, other.pocket_id)


async def test_the_source_public_asset_prefix_is_recorded():
    """The images do not move — their key is inside an immutable URL in the live
    HTML — so the prefix is recorded or the delete cascade can never reclaim them."""
    _sender, recipient, source, dest, site = await _offered()
    await sites_service.accept_site_transfer(
        workspace_id=dest, user_id=recipient, site_id=str(site.id)
    )
    fresh = await Site.get(site.id)
    # Read from the real helper rather than a literal: the prefix constant is the
    # store's business, and a copy here would pass while pointing somewhere else.
    assert fresh.asset_source_prefixes == [public_assets.prefix_for(source, fresh.pocket_id)]


async def test_accepting_twice_is_refused_rather_than_repeated():
    _sender, recipient, _source, dest, site = await _offered()
    await sites_service.accept_site_transfer(
        workspace_id=dest, user_id=recipient, site_id=str(site.id)
    )
    with pytest.raises(CloudError):
        await sites_service.accept_site_transfer(
            workspace_id=dest, user_id=recipient, site_id=str(site.id)
        )


async def test_cancel_withdraws_the_offer():
    sender, recipient, _source, dest, site = await _offered()
    await sites_service.cancel_site_transfer(
        workspace_id=_source_of(site), user_id=sender, site_id=str(site.id)
    )
    assert await sites_service.list_incoming_site_transfers(workspace_id=dest) == []
    with pytest.raises(CloudError):
        await sites_service.accept_site_transfer(
            workspace_id=dest, user_id=recipient, site_id=str(site.id)
        )


def _source_of(site: Site) -> str:
    return site.workspace


async def test_the_sites_leads_move_with_it():
    """THE STEP THAT IS EASIEST TO BELIEVE WITHOUT CHECKING.

    ``move_records`` reads its result off a Beanie ``UpdateResult`` and reports a
    boolean. If that attribute were absent or None the step would report "there was
    nothing here", the ledger would record a skip, and the leads would stay in a
    workspace that can no longer see the site they belong to — while every ordering
    test still passed, because they assert the step was CALLED.

    So this asserts the rows, not the call.

    Mutation that must break this: have ``_TransferDeps.move_records`` return False
    without running the update.
    """
    sender, recipient, source, dest, site = await _offered()
    for i in range(2):
        await Lead(
            workspace=source,
            site_id=str(site.id),
            form_type="AppointmentRequest",
            properties={"email": f"visitor{i}@example.test"},
            source=LeadSource(form_type="AppointmentRequest", site_id=str(site.id)),
        ).insert()
    # A lead belonging to a DIFFERENT site in the same workspace, which must stay.
    other = await _site(source, sender, name="Other")
    await Lead(
        workspace=source,
        site_id=str(other.id),
        form_type="AppointmentRequest",
        properties={"email": "not-this-one@example.test"},
        source=LeadSource(form_type="AppointmentRequest", site_id=str(other.id)),
    ).insert()

    await sites_service.accept_site_transfer(
        workspace_id=dest, user_id=recipient, site_id=str(site.id)
    )

    moved = await Lead.find({"workspace": dest}).to_list()
    assert {lead.site_id for lead in moved} == {str(site.id)}
    assert len(moved) == 2

    stayed = await Lead.find({"workspace": source}).to_list()
    assert [lead.site_id for lead in stayed] == [str(other.id)]

    fresh = await Site.get(site.id)
    assert fresh.transfer_ledger["records"] == "done"
