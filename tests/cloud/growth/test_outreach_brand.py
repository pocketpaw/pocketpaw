# tests/cloud/growth/test_outreach_brand.py — outreach rows (prospects, ICPs,
# drafts) belonging to a brand (a Growth › Social ``SocialProfile``).
#
# Covers: the optional ``profile_id`` filter on every outreach list / queue
# route, an absent or blank param returning every row (legacy null-brand rows
# included, and they still load); every create stamping a brand (named and
# validated, else inherited by a draft from its prospect, else the workspace's
# oldest brand, else a new one named after the workspace), on the router and
# the system seams (discovery, follow-ups); a foreign brand refused on create;
# the set-only upsert; the mapper round-trip; and the
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
    """Two brands in w1, a row of every kind under each, plus a legacy row of
    every kind with no ``profile_id`` key (the API can no longer write one);
    drafts are moved to ``proposed`` so they show in the queues."""
    a = await _brand("w1", "Brand A")
    b = await _brand("w1", "Brand B")
    ids: dict[str, Any] = {"a": a, "b": b}
    for key, profile in (("a", a), ("b", b)):
        body = _payload(
            domain=f"{key}.io",
            emails=[f"x@{key}.io"],
            linkedin_url=f"https://linkedin.com/in/{key}",
            profile_id=profile,
        )
        resp = await client.post(f"{API}/prospects", json=body)
        assert resp.status_code == 200, resp.text
        pid = resp.json()["id"]
        icp = {"name": f"icp {key}", "criteria": "dentists", "profile_id": profile}
        assert (await client.post(f"{API}/icps", json=icp)).status_code == 200
        for channel in ("email", "linkedin"):
            draft = {"channel": channel, "body": f"hi {key}"}
            if channel == "email":
                draft["subject"] = "hello"
            resp = await client.post(f"{API}/prospects/{pid}/drafts", json=draft)
            assert resp.status_code == 200, resp.text
    await _legacy_rows("w1", domain="none.io")
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
@pytest.mark.parametrize("param", [{}, {"profile_id": ""}], ids=["absent", "blank"])
async def test_absent_or_blank_profile_param_returns_every_row(w1, route, param):
    ids = await _seed_branded(w1)
    resp = await w1.get(f"{API}{route}", params=param)
    assert resp.status_code == 200, resp.text
    # The legacy null-brand rows load and list alongside the branded ones.
    assert set(_brands_of(route, resp.json())) == {ids["a"], ids["b"], None}


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
    await _brand("w1", "Oldest")  # the default, so inheriting is told apart from it
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
async def test_upsert_never_moves_a_prospect_to_another_brand(w1):
    a = await _brand("w1", "A")
    b = await _brand("w1", "B")
    await w1.post(f"{API}/prospects/bulk", json={"rows": [_payload(domain="one.io", profile_id=a)]})
    # A second brand's import (or hunt / pulse add) naming the same domain keeps brand A.
    await w1.post(f"{API}/prospects/bulk", json={"rows": [_payload(domain="one.io", profile_id=b)]})
    one = await ProspectDoc.find_one({"workspace": "w1", "domain": "one.io"})
    assert one.profile_id == a


@pytest.mark.asyncio
async def test_upsert_fills_the_brand_on_a_legacy_unbranded_row(w1):
    a = await _brand("w1", "A")
    await ProspectDoc(workspace="w1", domain="old.io", source="manual", profile_id=None).insert()
    await w1.post(f"{API}/prospects/bulk", json={"rows": [_payload(domain="old.io", profile_id=a)]})
    old = await ProspectDoc.find_one({"workspace": "w1", "domain": "old.io"})
    assert old.profile_id == a


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


@pytest.mark.asyncio
async def test_a_blank_brand_gets_the_default_brand(w1):
    brand = await _brand("w1", "Only")
    p = await w1.post(f"{API}/prospects", json=_payload(profile_id=""))
    icp = await w1.post(f"{API}/icps", json={"name": "n", "criteria": "c", "profile_id": " "})
    assert p.status_code == icp.status_code == 200, (p.text, icp.text)
    assert (await ProspectDoc.get(PydanticObjectId(p.json()["id"]))).profile_id == brand
    assert (await IcpDoc.get(PydanticObjectId(icp.json()["id"]))).profile_id == brand


@pytest.mark.asyncio
async def test_a_create_with_no_brand_takes_the_oldest_brand(w1):
    now = datetime.now(UTC)
    await _brand("w2", "Theirs", now - timedelta(days=30))  # never another workspace's
    await _brand("w1", "Newer", now - timedelta(days=1))
    oldest = await _brand("w1", "Older", now - timedelta(days=10))  # inserted last
    archived = SocialProfile(workspace="w1", company_name="Archived")
    archived.createdAt = now - timedelta(days=99)
    archived.archived_at = now
    await archived.insert()

    p = (await w1.post(f"{API}/prospects", json=_payload())).json()
    icp = (await w1.post(f"{API}/icps", json={"name": "n", "criteria": "c"})).json()
    d = (
        await w1.post(
            f"{API}/prospects/{p['id']}/drafts", json={"channel": "linkedin", "body": "x"}
        )
    ).json()

    assert p["profile_id"] == icp["profile_id"] == d["profile_id"] == oldest
    for doc_cls, row_id in ((ProspectDoc, p["id"]), (IcpDoc, icp["id"]), (DraftDoc, d["id"])):
        assert (await doc_cls.get(PydanticObjectId(row_id))).profile_id == oldest


@pytest.mark.asyncio
async def test_a_create_in_a_workspace_with_no_brand_creates_one(mongo_db):
    ws = Workspace(name="Acme Labs", slug="acme-labs", owner="u1")
    await ws.insert()
    wid = str(ws.id)
    async with AsyncClient(
        transport=ASGITransport(app=_build_app(workspace_id=wid)), base_url="http://t"
    ) as client:
        p = (await client.post(f"{API}/prospects", json=_payload())).json()
        icp = (await client.post(f"{API}/icps", json={"name": "n", "criteria": "c"})).json()

    (profile,) = await SocialProfile.find({"workspace": wid}).to_list()
    assert profile.company_name == "Acme Labs" and profile.active is False
    assert p["profile_id"] == icp["profile_id"] == str(profile.id)


@pytest.mark.asyncio
async def test_legacy_rows_get_the_default_brand_on_the_system_seams(w1):
    """Discovery under a legacy (null-brand) hunt and a follow-up for a legacy
    prospect both write their new rows under the workspace's default brand."""
    from pocketpaw_ee.cloud.growth import service as growth_service
    from pocketpaw_ee.cloud.growth.discovery import run_discovery
    from pocketpaw_ee.cloud.growth.dto import CreateDraftRequest

    from tests.cloud.growth.test_discovery import FakeResearch, _company

    brand = await _brand("w1", "Default")
    await _legacy_rows("w1")
    icp = await IcpDoc.get_pymongo_collection().find_one({"workspace": "w1"})
    prospect = await ProspectDoc.get_pymongo_collection().find_one({"workspace": "w1"})

    await run_discovery("w1", str(icp["_id"]), FakeResearch(_company("found.io")))
    found = await ProspectDoc.find_one({"workspace": "w1", "domain": "found.io"})
    assert found is not None and found.profile_id == brand

    draft = await growth_service.create_followup_draft(
        "w1",
        str(prospect["_id"]),
        CreateDraftRequest(channel="linkedin", body="Following up", variant="follow_up"),
    )
    assert draft.profile_id == brand


@pytest.mark.asyncio
async def test_a_brand_filter_never_reads_another_workspace(w1):
    brand = await _brand("w1", "A")
    mine = (await w1.post(f"{API}/prospects", json=_payload(profile_id=brand))).json()
    # A w2 row carrying w1's brand id (it cannot get there via the API).
    await ProspectDoc.get_pymongo_collection().insert_one(
        {"workspace": "w2", "domain": "theirs.io", "source": "manual", "profile_id": brand}
    )
    listed = (await w1.get(f"{API}/prospects", params={"profile_id": brand})).json()["items"]
    assert [r["id"] for r in listed] == [mine["id"]]


@pytest.mark.asyncio
async def test_discovery_files_prospects_under_the_hunts_brand(w1):
    from pocketpaw_ee.cloud.growth.discovery import run_discovery

    from tests.cloud.growth.test_discovery import FakeResearch, _company

    await _brand("w1", "Oldest")  # the default, so the hunt's brand is told apart
    brand = await _brand("w1", "A")
    icp = (
        await w1.post(f"{API}/icps", json={"name": "n", "criteria": "c", "profile_id": brand})
    ).json()
    await run_discovery("w1", icp["id"], FakeResearch(_company("found.io")))
    row = await ProspectDoc.find_one({"workspace": "w1", "domain": "found.io"})
    assert row is not None and row.profile_id == brand


@pytest.mark.asyncio
async def test_pulse_add_as_prospects_files_under_the_hunts_brand(w1):
    from pocketpaw_ee.cloud.growth.social.pulse import (
        PulseResult,
        PulseSource,
        set_production_pulse_fn,
    )

    from tests.cloud.growth.social.test_hunt_pulse import _theme

    await _brand("w1", "Oldest")  # the default, so the hunt's brand is told apart
    brand = await _brand("w1", "A")
    icp = (
        await w1.post(f"{API}/icps", json={"name": "n", "criteria": "c", "profile_id": brand})
    ).json()

    async def build(brief, sources):
        return PulseResult(
            sources=(PulseSource("reddit", "subreddit", "agency"),), themes=(_theme(),)
        )

    set_production_pulse_fn(build)
    try:
        url = f"{API}/icps/{icp['id']}/pulse"
        assert (await w1.post(f"{url}/refresh", json={})).status_code == 200
        assert (await w1.post(f"{url}/prospects", json={"theme": 0})).status_code == 200
    finally:
        set_production_pulse_fn(None)
    row = await ProspectDoc.find_one({"workspace": "w1", "domain": "acme-agency.com"})
    assert row is not None and row.profile_id == brand


# ---------------------------------------------------------------------------
# Mapper round-trip: fails if any hop drops the field
# ---------------------------------------------------------------------------


def test_mappers_carry_profile_id():
    from pocketpaw_ee.cloud.growth import service as s

    p = ProspectDoc(workspace="w1", domain="a.io", source="manual", profile_id="brand-1")
    assert s._to_response(s._to_domain(p)).profile_id == "brand-1"
    i = IcpDoc(workspace="w1", name="n", criteria="c", profile_id="brand-1")
    assert s._icp_to_response(s._icp_to_domain(i)).profile_id == "brand-1"
    d = DraftDoc(
        workspace="w1",
        prospect_id="p",
        channel="email",
        body="b",
        profile_id="brand-1",
        blueprint_version=3,
    )
    out = s._draft_to_response(s._draft_to_domain(d))
    assert (out.profile_id, out.blueprint_version) == ("brand-1", 3)


# ---------------------------------------------------------------------------
# Backfill script
# ---------------------------------------------------------------------------


def _script():
    spec = importlib.util.spec_from_file_location("backfill_growth_profile_id", _SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def _legacy_rows(workspace: str, domain: str | None = None) -> None:
    """Pre-brand rows as they really sit in Mongo: no ``profile_id`` key. With a
    ``domain``, the prospect is reachable on email and LinkedIn and owns one draft
    per channel (so it shows in the queues); without, the draft's prospect is a
    dangling ref, as the backfill tests want."""
    prospect: dict[str, Any] = {
        "workspace": workspace,
        "domain": domain or f"{workspace}.io",
        "source": "manual",
        "tier": "unqualified",
        "status": "new",
    }
    if domain:
        prospect["emails"] = [f"x@{domain}"]
        prospect["linkedin_url"] = f"https://linkedin.com/in/{domain.split('.')[0]}"
    pid = (await ProspectDoc.get_pymongo_collection().insert_one(prospect)).inserted_id
    await IcpDoc.get_pymongo_collection().insert_one(
        {"workspace": workspace, "name": "n", "criteria": "c"}
    )
    drafts = (
        [{"channel": "email", "subject": "hello"}, {"channel": "linkedin"}]
        if domain
        else [{"channel": "email"}]
    )
    for extra in drafts:
        await DraftDoc.get_pymongo_collection().insert_one(
            {
                "workspace": workspace,
                "prospect_id": str(pid) if domain else "p",
                "body": "b",
                **extra,
            }
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
    assert report["w1"]["counts"] == {
        "prospects": 1,
        "icps": 1,
        "drafts": 1,
        "drafts_from_prospect": 0,
    }
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


@pytest.mark.asyncio
async def test_backfill_gives_a_draft_its_prospects_brand_first(mongo_db):
    now = datetime.now(UTC)
    oldest = await _brand("w1", "Oldest", now - timedelta(days=10))
    prospect_brand = await _brand("w1", "Prospect's", now - timedelta(days=1))
    branded = await ProspectDoc.get_pymongo_collection().insert_one(
        {"workspace": "w1", "domain": "b.io", "source": "manual", "profile_id": prospect_brand}
    )
    bare = await ProspectDoc.get_pymongo_collection().insert_one(
        {"workspace": "w1", "domain": "n.io", "source": "manual"}
    )
    drafts = DraftDoc.get_pymongo_collection()
    for pid in (branded.inserted_id, bare.inserted_id):
        await drafts.insert_one(
            {"workspace": "w1", "prospect_id": str(pid), "channel": "email", "body": "b"}
        )

    module = _script()
    dry = await module.backfill(dry_run=True)
    assert dry["w1"]["counts"]["drafts_from_prospect"] == 1
    assert await drafts.count_documents({"profile_id": None}) == 2

    await module.backfill(dry_run=False)
    by_prospect = {d["prospect_id"]: d["profile_id"] async for d in drafts.find({})}
    assert by_prospect == {str(branded.inserted_id): prospect_brand, str(bare.inserted_id): oldest}


@pytest.mark.asyncio
async def test_backfill_never_takes_a_brand_from_another_workspaces_prospect(mongo_db):
    brand = await _brand("w1", "Only")
    pid = (
        await ProspectDoc.get_pymongo_collection().insert_one(
            {"workspace": "w1", "domain": "b.io", "source": "manual", "profile_id": brand}
        )
    ).inserted_id
    # A w2 draft pointing at a w1 prospect must NOT take w1's brand.
    await DraftDoc.get_pymongo_collection().insert_one(
        {"workspace": "w2", "prospect_id": str(pid), "channel": "email", "body": "b"}
    )
    await _script().backfill(dry_run=False)
    row = await DraftDoc.get_pymongo_collection().find_one({"workspace": "w2"})
    assert row["profile_id"] not in (None, brand)


def test_the_printed_uri_carries_no_credentials():
    safe = _script()._safe_uri(
        "mongodb://admin:s3cret@db.example:27017,db2:27017/paw-cloud?authSource=admin"
    )
    assert safe == "db.example:27017,db2:27017/paw-cloud"
    assert "s3cret" not in safe and "admin" not in safe
