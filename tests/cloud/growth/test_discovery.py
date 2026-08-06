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
from pocketpaw_ee.cloud._core.errors import NotFound
from pocketpaw_ee.cloud.growth.discovery import (
    DiscoveredCompany,
    ResearchRequest,
    ResearchResult,
    resolve_research_fn,
    run_discovery,
    set_production_research_fn,
)
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


# ---------------------------------------------------------------------------
# The discovery run — against a FAKE ResearchFn
# ---------------------------------------------------------------------------


class FakeResearch:
    """A deterministic stand-in for the agent research loop.

    The whole point of the ``ResearchFn`` seam (copied from belt/headless):
    code under test never calls a real LLM, and a test can hand the run
    EXACTLY the shape a misbehaving model would produce — a company with a
    guessed address, a result with no domain, two hundred rows against a limit
    of ten — and assert what the engine does with it.
    """

    def __init__(self, *companies: DiscoveredCompany, notes: str = "") -> None:
        self.result = ResearchResult(companies=tuple(companies), notes=notes)
        self.calls: list[ResearchRequest] = []

    async def __call__(self, request: ResearchRequest) -> ResearchResult:
        self.calls.append(request)
        return self.result


class ExplodingResearch:
    """A research loop that fails, which is what a real one does sometimes."""

    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, request: ResearchRequest) -> ResearchResult:
        self.calls += 1
        raise RuntimeError("the search provider returned 503")


def _company(domain: str, **overrides: Any) -> DiscoveredCompany:
    base: dict[str, Any] = {
        "name": "",
        "company": f"Co {domain}",
        "research_brief": "Three chairs, books by phone.",
        "source_urls": (f"https://{domain}/about",),
    }
    base.update(overrides)
    return DiscoveredCompany(domain=domain, **base)


async def _prospects(client: AsyncClient) -> list[dict[str, Any]]:
    resp = await client.get("/api/v1/growth/prospects")
    assert resp.status_code == 200, resp.text
    return resp.json()["items"]


class TestDiscoveryRun:
    """Files what it found, and nothing else. The assertions worth reading are
    the negative ones: no drafts, no invented emails, no reset statuses."""

    @pytest.mark.asyncio
    async def test_a_found_company_lands_as_a_new_prospect(self, w1):
        icp = await _create_icp(w1)
        research = FakeResearch(_company("acme-dental.com"))

        outcome = await run_discovery("w1", icp["id"], research)

        assert outcome.filed == 1
        (row,) = await _prospects(w1)
        assert row["domain"] == "acme-dental.com"
        assert row["source"] == "discovery"
        assert row["status"] == "new"
        assert row["tier"] == "unqualified"
        assert row["icp_id"] == icp["id"]
        assert row["source_urls"] == ["https://acme-dental.com/about"]

    @pytest.mark.asyncio
    async def test_the_icp_criteria_reach_the_research_loop(self, w1):
        icp = await _create_icp(w1, geography="Bengaluru", exclusions="No chains.")
        research = FakeResearch()

        await run_discovery("w1", icp["id"], research)

        (request,) = research.calls
        assert request.criteria == icp["criteria"]
        assert request.geography == "Bengaluru"
        assert request.exclusions == "No chains."
        assert request.max_results == icp["max_per_run"]
        assert request.workspace_id == "w1"

    @pytest.mark.asyncio
    async def test_an_unobserved_email_never_reaches_the_prospect(self, w1):
        """THE constraint, end to end. The research claims an address it built
        from a pattern; the filed row carries no email at all. A prospect with
        an empty ``emails`` is a good prospect — a human or a real verification
        provider picks it up from there. A guessed one bounces and burns the
        sending domain."""
        icp = await _create_icp(w1)
        research = FakeResearch(
            _company(
                "acme-dental.com",
                emails=(
                    EmailEvidence("sam@acme-dental.com", "guessed"),
                    EmailEvidence(
                        "info@acme-dental.com",
                        "claimed",
                        "https://some-directory.example/acme",
                    ),
                    # An ``observed`` claim with nowhere to check it.
                    EmailEvidence("hello@acme-dental.com", "observed"),
                ),
            )
        )

        outcome = await run_discovery("w1", icp["id"], research)

        assert outcome.filed == 1
        (row,) = await _prospects(w1)
        assert row["emails"] == []

    @pytest.mark.asyncio
    async def test_an_observed_email_does_reach_the_prospect(self, w1):
        """The other half — the rule refuses guesses, it does not refuse
        emails. An address read off the company's own contact page is exactly
        what the engine is for."""
        icp = await _create_icp(w1)
        research = FakeResearch(
            _company(
                "acme-dental.com",
                emails=(
                    EmailEvidence("guess@acme-dental.com", "guessed"),
                    EmailEvidence(
                        "hello@acme-dental.com",
                        "observed",
                        "https://acme-dental.com/contact",
                    ),
                ),
            )
        )

        await run_discovery("w1", icp["id"], research)

        (row,) = await _prospects(w1)
        assert row["emails"] == ["hello@acme-dental.com"]

    @pytest.mark.asyncio
    async def test_it_drafts_nothing_and_sends_nothing(self, w1):
        """Discovery adds rows to a list; it does not start conversations.
        Everything downstream still needs the human gate it always needed."""
        icp = await _create_icp(w1)

        await run_discovery("w1", icp["id"], FakeResearch(_company("acme-dental.com")))

        assert (await w1.get("/api/v1/growth/drafts")).json() == []

    @pytest.mark.asyncio
    async def test_a_domain_the_workspace_already_has_is_skipped(self, w1):
        """A daily cron that re-upserted a live prospect would reset its status
        and lose the follow-up thread. Discovery only ever inserts."""
        await w1.post(
            "/api/v1/growth/prospects",
            json={
                "name": "Sam",
                "company": "Acme Dental",
                "domain": "acme-dental.com",
                "source": "manual",
                "status": "in_sequence",
            },
        )
        icp = await _create_icp(w1)

        outcome = await run_discovery("w1", icp["id"], FakeResearch(_company("acme-dental.com")))

        assert (outcome.filed, outcome.skipped_existing) == (0, 1)
        (row,) = await _prospects(w1)
        assert row["status"] == "in_sequence"
        assert row["source"] == "manual"
        assert row["icp_id"] is None

    @pytest.mark.asyncio
    async def test_a_known_domain_is_recognised_through_its_url_form(self, w1):
        """The research reports what it read off the page; the existence check
        normalises the same way the dedupe key does."""
        await w1.post(
            "/api/v1/growth/prospects",
            json={"domain": "acme-dental.com", "source": "manual"},
        )
        icp = await _create_icp(w1)

        outcome = await run_discovery(
            "w1", icp["id"], FakeResearch(_company("https://www.Acme-Dental.com/pricing"))
        )

        assert outcome.skipped_existing == 1
        assert len(await _prospects(w1)) == 1

    @pytest.mark.asyncio
    async def test_max_per_run_truncates_the_result(self, w1):
        """A research loop that ignores the limit does not get to file 200
        rows: the number a human agreed to review is the number that appears."""
        icp = await _create_icp(w1, max_per_run=3)
        research = FakeResearch(*[_company(f"co-{i:02d}.com") for i in range(20)])

        outcome = await run_discovery("w1", icp["id"], research)

        assert (outcome.filed, outcome.considered) == (3, 3)
        assert len(await _prospects(w1)) == 3

    @pytest.mark.asyncio
    async def test_skipped_rows_do_not_free_budget_for_extra_ones(self, w1):
        """The cap is on what the run CONSIDERS, so a result full of known
        companies produces a short run rather than digging deeper."""
        await w1.post(
            "/api/v1/growth/prospects",
            json={"domain": "co-00.com", "source": "manual"},
        )
        icp = await _create_icp(w1, max_per_run=2)
        research = FakeResearch(*[_company(f"co-{i:02d}.com") for i in range(5)])

        outcome = await run_discovery("w1", icp["id"], research)

        assert (outcome.considered, outcome.filed, outcome.skipped_existing) == (2, 1, 1)

    @pytest.mark.asyncio
    async def test_a_result_without_a_domain_is_dropped(self, w1):
        """A company nobody can look up is not a lead — there is nothing to
        dedupe on and nothing for a human to open."""
        icp = await _create_icp(w1)
        research = FakeResearch(_company("   "), _company("acme-dental.com"))

        outcome = await run_discovery("w1", icp["id"], research)

        assert (outcome.filed, outcome.skipped_invalid) == (1, 1)

    @pytest.mark.asyncio
    async def test_a_research_crash_files_nothing_and_does_not_raise(self, w1):
        """One bad ICP must not take down a cron pass that has other
        workspaces left to serve."""
        icp = await _create_icp(w1)
        research = ExplodingResearch()

        outcome = await run_discovery("w1", icp["id"], research)

        assert outcome.filed == 0
        assert "research failed" in outcome.error
        assert await _prospects(w1) == []

    @pytest.mark.asyncio
    async def test_a_paused_icp_does_not_run(self, w1):
        icp = await _create_icp(w1, status="paused")
        research = FakeResearch(_company("acme-dental.com"))

        outcome = await run_discovery("w1", icp["id"], research)

        assert outcome.error == "icp is paused"
        assert research.calls == []
        assert await _prospects(w1) == []

    @pytest.mark.asyncio
    async def test_rows_land_in_the_icps_workspace_only(self, w1, w2):
        icp = await _create_icp(w1)

        await run_discovery("w1", icp["id"], FakeResearch(_company("acme-dental.com")))

        assert len(await _prospects(w1)) == 1
        assert await _prospects(w2) == []

    @pytest.mark.asyncio
    async def test_another_tenants_icp_id_is_a_404(self, w1, w2):
        icp = await _create_icp(w1)
        with pytest.raises(NotFound):
            await run_discovery("w2", icp["id"], FakeResearch(_company("acme.com")))


class TestProductionResearchSeam:
    """Until a real loop is wired, the engine is visibly idle rather than a
    source of half-researched rows."""

    def test_no_loop_is_wired_by_default(self):
        assert resolve_research_fn() is None

    def test_wiring_and_clearing_round_trip(self):
        fake = FakeResearch()
        set_production_research_fn(fake)
        try:
            assert resolve_research_fn() is fake
        finally:
            set_production_research_fn(None)
        assert resolve_research_fn() is None


# ---------------------------------------------------------------------------
# Preview — the same research, none of the writes
# ---------------------------------------------------------------------------


@pytest.fixture
def wired():
    """Wire a research loop for the duration of one test and clear it after.

    The route resolves the production seam, so an HTTP-level preview test has
    to install one. Cleared in a finally so a failure can't leak a fake into
    the next test."""
    installed: list[Any] = []

    def _install(fn: Any) -> Any:
        set_production_research_fn(fn)
        installed.append(fn)
        return fn

    try:
        yield _install
    finally:
        set_production_research_fn(None)


class TestPreview:
    """Trust before cadence. A preview shows exactly what a run would file —
    same projection, same email rule — and leaves the pipeline untouched."""

    @pytest.mark.asyncio
    async def test_preview_writes_nothing(self, w1, wired):
        icp = await _create_icp(w1)
        wired(FakeResearch(_company("acme-dental.com"), _company("brightsmile.com")))

        resp = await w1.post(f"{ICPS_URL}/{icp['id']}/preview")

        assert resp.status_code == 200, resp.text
        assert len(resp.json()["items"]) == 2
        assert await _prospects(w1) == []
        assert (await w1.get("/api/v1/growth/drafts")).json() == []

    @pytest.mark.asyncio
    async def test_preview_shows_only_recordable_emails(self, w1, wired):
        """A preview that displayed the raw evidence would advertise addresses
        the engine refuses to keep — the projection is shared with the run
        precisely so the two cannot drift."""
        icp = await _create_icp(w1)
        wired(
            FakeResearch(
                _company(
                    "acme-dental.com",
                    emails=(
                        EmailEvidence("guess@acme-dental.com", "guessed"),
                        EmailEvidence(
                            "hello@acme-dental.com",
                            "observed",
                            "https://acme-dental.com/contact",
                        ),
                    ),
                )
            )
        )

        (item,) = (await w1.post(f"{ICPS_URL}/{icp['id']}/preview")).json()["items"]

        assert item["emails"] == ["hello@acme-dental.com"]

    @pytest.mark.asyncio
    async def test_a_known_company_is_flagged_not_hidden(self, w1, wired):
        """A preview full of already_known rows is the useful signal that the
        criteria describe people you already have."""
        await w1.post(
            "/api/v1/growth/prospects",
            json={"domain": "acme-dental.com", "source": "manual"},
        )
        icp = await _create_icp(w1)
        wired(FakeResearch(_company("acme-dental.com"), _company("brightsmile.com")))

        items = (await w1.post(f"{ICPS_URL}/{icp['id']}/preview")).json()["items"]

        assert {i["domain"]: i["already_known"] for i in items} == {
            "acme-dental.com": True,
            "brightsmile.com": False,
        }

    @pytest.mark.asyncio
    async def test_a_paused_icp_still_previews(self, w1, wired):
        """Checking whether a profile is worth resuming is the reason to look
        at a paused one."""
        icp = await _create_icp(w1, status="paused")
        wired(FakeResearch(_company("acme-dental.com")))

        resp = await w1.post(f"{ICPS_URL}/{icp['id']}/preview")

        assert resp.status_code == 200, resp.text
        assert len(resp.json()["items"]) == 1

    @pytest.mark.asyncio
    async def test_a_research_failure_is_reported_not_swallowed(self, w1, wired):
        """ "Found nobody" and "the search provider was down" must not look the
        same to someone tuning criteria."""
        icp = await _create_icp(w1)
        wired(ExplodingResearch())

        body = (await w1.post(f"{ICPS_URL}/{icp['id']}/preview")).json()

        assert body["items"] == []
        assert "research failed" in body["error"]

    @pytest.mark.asyncio
    async def test_preview_respects_max_per_run(self, w1, wired):
        icp = await _create_icp(w1, max_per_run=2)
        wired(FakeResearch(*[_company(f"co-{i:02d}.com") for i in range(6)]))

        items = (await w1.post(f"{ICPS_URL}/{icp['id']}/preview")).json()["items"]

        assert len(items) == 2

    @pytest.mark.asyncio
    async def test_no_research_backend_is_a_503_not_an_empty_preview(self, w1):
        """An operator tuning criteria against a silently-disabled engine would
        rewrite them forever."""
        icp = await _create_icp(w1)

        resp = await w1.post(f"{ICPS_URL}/{icp['id']}/preview")

        assert resp.status_code == 503, resp.text
        assert resp.json()["error"]["code"] == "icp.research_unavailable"

    @pytest.mark.asyncio
    async def test_another_tenants_icp_is_a_404(self, w1, w2, wired):
        icp = await _create_icp(w1)
        wired(FakeResearch(_company("acme-dental.com")))

        resp = await w2.post(f"{ICPS_URL}/{icp['id']}/preview")

        assert resp.status_code == 404, resp.text
