# tests/cloud/growth/test_list_query.py — HTTP-layer tests for the G-10a
# prospect-list scale surface: ``q`` full-text-ish search, the four ``sort``
# modes, cursor pagination + ``total``, and the facets endpoint. Reuses the
# ``test_router.py`` app harness (fixed RequestContext per workspace,
# mongomock-backed Beanie) so the RBAC guards and the license gate are wired
# exactly as in production.
#
# Created 2026-07-28 (feat/growth-api-scale): G-10a — the list endpoint could
# not reach row 3,000, find a company by name, or say "n of m". Split into its
# own file rather than growing test_router.py past readability.

from __future__ import annotations

from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from tests.cloud.growth.test_router import _build_app, _payload

LIST_URL = "/api/v1/growth/prospects"
FACETS_URL = "/api/v1/growth/prospects/facets"


@pytest_asyncio.fixture
async def w1_client(mongo_db: Any) -> AsyncClient:
    app = _build_app(workspace_id="w1")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        yield client


@pytest_asyncio.fixture
async def w2_client(mongo_db: Any) -> AsyncClient:
    app = _build_app(workspace_id="w2")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        yield client


async def _seed(client: AsyncClient, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Create rows one at a time (sequential inserts give a stable createdAt
    order, which the newest/oldest sort assertions depend on)."""
    created = []
    for row in rows:
        resp = await client.post(LIST_URL, json=_payload(**row))
        assert resp.status_code == 200, resp.text
        created.append(resp.json())
    return created


# ---------------------------------------------------------------------------
# 1. ``q`` search
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("term", "expected_domain"),
    [
        ("norah", "alpha.io"),  # name
        ("zenith", "beta.io"),  # company
        ("gamma-corp", "gamma-corp.io"),  # domain
        ("periodontics", "delta.io"),  # research_brief
    ],
)
async def test_q_matches_across_all_four_fields(w1_client, term, expected_domain):
    await _seed(
        w1_client,
        [
            {"domain": "alpha.io", "name": "Norah Bright", "company": "Alpha Dental"},
            {"domain": "beta.io", "name": "Sam Founder", "company": "Zenith Clinics"},
            {"domain": "gamma-corp.io", "name": "Sam Founder", "company": "Gamma"},
            {
                "domain": "delta.io",
                "name": "Sam Founder",
                "company": "Delta",
                "research_brief": "Runs a periodontics practice in Pune",
            },
        ],
    )
    resp = await w1_client.get(LIST_URL, params={"q": term})
    assert resp.status_code == 200, resp.text
    assert [p["domain"] for p in resp.json()] == [expected_domain]


@pytest.mark.asyncio
async def test_q_is_case_insensitive(w1_client):
    await _seed(w1_client, [{"domain": "alpha.io", "company": "Alpha Dental"}])
    for term in ("alpha dental", "ALPHA DENTAL", "AlPhA dEnTaL"):
        resp = await w1_client.get(LIST_URL, params={"q": term})
        assert [p["domain"] for p in resp.json()] == ["alpha.io"], term


@pytest.mark.asyncio
async def test_q_is_workspace_scoped(w1_client, w2_client):
    """The other tenant's matching row is never returned — the search runs
    inside the workspace filter, not beside it."""
    await _seed(w1_client, [{"domain": "alpha.io", "company": "Sharedname Clinics"}])
    await _seed(w2_client, [{"domain": "beta.io", "company": "Sharedname Clinics"}])

    w1_hits = (await w1_client.get(LIST_URL, params={"q": "sharedname"})).json()
    w2_hits = (await w2_client.get(LIST_URL, params={"q": "sharedname"})).json()
    assert [p["domain"] for p in w1_hits] == ["alpha.io"]
    assert [p["domain"] for p in w2_hits] == ["beta.io"]


@pytest.mark.asyncio
async def test_q_composes_with_the_existing_filters(w1_client):
    await _seed(
        w1_client,
        [
            {"domain": "alpha.io", "company": "Acme One", "tier": "a"},
            {"domain": "beta.io", "company": "Acme Two", "tier": "b"},
        ],
    )
    resp = await w1_client.get(LIST_URL, params={"q": "acme", "tier": "a"})
    assert [p["domain"] for p in resp.json()] == ["alpha.io"]


@pytest.mark.asyncio
async def test_q_metacharacters_are_matched_literally(w1_client):
    """A regex metacharacter is escaped, not compiled — ``.*`` matches nothing
    rather than everything, and a catastrophic pattern can't be injected."""
    await _seed(w1_client, [{"domain": "alpha.io", "company": "Acme"}])
    resp = await w1_client.get(LIST_URL, params={"q": ".*"})
    assert resp.json() == []


@pytest.mark.asyncio
async def test_blank_q_is_ignored(w1_client):
    await _seed(w1_client, [{"domain": "alpha.io"}, {"domain": "beta.io"}])
    resp = await w1_client.get(LIST_URL, params={"q": "   "})
    assert len(resp.json()) == 2


# ---------------------------------------------------------------------------
# 2. ``sort``
# ---------------------------------------------------------------------------


SORT_ROWS = [
    {"domain": "zulu.io", "company": "Zulu Clinics", "tier": "unqualified"},
    {"domain": "alpha.io", "company": "Alpha Dental", "tier": "c"},
    {"domain": "mike.io", "company": "Mike Ortho", "tier": "a"},
    {"domain": "bravo.io", "company": "Bravo Health", "tier": "b"},
]


@pytest.mark.asyncio
async def test_sort_newest_is_the_default_and_is_creation_order_reversed(w1_client):
    await _seed(w1_client, SORT_ROWS)
    default = (await w1_client.get(LIST_URL)).json()
    explicit = (await w1_client.get(LIST_URL, params={"sort": "newest"})).json()
    assert [p["domain"] for p in default] == ["bravo.io", "mike.io", "alpha.io", "zulu.io"]
    assert default == explicit


@pytest.mark.asyncio
async def test_sort_oldest_is_creation_order(w1_client):
    await _seed(w1_client, SORT_ROWS)
    rows = (await w1_client.get(LIST_URL, params={"sort": "oldest"})).json()
    assert [p["domain"] for p in rows] == ["zulu.io", "alpha.io", "mike.io", "bravo.io"]


@pytest.mark.asyncio
async def test_sort_company_is_alphabetical(w1_client):
    await _seed(w1_client, SORT_ROWS)
    rows = (await w1_client.get(LIST_URL, params={"sort": "company"})).json()
    assert [p["company"] for p in rows] == [
        "Alpha Dental",
        "Bravo Health",
        "Mike Ortho",
        "Zulu Clinics",
    ]


@pytest.mark.asyncio
async def test_sort_tier_walks_the_declared_rank(w1_client):
    """a → b → c → unqualified. Asserted on the tier sequence itself, not on
    the row order, so the test states the rank rather than restating the
    fixture."""
    await _seed(w1_client, SORT_ROWS)
    rows = (await w1_client.get(LIST_URL, params={"sort": "tier"})).json()
    assert [p["tier"] for p in rows] == ["a", "b", "c", "unqualified"]
    assert [p["domain"] for p in rows] == ["mike.io", "bravo.io", "alpha.io", "zulu.io"]


@pytest.mark.asyncio
async def test_sort_tier_orders_within_a_bucket_newest_first(w1_client):
    await _seed(
        w1_client,
        [
            {"domain": "a-old.io", "tier": "a"},
            {"domain": "b-only.io", "tier": "b"},
            {"domain": "a-new.io", "tier": "a"},
        ],
    )
    rows = (await w1_client.get(LIST_URL, params={"sort": "tier"})).json()
    assert [p["domain"] for p in rows] == ["a-new.io", "a-old.io", "b-only.io"]


@pytest.mark.asyncio
async def test_sort_tier_composes_with_a_tier_filter(w1_client):
    await _seed(w1_client, SORT_ROWS)
    rows = (await w1_client.get(LIST_URL, params={"sort": "tier", "tier": "b"})).json()
    assert [p["domain"] for p in rows] == ["bravo.io"]


@pytest.mark.asyncio
async def test_sort_tier_is_workspace_scoped(w1_client, w2_client):
    await _seed(w1_client, [{"domain": "alpha.io", "tier": "a"}])
    await _seed(w2_client, [{"domain": "beta.io", "tier": "a"}])
    rows = (await w1_client.get(LIST_URL, params={"sort": "tier"})).json()
    assert [p["domain"] for p in rows] == ["alpha.io"]


@pytest.mark.asyncio
async def test_unknown_sort_mode_is_422(w1_client):
    resp = await w1_client.get(LIST_URL, params={"sort": "cheapest"})
    assert resp.status_code == 422
