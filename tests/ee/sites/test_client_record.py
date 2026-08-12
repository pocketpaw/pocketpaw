# tests/ee/sites/test_client_record.py
# Created: 2026-08-12 (sites Settings consolidation — the client record gets a backend).
#
# WHAT THIS COVERS AND WHY IT DID NOT EXIST BEFORE. The builder's Settings surface
# has shipped a Client panel and a "Record payment" button since June, both backed
# by COMPONENT STATE with a comment saying persistence was a later task. So the
# panel exercised its own onChange contract and threw the value away: reload the
# page, or switch to another site and back, and the client's name was gone. There
# was nothing to test because there was nothing persisted.
#
# The four behaviours pinned here are the ones that make the difference between
# "the form submits" and "the record is kept":
#   1. A site with no client recorded returns a BLANK record, not a 404 — "no
#      client yet" is the ordinary starting state, and reserving 404 for "no such
#      site" is what keeps the two failures distinguishable at the edge.
#   2. PATCH is THREE-WAY. An absent field is left alone; an explicit "" clears.
#      This is the one that breaks silently: an autosaving form that sends only
#      the field it touched would, under a two-way patch, blank everything else.
#   3. A recorded receipt persists, is newest-first, and money survives the round
#      trip as an integer (minor units) rather than a float.
#   4. Tenant scoping — another workspace's site is a 404 on read AND on write.
from __future__ import annotations

import pytest
from pocketpaw_ee.cloud._core.errors import NotFound
from pocketpaw_ee.sites import service as sites_service
from pocketpaw_ee.sites.dto import SiteClientUpdate, SiteInvoiceCreate

pytestmark = pytest.mark.asyncio


class _FakeGenerator:
    async def build(self, **kw):
        from pocketpaw_ee.sites.generator_client import BuildResult

        return BuildResult(project_dir="/tmp/site", ripple_version="0.2.0")


class _FakeCF:
    async def put_worker(self, *, script_name, bundle, bindings=None):
        return True


async def _make_site(workspace_id: str = "ws1", pocket_id: str = "pk-client") -> str:
    """Publish a throwaway site and return its id. The client record hangs off the
    Site doc, so a site has to exist before there is anything to record against."""
    site = await sites_service.publish(
        workspace_id=workspace_id,
        user_id="u1",
        pocket_id=pocket_id,
        ripple_spec={"type": "container"},
        theme={"primary": "#0A84FF"},
        name="Bright Smile",
        _generator=_FakeGenerator(),
        _cloudflare=_FakeCF(),
        # The fake generator returns a project dir that does not exist on disk, so
        # the real bundle reader would fail looking for _worker.js. Nothing here
        # cares what got deployed — only that a Site doc exists to hang a client
        # record on.
        _bundle_reader=lambda _d: b"x",
    )
    return str(site.id)


async def test_unrecorded_client_reads_as_blank_not_404(beanie_test_db):
    """A site nobody has recorded a client for returns an empty record. The
    Settings form renders the same fields whether or not a client exists, so a 404
    here would make the ordinary first visit look like an error."""
    site_id = await _make_site()

    rec = await sites_service.get_site_client(workspace_id="ws1", site_id=site_id)

    assert rec.site_id == site_id
    assert rec.name == ""
    assert rec.contact == ""
    assert rec.notes == ""
    assert rec.invoices == []


async def test_patch_persists_and_survives_a_reread(beanie_test_db):
    """The whole point of the endpoint: what the owner types is still there on the
    next read. Under the old component-state panel this assertion was unwritable."""
    site_id = await _make_site(pocket_id="pk-persist")

    await sites_service.update_site_client(
        workspace_id="ws1",
        site_id=site_id,
        body=SiteClientUpdate(name="Ravi Menon", contact="ravi@brightsmile.example"),
    )

    rec = await sites_service.get_site_client(workspace_id="ws1", site_id=site_id)
    assert rec.name == "Ravi Menon"
    assert rec.contact == "ravi@brightsmile.example"


async def test_patch_is_three_way_absent_keeps_empty_clears(beanie_test_db):
    """MUTATION THAT BREAKS THIS: dropping the ``model_fields_set`` check in
    ``update_site_client`` and writing every field from the body. A form that
    autosaves only the field the user touched then blanks the other two on every
    keystroke — the failure is invisible in a manual click-through, because a
    human editing a form usually has all three fields filled in already."""
    site_id = await _make_site(pocket_id="pk-threeway")

    await sites_service.update_site_client(
        workspace_id="ws1",
        site_id=site_id,
        body=SiteClientUpdate(name="Ravi Menon", contact="ravi@x.example", notes="Prefers email"),
    )

    # ABSENT ≠ empty: patching only `notes` must leave name and contact alone.
    rec = await sites_service.update_site_client(
        workspace_id="ws1", site_id=site_id, body=SiteClientUpdate(notes="Renewal in March")
    )
    assert rec.name == "Ravi Menon"
    assert rec.contact == "ravi@x.example"
    assert rec.notes == "Renewal in March"

    # An EXPLICIT empty string is how the form deletes a value.
    rec = await sites_service.update_site_client(
        workspace_id="ws1", site_id=site_id, body=SiteClientUpdate(contact="")
    )
    assert rec.contact == ""
    assert rec.name == "Ravi Menon"


async def test_empty_patch_is_a_noop_not_an_error(beanie_test_db):
    """A form that saves on blur sends an empty patch when nothing changed.
    Failing it would surface to the owner as a spurious error toast."""
    site_id = await _make_site(pocket_id="pk-noop")
    await sites_service.update_site_client(
        workspace_id="ws1", site_id=site_id, body=SiteClientUpdate(name="Ravi Menon")
    )

    rec = await sites_service.update_site_client(
        workspace_id="ws1", site_id=site_id, body=SiteClientUpdate()
    )
    assert rec.name == "Ravi Menon"


async def test_recorded_invoice_persists_newest_first_with_integer_money(beanie_test_db):
    """Receipts accumulate newest-first and money crosses the wire as an integer.

    MUTATION THAT BREAKS THIS: appending instead of prepending in
    ``record_site_invoice`` (``[*site.client_invoices, entry]``). The list is what
    the owner scans for "did this month's payment land", so the newest receipt
    being at the bottom of a years-long list is the difference between a glance and
    a scroll — and the bug is invisible until a site has more than one receipt.

    A SECOND mutation this catches: dropping ``.upper()`` from the currency
    validator, which lets the same currency render as both "usd" and "USD"."""
    site_id = await _make_site(pocket_id="pk-invoice")

    first = await sites_service.record_site_invoice(
        workspace_id="ws1",
        site_id=site_id,
        body=SiteInvoiceCreate(amount_cents=25_000, currency="usd", note="Deposit"),
    )
    assert len(first.invoices) == 1
    assert first.invoices[0].amount_cents == 25_000
    assert first.invoices[0].currency == "USD"  # normalized, so the list can't show usd AND USD
    assert first.invoices[0].note == "Deposit"
    assert first.invoices[0].paid is True

    second = await sites_service.record_site_invoice(
        workspace_id="ws1", site_id=site_id, body=SiteInvoiceCreate(amount_cents=50_000)
    )
    assert len(second.invoices) == 2
    assert second.invoices[0].amount_cents == 50_000  # newest first

    reread = await sites_service.get_site_client(workspace_id="ws1", site_id=site_id)
    assert [i.amount_cents for i in reread.invoices] == [50_000, 25_000]


async def test_invoice_rejects_negative_and_malformed_currency():
    """Bounded at the edge so a bad value is a 422 the form can show, never a
    record that quietly reverses the owner's running total."""
    with pytest.raises(ValueError):
        SiteInvoiceCreate(amount_cents=-1)
    with pytest.raises(ValueError):
        SiteInvoiceCreate(currency="dollars")


async def test_client_record_is_tenant_scoped(beanie_test_db):
    """Another workspace's site is invisible for BOTH read and write. The write
    half matters independently: a leak there does not just expose a record, it
    lets one tenant overwrite another's."""
    site_id = await _make_site(workspace_id="ws-a", pocket_id="pk-tenant")

    with pytest.raises(NotFound):
        await sites_service.get_site_client(workspace_id="ws-b", site_id=site_id)

    with pytest.raises(NotFound):
        await sites_service.update_site_client(
            workspace_id="ws-b", site_id=site_id, body=SiteClientUpdate(name="Intruder")
        )

    with pytest.raises(NotFound):
        await sites_service.record_site_invoice(
            workspace_id="ws-b", site_id=site_id, body=SiteInvoiceCreate(amount_cents=1)
        )
