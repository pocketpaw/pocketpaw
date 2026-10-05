# tests/cloud/growth/test_prospect_research.py — researching ONE prospect.
#
# ``POST /growth/prospects/{id}/research`` runs the researcher on a single known
# company and folds what comes back into the row. The write rules are the point:
# fill gaps and never overwrite what a person typed, take emails only through
# ``recordable_emails``, accept a LinkedIn URL only on linkedin.com, suggest a
# tier only to an unqualified row, and never move status backwards.
#
# The agent is a fake installed on the ``ProspectResearchFn`` seam; one test
# drives ``agent_prospect_research`` through a fake agent pool. No real run.

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
import pytest_asyncio
from beanie import PydanticObjectId
from httpx import ASGITransport, AsyncClient
from pocketpaw_ee.cloud.growth.discovery import DiscoveredCompany
from pocketpaw_ee.cloud.growth.domain import EmailEvidence
from pocketpaw_ee.cloud.growth.dto import ProspectResearch
from pocketpaw_ee.cloud.growth.researcher import (
    GROWTH_RESEARCHER_SLUG,
    ProspectResearchOutcome,
    ResearchUnavailable,
    agent_prospect_research,
    build_prospect_research_prompt,
    parse_prospect_research_response,
    set_production_prospect_research_fn,
)
from pocketpaw_ee.cloud.models.agent import Agent
from pocketpaw_ee.cloud.models.prospect import Prospect as ProspectDoc
from pocketpaw_ee.cloud.models.workspace import Workspace

from tests.cloud.growth.test_discovery import _create_icp
from tests.cloud.growth.test_router import _build_app

PROSPECTS = "/api/v1/growth/prospects"


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


class FakeProspectResearch:
    def __init__(self, company: DiscoveredCompany | None = None, *, exc: Exception | None = None):
        self.company = company
        self.exc = exc
        self.calls: list[tuple[Any, Any]] = []

    async def __call__(self, prospect, icp):
        self.calls.append((prospect, icp))
        if self.exc is not None:
            raise self.exc
        return ProspectResearchOutcome(company=self.company, notes="")


@pytest.fixture
def seam():
    def _install(fn: Any) -> Any:
        set_production_prospect_research_fn(fn)
        return fn

    try:
        yield _install
    finally:
        set_production_prospect_research_fn(None)


def _found(domain: str = "acme.com", **overrides: Any) -> DiscoveredCompany:
    base: dict[str, Any] = {
        "name": "Dana Reyes",
        "company": "Acme Dental",
        "research_brief": "",
        "source_urls": (f"https://{domain}/about",),
        "profile": {
            "summary": "Three-chair practice in Austin.",
            "suggested_tier": "a",
            "tier_reason": "Books by phone only.",
            "fit": "Exactly the chair count we want.",
            "hook": "Their booking page is a phone number.",
            "sources": [f"https://{domain}/team"],
        },
    }
    base.update(overrides)
    return DiscoveredCompany(domain=domain, **base)


async def _create(client: AsyncClient, **fields: Any) -> dict[str, Any]:
    payload = {"domain": "acme.com", "source": "manual", **fields}
    resp = await client.post(PROSPECTS, json=payload)
    assert resp.status_code in (200, 201), resp.text
    return resp.json()


async def _research(client: AsyncClient, prospect_id: str):
    return await client.post(f"{PROSPECTS}/{prospect_id}/research")


async def _set(prospect_id: str, **fields: Any) -> None:
    doc = await ProspectDoc.get(PydanticObjectId(prospect_id))
    for key, value in fields.items():
        setattr(doc, key, value)
    await doc.save()


# ---------------------------------------------------------------------------
# Parser and prompt
# ---------------------------------------------------------------------------


class TestParser:
    def test_picks_the_entry_for_the_requested_domain(self):
        text = json.dumps(
            {
                "companies": [
                    {"domain": "other.com", "company": "Other"},
                    {
                        "domain": "www.Acme.com",
                        "company": "Acme",
                        "linkedin_url": " https://www.linkedin.com/company/acme ",
                        "profile": {"summary": "S"},
                    },
                ],
                "notes": "n",
            }
        )
        out = parse_prospect_research_response("Here you go:\n" + text, "acme.com")
        assert out.company is not None
        assert out.company.company == "Acme"
        assert out.company.linkedin_url == "https://www.linkedin.com/company/acme"
        assert out.company.profile == {"summary": "S"}
        assert out.notes == "n"

        other_only = json.dumps({"companies": [{"domain": "other.com"}], "notes": ""})
        assert parse_prospect_research_response(other_only, "acme.com").company is None

    @pytest.mark.parametrize("text", ["", "not json", "{broken", '{"companies": "x"}', "[]"])
    def test_never_raises_on_junk(self, text):
        assert parse_prospect_research_response(text, "acme.com").company is None


class TestResearchDto:
    def test_coerces_and_caps(self):
        r = ProspectResearch.model_validate(
            {
                "summary": "x" * 900,
                "suggested_tier": "z",
                "caveats": [f"c{i}" for i in range(40)],
                "channels": [{"kind": "fax", "value": "123"}, {"kind": "phone", "value": ""}],
                "locations": [{"name": "", "address": ""}, {"name": "HQ"}],
                "sources": ["javascript:alert(1)", "https://acme.com"],
            }
        )
        assert len(r.summary) == 600
        assert r.suggested_tier == ""
        assert len(r.caveats) == 15
        assert [(c.kind, c.value) for c in r.channels] == [("other", "123")]
        assert [loc.name for loc in r.locations] == ["HQ"]
        assert r.sources == ["https://acme.com"]


class TestPrompt:
    def test_carries_the_prospect_and_the_rules_without_a_json_template(self):
        from pocketpaw_ee.cloud.growth.domain import Icp, Prospect

        prospect = Prospect(
            id="p1",
            workspace_id="w1",
            name="Dana",
            company="Acme Dental",
            domain="acme.com",
            source="manual",
        )
        icp = Icp(id="i1", workspace_id="w1", name="n", criteria="Small dental", geography="Texas")
        text = build_prospect_research_prompt(prospect, icp)
        for needle in ("acme.com", "Acme Dental", "Dana", "Small dental", "Texas", "profile"):
            assert needle in text
        assert "never construct an address" in text
        assert "{" not in text and "}" not in text


# ---------------------------------------------------------------------------
# The route: errors
# ---------------------------------------------------------------------------


class TestErrors:
    @pytest.mark.asyncio
    async def test_503_when_no_backend_is_wired(self, w1):
        p = await _create(w1)
        resp = await _research(w1, p["id"])
        assert resp.status_code == 503
        assert resp.json()["error"]["code"] == "prospect.research_unavailable"

    @pytest.mark.asyncio
    async def test_404_for_missing_and_foreign_prospects(self, w1, w2, seam):
        fake = seam(FakeProspectResearch(_found()))
        p = await _create(w1)
        assert (await _research(w1, "not-an-id")).status_code == 404
        assert (await _research(w2, p["id"])).status_code == 404
        assert fake.calls == []

    @pytest.mark.parametrize(
        "fake",
        [
            FakeProspectResearch(exc=ResearchUnavailable("the research run failed")),
            FakeProspectResearch(exc=RuntimeError("boom")),
            FakeProspectResearch(None),
        ],
        ids=["run_failed", "unexpected_error", "nothing_usable"],
    )
    @pytest.mark.asyncio
    async def test_502_and_no_write(self, w1, seam, fake):
        seam(fake)
        p = await _create(w1)
        resp = await _research(w1, p["id"])
        assert resp.status_code == 502
        assert resp.json()["error"]["code"] == "prospect.research_failed"
        after = (await w1.get(f"{PROSPECTS}/{p['id']}")).json()
        assert after["research"] is None and after["researched_at"] is None
        assert after["status"] == "new"


# ---------------------------------------------------------------------------
# The route: write rules
# ---------------------------------------------------------------------------


class TestWriteRules:
    @pytest.mark.asyncio
    async def test_fills_a_fresh_row(self, w1, seam):
        fake = seam(FakeProspectResearch(_found()))
        p = await _create(w1)
        resp = await _research(w1, p["id"])
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["name"] == "Dana Reyes"
        assert body["company"] == "Acme Dental"
        assert body["tier"] == "a"
        assert body["status"] == "qualified"
        assert body["research"]["summary"] == "Three-chair practice in Austin."
        assert body["research"]["suggested_tier"] == "a"
        assert body["researched_at"]
        assert body["research_brief"] == (
            "Three-chair practice in Austin.\n\n"
            "Fit: Exactly the chair count we want.\n\n"
            "Hook: Their booking page is a phone number."
        )
        assert body["source_urls"] == ["https://acme.com/about", "https://acme.com/team"]
        assert fake.calls[0][0].domain == "acme.com"
        assert fake.calls[0][1] is None

        stored = (await w1.get(f"{PROSPECTS}/{p['id']}")).json()
        assert stored["research"] == body["research"]

    @pytest.mark.asyncio
    async def test_never_overwrites_name_company_or_linkedin(self, w1, seam):
        seam(FakeProspectResearch(_found(linkedin_url="https://www.linkedin.com/company/other")))
        p = await _create(
            w1,
            name="Typed Name",
            company="Typed Co",
            linkedin_url="https://www.linkedin.com/company/typed",
        )
        body = (await _research(w1, p["id"])).json()
        assert body["name"] == "Typed Name"
        assert body["company"] == "Typed Co"
        assert body["linkedin_url"] == "https://www.linkedin.com/company/typed"

    @pytest.mark.parametrize(
        ("url", "accepted"),
        [
            ("https://in.linkedin.com/company/acme", True),
            ("http://www.linkedin.com/company/acme", False),
            ("https://linkedin.com.evil.io/company/acme", False),
            ("https://linkedin.com@evil.io/", False),
            ("https://notlinkedin.com/company/acme", False),
            ("javascript:alert(1)", False),
            ("linkedin.com/company/acme", False),
        ],
    )
    @pytest.mark.asyncio
    async def test_blank_linkedin_is_filled_only_from_https_linkedin_com(
        self, w1, seam, url, accepted
    ):
        seam(FakeProspectResearch(_found(linkedin_url=url)))
        p = await _create(w1)
        body = (await _research(w1, p["id"])).json()
        if accepted:
            assert body["linkedin_url"] == url
        else:
            assert body["linkedin_url"] in (None, "")

    @pytest.mark.asyncio
    async def test_emails_only_through_recordable_evidence_and_merged(self, w1, seam):
        seam(
            FakeProspectResearch(
                _found(
                    emails=(
                        EmailEvidence("Hello@Acme.com", "observed", "https://acme.com/contact"),
                        EmailEvidence("new@acme.com", "observed", "https://acme.com/team"),
                        EmailEvidence("guess@acme.com", "guessed", "https://acme.com"),
                        EmailEvidence("nosource@acme.com", "observed", ""),
                    ),
                    profile={
                        "channels": [{"kind": "email", "value": "agent@acme.com"}],
                        "summary": "S",
                    },
                )
            )
        )
        p = await _create(w1, emails=["hello@acme.com"])
        body = (await _research(w1, p["id"])).json()
        assert body["emails"] == ["hello@acme.com", "new@acme.com"]
        assert body["research"]["channels"] == [
            {"kind": "email", "value": "agent@acme.com", "notes": ""}
        ]

    @pytest.mark.asyncio
    async def test_brief_falls_back_then_keeps_the_old_one(self, w1, seam):
        fake = seam(FakeProspectResearch(_found(profile={}, research_brief="From the entry.")))
        p = await _create(w1, research_brief="Old brief.")
        assert (await _research(w1, p["id"])).json()["research_brief"] == "From the entry."

        fake.company = _found(profile=None, research_brief="")
        assert (await _research(w1, p["id"])).json()["research_brief"] == "From the entry."

    @pytest.mark.asyncio
    async def test_sources_are_a_deduped_ordered_union(self, w1, seam):
        seam(
            FakeProspectResearch(
                _found(
                    source_urls=("https://acme.com/about", "https://acme.com/new", "ftp://x"),
                    profile={"sources": ["https://acme.com/new", "https://acme.com/team"]},
                )
            )
        )
        p = await _create(w1, source_urls=["https://acme.com/about", "https://x.com/list"])
        body = (await _research(w1, p["id"])).json()
        assert body["source_urls"] == [
            "https://acme.com/about",
            "https://x.com/list",
            "https://acme.com/new",
            "https://acme.com/team",
        ]

    @pytest.mark.parametrize(
        ("fields", "suggested", "tier"),
        [({"tier": "c"}, "a", "c"), ({}, "q", "unqualified")],
        ids=["human_set_tier_kept", "invalid_suggestion_ignored"],
    )
    @pytest.mark.asyncio
    async def test_tier_is_only_suggested_to_an_unqualified_row(
        self, w1, seam, fields, suggested, tier
    ):
        seam(FakeProspectResearch(_found(profile={"suggested_tier": suggested, "summary": "S"})))
        p = await _create(w1, **fields)
        assert (await _research(w1, p["id"])).json()["tier"] == tier

    @pytest.mark.parametrize("status", ["drafted", "replied", "dead"])
    @pytest.mark.asyncio
    async def test_status_never_regresses(self, w1, seam, status):
        seam(FakeProspectResearch(_found()))
        p = await _create(w1)
        await _set(p["id"], status=status)
        assert (await _research(w1, p["id"])).json()["status"] == status

    @pytest.mark.asyncio
    async def test_icp_is_passed_as_context_and_a_deleted_one_is_none(self, w1, seam):
        fake = seam(FakeProspectResearch(_found()))
        icp = await _create_icp(w1)
        p = await _create(w1)
        await _set(p["id"], icp_id=icp["id"])
        assert (await _research(w1, p["id"])).status_code == 200
        assert fake.calls[-1][1].criteria == icp["criteria"]

        assert (await w1.delete(f"/api/v1/growth/icps/{icp['id']}")).status_code in (200, 204)
        assert (await _research(w1, p["id"])).status_code == 200
        assert fake.calls[-1][1] is None

    @pytest.mark.asyncio
    async def test_an_edit_made_during_the_run_survives(self, w1, seam):
        p = await _create(w1)

        class EditsMidRun(FakeProspectResearch):
            async def __call__(self, prospect, icp):
                await _set(p["id"], whatsapp_number="+15550001111")
                return await super().__call__(prospect, icp)

        seam(EditsMidRun(_found()))
        body = (await _research(w1, p["id"])).json()
        assert body["whatsapp_number"] == "+15550001111"
        assert body["research"] is not None


# ---------------------------------------------------------------------------
# The production fn, through a fake agent pool
# ---------------------------------------------------------------------------


class _FakePool:
    def __init__(self, answer: str) -> None:
        self.answer = answer
        self.calls: list[dict] = []

    async def run(self, **kwargs):
        self.calls.append(kwargs)
        yield SimpleNamespace(type="tool_result", content='{"companies": [{"domain": "x.io"}]}')
        yield SimpleNamespace(type="message", content=self.answer)


@pytest.mark.asyncio
async def test_agent_prospect_research_seeds_and_runs(mongo_db):
    from pocketpaw_ee.cloud.growth.domain import Prospect

    ws = Workspace(name="acme", slug="acme", owner="owner-1")
    await ws.insert()
    workspace_id = str(ws.id)
    answer = json.dumps(
        {"companies": [{"domain": "acme.com", "profile": {"summary": "S"}}], "notes": ""}
    )
    pool = _FakePool(answer)
    prospect = Prospect(
        id="p1", workspace_id=workspace_id, name="", company="", domain="acme.com", source="x"
    )
    with patch("pocketpaw.agents.pool.get_agent_pool", return_value=pool):
        out = await agent_prospect_research(prospect, None)

    seeded = await Agent.find_one(
        Agent.workspace == workspace_id, Agent.slug == GROWTH_RESEARCHER_SLUG
    )
    assert seeded is not None
    assert out.company is not None and out.company.profile == {"summary": "S"}
    call = pool.calls[0]
    assert call["agent_id"] == str(seeded.id)
    assert call["session_key"].startswith(f"growth-prospect-research:{workspace_id}:p1:")
    assert "acme.com" in call["message"]
