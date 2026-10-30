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


# --------------------------------------------------------------------------- #
# Money units: every new invoice is stamped "iso4217"; a legacy client's amount
# (no X-Paw-Money-Units header, major x 100) is converted on the way in.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("minor_units", "currency", "sent", "stored"),
    [
        (True, "JPY", 1500, 1500),  # header: stored as sent
        (False, "JPY", 150000, 1500),  # no header: legacy major x 100, converted
        (True, "USD", 350, 350),
        (False, "USD", 350, 350),  # two decimals: the same either way
        (False, "KWD", 125, 1250),
    ],
)
async def test_invoice_create_stamps_and_converts_only_a_legacy_amount(
    beanie_test_db, minor_units, currency, sent, stored
):
    from collections import Counter

    from tests.test_invoice_minor_units_migration import _load

    site_id = await _make_site(pocket_id="pk-money-units")
    rec = await sites_service.record_site_invoice(
        workspace_id="ws1",
        site_id=site_id,
        body=SiteInvoiceCreate(amount_cents=sent, currency=currency),
        minor_units=minor_units,
    )
    [inv] = rec.invoices
    assert (inv.amount_cents, inv.amount_unit) == (stored, "iso4217")

    # The migration leaves the stamped row alone, however often it runs.
    from pocketpaw_ee.cloud.models.site import Site

    site = await Site.get(site_id)
    raw = await Site.get_pymongo_collection().find_one({"_id": site.id})
    assert _load().convert_invoices(raw["client_invoices"], Counter()) is None


async def test_a_legacy_row_stays_unstamped_when_a_new_invoice_lands(beanie_test_db):
    """Recording an invoice rewrites the list; an old row must keep amount_unit ""
    so the migration still converts it (and only it)."""
    from pocketpaw_ee.cloud.models.site import Site

    site_id = await _make_site(pocket_id="pk-money-legacy")
    site = await Site.get(site_id)
    legacy = {
        "id": "inv_old",
        "issued_at": site.createdAt,
        "amount_cents": 150000,
        "currency": "JPY",
        "paid": True,
        "note": "",
    }
    await Site.get_pymongo_collection().update_one(
        {"_id": site.id}, {"$set": {"client_invoices": [legacy]}}
    )
    rec = await sites_service.record_site_invoice(
        workspace_id="ws1",
        site_id=site_id,
        body=SiteInvoiceCreate(amount_cents=1500, currency="JPY"),
        minor_units=True,
    )
    assert [(i.amount_cents, i.amount_unit) for i in rec.invoices] == [
        (1500, "iso4217"),
        (150000, ""),
    ]


async def test_invoice_route_reads_the_money_units_header(beanie_test_db):
    from datetime import UTC, datetime

    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient
    from pocketpaw_ee.cloud._core.context import RequestContext, ScopeKind, request_context
    from pocketpaw_ee.cloud._core.deps import current_workspace_id
    from pocketpaw_ee.cloud._core.http import add_error_handler
    from pocketpaw_ee.cloud.auth import current_active_user
    from pocketpaw_ee.cloud.license import require_license
    from pocketpaw_ee.sites.router import router as sites_router

    from tests.ee.sites.test_delete_endpoint import _FakeUser

    site_id = await _make_site(pocket_id="pk-money-route")
    user = _FakeUser("ws1", "u1")
    app = FastAPI()
    add_error_handler(app)
    app.include_router(sites_router, prefix="/api/v1")

    async def _ctx() -> RequestContext:
        return RequestContext(
            user_id="u1",
            workspace_id="ws1",
            request_id="test",
            scope=ScopeKind.WORKSPACE,
            started_at=datetime.now(UTC),
        )

    app.dependency_overrides[request_context] = _ctx
    app.dependency_overrides[current_active_user] = lambda: user
    app.dependency_overrides[current_workspace_id] = lambda: "ws1"
    app.dependency_overrides[require_license] = lambda: None

    url = f"/api/v1/sites/{site_id}/invoices"
    body = {"amount_cents": 150000, "currency": "JPY"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        legacy = await c.post(url, json=body)
        assert legacy.status_code == 200, legacy.text
        current = await c.post(url, json=body, headers={"X-Paw-Money-Units": "iso4217"})
        assert current.status_code == 200, current.text
    amounts = [i["amount_cents"] for i in current.json()["invoices"]]
    assert amounts == [150000, 1500]  # newest first: as sent, then the converted legacy one
