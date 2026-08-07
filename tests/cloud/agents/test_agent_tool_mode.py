# tests/cloud/agents/test_agent_tool_mode.py
# Created 2026-07-24 (CX-2, feat/code-agent-exclusive-tools) — pins the
# ``tool_mode`` field round-tripping through the agents service:
#   * mapper round-trip spec -> doc -> spec keeps "exclusive" (and the default
#     "additive").
#   * update() via the body.config-dict branch sets tool_mode -> "exclusive" and
#     it persists to Mongo (re-read through the service), and is preserved when
#     the dict omits it.
# Updated 2026-08-07 (C4-b, feat/coupling-c4b-agentconfig-parity) — added the
# FLAT-branch coverage the field never had. CX-2 only wired the config-dict path,
# so ``CreateAgentRequest.tool_mode`` / ``UpdateAgentRequest.tool_mode`` could
# parse and then be silently dropped by ``_build_create_config`` /
# ``_apply_update``. Also pins that the persisted value REACHES THE WIRE via
# ``agent_to_dict`` — the whole point of C4-b, since a locked-down agent was
# previously indistinguishable from an open one in every API response.
"""``AgentConfig.tool_mode`` round-trips through the agents service."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pocketpaw_ee.cloud._core.context import RequestContext, ScopeKind
from pocketpaw_ee.cloud.agents import service as agents_service
from pocketpaw_ee.cloud.agents.domain import AgentConfigSpec
from pocketpaw_ee.cloud.agents.dto import (
    CreateAgentRequest,
    UpdateAgentRequest,
    agent_to_dict,
)

# ---------------------------------------------------------------------------
# Mapper round-trip (no Mongo)
# ---------------------------------------------------------------------------


def test_mapper_round_trip_preserves_exclusive():
    spec = AgentConfigSpec(tool_mode="exclusive", tools=("mcp__code__read",))
    doc = agents_service._config_to_doc(spec)
    assert doc.tool_mode == "exclusive"
    back = agents_service._config_to_domain(doc)
    assert back.tool_mode == "exclusive"
    assert back.tools == ("mcp__code__read",)


def test_mapper_default_is_additive():
    spec = AgentConfigSpec()
    assert spec.tool_mode == "additive"
    doc = agents_service._config_to_doc(spec)
    assert doc.tool_mode == "additive"
    assert agents_service._config_to_domain(doc).tool_mode == "additive"


# ---------------------------------------------------------------------------
# Service round-trip through Mongo (via the config-dict update branch)
# ---------------------------------------------------------------------------


def _ctx(user_id: str = "u1", workspace_id: str | None = "w1") -> RequestContext:
    return RequestContext(
        user_id=user_id,
        workspace_id=workspace_id,
        request_id="r",
        scope=ScopeKind.NONE,
        started_at=datetime.now(UTC),
    )


def _create_body(**kw) -> CreateAgentRequest:
    base = dict(name="Coder", slug="coder", soul_enabled=False)
    base.update(kw)
    return CreateAgentRequest(**base)


@pytest.mark.usefixtures("mongo_db")
async def test_create_defaults_additive(recording_bus) -> None:
    agent = await agents_service.create(_ctx(), "w1", _create_body())
    assert agent.config.tool_mode == "additive"
    reloaded = await agents_service.get(agent.id)
    assert reloaded.config.tool_mode == "additive"


@pytest.mark.usefixtures("mongo_db")
async def test_update_config_dict_sets_and_persists_exclusive(recording_bus) -> None:
    agent = await agents_service.create(_ctx(), "w1", _create_body())
    updated = await agents_service.update(
        _ctx(),
        agent.id,
        UpdateAgentRequest(config={"tool_mode": "exclusive", "tools": ["mcp__code__read"]}),
    )
    assert updated.config.tool_mode == "exclusive"
    # Re-read through the service to prove it persisted to Mongo.
    reloaded = await agents_service.get(agent.id)
    assert reloaded.config.tool_mode == "exclusive"
    assert reloaded.config.tools == ("mcp__code__read",)


@pytest.mark.usefixtures("mongo_db")
async def test_update_config_dict_preserves_when_omitted(recording_bus) -> None:
    """The config-dict branch keeps the current tool_mode when the dict omits it."""
    agent = await agents_service.create(_ctx(), "w1", _create_body())
    # First flip to exclusive, then update an unrelated field.
    await agents_service.update(
        _ctx(), agent.id, UpdateAgentRequest(config={"tool_mode": "exclusive"})
    )
    updated = await agents_service.update(
        _ctx(), agent.id, UpdateAgentRequest(config={"temperature": 0.4})
    )
    assert updated.config.tool_mode == "exclusive"


# ---------------------------------------------------------------------------
# Flat request-field branch (C4-b) — the field is first-class on the wire
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("mongo_db")
async def test_create_flat_field_sets_exclusive(recording_bus) -> None:
    """``CreateAgentRequest.tool_mode`` reaches the persisted config.

    Mutation that must break this: drop the ``tool_mode`` override from
    ``_build_create_config``. The DTO would still parse the field and the agent
    would still be created — just silently additive.
    """
    agent = await agents_service.create(
        _ctx(), "w1", _create_body(tool_mode="exclusive", tools=["mcp__code__read"])
    )
    assert agent.config.tool_mode == "exclusive"
    reloaded = await agents_service.get(agent.id)
    assert reloaded.config.tool_mode == "exclusive"


@pytest.mark.usefixtures("mongo_db")
async def test_update_flat_field_sets_exclusive(recording_bus) -> None:
    """``UpdateAgentRequest.tool_mode`` reaches the persisted config.

    Mutation that must break this: drop the ``("tool_mode", body.tool_mode)``
    tuple from the ``_apply_update`` flat-field loop.
    """
    agent = await agents_service.create(_ctx(), "w1", _create_body())
    assert agent.config.tool_mode == "additive"
    updated = await agents_service.update(
        _ctx(), agent.id, UpdateAgentRequest(tool_mode="exclusive")
    )
    assert updated.config.tool_mode == "exclusive"
    reloaded = await agents_service.get(agent.id)
    assert reloaded.config.tool_mode == "exclusive"


@pytest.mark.usefixtures("mongo_db")
async def test_update_flat_field_omitted_leaves_it_alone(recording_bus) -> None:
    """None on the flat field means "leave unchanged", never a silent reset."""
    agent = await agents_service.create(_ctx(), "w1", _create_body(tool_mode="exclusive"))
    updated = await agents_service.update(_ctx(), agent.id, UpdateAgentRequest(name="Renamed"))
    assert updated.config.tool_mode == "exclusive"


# ---------------------------------------------------------------------------
# The wire (C4-b) — a locked-down agent must be visibly locked down
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("mongo_db")
async def test_wire_dict_shows_the_agent_is_locked_down(recording_bus) -> None:
    """``agent_to_dict`` tells a client whether the tool list is a cap.

    Before C4-b an ``exclusive`` agent and an ``additive`` one with the same
    ``tools`` serialised IDENTICALLY, so no client could tell an allow-list from
    an additive grant. Mutation that must break this: delete the ``tool_mode``
    line from ``_config_to_dict``.
    """
    locked = await agents_service.create(
        _ctx(),
        "w1",
        _create_body(slug="locked", tool_mode="exclusive", tools=["mcp__code__read"]),
    )
    open_agent = await agents_service.create(
        _ctx(),
        "w1",
        _create_body(name="Open", slug="open", tools=["mcp__code__read"]),
    )

    locked_wire = agent_to_dict(locked)
    open_wire = agent_to_dict(open_agent)

    assert locked_wire["config"]["tool_mode"] == "exclusive"
    assert open_wire["config"]["tool_mode"] == "additive"
    # The two must be distinguishable on the wire — that is the whole finding.
    assert locked_wire["config"] != open_wire["config"]
