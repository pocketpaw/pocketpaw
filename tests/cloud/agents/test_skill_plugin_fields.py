# tests/cloud/agents/test_skill_plugin_fields.py
# Created 2026-06-08 (feat/agent-plugin-fields, M2) — pins the per-agent
# ``skill_refs`` + ``plugins`` fields end-to-end through the agents service:
#   * create() persists both, round-tripping back through _to_domain.
#   * update() via the body.config-dict branch sets/clears both.
#   * update() via the flat-field branch sets/clears both.
#   * the DTO wire dict (_config_to_dict) emits both for FE round-trip.
"""Per-agent skill_refs + plugins round-trip through the agents service."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pocketpaw_ee.cloud._core.context import RequestContext, ScopeKind
from pocketpaw_ee.cloud.agents import service as agents_service
from pocketpaw_ee.cloud.agents.dto import (
    CreateAgentRequest,
    UpdateAgentRequest,
    _config_to_dict,
)

pytestmark = pytest.mark.usefixtures("mongo_db")


def _ctx(user_id: str = "u1", workspace_id: str | None = "w1") -> RequestContext:
    return RequestContext(
        user_id=user_id,
        workspace_id=workspace_id,
        request_id="r",
        scope=ScopeKind.NONE,
        started_at=datetime.now(UTC),
    )


def _create_body(**kw) -> CreateAgentRequest:
    # soul_enabled=False avoids the eager-soul import path (irrelevant here).
    base = dict(name="Buddy", slug="buddy", soul_enabled=False)
    base.update(kw)
    return CreateAgentRequest(**base)


async def test_create_persists_skill_refs_and_plugins(recording_bus) -> None:
    agent = await agents_service.create(
        _ctx(),
        "w1",
        _create_body(skill_refs=["github", "jira"], plugins=["acme-suite"]),
    )
    assert agent.config.skill_refs == ("github", "jira")
    assert agent.config.plugins == ("acme-suite",)

    # Re-read through the service to prove the values persisted to Mongo.
    reloaded = await agents_service.get(agent.id)
    assert reloaded.config.skill_refs == ("github", "jira")
    assert reloaded.config.plugins == ("acme-suite",)


async def test_create_defaults_empty(recording_bus) -> None:
    agent = await agents_service.create(_ctx(), "w1", _create_body())
    assert agent.config.skill_refs == ()
    assert agent.config.plugins == ()


async def test_update_via_config_dict_branch(recording_bus) -> None:
    agent = await agents_service.create(_ctx(), "w1", _create_body())
    updated = await agents_service.update(
        _ctx(),
        agent.id,
        UpdateAgentRequest(config={"skill_refs": ["pdf"], "plugins": ["p1", "p2"]}),
    )
    assert updated.config.skill_refs == ("pdf",)
    assert updated.config.plugins == ("p1", "p2")


async def test_update_via_config_dict_preserves_when_omitted(recording_bus) -> None:
    """The config-dict branch keeps current skill_refs/plugins when the dict
    omits them (only touches what it carries)."""
    agent = await agents_service.create(
        _ctx(), "w1", _create_body(skill_refs=["keep"], plugins=["keep-plugin"])
    )
    updated = await agents_service.update(
        _ctx(), agent.id, UpdateAgentRequest(config={"temperature": 0.4})
    )
    assert updated.config.skill_refs == ("keep",)
    assert updated.config.plugins == ("keep-plugin",)


async def test_update_via_flat_field_branch(recording_bus) -> None:
    agent = await agents_service.create(_ctx(), "w1", _create_body())
    updated = await agents_service.update(
        _ctx(),
        agent.id,
        UpdateAgentRequest(skill_refs=["docx"], plugins=["flat-plugin"]),
    )
    assert updated.config.skill_refs == ("docx",)
    assert updated.config.plugins == ("flat-plugin",)


async def test_update_flat_field_can_clear_to_empty(recording_bus) -> None:
    agent = await agents_service.create(_ctx(), "w1", _create_body(skill_refs=["x"], plugins=["y"]))
    updated = await agents_service.update(
        _ctx(), agent.id, UpdateAgentRequest(skill_refs=[], plugins=[])
    )
    assert updated.config.skill_refs == ()
    assert updated.config.plugins == ()


def test_config_to_dict_emits_skill_refs_and_plugins() -> None:
    from pocketpaw_ee.cloud.agents.domain import AgentConfigSpec

    spec = AgentConfigSpec(skill_refs=("a", "b"), plugins=("c",))
    wire = _config_to_dict(spec)
    assert wire["skill_refs"] == ["a", "b"]
    assert wire["plugins"] == ["c"]
