# test_ui_agent_invariant.py — connector-store-unification CS-4 — one truth.
# Created: 2026-06-12 — THE INVARIANT TEST: the UI-facing connectors list DTO
#   (service.list_connectors) and the agent MCP surface
#   (service.list_pocket_connectors) must report the SAME connector set and
#   state from the SAME durable source (WorkspaceConnector docs + registry
#   definitions). This is the structural kill of the 2026-06-12 bug class,
#   where the two surfaces read different state (durable docs vs in-process
#   adapter connection state) and could disagree — the UI showing a connector
#   "connected" while the agent couldn't see it after a restart, or vice
#   versa. Both surfaces are also asserted against a FRESH registry instance
#   (simulated restart): neither may depend on in-process adapter state.

from __future__ import annotations

from pathlib import Path

import pytest
from pocketpaw_ee.cloud.connectors import service as connectors_service
from pocketpaw_ee.cloud.connectors.state_provider import CloudConnectorStateStore
from pocketpaw_ee.cloud.models.connector import WorkspaceConnector

from pocketpaw.connectors.registry import ConnectorRegistry
from pocketpaw.connectors.state_store import FileConnectorStateStore

_WS = "ws-1"
_POCKET = "pk-1"


async def _seed_pocket_connector(
    name: str = "github", *, enabled: bool = True, pocket_id: str = _POCKET
) -> None:
    """The ONE workspace_connectors fixture both surfaces must agree on."""
    await WorkspaceConnector(
        workspace=_WS,
        name=name,
        enabled=enabled,
        scope="pocket",
        pocket_id=pocket_id,
        config={"GITHUB_TOKEN": "ghp_test"},
    ).insert()


async def _ui_connected_names() -> set[str]:
    """The UI surface: GET /cloud/connectors rows with status=connected."""
    rows = await connectors_service.list_connectors(_WS)
    return {r.name for r in rows if r.status == "connected"}


async def _agent_visible_names(pocket_id: str = _POCKET) -> set[str]:
    """The agent surface: what the MCP server enumerates for this pocket."""
    infos = await connectors_service.list_pocket_connectors(_WS, pocket_id)
    return {i.name for i in infos}


@pytest.fixture
def fresh_registry(tmp_path, monkeypatch) -> ConnectorRegistry:
    """Simulated restart: a registry with no live adapters, installed as the
    service singleton. Both surfaces must read identically through it."""
    reg = ConnectorRegistry(
        Path("connectors"),
        state_store=CloudConnectorStateStore(
            file_fallback=FileConnectorStateStore(base_dir=tmp_path / "file-state")
        ),
    )
    monkeypatch.setattr(connectors_service, "_registry", reg)
    return reg


@pytest.mark.asyncio
async def test_ui_and_agent_connector_surfaces_never_disagree(
    mongo_db,  # noqa: ARG001 — wires Beanie
    fresh_registry,  # noqa: ARG001 — simulated restart, no live adapters
):
    """Structural kill of the 2026-06-12 UI-vs-agent divergence bug class.

    One seeded workspace_connectors row; the UI list DTO and the agent MCP
    enumeration must report the same connector set and state — derived from
    durable state only, on a registry with zero in-process adapters.
    """
    await _seed_pocket_connector("github")

    ui = await _ui_connected_names()
    agent = await _agent_visible_names()

    assert ui == agent == {"github"}


@pytest.mark.asyncio
async def test_surfaces_agree_on_disabled_connector(
    mongo_db,  # noqa: ARG001 — wires Beanie
    fresh_registry,  # noqa: ARG001
):
    """A disabled row must vanish from BOTH surfaces, not just one."""
    await _seed_pocket_connector("github", enabled=False)

    assert await _ui_connected_names() == set()
    assert await _agent_visible_names() == set()


@pytest.mark.asyncio
async def test_surfaces_agree_after_disable_transition(
    mongo_db,  # noqa: ARG001 — wires Beanie
    fresh_registry,  # noqa: ARG001
):
    """Enable → both see it; disable → both drop it. No surface may lag the
    other through the transition (the in-process-state failure mode).

    Uses a REAL pocket (created through the pockets service) because
    disabling a pocket-scoped connector re-derives that pocket's
    surface_profile — a synthetic pocket id would 404 the rederive."""
    from pocketpaw_ee.cloud.pockets import service as pockets_service
    from pocketpaw_ee.cloud.pockets.dto import CreatePocketRequest

    wire = await pockets_service.create(_WS, "u-1", CreatePocketRequest(name="Invariant"))
    pocket_id = wire["_id"]

    await _seed_pocket_connector("github", pocket_id=pocket_id)
    ui = await _ui_connected_names()
    agent = await _agent_visible_names(pocket_id)
    assert ui == agent == {"github"}

    await connectors_service.disable_connector(_WS, "github")

    assert await _ui_connected_names() == set()
    assert await _agent_visible_names(pocket_id) == set()


@pytest.mark.asyncio
async def test_widget_recipes_follow_durable_state(
    mongo_db,  # noqa: ARG001 — wires Beanie
    fresh_registry,  # noqa: ARG001
):
    """The widget-recipe rail derives from _WCDoc presence + registry
    definition presence — a fresh process (no adapter ever connected) still
    serves recipes for an enabled connector, and none for a disabled one."""
    await WorkspaceConnector(
        workspace=_WS,
        name="gmail",
        enabled=True,
        scope="workspace",
        config={},
    ).insert()

    recipes = await connectors_service.list_widget_recipes(_WS)
    assert {r.connector for r in recipes} == {"gmail"}

    await connectors_service.disable_connector(_WS, "gmail")
    assert await connectors_service.list_widget_recipes(_WS) == []
