# tests/cloud/agents/test_agent_disable.py
# Created: 2026-06-28 (feat/aiam-agent-revoke, AW-4) — pins the EE service +
# router layer of the agent soft-disable / revoke-everywhere flow:
#   * disable()/enable() flip the doc's ``disabled`` flag and persist it.
#   * disable()/enable() emit AgentDisabled / AgentEnabled (mirroring
#     AgentDeleted), and invalidate the run-pool cache immediately (must-fix).
#   * a non-owner caller is rejected with Forbidden (agent.not_owner) — the
#     same owner-check as delete(), so the protection is symmetric.
#   * the router's /disable + /enable routes carry the SAME owner/admin guard
#     dependency as DELETE (symmetric tenant-scope protection — asymmetric
#     protection = no protection).
#   * the wire dict (agent_to_dict) surfaces the ``disabled`` flag.
#
# Updated: 2026-06-28 (feat/aiam-agent-revoke, AW-5) — roster surfacing:
#   * list_agents() keeps a disabled agent IN the list (no hard filter) and the
#     wire dict carries disabled=True, so the client can grey/inactive the row
#     instead of the agent just silently vanishing.

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pocketpaw_ee.cloud._core.context import RequestContext, ScopeKind
from pocketpaw_ee.cloud._core.errors import Forbidden
from pocketpaw_ee.cloud._core.realtime.events import AgentDisabled, AgentEnabled
from pocketpaw_ee.cloud.agents import router as agents_router
from pocketpaw_ee.cloud.agents import service as agents_service
from pocketpaw_ee.cloud.agents.dto import CreateAgentRequest, agent_to_dict
from pocketpaw_ee.cloud.models.agent import Agent as _AgentDoc
from pocketpaw_ee.cloud.shared.deps import require_agent_owner_or_admin

pytestmark = pytest.mark.usefixtures("mongo_db")


def _ctx(user_id: str = "u1", workspace_id: str | None = "w1") -> RequestContext:
    return RequestContext(
        user_id=user_id,
        workspace_id=workspace_id,
        request_id="r",
        scope=ScopeKind.NONE,
        started_at=datetime.now(UTC),
    )


def _create_body(slug: str = "buddy", name: str = "Buddy") -> CreateAgentRequest:
    # soul_enabled=False keeps create() off the eager-soul pool path.
    return CreateAgentRequest(name=name, slug=slug, soul_enabled=False)


# --- service: flag flips + persists ---------------------------------------


async def test_disable_sets_flag_and_persists(recording_bus) -> None:
    agent = await agents_service.create(_ctx(), "w1", _create_body())
    assert agent.disabled is False

    updated = await agents_service.disable(_ctx(), agent.id)
    assert updated.disabled is True

    # Re-read the doc from Mongo to confirm persistence (not just the return).
    from beanie import PydanticObjectId

    doc = await _AgentDoc.get(PydanticObjectId(agent.id))
    assert doc.disabled is True


async def test_enable_clears_flag(recording_bus) -> None:
    agent = await agents_service.create(_ctx(), "w1", _create_body())
    await agents_service.disable(_ctx(), agent.id)

    restored = await agents_service.enable(_ctx(), agent.id)
    assert restored.disabled is False

    from beanie import PydanticObjectId

    doc = await _AgentDoc.get(PydanticObjectId(agent.id))
    assert doc.disabled is False


# --- service: events emitted ----------------------------------------------


async def test_disable_emits_agent_disabled(recording_bus) -> None:
    agent = await agents_service.create(_ctx(), "w1", _create_body())
    recording_bus.events.clear()

    await agents_service.disable(_ctx(), agent.id)

    evs = [e for e in recording_bus.events if isinstance(e, AgentDisabled)]
    assert len(evs) == 1
    ev = evs[0]
    assert ev.type == "agent.disabled"
    assert ev.data["agent_id"] == agent.id
    assert ev.data["workspace_id"] == "w1"


async def test_enable_emits_agent_enabled(recording_bus) -> None:
    agent = await agents_service.create(_ctx(), "w1", _create_body())
    await agents_service.disable(_ctx(), agent.id)
    recording_bus.events.clear()

    await agents_service.enable(_ctx(), agent.id)

    evs = [e for e in recording_bus.events if isinstance(e, AgentEnabled)]
    assert len(evs) == 1
    assert evs[0].type == "agent.enabled"
    assert evs[0].data["agent_id"] == agent.id


# --- service: immediate cache invalidation (must-fix) ----------------------


async def test_disable_invalidates_pool_cache(recording_bus, monkeypatch) -> None:
    """disable() must call the run pool's invalidate() so the cached instance is
    dropped the instant the flag flips — not left to the staleness fallback."""
    agent = await agents_service.create(_ctx(), "w1", _create_body())

    invalidated: list[str] = []

    class _FakePool:
        async def invalidate(self, agent_id: str) -> None:
            invalidated.append(agent_id)

    monkeypatch.setattr(
        "pocketpaw.agents.pool.get_agent_pool",
        lambda: _FakePool(),
    )

    await agents_service.disable(_ctx(), agent.id)
    assert invalidated == [agent.id]


# --- service: symmetric owner protection -----------------------------------


async def test_disable_rejects_non_owner(recording_bus) -> None:
    """A caller who is not the owner is rejected — same check as delete()."""
    agent = await agents_service.create(_ctx(user_id="u1"), "w1", _create_body())

    with pytest.raises(Forbidden):
        await agents_service.disable(_ctx(user_id="someone-else"), agent.id)

    # Flag must NOT have flipped.
    from beanie import PydanticObjectId

    doc = await _AgentDoc.get(PydanticObjectId(agent.id))
    assert doc.disabled is False


async def test_enable_rejects_non_owner(recording_bus) -> None:
    agent = await agents_service.create(_ctx(user_id="u1"), "w1", _create_body())
    await agents_service.disable(_ctx(user_id="u1"), agent.id)

    with pytest.raises(Forbidden):
        await agents_service.enable(_ctx(user_id="cross-workspace-attacker"), agent.id)

    from beanie import PydanticObjectId

    doc = await _AgentDoc.get(PydanticObjectId(agent.id))
    assert doc.disabled is True  # still disabled — attacker can't restore it


# --- router: symmetric guard parity with DELETE ----------------------------


def _route_dep_callables(path: str, method: str) -> set:
    for route in agents_router.router.routes:
        if getattr(route, "path", None) == path and method in getattr(route, "methods", set()):
            return {d.call for d in route.dependant.dependencies}
    raise AssertionError(f"route not found: {method} {path}")


def test_disable_enable_routes_carry_same_guard_as_delete() -> None:
    """The /disable + /enable routes must depend on the SAME owner/admin guard
    as DELETE — symmetric tenant-scope protection."""
    delete_deps = _route_dep_callables("/agents/{agent_id}", "DELETE")
    disable_deps = _route_dep_callables("/agents/{agent_id}/disable", "PATCH")
    enable_deps = _route_dep_callables("/agents/{agent_id}/enable", "PATCH")

    assert require_agent_owner_or_admin in delete_deps
    assert require_agent_owner_or_admin in disable_deps
    assert require_agent_owner_or_admin in enable_deps


# --- wire format -----------------------------------------------------------


async def test_agent_to_dict_surfaces_disabled(recording_bus) -> None:
    agent = await agents_service.create(_ctx(), "w1", _create_body())
    assert agent_to_dict(agent)["disabled"] is False

    disabled = await agents_service.disable(_ctx(), agent.id)
    assert agent_to_dict(disabled)["disabled"] is True


# --- roster surfacing (AW-5) ----------------------------------------------


async def test_list_agents_includes_disabled_with_flag(recording_bus) -> None:
    """A disabled agent must STAY in list_agents() — surfaced with
    ``disabled=True`` so the client can render it greyed/inactive, not hard
    filtered out (owners need to see and re-enable it)."""
    live = await agents_service.create(_ctx(), "w1", _create_body(slug="live", name="Live"))
    dead = await agents_service.create(_ctx(), "w1", _create_body(slug="dead", name="Dead"))
    await agents_service.disable(_ctx(), dead.id)

    listed = await agents_service.list_agents("w1")
    by_id = {a.id: a for a in listed}

    # Both agents present — the disabled one is NOT filtered out.
    assert live.id in by_id
    assert dead.id in by_id
    assert by_id[live.id].disabled is False
    assert by_id[dead.id].disabled is True

    # And the wire dicts the router returns carry the same flag.
    wire = {d["_id"]: d for d in (agent_to_dict(a) for a in listed)}
    assert wire[live.id]["disabled"] is False
    assert wire[dead.id]["disabled"] is True
