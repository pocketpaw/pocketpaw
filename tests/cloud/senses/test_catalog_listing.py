# Catalog listing tests — grouping, bound overlay, availability, route + tenant.
# Created: 2026-07-16 (SR-2 catalog listing API) — coverage for the browse half
#   of the catalog: ``cloud.senses.catalog.list_catalog`` (pure, injected
#   registry over the real 35-connector /connectors dir) and the
#   ``GET /api/v1/cloud/senses/catalog`` route (FastAPI app with the auth deps
#   overridden + a mongomock store). The PURE tests assert every connector is
#   listed exactly once, grouped by its ``type`` category (deterministically
#   sorted), that BOUND tracks the caller-supplied reachable set, and that a
#   LOCAL-execution connector (firebase) is marked UNAVAILABLE. The ROUTE tests
#   seed WorkspaceConnector docs and prove the bound overlay comes from the
#   tenant-filtered EE store — a second workspace never sees the first's bound
#   state — and that the optional ``pocket_id`` query param narrows the overlay.

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

pytest.importorskip("pocketpaw_ee")

from fastapi import FastAPI  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402
from pocketpaw_ee.cloud.senses import catalog  # noqa: E402

from pocketpaw.connectors.registry import ConnectorRegistry  # noqa: E402
from pocketpaw.connectors.state_store import FileConnectorStateStore  # noqa: E402

# Repo-root connectors/ dir (tests/cloud/senses/ -> up 3 -> repo root).
CONNECTORS_DIR = Path(__file__).resolve().parents[3] / "connectors"


@pytest.fixture
def registry(tmp_path):
    """A real registry over the repo connector catalog, hermetic (no home dir,
    no DB-backed state store) so the pure listing tests need no Mongo."""
    return ConnectorRegistry(
        CONNECTORS_DIR,
        state_store=FileConnectorStateStore(tmp_path / "state"),
        home_connectors_dir=tmp_path / "home",
    )


def _all_connectors(groups) -> dict[str, Any]:
    """Flatten grouped output to {connector_name: connector_entry}."""
    return {c.connector: c for g in groups for c in g.connectors}


# ---------------------------------------------------------------------------
# Grouping (pure)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lists_all_connectors_once(registry) -> None:
    """Every connector the registry knows (all 35) is listed exactly once."""
    groups = await catalog.list_catalog(bound_connectors=set(), registry=registry)
    flat = [c.connector for g in groups for c in g.connectors]
    assert len(flat) == 35
    assert len(set(flat)) == 35  # no duplicates across categories


@pytest.mark.asyncio
async def test_grouped_by_connector_type_category(registry) -> None:
    """Each connector lands under a category equal to its def ``type``, and a
    connector's category on the entry matches the group it sits in."""
    groups = await catalog.list_catalog(bound_connectors=set(), registry=registry)
    by_name = _all_connectors(groups)
    # github is type=developer, gmail is type=communication (from the YAML defs).
    assert by_name["github"].category == "developer"
    assert by_name["gmail"].category == "communication"
    for g in groups:
        for c in g.connectors:
            assert c.category == g.category


@pytest.mark.asyncio
async def test_grouping_is_deterministically_sorted(registry) -> None:
    """Categories sorted alphabetically; connectors sorted by name within each."""
    groups = await catalog.list_catalog(bound_connectors=set(), registry=registry)
    categories = [g.category for g in groups]
    assert categories == sorted(categories)
    for g in groups:
        names = [c.connector for c in g.connectors]
        assert names == sorted(names)


# ---------------------------------------------------------------------------
# Bound overlay (pure)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bound_overlay_tracks_reachable_set(registry) -> None:
    groups = await catalog.list_catalog(bound_connectors={"github"}, registry=registry)
    by_name = _all_connectors(groups)
    assert by_name["github"].bound is True
    # Everything not in the reachable set is unbound.
    assert by_name["gmail"].bound is False
    assert all(c.bound is False for n, c in by_name.items() if n != "github")


@pytest.mark.asyncio
async def test_nothing_bound_when_reachable_set_empty(registry) -> None:
    groups = await catalog.list_catalog(bound_connectors=set(), registry=registry)
    assert all(c.bound is False for c in _all_connectors(groups).values())


# ---------------------------------------------------------------------------
# Availability + action metadata (pure)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_local_connector_actions_unavailable(registry) -> None:
    """Firebase actions are execution_mode=local -> the shared cloud can't
    dispatch them, so they must be marked UNAVAILABLE (same rule as search)."""
    groups = await catalog.list_catalog(bound_connectors=set(), registry=registry)
    fb = _all_connectors(groups)["firebase"]
    assert fb.actions, "firebase should expose actions"
    for a in fb.actions:
        assert a.execution_mode == "local"
        assert a.available is False
        assert a.unavailable_reason == "local_runtime_unavailable"
        assert a.cost_estimate is None  # placeholder until pricing ships


@pytest.mark.asyncio
async def test_cloud_connector_actions_available_with_trust(registry) -> None:
    """GitHub is cloud-dispatchable: actions available, trust populated, and the
    write action (create_issue) carries confirm trust."""
    groups = await catalog.list_catalog(bound_connectors=set(), registry=registry)
    gh = _all_connectors(groups)["github"]
    assert gh.senses == ("paw.code.v1",)
    assert all(a.available is True and a.unavailable_reason is None for a in gh.actions)
    create = next((a for a in gh.actions if a.action == "create_issue"), None)
    assert create is not None
    assert create.trust_level == "confirm"
    assert create.execution_mode == "cloud"


# ---------------------------------------------------------------------------
# Route — auth + tenant filter (mongomock store)
# ---------------------------------------------------------------------------


def _build_app(workspace_id: str, user_id: str = "u-1") -> FastAPI:
    from pocketpaw_ee.cloud._core.http import add_error_handler
    from pocketpaw_ee.cloud.license import require_license
    from pocketpaw_ee.cloud.senses.router import router as senses_router
    from pocketpaw_ee.cloud.shared.deps import current_user_id, current_workspace_id

    app = FastAPI()
    add_error_handler(app)
    app.include_router(senses_router, prefix="/api/v1")
    app.dependency_overrides[current_workspace_id] = lambda: workspace_id
    app.dependency_overrides[current_user_id] = lambda: user_id
    app.dependency_overrides[require_license] = lambda: None
    return app


@pytest_asyncio.fixture
async def w1_client(mongo_db) -> AsyncClient:  # noqa: ARG001 — wires Beanie
    app = _build_app("ws-1")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        yield client


@pytest_asyncio.fixture
async def w2_client(mongo_db) -> AsyncClient:  # noqa: ARG001 — same DB, other tenant
    app = _build_app("ws-2")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        yield client


def _flatten_route(payload: list[dict]) -> dict[str, dict]:
    return {c["connector"]: c for g in payload for c in g["connectors"]}


@pytest.mark.asyncio
async def test_route_returns_full_catalog_grouped(w1_client: AsyncClient) -> None:
    r = await w1_client.get("/api/v1/cloud/senses/catalog")
    assert r.status_code == 200, r.text
    payload = r.json()
    # A list of {category, connectors:[...]} groups, sorted by category.
    assert [g["category"] for g in payload] == sorted(g["category"] for g in payload)
    flat = _flatten_route(payload)
    assert len(flat) == 35
    gh = flat["github"]
    assert gh["type"] == "developer"
    assert gh["actions"], "connector actions must be present"


@pytest.mark.asyncio
async def test_route_bound_state_from_store(w1_client: AsyncClient) -> None:
    """A workspace-scoped enabled connector shows bound=true for its workspace."""
    from pocketpaw_ee.cloud.models.connector import WorkspaceConnector

    await WorkspaceConnector(
        workspace="ws-1", name="github", enabled=True, scope="workspace", config={}
    ).insert()

    r = await w1_client.get("/api/v1/cloud/senses/catalog")
    flat = _flatten_route(r.json())
    assert flat["github"]["bound"] is True
    assert flat["gmail"]["bound"] is False  # not enabled


@pytest.mark.asyncio
async def test_route_tenant_filter_no_leak(w1_client: AsyncClient, w2_client: AsyncClient) -> None:
    """ws-1's bound state must never appear in ws-2's catalog."""
    from pocketpaw_ee.cloud.models.connector import WorkspaceConnector

    await WorkspaceConnector(
        workspace="ws-1", name="github", enabled=True, scope="workspace", config={}
    ).insert()

    r1 = await w1_client.get("/api/v1/cloud/senses/catalog")
    r2 = await w2_client.get("/api/v1/cloud/senses/catalog")
    assert _flatten_route(r1.json())["github"]["bound"] is True
    assert _flatten_route(r2.json())["github"]["bound"] is False  # no cross-tenant leak


@pytest.mark.asyncio
async def test_route_pocket_scope_query_param(
    w1_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pocket-scoped connector is bound only when ?pocket_id matches AND the
    caller has run-access to that pocket; the default (no pocket_id) is
    workspace-scope and does not see it."""
    from pocketpaw_ee.cloud.models.connector import WorkspaceConnector
    from pocketpaw_ee.cloud.senses import router as senses_router

    async def _grant(_pocket_id: str, _user_id: str) -> bool:
        return True

    monkeypatch.setattr(senses_router.pockets_service, "has_action_run_access", _grant)

    await WorkspaceConnector(
        workspace="ws-1",
        name="github",
        enabled=True,
        scope="pocket",
        pocket_id="pkX",
        config={},
    ).insert()

    with_pocket = await w1_client.get("/api/v1/cloud/senses/catalog", params={"pocket_id": "pkX"})
    without_pocket = await w1_client.get("/api/v1/cloud/senses/catalog")
    assert _flatten_route(with_pocket.json())["github"]["bound"] is True
    assert _flatten_route(without_pocket.json())["github"]["bound"] is False


@pytest.mark.asyncio
async def test_route_pocket_scope_denied_falls_back_to_workspace(
    w1_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SR-9: a caller WITHOUT run-access to the requested pocket must NOT see that
    pocket's private bindings. The route silently drops pocket_id and falls back to
    workspace-scope (status 200, no bound leak, no 403 existence oracle)."""
    from pocketpaw_ee.cloud.models.connector import WorkspaceConnector
    from pocketpaw_ee.cloud.senses import router as senses_router

    async def _deny(_pocket_id: str, _user_id: str) -> bool:
        return False

    monkeypatch.setattr(senses_router.pockets_service, "has_action_run_access", _deny)

    # A pocket-scoped binding in another member's private pocket.
    await WorkspaceConnector(
        workspace="ws-1",
        name="github",
        enabled=True,
        scope="pocket",
        pocket_id="pk-private",
        config={},
    ).insert()

    r = await w1_client.get("/api/v1/cloud/senses/catalog", params={"pocket_id": "pk-private"})
    assert r.status_code == 200, r.text  # silent fallback, not a 403 oracle
    # The private pocket's binding must NOT surface — the overlay fell back to
    # workspace-scope, where github is not bound.
    assert _flatten_route(r.json())["github"]["bound"] is False
