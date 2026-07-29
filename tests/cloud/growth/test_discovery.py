# tests/cloud/growth/test_discovery.py — tests for the /growth ICP discovery
# engine: the email-provenance rule, the ICP CRUD service seams, the discovery
# run against a FAKE ``ResearchFn`` (code under test never calls a real LLM —
# the belt/headless pattern), the preview path, and the bounds.
#
# Created 2026-07-29 (feat/growth-discovery): the provenance rule first,
# because it is the constraint the rest of the slice exists to protect. An LLM
# cannot run Clay's verification waterfall, so a "found" address that nobody
# read off a page is a guess — and a guessed address bounces, burns the sending
# domain's reputation, and poisons the list. These tests pin that the rule is
# STRUCTURAL (a filter the data must pass) rather than a request in a prompt.

from __future__ import annotations

from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from pocketpaw_ee.cloud.growth.domain import (
    RECORDABLE_EMAIL_CONFIDENCE,
    EmailEvidence,
    recordable_emails,
)

from tests.cloud.growth.test_router import _build_app

ICPS_URL = "/api/v1/growth/icps"


@pytest_asyncio.fixture
async def w1(mongo_db: Any) -> AsyncClient:
    transport = ASGITransport(app=_build_app(workspace_id="w1"))
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        yield client


@pytest_asyncio.fixture
async def w2(mongo_db: Any) -> AsyncClient:
    transport = ASGITransport(app=_build_app(workspace_id="w2"))
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        yield client


def _icp_payload(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "name": "Small dental practices",
        "criteria": "Dental practices with 2-6 chairs that still book by phone.",
    }
    base.update(overrides)
    return base


async def _create_icp(client: AsyncClient, **overrides: Any) -> dict[str, Any]:
    resp = await client.post(ICPS_URL, json=_icp_payload(**overrides))
    assert resp.status_code == 200, resp.text
    return resp.json()


# ---------------------------------------------------------------------------
# The provenance rule at the domain level
# ---------------------------------------------------------------------------


class TestRecordableEmails:
    """``recordable_emails`` is THE door between "the research mentioned an
    address" and "we stored one". Nothing else in the discovery path may open
    it, so every way of failing to prove where an address came from is pinned
    here."""

    def test_an_observed_address_with_its_page_is_recorded(self):
        found = recordable_emails(
            [
                EmailEvidence(
                    address="hello@acme-dental.com",
                    confidence="observed",
                    seen_at_url="https://acme-dental.com/contact",
                )
            ]
        )
        assert found == ("hello@acme-dental.com",)

    @pytest.mark.parametrize("confidence", ["guessed", "claimed"])
    def test_an_unobserved_address_produces_nothing(self, confidence: str):
        """The core case. A pattern-built ``first@company.com`` and an address
        an aggregator merely asserted are both refused, even when they carry a
        URL — the URL is where the CLAIM was made, not where the address was
        read."""
        found = recordable_emails(
            [
                EmailEvidence(
                    address="sam@acme-dental.com",
                    confidence=confidence,
                    seen_at_url="https://directory.example/acme",
                )
            ]
        )
        assert found == ()

    def test_observed_without_a_url_produces_nothing(self):
        """An ``observed`` claim with no page is unfalsifiable — which is
        exactly the shape a model produces when it wants to say yes."""
        assert recordable_emails([EmailEvidence("sam@acme.com", confidence="observed")]) == ()

    def test_the_default_confidence_is_the_untrusted_one(self):
        """Evidence that never says how it was obtained fails closed. A
        research implementation that forgets the field gets no address, not a
        free promotion to observed."""
        assert EmailEvidence(address="sam@acme.com").confidence not in RECORDABLE_EMAIL_CONFIDENCE
        assert recordable_emails([EmailEvidence(address="sam@acme.com")]) == ()

    def test_a_blank_address_is_dropped_even_when_observed(self):
        assert recordable_emails([EmailEvidence("   ", "observed", "https://acme.com")]) == ()

    def test_the_same_address_on_two_pages_is_one_address(self):
        found = recordable_emails(
            [
                EmailEvidence("Hello@Acme.com", "observed", "https://acme.com/contact"),
                EmailEvidence("hello@acme.com", "observed", "https://acme.com/about"),
                EmailEvidence("sales@acme.com", "observed", "https://acme.com/about"),
            ]
        )
        assert found == ("hello@acme.com", "sales@acme.com")

    def test_a_good_address_survives_a_batch_of_bad_ones(self):
        """Mixed evidence keeps what it can prove instead of failing the whole
        prospect — the row is still worth having without the guesses."""
        found = recordable_emails(
            [
                EmailEvidence("guess@acme.com", "guessed"),
                EmailEvidence("hello@acme.com", "observed", "https://acme.com/contact"),
                EmailEvidence("claimed@acme.com", "claimed", "https://directory.example/acme"),
            ]
        )
        assert found == ("hello@acme.com",)


# ---------------------------------------------------------------------------
# ICP CRUD
# ---------------------------------------------------------------------------


class TestIcpCrud:
    """The ICP is a hand-written artifact: create it, read it, tune it, retire
    it. The interesting assertions are the DEFAULTS (a new ICP does not run)
    and the tenant boundary (identical 404s, never a cross-tenant read)."""

    @pytest.mark.asyncio
    async def test_a_new_icp_does_not_run(self, w1):
        """The default that matters. Writing down who you want is free;
        going looking for them on a schedule is a recurring spend, so the
        cadence has to be switched on deliberately."""
        icp = await _create_icp(w1)
        assert icp["cadence"] == "off"
        assert icp["status"] == "active"
        assert icp["max_per_run"] == 10
        assert icp["workspace_id"] == "w1"
        assert icp["last_run_at"] is None

    @pytest.mark.asyncio
    async def test_criteria_is_required_and_cannot_be_blank(self, w1):
        assert (await w1.post(ICPS_URL, json={"name": "n"})).status_code == 422
        assert (await w1.post(ICPS_URL, json={"name": "n", "criteria": "  "})).status_code == 422

    @pytest.mark.asyncio
    async def test_max_per_run_is_capped_at_the_boundary(self, w1):
        """A run is an LLM research pass, not a database scan — past the cap
        the right tool is a second ICP with narrower criteria."""
        resp = await w1.post(ICPS_URL, json=_icp_payload(max_per_run=500))
        assert resp.status_code == 422
        assert (await w1.post(ICPS_URL, json=_icp_payload(max_per_run=0))).status_code == 422

    @pytest.mark.asyncio
    async def test_get_and_list_return_the_created_icp(self, w1):
        icp = await _create_icp(w1)
        assert (await w1.get(f"{ICPS_URL}/{icp['id']}")).json()["name"] == icp["name"]
        listed = (await w1.get(ICPS_URL)).json()
        assert [row["id"] for row in listed] == [icp["id"]]

    @pytest.mark.asyncio
    async def test_two_icps_may_share_a_name(self, w1):
        """No uniqueness constraint: an agency runs "dental clinics" for two
        clients and holds two profiles, told apart by their project."""
        first = await _create_icp(w1)
        second = await _create_icp(w1)
        assert first["id"] != second["id"]
        assert len((await w1.get(ICPS_URL)).json()) == 2

    @pytest.mark.asyncio
    async def test_patch_switches_the_cadence_on(self, w1):
        icp = await _create_icp(w1)
        resp = await w1.patch(f"{ICPS_URL}/{icp['id']}", json={"cadence": "weekly"})
        assert resp.status_code == 200, resp.text
        assert resp.json()["cadence"] == "weekly"
        # Untouched fields survive a partial update.
        assert resp.json()["criteria"] == icp["criteria"]

    @pytest.mark.asyncio
    async def test_patch_rejects_an_unknown_cadence(self, w1):
        icp = await _create_icp(w1)
        resp = await w1.patch(f"{ICPS_URL}/{icp['id']}", json={"cadence": "hourly"})
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_delete_removes_it_from_the_list(self, w1):
        icp = await _create_icp(w1)
        assert (await w1.delete(f"{ICPS_URL}/{icp['id']}")).status_code == 204
        assert (await w1.get(f"{ICPS_URL}/{icp['id']}")).status_code == 404
        assert (await w1.get(ICPS_URL)).json() == []

    @pytest.mark.asyncio
    async def test_list_filters_by_status(self, w1):
        active = await _create_icp(w1)
        paused = await _create_icp(w1, status="paused")
        ids = [r["id"] for r in (await w1.get(ICPS_URL, params={"status": "paused"})).json()]
        assert ids == [paused["id"]]
        ids = [r["id"] for r in (await w1.get(ICPS_URL, params={"status": "active"})).json()]
        assert ids == [active["id"]]

    @pytest.mark.parametrize("method", ["get", "patch", "delete"])
    @pytest.mark.asyncio
    async def test_another_tenants_icp_is_a_404(self, w1, w2, method: str):
        """Identical 404s for a foreign row and a row that never existed —
        existence must not leak across tenants."""
        icp = await _create_icp(w1)
        url = f"{ICPS_URL}/{icp['id']}"
        resp = (
            await w2.patch(url, json={"name": "stolen"})
            if method == "patch"
            else await getattr(w2, method)(url)
        )
        assert resp.status_code == 404, resp.text

    @pytest.mark.asyncio
    async def test_a_foreign_icp_never_appears_in_the_list(self, w1, w2):
        await _create_icp(w1)
        assert (await w2.get(ICPS_URL)).json() == []

    @pytest.mark.asyncio
    async def test_a_malformed_id_is_a_404_not_a_500(self, w1):
        assert (await w1.get(f"{ICPS_URL}/not-an-object-id")).status_code == 404
