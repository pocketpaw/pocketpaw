# Catalog search tests — BM25 relevance, bound overlay, availability, tenant read.
# Created: 2026-07-16 (SR-1 catalog-wide discovery) — coverage for the new
#   ``cloud.senses.catalog.search_catalog`` and the
#   ``connectors.service.list_bound_connector_names`` helper that feeds it.
#   The PURE tests inject a registry built from the repo /connectors dir (real
#   35-connector catalog, no DB) and assert on the returned CatalogHit fields:
#   that a query ranks the right connector/action first, that BOUND-vs-unbound
#   tracks the caller-supplied reachable set, that catalog-wide search surfaces
#   connectors NOT bound to the pocket, and that a LOCAL-execution-mode connector
#   (firebase) is marked UNAVAILABLE. The INTEGRATION tests (mongo_db) seed
#   WorkspaceConnector docs directly and assert list_bound_connector_names
#   returns the pocket-reachable name set (pocket-scoped narrowing +
#   workspace-scoped fall-through), then prove the end-to-end overlay.

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("pocketpaw_ee")

from pocketpaw_ee.cloud.senses import catalog  # noqa: E402

from pocketpaw.connectors.registry import ConnectorRegistry  # noqa: E402
from pocketpaw.connectors.state_store import FileConnectorStateStore  # noqa: E402

# Repo-root connectors/ dir (tests/cloud/senses/ -> up 3 -> repo root).
CONNECTORS_DIR = Path(__file__).resolve().parents[3] / "connectors"


@pytest.fixture
def registry(tmp_path):
    """A real registry over the repo connector catalog, hermetic (no home dir,
    no DB-backed state store) so the pure search tests need no Mongo."""
    return ConnectorRegistry(
        CONNECTORS_DIR,
        state_store=FileConnectorStateStore(tmp_path / "state"),
        home_connectors_dir=tmp_path / "home",
    )


# ---------------------------------------------------------------------------
# Relevance / ranking (pure)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ranks_github_create_issue_first(registry) -> None:
    """'create a github issue' ranks GitHub's create_issue action at the top,
    with trust level populated (confirm — it's a write) and available (cloud)."""
    hits = await catalog.search_catalog(
        "create a github issue", bound_connectors={"gmail"}, registry=registry
    )
    assert hits, "expected at least one catalog hit"
    top = hits[0]
    assert top.connector == "github"
    assert top.action == "create_issue"
    assert top.trust_level == "confirm"  # write action, trust populated
    assert top.execution_mode == "cloud"
    assert top.available is True
    assert top.unavailable_reason is None
    assert top.senses == ("paw.code.v1",)
    assert top.cost_estimate is None  # placeholder until pricing ships


@pytest.mark.asyncio
async def test_read_action_trust_is_auto(registry) -> None:
    """Email search surfaces gmail's read (auto-trust) action with the email
    sense attached."""
    hits = await catalog.search_catalog(
        "search my email inbox", bound_connectors={"gmail"}, registry=registry
    )
    gmail_hits = [h for h in hits if h.connector == "gmail"]
    assert gmail_hits, "expected gmail to match an email query"
    assert any("paw.email.v1" in h.senses for h in gmail_hits)
    assert any(h.trust_level == "auto" for h in gmail_hits)


# ---------------------------------------------------------------------------
# Bound vs unbound (pure — overlay from the caller-supplied reachable set)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bound_flag_tracks_reachable_set(registry) -> None:
    # github NOT in the bound set -> UNBOUND.
    unbound = await catalog.search_catalog(
        "create a github issue", bound_connectors={"gmail"}, registry=registry
    )
    gh_unbound = next(h for h in unbound if h.connector == "github")
    assert gh_unbound.bound is False

    # github IN the bound set -> BOUND (same query, different reachable set).
    bound = await catalog.search_catalog(
        "create a github issue", bound_connectors={"github"}, registry=registry
    )
    gh_bound = next(h for h in bound if h.connector == "github")
    assert gh_bound.bound is True


@pytest.mark.asyncio
async def test_search_is_catalog_wide_includes_unbound_connectors(registry) -> None:
    """With NOTHING bound, github still surfaces — proving the search covers
    connectors not bound to the pocket (the whole point of SR-1)."""
    hits = await catalog.search_catalog(
        "create a github issue", bound_connectors=set(), registry=registry
    )
    connectors = {h.connector for h in hits}
    assert "github" in connectors
    assert all(h.bound is False for h in hits)


# ---------------------------------------------------------------------------
# Availability — local execution mode is UNAVAILABLE
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_local_connector_marked_unavailable(registry) -> None:
    """Firebase actions are execution_mode=local; the shared cloud has no local
    runtime listener, so they must be marked UNAVAILABLE (not selectable)."""
    hits = await catalog.search_catalog(
        "firebase firestore collections", bound_connectors=set(), registry=registry
    )
    fb_hits = [h for h in hits if h.connector == "firebase"]
    assert fb_hits, "expected firebase to match"
    fb = fb_hits[0]
    assert fb.execution_mode == "local"
    assert fb.available is False
    assert fb.unavailable_reason == "local_runtime_unavailable"


@pytest.mark.asyncio
async def test_cloud_connector_is_available(registry) -> None:
    hits = await catalog.search_catalog(
        "list github repositories", bound_connectors=set(), registry=registry
    )
    gh_hits = [h for h in hits if h.connector == "github"]
    assert gh_hits
    assert all(h.available is True and h.unavailable_reason is None for h in gh_hits)


# ---------------------------------------------------------------------------
# Edges — empty / no-match / limit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_match_returns_empty(registry) -> None:
    assert (
        await catalog.search_catalog(
            "zzzqqx wubbleflonk", bound_connectors=set(), registry=registry
        )
        == []
    )


@pytest.mark.asyncio
async def test_blank_query_returns_empty(registry) -> None:
    assert await catalog.search_catalog("   ", bound_connectors=set(), registry=registry) == []


@pytest.mark.asyncio
async def test_limit_caps_result_count(registry) -> None:
    hits = await catalog.search_catalog("list", bound_connectors=set(), limit=3, registry=registry)
    assert len(hits) <= 3


# ---------------------------------------------------------------------------
# Integration — list_bound_connector_names reads the EE store, then overlay
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.usefixtures("mongo_db")
async def test_list_bound_connector_names_scope_filtering() -> None:
    """The tenant read returns the pocket-reachable name set: workspace-scoped
    rows reach every pocket, a pocket-scoped row reaches only its own pocket."""
    from pocketpaw_ee.cloud.connectors import service as connectors_service
    from pocketpaw_ee.cloud.models.connector import WorkspaceConnector

    ws = "ws-catalog-bound"
    # workspace-scoped gmail -> reachable from any pocket.
    await WorkspaceConnector(
        workspace=ws, name="gmail", enabled=True, scope="workspace", config={}
    ).insert()
    # pocket-scoped github -> reachable only from pk1.
    await WorkspaceConnector(
        workspace=ws, name="github", enabled=True, scope="pocket", pocket_id="pk1", config={}
    ).insert()
    # a DISABLED workspace row must never count.
    await WorkspaceConnector(
        workspace=ws, name="stripe", enabled=False, scope="workspace", config={}
    ).insert()
    # a foreign tenant's row must never leak.
    await WorkspaceConnector(
        workspace="ws-OTHER", name="slack_data", enabled=True, scope="workspace", config={}
    ).insert()

    pk1 = await connectors_service.list_bound_connector_names(ws, "pk1")
    assert pk1 == {"gmail", "github"}

    pk2 = await connectors_service.list_bound_connector_names(ws, "pk2")
    assert pk2 == {"gmail"}  # github is pocket-scoped to pk1 only

    unanchored = await connectors_service.list_bound_connector_names(ws, "")
    assert unanchored == {"gmail"}  # only workspace-scoped rows


@pytest.mark.asyncio
@pytest.mark.usefixtures("mongo_db")
async def test_end_to_end_bound_overlay_from_store() -> None:
    """A real store read feeds the overlay: with only gmail bound to the pocket,
    a github search returns github UNBOUND (but still surfaced — catalog-wide)."""
    from pocketpaw_ee.cloud.connectors import service as connectors_service
    from pocketpaw_ee.cloud.models.connector import WorkspaceConnector

    ws = "ws-catalog-e2e"
    await WorkspaceConnector(
        workspace=ws, name="gmail", enabled=True, scope="pocket", pocket_id="pkX", config={}
    ).insert()

    bound = await connectors_service.list_bound_connector_names(ws, "pkX")
    assert bound == {"gmail"}

    hits = await catalog.search_catalog("create a github issue", bound_connectors=bound)
    gh = next(h for h in hits if h.connector == "github")
    assert gh.bound is False  # gmail is bound, github is not — but still found
    assert gh.available is True
