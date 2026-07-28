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


def _items(resp: Any) -> list[dict[str, Any]]:
    """The page envelope's rows. G-10a replaced the bare array with
    ``{items, next_cursor, total}``."""
    assert resp.status_code == 200, resp.text
    return resp.json()["items"]


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
    assert [p["domain"] for p in _items(resp)] == [expected_domain]


@pytest.mark.asyncio
async def test_q_is_case_insensitive(w1_client):
    await _seed(w1_client, [{"domain": "alpha.io", "company": "Alpha Dental"}])
    for term in ("alpha dental", "ALPHA DENTAL", "AlPhA dEnTaL"):
        resp = await w1_client.get(LIST_URL, params={"q": term})
        assert [p["domain"] for p in _items(resp)] == ["alpha.io"], term


@pytest.mark.asyncio
async def test_q_is_workspace_scoped(w1_client, w2_client):
    """The other tenant's matching row is never returned — the search runs
    inside the workspace filter, not beside it."""
    await _seed(w1_client, [{"domain": "alpha.io", "company": "Sharedname Clinics"}])
    await _seed(w2_client, [{"domain": "beta.io", "company": "Sharedname Clinics"}])

    w1_hits = _items(await w1_client.get(LIST_URL, params={"q": "sharedname"}))
    w2_hits = _items(await w2_client.get(LIST_URL, params={"q": "sharedname"}))
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
    assert [p["domain"] for p in _items(resp)] == ["alpha.io"]


@pytest.mark.asyncio
async def test_q_metacharacters_are_matched_literally(w1_client):
    """A regex metacharacter is escaped, not compiled — ``.*`` matches nothing
    rather than everything, and a catastrophic pattern can't be injected."""
    await _seed(w1_client, [{"domain": "alpha.io", "company": "Acme"}])
    resp = await w1_client.get(LIST_URL, params={"q": ".*"})
    assert _items(resp) == []


@pytest.mark.asyncio
async def test_blank_q_is_ignored(w1_client):
    await _seed(w1_client, [{"domain": "alpha.io"}, {"domain": "beta.io"}])
    resp = await w1_client.get(LIST_URL, params={"q": "   "})
    assert len(_items(resp)) == 2


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
    default = _items(await w1_client.get(LIST_URL))
    explicit = _items(await w1_client.get(LIST_URL, params={"sort": "newest"}))
    assert [p["domain"] for p in default] == ["bravo.io", "mike.io", "alpha.io", "zulu.io"]
    assert default == explicit


@pytest.mark.asyncio
async def test_sort_oldest_is_creation_order(w1_client):
    await _seed(w1_client, SORT_ROWS)
    rows = _items(await w1_client.get(LIST_URL, params={"sort": "oldest"}))
    assert [p["domain"] for p in rows] == ["zulu.io", "alpha.io", "mike.io", "bravo.io"]


@pytest.mark.asyncio
async def test_sort_company_is_alphabetical(w1_client):
    await _seed(w1_client, SORT_ROWS)
    rows = _items(await w1_client.get(LIST_URL, params={"sort": "company"}))
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
    rows = _items(await w1_client.get(LIST_URL, params={"sort": "tier"}))
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
    rows = _items(await w1_client.get(LIST_URL, params={"sort": "tier"}))
    assert [p["domain"] for p in rows] == ["a-new.io", "a-old.io", "b-only.io"]


@pytest.mark.asyncio
async def test_sort_tier_composes_with_a_tier_filter(w1_client):
    await _seed(w1_client, SORT_ROWS)
    rows = _items(await w1_client.get(LIST_URL, params={"sort": "tier", "tier": "b"}))
    assert [p["domain"] for p in rows] == ["bravo.io"]


@pytest.mark.asyncio
async def test_sort_tier_is_workspace_scoped(w1_client, w2_client):
    await _seed(w1_client, [{"domain": "alpha.io", "tier": "a"}])
    await _seed(w2_client, [{"domain": "beta.io", "tier": "a"}])
    rows = _items(await w1_client.get(LIST_URL, params={"sort": "tier"}))
    assert [p["domain"] for p in rows] == ["alpha.io"]


@pytest.mark.asyncio
async def test_unknown_sort_mode_is_422(w1_client):
    resp = await w1_client.get(LIST_URL, params={"sort": "cheapest"})
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# 3. Cursor pagination + total
# ---------------------------------------------------------------------------


# 9 rows spread across all four tiers, so the tier sort's bucket walk has to
# cross a bucket boundary mid-page (limit=2 over 3/2/2/2 buckets).
_TIER_CYCLE = ("a", "b", "c", "unqualified")
PAGE_ROWS = [
    {"domain": f"co-{i:02d}.io", "company": f"Company {i:02d}", "tier": _TIER_CYCLE[i % 4]}
    for i in range(9)
]


async def _drain(client: AsyncClient, params: dict[str, Any]) -> tuple[list[str], list[int]]:
    """Page through the whole result set. Returns the ids in page order plus
    the ``total`` seen on each page (so a test can assert it never moves)."""
    ids: list[str] = []
    totals: list[int] = []
    cursor: str | None = None
    for _ in range(20):  # loop guard — a non-terminating cursor is a bug
        page_params = dict(params)
        if cursor:
            page_params["cursor"] = cursor
        resp = await client.get(LIST_URL, params=page_params)
        assert resp.status_code == 200, resp.text
        page = resp.json()
        ids.extend(row["id"] for row in page["items"])
        totals.append(page["total"])
        cursor = page["next_cursor"]
        if cursor is None:
            return ids, totals
    raise AssertionError("cursor never terminated")


@pytest.mark.asyncio
@pytest.mark.parametrize("sort", ["newest", "oldest", "company", "tier"])
async def test_cursor_round_trips_with_no_overlap_and_no_gap(w1_client, sort):
    """Page 1 → cursor → page 2 … covers every row exactly once, in the same
    order a single unpaginated call would have produced."""
    await _seed(w1_client, PAGE_ROWS)

    one_shot = _items(await w1_client.get(LIST_URL, params={"sort": sort, "limit": 100}))
    assert len(one_shot) == 9

    paged_ids, totals = await _drain(w1_client, {"sort": sort, "limit": 2})
    assert paged_ids == [row["id"] for row in one_shot]  # no overlap, no gap
    assert len(set(paged_ids)) == 9
    assert totals == [9] * len(totals)  # total is stable across pages


@pytest.mark.asyncio
async def test_next_cursor_is_null_on_the_last_page(w1_client):
    await _seed(w1_client, PAGE_ROWS[:3])
    page = (await w1_client.get(LIST_URL, params={"limit": 3})).json()
    assert len(page["items"]) == 3
    assert page["next_cursor"] is None


@pytest.mark.asyncio
async def test_total_counts_the_filtered_set_not_the_page(w1_client):
    await _seed(w1_client, PAGE_ROWS)
    page = (await w1_client.get(LIST_URL, params={"limit": 2, "tier": "a"})).json()
    assert len(page["items"]) == 2
    assert page["total"] == 3  # 3 tier-a rows out of 9
    assert page["next_cursor"] is not None


@pytest.mark.asyncio
async def test_total_respects_the_q_filter(w1_client):
    await _seed(w1_client, PAGE_ROWS)
    page = (await w1_client.get(LIST_URL, params={"q": "Company 0"})).json()
    assert page["total"] == len(page["items"]) == 9  # Company 00..08


@pytest.mark.asyncio
async def test_paging_is_workspace_scoped(w1_client, w2_client):
    await _seed(w1_client, PAGE_ROWS[:4])
    await _seed(w2_client, PAGE_ROWS[:4])
    w1_ids, w1_totals = await _drain(w1_client, {"limit": 2})
    w2_ids, w2_totals = await _drain(w2_client, {"limit": 2})
    assert w1_totals[0] == w2_totals[0] == 4
    assert set(w1_ids).isdisjoint(set(w2_ids))


@pytest.mark.asyncio
async def test_a_cursor_from_another_sort_is_rejected(w1_client):
    """Changing the sort while holding a cursor would otherwise resume against
    a key that means something else — silently wrong rows."""
    await _seed(w1_client, PAGE_ROWS)
    page = (await w1_client.get(LIST_URL, params={"limit": 2, "sort": "newest"})).json()
    resp = await w1_client.get(
        LIST_URL, params={"limit": 2, "sort": "company", "cursor": page["next_cursor"]}
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "prospect.bad_cursor"


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["garbage", "newest:not-a-date|deadbeef", "newest:x|y"])
async def test_a_malformed_cursor_is_422_not_a_wrong_page(w1_client, bad):
    await _seed(w1_client, PAGE_ROWS[:2])
    resp = await w1_client.get(LIST_URL, params={"cursor": bad})
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "prospect.bad_cursor"


@pytest.mark.asyncio
async def test_a_cursor_from_another_workspace_leaks_nothing(w1_client, w2_client):
    """A stolen cursor is just a sort key — it cannot pull the other tenant's
    rows, because the workspace filter is applied independently."""
    await _seed(w1_client, PAGE_ROWS[:4])
    page = (await w1_client.get(LIST_URL, params={"limit": 2})).json()
    resp = await w2_client.get(LIST_URL, params={"limit": 2, "cursor": page["next_cursor"]})
    assert resp.status_code == 200
    assert resp.json() == {"items": [], "next_cursor": None, "total": 0}
