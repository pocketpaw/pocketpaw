# tests/cloud/growth/test_outreach_brand.py — outreach rows (prospects, ICPs,
# drafts) belonging to a brand (a Growth › Social ``SocialProfile``).
#
# Covers: the optional ``profile_id`` filter on every outreach list / queue
# route, with the absent param returning every row as before; creates storing
# the brand (drafts inheriting their prospect's); a foreign brand refused on
# create; the set-only upsert; the mapper round-trip; and the
# ``scripts/backfill_growth_profile_id.py`` backfill (dry-run writes nothing,
# oldest brand wins, a brand is created when none exists, idempotent, never
# crosses workspaces, never touches a branded row). Reuses the
# ``test_router.py`` app harness (mongomock-backed Beanie).

from __future__ import annotations

import importlib.util
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from beanie import PydanticObjectId
from httpx import ASGITransport, AsyncClient
from pocketpaw_ee.cloud.models.draft import Draft as DraftDoc
from pocketpaw_ee.cloud.models.icp import Icp as IcpDoc
from pocketpaw_ee.cloud.models.prospect import Prospect as ProspectDoc
from pocketpaw_ee.cloud.models.social_profile import SocialProfile
from pocketpaw_ee.cloud.models.workspace import Workspace

from tests.cloud.growth.test_router import _build_app, _payload

API = "/api/v1/growth"
_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "backfill_growth_profile_id.py"


@pytest_asyncio.fixture
async def w1(mongo_db: Any) -> AsyncClient:
    async with AsyncClient(
        transport=ASGITransport(app=_build_app(workspace_id="w1")), base_url="http://t"
    ) as client:
        yield client


async def _brand(workspace: str, name: str, created_at: datetime | None = None) -> str:
    doc = SocialProfile(workspace=workspace, company_name=name)
    if created_at is not None:
        doc.createdAt = created_at
    await doc.insert()
    return str(doc.id)


async def _seed_branded(client: AsyncClient) -> dict[str, Any]:
    """Two brands in w1 plus a pre-brand row of every kind; drafts are moved to
    ``proposed`` so they show in the queues."""
    a = await _brand("w1", "Brand A")
    b = await _brand("w1", "Brand B")
    ids: dict[str, Any] = {"a": a, "b": b}
    for key, profile in (("a", a), ("b", b), ("none", None)):
        body = _payload(
            domain=f"{key}.io",
            emails=[f"x@{key}.io"],
            linkedin_url=f"https://linkedin.com/in/{key}",
        )
        if profile:
            body["profile_id"] = profile
        resp = await client.post(f"{API}/prospects", json=body)
        assert resp.status_code == 200, resp.text
        pid = resp.json()["id"]
        icp = {"name": f"icp {key}", "criteria": "dentists"}
        if profile:
            icp["profile_id"] = profile
        assert (await client.post(f"{API}/icps", json=icp)).status_code == 200
        for channel in ("email", "linkedin"):
            draft = {"channel": channel, "body": f"hi {key}"}
            if channel == "email":
                draft["subject"] = "hello"
            resp = await client.post(f"{API}/prospects/{pid}/drafts", json=draft)
            assert resp.status_code == 200, resp.text
    await DraftDoc.get_pymongo_collection().update_many({}, {"$set": {"status": "proposed"}})
    return ids


def _brands_of(route: str, body: Any) -> list[str | None]:
    """The profile_id of every row a list route returned."""
    if route == "/prospects":
        return [r["profile_id"] for r in body["items"]]
    if route in ("/linkedin/queue", "/queue/email"):
        return [r["draft"]["profile_id"] for r in body]
    return [r["profile_id"] for r in body]


LIST_ROUTES = ["/prospects", "/icps", "/drafts", "/linkedin/queue", "/queue/email"]


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("route", LIST_ROUTES)
async def test_profile_filter_scopes_every_list_route(w1, route):
    ids = await _seed_branded(w1)
    resp = await w1.get(f"{API}{route}", params={"profile_id": ids["a"]})
    assert resp.status_code == 200, resp.text
    brands = _brands_of(route, resp.json())
    assert brands and set(brands) == {ids["a"]}


@pytest.mark.asyncio
@pytest.mark.parametrize("route", LIST_ROUTES)
async def test_absent_profile_param_returns_every_row(w1, route):
    ids = await _seed_branded(w1)
    resp = await w1.get(f"{API}{route}")
    assert resp.status_code == 200, resp.text
    assert set(_brands_of(route, resp.json())) == {ids["a"], ids["b"], None}


@pytest.mark.asyncio
@pytest.mark.parametrize("route", LIST_ROUTES)
async def test_empty_profile_param_selects_unbranded_rows(w1, route):
    await _seed_branded(w1)
    resp = await w1.get(f"{API}{route}", params={"profile_id": ""})
    assert resp.status_code == 200, resp.text
    brands = _brands_of(route, resp.json())
    assert brands and set(brands) == {None}


@pytest.mark.asyncio
async def test_markdown_linkedin_queue_is_scoped_too(w1):
    ids = await _seed_branded(w1)
    resp = await w1.get(f"{API}/linkedin/queue", params={"format": "md", "profile_id": ids["b"]})
    assert resp.status_code == 200
    assert "in/b)" in resp.text
    assert "in/a)" not in resp.text and "in/none)" not in resp.text


@pytest.mark.asyncio
async def test_facets_count_only_the_brand(w1):
    ids = await _seed_branded(w1)
    scoped = (await w1.get(f"{API}/prospects/facets", params={"profile_id": ids["a"]})).json()
    everything = (await w1.get(f"{API}/prospects/facets")).json()
    assert sum(scoped["tier"].values()) == 1
    assert sum(everything["tier"].values()) == 3


# ---------------------------------------------------------------------------
# Creates store the brand
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_new_rows_store_the_brand(w1):
    brand = await _brand("w1", "Brand A")
    p = (await w1.post(f"{API}/prospects", json=_payload(profile_id=brand))).json()
    assert p["profile_id"] == brand
    assert (await ProspectDoc.get(PydanticObjectId(p["id"]))).profile_id == brand

    icp = (
        await w1.post(f"{API}/icps", json={"name": "n", "criteria": "c", "profile_id": brand})
    ).json()
    assert icp["profile_id"] == brand
    assert (await IcpDoc.get(PydanticObjectId(icp["id"]))).profile_id == brand

    # A draft with no brand of its own inherits its prospect's.
    d = (
        await w1.post(
            f"{API}/prospects/{p['id']}/drafts", json={"channel": "linkedin", "body": "x"}
        )
    ).json()
    assert d["profile_id"] == brand
    assert (await DraftDoc.get(PydanticObjectId(d["id"]))).profile_id == brand


@pytest.mark.asyncio
async def test_a_draft_can_name_its_own_brand(w1):
    a = await _brand("w1", "A")
    b = await _brand("w1", "B")
    p = (await w1.post(f"{API}/prospects", json=_payload(profile_id=a))).json()
    d = await w1.post(
        f"{API}/prospects/{p['id']}/drafts",
        json={"channel": "linkedin", "body": "x", "profile_id": b},
    )
    assert d.json()["profile_id"] == b


@pytest.mark.asyncio
async def test_bulk_rows_store_the_brand_and_upsert_never_unbrands(w1):
    brand = await _brand("w1", "A")
    rows = [_payload(domain="one.io", profile_id=brand), _payload(domain="two.io")]
    resp = await w1.post(f"{API}/prospects/bulk", json={"rows": rows})
    assert resp.json()["created"] == 2
    one = await ProspectDoc.find_one({"workspace": "w1", "domain": "one.io"})
    assert one.profile_id == brand
    # A re-import that names no brand keeps the one already set.
    await w1.post(f"{API}/prospects/bulk", json={"rows": [_payload(domain="one.io")]})
    one = await ProspectDoc.find_one({"workspace": "w1", "domain": "one.io"})
    assert one.profile_id == brand


@pytest.mark.asyncio
async def test_another_workspaces_brand_is_refused_on_create(w1):
    foreign = await _brand("w2", "Theirs")
    assert (await w1.post(f"{API}/prospects", json=_payload(profile_id=foreign))).status_code == 404
    assert (
        await w1.post(f"{API}/icps", json={"name": "n", "criteria": "c", "profile_id": foreign})
    ).status_code == 404
    p = (await w1.post(f"{API}/prospects", json=_payload())).json()
    assert (
        await w1.post(
            f"{API}/prospects/{p['id']}/drafts",
            json={"channel": "linkedin", "body": "x", "profile_id": foreign},
        )
    ).status_code == 404
    bulk = await w1.post(
        f"{API}/prospects/bulk", json={"rows": [_payload(domain="z.io", profile_id=foreign)]}
    )
    assert bulk.json()["created"] == 0 and bulk.json()["errors"][0]["index"] == 0
    assert await ProspectDoc.find({"profile_id": foreign}).count() == 0


# ---------------------------------------------------------------------------
# Mapper round-trip: fails if any hop drops the field
# ---------------------------------------------------------------------------


def test_mappers_carry_profile_id():
    from pocketpaw_ee.cloud.growth import service as s

    p = ProspectDoc(workspace="w1", domain="a.io", source="manual", profile_id="brand-1")
    assert s._to_response(s._to_domain(p)).profile_id == "brand-1"
    i = IcpDoc(workspace="w1", name="n", criteria="c", profile_id="brand-1")
    assert s._icp_to_response(s._icp_to_domain(i)).profile_id == "brand-1"
    d = DraftDoc(workspace="w1", prospect_id="p", channel="email", body="b", profile_id="brand-1")
    assert s._draft_to_response(s._draft_to_domain(d)).profile_id == "brand-1"


# ---------------------------------------------------------------------------
# Backfill script
# ---------------------------------------------------------------------------


def _script():
    spec = importlib.util.spec_from_file_location("backfill_growth_profile_id", _SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def _legacy_rows(workspace: str) -> None:
    """Pre-brand rows as they really sit in Mongo: no ``profile_id`` key."""
    await ProspectDoc.get_pymongo_collection().insert_one(
        {"workspace": workspace, "domain": f"{workspace}.io", "source": "manual"}
    )
    await IcpDoc.get_pymongo_collection().insert_one(
        {"workspace": workspace, "name": "n", "criteria": "c"}
    )
    await DraftDoc.get_pymongo_collection().insert_one(
        {"workspace": workspace, "prospect_id": "p", "channel": "email", "body": "b"}
    )


async def _brands_in(workspace: str) -> set[Any]:
    out: set[Any] = set()
    for model in (ProspectDoc, IcpDoc, DraftDoc):
        async for row in model.get_pymongo_collection().find({"workspace": workspace}):
            out.add(row.get("profile_id"))
    return out


@pytest.mark.asyncio
async def test_dry_run_changes_nothing(mongo_db):
    await _legacy_rows("w1")  # no brand: dry-run would create one
    await _legacy_rows("w2")
    await _brand("w2", "B")
    before = await SocialProfile.find_all().count()

    report = await _script().backfill(dry_run=True)

    assert report["w1"]["created"] and report["w1"]["profile_id"] is None
    assert report["w1"]["counts"] == {"prospects": 1, "icps": 1, "drafts": 1}
    assert await SocialProfile.find_all().count() == before
    assert await _brands_in("w1") == {None}
    assert await _brands_in("w2") == {None}


@pytest.mark.asyncio
async def test_backfill_assigns_the_oldest_brand_per_workspace(mongo_db):
    now = datetime.now(UTC)
    # w2's brand is the oldest anywhere: it must never reach w1's rows.
    w2_brand = await _brand("w2", "Theirs", now - timedelta(days=30))
    # w1's oldest is inserted LAST, so _id order and createdAt order disagree.
    await _brand("w1", "Newer", now - timedelta(days=1))
    w1_oldest = await _brand("w1", "Older", now - timedelta(days=10))
    await _legacy_rows("w1")
    await _legacy_rows("w2")

    await _script().backfill(dry_run=False)

    assert await _brands_in("w1") == {w1_oldest}
    assert await _brands_in("w2") == {w2_brand}


@pytest.mark.asyncio
async def test_backfill_creates_a_brand_named_after_the_workspace(mongo_db):
    ws = Workspace(name="Acme Labs", slug="acme-labs", owner="u1")
    await ws.insert()
    wid = str(ws.id)
    await _legacy_rows(wid)

    report = await _script().backfill(dry_run=False)

    profiles = await SocialProfile.find({"workspace": wid}).to_list()
    assert len(profiles) == 1 and profiles[0].company_name == "Acme Labs"
    assert profiles[0].active is False
    assert report[wid]["created"] is True
    assert await _brands_in(wid) == {str(profiles[0].id)}


@pytest.mark.asyncio
async def test_backfill_is_idempotent_and_leaves_branded_rows_alone(mongo_db):
    await _legacy_rows("w1")
    keep = await _brand("w1", "Kept")
    other = await _brand("w1", "Other")
    await ProspectDoc.get_pymongo_collection().insert_one(
        {"workspace": "w1", "domain": "branded.io", "source": "manual", "profile_id": other}
    )
    module = _script()

    await module.backfill(dry_run=False)
    first = await _brands_in("w1")
    second_report = await module.backfill(dry_run=False)

    assert second_report == {}
    assert await _brands_in("w1") == first == {keep, other}
    branded = await ProspectDoc.get_pymongo_collection().find_one({"domain": "branded.io"})
    assert branded["profile_id"] == other
    assert await SocialProfile.find({"workspace": "w1"}).count() == 2
