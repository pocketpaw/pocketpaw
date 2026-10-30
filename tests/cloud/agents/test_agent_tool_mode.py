# tests/cloud/agents/test_agent_tool_mode.py
# Created 2026-07-24 (CX-2, feat/code-agent-exclusive-tools) — pins the
# ``tool_mode`` field round-tripping through the agents service:
#   * mapper round-trip spec -> doc -> spec keeps "exclusive" (and the default
#     "additive").
#   * update() via the body.config-dict branch sets tool_mode -> "exclusive" and
#     it persists to Mongo (re-read through the service), and is preserved when
#     the dict omits it.
"""``AgentConfig.tool_mode`` round-trips through the agents service."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pocketpaw_ee.cloud._core.context import RequestContext, ScopeKind
from pocketpaw_ee.cloud.agents import service as agents_service
from pocketpaw_ee.cloud.agents.domain import AgentConfigSpec
from pocketpaw_ee.cloud.agents.dto import CreateAgentRequest, UpdateAgentRequest

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
