# tests/cloud/growth/social/test_hunt_pulse.py — the outreach side of Pulse over HTTP, with fake
# provider/agent seams (no network, no model): a hunt's market pulse is ICP-scoped and runs in
# "hunt" mode, its named companies file as prospects (no website = skipped), prospect signals and
# the suggested first touch land in research and survive a re-research, and the daily sweep
# refreshes a used Pulse and marks evidence it had not seen before as new.

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from pocketpaw_ee.cloud.growth.social import service as social_service
from pocketpaw_ee.cloud.growth.social.pulse import (
    Evidence,
    ProspectSignals,
    PulseResult,
    PulseSource,
    parse_signals,
    parse_themes,
    set_production_pulse_fn,
    set_production_signals_fn,
)
from pocketpaw_ee.cloud.growth.writer import _research_lines

from tests.cloud.growth.test_router import _build_app, _payload

BASE = "/api/v1/growth"
EV = {
    "id": "r_1",
    "platform": "reddit",
    "url": "https://www.reddit.com/r/agency/1",
    "title": "t",
    "text": "x",
    "score": 9,
    "source": "r/agency",
    "comments": [],
}


def _theme(url: str = EV["url"]) -> dict[str, Any]:
    return {
        "title": "Agencies want white-label reports",
        "why": "Your differentiator is exactly what they ask for.",
        "sentiment": "negative",
        "phrases": ["clients want branded PDFs"],
        "evidence": [{**EV, "url": url}],
        "suggestions": [
            {"platform": "x", "hook": "Lead with white-label", "caption": "Hi", "subreddit": ""}
        ],
        "companies": [
            {
                "name": "Acme Agency",
                "domain": "acme-agency.com",
                "reason": "asked for it",
                "url": url,
            },
            {"name": "No Site Co", "domain": "", "reason": "asked too", "url": url},
        ],
    }


@pytest_asyncio.fixture
async def w1(mongo_db: Any):
    transport = ASGITransport(app=_build_app(workspace_id="w1"))
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        yield client


@pytest_asyncio.fixture
async def w2(mongo_db: Any):
    transport = ASGITransport(app=_build_app(workspace_id="w2"))
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        yield client


@pytest.fixture(autouse=True)
def _reset():
    yield
    set_production_pulse_fn(None)
    set_production_signals_fn(None)


async def _icp(client: AsyncClient) -> dict[str, Any]:
    body = {"name": "Agencies", "criteria": "Marketing agencies with 10-50 staff"}
    resp = await client.post(f"{BASE}/icps", json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()


@pytest.mark.asyncio
async def test_hunt_pulse_is_icp_scoped_and_files_named_companies(w1, w2):
    icp = await _icp(w1)
    url = f"{BASE}/icps/{icp['id']}/pulse"
    assert (await w1.post(f"{url}/refresh", json={})).status_code == 503

    seen: dict[str, Any] = {}

    async def build(brief, sources):
        seen["brief"] = brief
        return PulseResult(
            sources=(PulseSource("reddit", "subreddit", "agency"),), themes=(_theme(),)
        )

    set_production_pulse_fn(build)
    pulse = (await w1.post(f"{url}/refresh", json={})).json()
    assert seen["brief"].mode == "hunt" and any(
        "Marketing agencies" in line for line in seen["brief"].lines
    )
    assert pulse["themes"][0]["companies"][0]["domain"] == "acme-agency.com"
    assert (await w1.get(url)).json()["themes"][0]["title"] == "Agencies want white-label reports"
    assert (await w2.get(url)).status_code == 404

    filed = (await w1.post(f"{url}/prospects", json={"theme": 0})).json()
    assert filed == {"added": ["Acme Agency"], "skipped": ["No Site Co"]}
    items = (await w1.get(f"{BASE}/prospects")).json()["items"]
    row = next(p for p in items if p["domain"] == "acme-agency.com")
    assert row["source"] == "discovery" and row["icp_id"] == icp["id"]
    assert (await w1.post(f"{url}/prospects", json={"theme": 4})).status_code == 404


@pytest.mark.asyncio
async def test_prospect_signals_land_in_research(w1):
    prospect = (await w1.post(f"{BASE}/prospects", json=_payload())).json()
    url = f"{BASE}/prospects/{prospect['id']}/signals"
    assert (await w1.post(url)).status_code == 503

    async def signals(brief, company, domain, linkedin_url):
        assert company == "Acme Dental" and domain == "acme-dental.com"
        sig = {
            "kind": "hiring",
            "text": "Hiring 3 SDRs",
            "why": "Growing outbound",
            "url": "https://x.com/a/1",
        }
        return ProspectSignals((sig,), "Saw you're hiring SDRs; we help them ramp faster.")

    set_production_signals_fn(signals)
    research = (await w1.post(url)).json()["research"]
    assert research["signals"][0]["why"] == "Growing outbound"
    assert research["first_touch"].startswith("Saw you're hiring")


@pytest.mark.asyncio
async def test_sweep_refreshes_used_pulses_and_marks_new_evidence(w1):
    icp = await _icp(w1)
    urls = iter(["https://www.reddit.com/r/agency/1", "https://www.reddit.com/r/agency/2"])

    async def build(brief, sources):
        return PulseResult(
            sources=(PulseSource("reddit", "subreddit", "agency"),), themes=(_theme(next(urls)),)
        )

    set_production_pulse_fn(build)
    await w1.post(f"{BASE}/icps/{icp['id']}/pulse/refresh", json={})
    assert await social_service.refresh_due_pulses(datetime.now(UTC)) == {
        "due": 0,
        "refreshed": 0,
        "failed": 0,
    }
    result = await social_service.refresh_due_pulses(datetime.now(UTC) + timedelta(days=1))
    assert result == {"due": 1, "refreshed": 1, "failed": 0}
    ev = (await w1.get(f"{BASE}/icps/{icp['id']}/pulse")).json()["themes"][0]["evidence"][0]
    assert ev["url"].endswith("/2") and ev["new"] is True


def test_parsers_for_companies_signals_and_writer_lines():
    ev = [Evidence("r_1", "reddit", "https://www.reddit.com/r/a/1", "t", "x", 5, "r/a")]
    text = (
        '{"themes": [{"title": "T", "why": "W", "evidence": ["r_1"], "companies": ['
        '{"name": "Good", "domain": "https://www.good.io/x", "evidence": "r_1"}, '
        '{"name": "Bad ref", "evidence": "nope"}, {"name": "Junk domain", "domain": "not a site", '
        '"evidence": "r_1"}]}]}'
    )
    companies = parse_themes(text, ev)[0]["companies"]
    assert [(c["name"], c["domain"]) for c in companies] == [
        ("Good", "good.io"),
        ("Junk domain", ""),
    ]
    got = parse_signals(
        '{"signals": [{"kind": "rocket", "text": "Raised A", "why": "Budget", "url": "http://x"}, '
        '{"text": "no why"}], "first_touch": "Congrats on the round"}'
    )
    assert got.signals == ({"kind": "company", "text": "Raised A", "why": "Budget", "url": ""},)
    assert got.first_touch == "Congrats on the round"

    class P:
        research = {
            "signals": [{"text": "Hiring SDRs", "why": "growing"}],
            "first_touch": "Saw the roles",
        }
        research_brief = ""

    lines = _research_lines(P())
    assert (
        "Buying signals: Hiring SDRs (growing)" in lines
        and "Suggested first touch: Saw the roles" in lines
    )
