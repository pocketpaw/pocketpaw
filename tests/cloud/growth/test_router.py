# tests/cloud/growth/test_router.py — HTTP-layer tests for
# ``ee/cloud/growth/router.py``. Follows the cycles-router test pattern: build
# a FastAPI app with the growth router + a fixed RequestContext override per
# workspace (w1/w2 clients), mongomock-backed Beanie via the shared
# ``mongo_db`` fixture. Covers create/get/list/update wiring, list filters,
# the duplicate-domain 409, and — parametrized across get/update/list —
# cross-tenant isolation (foreign ids 404, foreign rows never listed).
#
# Created 2026-07-27 (feat/growth-g1): first slice of /growth.
# Updated 2026-07-27 (feat/growth-g2): bulk-ingestion coverage — POST /bulk
# idempotency (20 rows twice → second run all-updated), mixed-validity payloads
# (bad rows become indexed error entries, good rows land), the 501-row 422 cap,
# and cross-tenant scoping (bulk rows land only in the caller's workspace).

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pocketpaw_ee.cloud._core.context import RequestContext, ScopeKind, request_context
from pocketpaw_ee.cloud._core.http import add_error_handler
from pocketpaw_ee.cloud.growth.router import router as growth_router
from pocketpaw_ee.cloud.license import require_license


def _make_ctx(workspace_id: str | None, user_id: str = "u1") -> RequestContext:
    return RequestContext(
        user_id=user_id,
        workspace_id=workspace_id,
        request_id="test",
        scope=ScopeKind.WORKSPACE,
        started_at=datetime.now(UTC),
    )


def _build_app(workspace_id: str | None = "w1", user_id: str = "u1") -> FastAPI:
    app = FastAPI()
    add_error_handler(app)
    app.include_router(growth_router, prefix="/api/v1")

    async def _ctx() -> RequestContext:
        return _make_ctx(workspace_id, user_id)

    app.dependency_overrides[request_context] = _ctx
    app.dependency_overrides[require_license] = lambda: None
    return app


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


def _payload(**overrides: Any) -> dict[str, Any]:
    base = {
        "name": "Sam Founder",
        "company": "Acme Dental",
        "domain": "acme-dental.com",
        "source": "manual",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# CRUD wiring
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_returns_prospect_with_defaults(w1_client):
    resp = await w1_client.post("/api/v1/growth/prospects", json=_payload())
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["workspace_id"] == "w1"
    assert body["name"] == "Sam Founder"
    assert body["company"] == "Acme Dental"
    assert body["domain"] == "acme-dental.com"
    assert body["source"] == "manual"
    # Spec defaults.
    assert body["tier"] == "unqualified"
    assert body["status"] == "new"
    assert body["research_brief"] == ""
    assert body["emails"] == []
    assert body["linkedin_url"] is None
    assert body["whatsapp_number"] is None
    assert body["opted_in"] is False
    assert body["id"]
    assert body["created_at"]


@pytest.mark.asyncio
async def test_create_normalises_domain(w1_client):
    resp = await w1_client.post(
        "/api/v1/growth/prospects",
        json=_payload(domain="https://www.Acme-Dental.com/about"),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["domain"] == "acme-dental.com"


@pytest.mark.asyncio
async def test_create_duplicate_domain_is_409(w1_client):
    assert (await w1_client.post("/api/v1/growth/prospects", json=_payload())).status_code == 200
    resp = await w1_client.post("/api/v1/growth/prospects", json=_payload(name="Someone Else"))
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "prospect.domain_taken"


@pytest.mark.asyncio
async def test_create_rejects_bad_source(w1_client):
    resp = await w1_client.post("/api/v1/growth/prospects", json=_payload(source="scraped"))
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_get_roundtrips(w1_client):
    created = (await w1_client.post("/api/v1/growth/prospects", json=_payload())).json()
    resp = await w1_client.get(f"/api/v1/growth/prospects/{created['id']}")
    assert resp.status_code == 200
    fetched = resp.json()
    # Mongo truncates datetimes to milliseconds on persist, so the create
    # response (pre-persist, microsecond precision) differs in the timestamp
    # tail — compare everything else exactly.
    drop = {"created_at", "updated_at"}
    assert {k: v for k, v in fetched.items() if k not in drop} == {
        k: v for k, v in created.items() if k not in drop
    }


@pytest.mark.asyncio
async def test_update_patches_only_sent_fields(w1_client):
    created = (await w1_client.post("/api/v1/growth/prospects", json=_payload())).json()
    resp = await w1_client.patch(
        f"/api/v1/growth/prospects/{created['id']}",
        json={"tier": "a", "status": "qualified", "emails": ["sam@acme-dental.com"]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["tier"] == "a"
    assert body["status"] == "qualified"
    assert body["emails"] == ["sam@acme-dental.com"]
    # Untouched fields survive.
    assert body["name"] == "Sam Founder"
    assert body["domain"] == "acme-dental.com"


@pytest.mark.asyncio
async def test_update_rejects_bad_tier(w1_client):
    created = (await w1_client.post("/api/v1/growth/prospects", json=_payload())).json()
    resp = await w1_client.patch(f"/api/v1/growth/prospects/{created['id']}", json={"tier": "s"})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_list_filters_by_tier_status_source(w1_client):
    await w1_client.post("/api/v1/growth/prospects", json=_payload())
    await w1_client.post(
        "/api/v1/growth/prospects",
        json=_payload(domain="beta.io", company="Beta", tier="a", source="clay"),
    )
    await w1_client.post(
        "/api/v1/growth/prospects",
        json=_payload(domain="gamma.io", company="Gamma", tier="a", source="directory"),
    )

    resp = await w1_client.get("/api/v1/growth/prospects")
    assert resp.status_code == 200
    assert len(resp.json()) == 3

    resp = await w1_client.get("/api/v1/growth/prospects", params={"tier": "a"})
    assert {p["domain"] for p in resp.json()} == {"beta.io", "gamma.io"}

    resp = await w1_client.get("/api/v1/growth/prospects", params={"source": "clay"})
    assert [p["domain"] for p in resp.json()] == ["beta.io"]

    resp = await w1_client.get("/api/v1/growth/prospects", params={"status": "new"})
    assert len(resp.json()) == 3

    resp = await w1_client.get(
        "/api/v1/growth/prospects", params={"tier": "a", "source": "directory"}
    )
    assert [p["domain"] for p in resp.json()] == ["gamma.io"]


@pytest.mark.asyncio
async def test_list_rejects_unknown_filter_value(w1_client):
    resp = await w1_client.get("/api/v1/growth/prospects", params={"tier": "platinum"})
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Bulk ingestion — POST /bulk
# ---------------------------------------------------------------------------


def _bulk_rows(n: int, source: str = "clay") -> list[dict[str, Any]]:
    return [
        {
            "name": f"Contact {i}",
            "company": f"Company {i}",
            "domain": f"company-{i}.com",
            "source": source,
        }
        for i in range(n)
    ]


@pytest.mark.asyncio
async def test_bulk_ingest_is_idempotent(w1_client):
    """POSTing the same 20 rows twice: first run creates all, second run
    updates all — never duplicates (upsert keyed on workspace+domain)."""
    rows = _bulk_rows(20)

    first = await w1_client.post("/api/v1/growth/prospects/bulk", json={"rows": rows})
    assert first.status_code == 200, first.text
    assert first.json() == {"created": 20, "updated": 0, "errors": []}

    second = await w1_client.post("/api/v1/growth/prospects/bulk", json={"rows": rows})
    assert second.status_code == 200, second.text
    assert second.json() == {"created": 0, "updated": 20, "errors": []}

    listed = await w1_client.get("/api/v1/growth/prospects", params={"limit": 500})
    assert len(listed.json()) == 20


@pytest.mark.asyncio
async def test_bulk_ingest_mixed_validity_records_errors_and_lands_good_rows(w1_client):
    """A bad row becomes an indexed error entry; the rows around it land."""
    rows = [
        _payload(domain="alpha.io", company="Alpha"),
        _payload(domain="bad.io", source="scraped"),  # invalid enum → error at index 1
        {"name": "No Domain", "company": "Ghost", "source": "manual"},  # missing domain → index 2
        _payload(domain="omega.io", company="Omega"),
    ]

    resp = await w1_client.post("/api/v1/growth/prospects/bulk", json={"rows": rows})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["created"] == 2
    assert body["updated"] == 0
    assert [e["index"] for e in body["errors"]] == [1, 2]
    for err in body["errors"]:
        assert err["code"] == "prospect.invalid_row"
        assert err["message"]

    listed = await w1_client.get("/api/v1/growth/prospects")
    assert {p["domain"] for p in listed.json()} == {"alpha.io", "omega.io"}


@pytest.mark.asyncio
async def test_bulk_ingest_updates_existing_and_creates_new_in_one_call(w1_client):
    """A payload mixing known and new domains splits into updated + created."""
    await w1_client.post("/api/v1/growth/prospects", json=_payload(domain="alpha.io"))

    rows = [
        _payload(domain="alpha.io", name="Refreshed Contact", tier="a"),
        _payload(domain="brand-new.io", company="Brand New"),
    ]
    resp = await w1_client.post("/api/v1/growth/prospects/bulk", json={"rows": rows})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"created": 1, "updated": 1, "errors": []}

    listed = (await w1_client.get("/api/v1/growth/prospects")).json()
    by_domain = {p["domain"]: p for p in listed}
    assert by_domain["alpha.io"]["name"] == "Refreshed Contact"
    assert by_domain["alpha.io"]["tier"] == "a"


@pytest.mark.asyncio
async def test_bulk_ingest_rejects_more_than_500_rows(w1_client):
    resp = await w1_client.post("/api/v1/growth/prospects/bulk", json={"rows": _bulk_rows(501)})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_bulk_ingest_is_workspace_scoped(w1_client, w2_client):
    """Bulk rows land only in the caller's workspace: w2 sees none of w1's
    ingested rows, and the same domains ingested by w2 create fresh rows."""
    rows = _bulk_rows(3)
    resp = await w1_client.post("/api/v1/growth/prospects/bulk", json={"rows": rows})
    assert resp.json() == {"created": 3, "updated": 0, "errors": []}

    # w2 sees nothing from w1's ingest.
    assert (await w2_client.get("/api/v1/growth/prospects")).json() == []

    # The same domains in w2 are creates (fresh rows), not cross-tenant updates.
    resp = await w2_client.post("/api/v1/growth/prospects/bulk", json={"rows": rows})
    assert resp.json() == {"created": 3, "updated": 0, "errors": []}

    w1_listed = (await w1_client.get("/api/v1/growth/prospects")).json()
    w2_listed = (await w2_client.get("/api/v1/growth/prospects")).json()
    assert len(w1_listed) == 3 and len(w2_listed) == 3
    assert {p["id"] for p in w1_listed}.isdisjoint({p["id"] for p in w2_listed})


# ---------------------------------------------------------------------------
# Tenancy — a foreign workspace's ids 404, its rows never list.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("op", ["get", "update", "list"])
async def test_cross_tenant_access_is_isolated(w1_client, w2_client, op):
    """A prospect created in w1 is invisible to w2: GET and PATCH by id 404
    (existence never leaks), and w2's list never contains it."""
    created = (await w1_client.post("/api/v1/growth/prospects", json=_payload())).json()

    if op == "get":
        resp = await w2_client.get(f"/api/v1/growth/prospects/{created['id']}")
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "prospect.not_found"
    elif op == "update":
        resp = await w2_client.patch(
            f"/api/v1/growth/prospects/{created['id']}", json={"tier": "a"}
        )
        assert resp.status_code == 404
        # The cross-tenant PATCH must not have mutated the row.
        same = (await w1_client.get(f"/api/v1/growth/prospects/{created['id']}")).json()
        assert same["tier"] == "unqualified"
    else:  # list
        resp = await w2_client.get("/api/v1/growth/prospects")
        assert resp.status_code == 200
        assert resp.json() == []
