# test_fabric_self_serve_analysis.py — self-serve-analysis Slice 1 (read engine).
# Created: 2026-07-11 (feat/self-serve-analysis-s1) — covers the flag-gated SQL
# aggregation + reasoning-steps path on FabricStore.query / POST /fabric/query:
#   1. FLAG GATE: POCKETPAW_FABRIC_ANALYST off (the default) -> an aggregation
#      query raises FabricAnalystDisabledError at the store and 422
#      fabric.analyst_disabled over HTTP; plain queries pass either way.
#   2. CORRECTNESS: group-by count and sum/avg/min/max fold the right numbers;
#      numeric folds CAST to REAL (JSON-string numbers still add up).
#   3. SCOPE-THEN-AGGREGATE: a cross-workspace object is NEVER counted into a
#      group total (the W4a scope applies BEFORE grouping).
#   4. RANGES: RangeBucket bucketing (explicit + derived labels, min-inclusive /
#      max-exclusive, out-of-bucket and missing values dropped).
#   5. BACK-COMPAT: plain queries return objects exactly as before with
#      aggregates/steps None (null on the wire).
#   6. WIRE CONTRACT: steps serialize as exactly {title, detail, status} — the
#      QueryPlanStep shape ripple's ReasoningTrace consumes, never {label, count}.
#   7. MODEL VALIDATION: inconsistent aggregation field combinations are
#      rejected at the FabricQuery boundary; group_by alone defaults to count.
#
# Store tests drive the real FabricStore against a tmp SQLite file; the HTTP
# tests wire the real EE router + real RBAC with an isolated per-workspace
# store (same harness as test_fabric_ontology_operator).

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pocketpaw_ee.fabric.router as fabric_router_module
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pocketpaw_ee.cloud._core.deps import current_workspace_id
from pocketpaw_ee.cloud._core.http import add_error_handler
from pocketpaw_ee.cloud.auth import current_active_user
from pocketpaw_ee.cloud.license import require_license

from pocketpaw.config import get_settings
from pocketpaw.fabric.models import (
    FabricAnalystDisabledError,
    FabricQuery,
    RangeBucket,
)
from pocketpaw.fabric.store import FabricStore

WS_A = "ws-alpha"
WS_B = "ws-bravo"


@pytest.fixture(autouse=True)
def _fresh_settings(monkeypatch: pytest.MonkeyPatch):
    """Every test starts and ends with a pristine settings cache (flag off)."""
    monkeypatch.delenv("POCKETPAW_FABRIC_ANALYST", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def analyst_on(monkeypatch: pytest.MonkeyPatch):
    """Turn the POCKETPAW_FABRIC_ANALYST flag ON for one test."""
    monkeypatch.setenv("POCKETPAW_FABRIC_ANALYST", "true")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def store(tmp_path: Path) -> FabricStore:
    return FabricStore(tmp_path / "analysis.db")


async def _seed_products(store: FabricStore) -> None:
    """Products across two workspaces.

    WS_A: 4 products — categories a/a/b plus one with NO category;
    WS_B: 1 product in category b (the cross-tenant canary).
    """
    t = await store.define_type(name="Product", properties=[])
    rows = [
        ({"category": "a", "stock": 5, "price": 10}, WS_A),
        ({"category": "a", "stock": 3, "price": "20"}, WS_A),  # JSON-string number
        ({"category": "b", "stock": 50, "price": 30}, WS_A),
        ({"stock": 1, "price": 5}, WS_A),  # no category
        ({"category": "b", "stock": 2, "price": 1000}, WS_B),
    ]
    for props, ws in rows:
        await store.create_object(t.id, props, workspace_id=ws)


# ---------------------------------------------------------------------------
# Flag gate
# ---------------------------------------------------------------------------


class TestFlagGate:
    @pytest.mark.asyncio
    async def test_flag_off_rejects_aggregation(self, store: FabricStore) -> None:
        await _seed_products(store)
        with pytest.raises(FabricAnalystDisabledError, match="POCKETPAW_FABRIC_ANALYST"):
            await store.query(FabricQuery(type_name="Product", group_by="category"))

    @pytest.mark.asyncio
    async def test_flag_off_plain_query_unaffected(self, store: FabricStore) -> None:
        await _seed_products(store)
        result = await store.query(FabricQuery(type_name="Product"), workspace_id=WS_A)
        assert result.total == 4
        assert len(result.objects) == 4

    @pytest.mark.asyncio
    async def test_flag_on_allows_aggregation(self, analyst_on, store: FabricStore) -> None:
        await _seed_products(store)
        result = await store.query(
            FabricQuery(type_name="Product", group_by="category"), workspace_id=WS_A
        )
        assert result.aggregates is not None


# ---------------------------------------------------------------------------
# Aggregation correctness (flag on)
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("analyst_on")
class TestAggregationCorrectness:
    @pytest.mark.asyncio
    async def test_group_by_count(self, store: FabricStore) -> None:
        await _seed_products(store)
        result = await store.query(
            FabricQuery(type_name="Product", group_by="category"), workspace_id=WS_A
        )
        assert result.objects == []  # analysis read, not a fetch
        assert result.total == 4  # scoped+filtered rows, incl. the no-category one
        assert {r["key"]: r["value"] for r in result.aggregates} == {"a": 2, "b": 1}

    @pytest.mark.asyncio
    async def test_group_by_sum_casts_string_numbers(self, store: FabricStore) -> None:
        await _seed_products(store)
        result = await store.query(
            FabricQuery(
                type_name="Product",
                group_by="category",
                aggregate="sum",
                aggregate_field="price",
            ),
            workspace_id=WS_A,
        )
        # "20" (a JSON-string number) still adds: a = 10 + 20, b = 30.
        assert {r["key"]: r["value"] for r in result.aggregates} == {"a": 30.0, "b": 30.0}

    @pytest.mark.asyncio
    async def test_avg_min_max(self, store: FabricStore) -> None:
        await _seed_products(store)
        for fn, expected_a in [("avg", 15.0), ("min", 10.0), ("max", 20.0)]:
            result = await store.query(
                FabricQuery(
                    type_name="Product",
                    group_by="category",
                    aggregate=fn,
                    aggregate_field="price",
                ),
                workspace_id=WS_A,
            )
            by_key = {r["key"]: r["value"] for r in result.aggregates}
            assert by_key["a"] == expected_a, fn

    @pytest.mark.asyncio
    async def test_filters_apply_before_grouping(self, store: FabricStore) -> None:
        await _seed_products(store)
        result = await store.query(
            FabricQuery(
                type_name="Product",
                filters={"stock": {"<": 10}},
                group_by="category",
            ),
            workspace_id=WS_A,
        )
        # stock<10 in WS_A: the two category-a products + the no-category one.
        assert result.total == 3
        assert {r["key"]: r["value"] for r in result.aggregates} == {"a": 2}

    @pytest.mark.asyncio
    async def test_sort_orders(self, store: FabricStore) -> None:
        await _seed_products(store)

        async def keys(sort: str | None) -> list:
            result = await store.query(
                FabricQuery(type_name="Product", group_by="category", sort=sort),
                workspace_id=WS_A,
            )
            return [r["key"] for r in result.aggregates]

        assert await keys(None) == ["a", "b"]  # default: value desc
        assert await keys("value_asc") == ["b", "a"]
        assert await keys("key_asc") == ["a", "b"]
        assert await keys("key_desc") == ["b", "a"]

    @pytest.mark.asyncio
    async def test_scope_then_aggregate_cross_workspace_never_counted(
        self, store: FabricStore
    ) -> None:
        await _seed_products(store)
        # WS_B holds a category-b product priced 1000. Neither its count nor its
        # price may leak into WS_A's aggregates.
        count = await store.query(
            FabricQuery(type_name="Product", group_by="category"), workspace_id=WS_A
        )
        assert {r["key"]: r["value"] for r in count.aggregates} == {"a": 2, "b": 1}
        total = await store.query(
            FabricQuery(
                type_name="Product",
                group_by="category",
                aggregate="sum",
                aggregate_field="price",
            ),
            workspace_id=WS_A,
        )
        assert {r["key"]: r["value"] for r in total.aggregates}["b"] == 30.0

    @pytest.mark.asyncio
    async def test_ranges_bucketing(self, store: FabricStore) -> None:
        await _seed_products(store)
        result = await store.query(
            FabricQuery(
                type_name="Product",
                group_by="stock",
                ranges=[
                    RangeBucket(min=0, max=10),  # derived label "0-10"
                    RangeBucket(min=10, max=40, label="mid"),
                    RangeBucket(min=40, label="lots"),  # open-ended
                ],
            ),
            workspace_id=WS_A,
        )
        by_key = {r["key"]: r["value"] for r in result.aggregates}
        # WS_A stocks: 5, 3, 50, 1 -> three in 0-10, none in mid, one in lots.
        assert by_key == {"0-10": 3, "lots": 1}

    @pytest.mark.asyncio
    async def test_ranges_min_inclusive_max_exclusive(self, store: FabricStore) -> None:
        t = await store.define_type(name="Reading", properties=[])
        for v in (10, 20):
            await store.create_object(t.id, {"value": v})
        result = await store.query(
            FabricQuery(
                type_name="Reading",
                group_by="value",
                ranges=[RangeBucket(min=10, max=20, label="low")],
            ),
        )
        # 10 falls in [10, 20); 20 does not.
        assert [(r["key"], r["value"]) for r in result.aggregates] == [("low", 1)]

    @pytest.mark.asyncio
    async def test_steps_narrate_the_run(self, store: FabricStore) -> None:
        await _seed_products(store)
        result = await store.query(
            FabricQuery(
                type_name="Product",
                filters={"stock": {"<": 10}},
                group_by="category",
            ),
            workspace_id=WS_A,
        )
        assert result.steps is not None
        titles = [s.title for s in result.steps]
        assert titles == [
            "Filtered Product where stock < 10",
            "Grouped by category",
            "Computed count",
        ]
        assert all(s.status == "done" for s in result.steps)
        assert result.steps[0].detail == "3 matching objects"

    @pytest.mark.asyncio
    async def test_plain_query_unchanged_with_flag_on(self, store: FabricStore) -> None:
        await _seed_products(store)
        result = await store.query(FabricQuery(type_name="Product"), workspace_id=WS_A)
        assert len(result.objects) == 4
        assert result.total == 4
        assert result.aggregates is None
        assert result.steps is None


# ---------------------------------------------------------------------------
# FabricQuery model validation
# ---------------------------------------------------------------------------


class TestQueryModelValidation:
    def test_group_by_alone_defaults_to_count(self) -> None:
        q = FabricQuery(group_by="category")
        assert q.aggregate == "count"

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"aggregate": "count"},  # aggregate without group_by
            {"aggregate_field": "price"},
            {"sort": "value_desc"},
            {"ranges": [{"min": 0, "max": 1}]},
            {"group_by": "x", "aggregate": "sum"},  # sum without aggregate_field
            {"group_by": "x", "aggregate": "count", "aggregate_field": "y"},
            {"group_by": "bad name"},  # non-identifier property
            {"group_by": "x", "path": [{"link_type": "t"}]},  # no path aggregation
        ],
    )
    def test_inconsistent_combinations_rejected(self, kwargs: dict) -> None:
        with pytest.raises(ValueError):
            FabricQuery(**kwargs)

    def test_range_bucket_needs_a_bound(self) -> None:
        with pytest.raises(ValueError):
            RangeBucket(label="empty")


# ---------------------------------------------------------------------------
# HTTP contract on POST /fabric/query
# ---------------------------------------------------------------------------


class _FakeMembership:
    def __init__(self, workspace: str, role: str) -> None:
        self.workspace = workspace
        self.role = role


class _FakeUser:
    def __init__(self, role: str, workspace_id: str = "ws-test") -> None:
        self.id = f"user-{role}"
        self.active_workspace = workspace_id
        self.workspaces = [_FakeMembership(workspace=workspace_id, role=role)]


@pytest.fixture
def member_client(tmp_path: Path, monkeypatch) -> TestClient:
    """The real fabric router + real RBAC over an isolated per-workspace store."""
    import pocketpaw.stores as stores

    monkeypatch.setattr(stores, "_DATA_DIR", tmp_path)
    stores.reset_store_caches()

    import pocketpaw_ee.cloud.workspace.service as ws_svc

    monkeypatch.setattr(ws_svc, "get_workspace_plan", AsyncMock(return_value="enterprise"))

    app = FastAPI()
    add_error_handler(app)
    app.include_router(fabric_router_module.router, prefix="/api/v1")
    app.dependency_overrides[require_license] = lambda: None
    user = _FakeUser(role="member")
    app.dependency_overrides[current_active_user] = lambda: user
    app.dependency_overrides[current_workspace_id] = lambda: user.active_workspace
    return TestClient(app, raise_server_exceptions=True)


async def _seed_http_store(tmp_path_ignored, workspace_id: str = "ws-test") -> None:
    import pocketpaw.stores as stores

    store = stores.get_fabric_store(workspace_id=workspace_id)
    t = await store.define_type(name="Product", properties=[])
    for props in ({"category": "a", "stock": 5}, {"category": "b", "stock": 3}):
        await store.create_object(t.id, props, workspace_id=workspace_id)


class TestHttpContract:
    def test_steps_serialize_title_detail_status(
        self, analyst_on, member_client: TestClient, tmp_path: Path
    ) -> None:
        import asyncio

        asyncio.run(_seed_http_store(tmp_path))
        resp = member_client.post(
            "/api/v1/fabric/query",
            json={"type_name": "Product", "group_by": "category"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["objects"] == []
        assert {r["key"]: r["value"] for r in body["aggregates"]} == {"a": 1, "b": 1}
        assert body["steps"], "aggregation must narrate its steps"
        for step in body["steps"]:
            # The exact QueryPlanStep wire shape ripple's ReasoningTrace
            # consumes: {title, detail, status} — never {label, count}.
            assert set(step.keys()) == {"title", "detail", "status"}
            assert isinstance(step["title"], str) and step["title"]
            assert step["status"] == "done"

    def test_flag_off_maps_to_422_analyst_disabled(
        self, member_client: TestClient, tmp_path: Path
    ) -> None:
        import asyncio

        asyncio.run(_seed_http_store(tmp_path))
        resp = member_client.post(
            "/api/v1/fabric/query",
            json={"type_name": "Product", "group_by": "category"},
        )
        assert resp.status_code == 422, resp.text
        assert resp.json()["error"]["code"] == "fabric.analyst_disabled"

    def test_plain_query_wire_shape_unchanged(
        self, member_client: TestClient, tmp_path: Path
    ) -> None:
        import asyncio

        asyncio.run(_seed_http_store(tmp_path))
        resp = member_client.post("/api/v1/fabric/query", json={"type_name": "Product"})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["total"] == 2
        assert len(body["objects"]) == 2
        # Additive fields serialize as null on a plain query.
        assert body["aggregates"] is None
        assert body["steps"] is None
