"""Tests that ``agents.service`` emits realtime events via the bus.

Each state-mutating service function must fire the appropriate Event
class through ``emit()``. Tests run against a real Beanie in-memory
database (``mongo_db`` fixture) and assert on ``recording_bus.events``;
no fake repositories or seam-patching needed.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pocketpaw_ee.cloud._core.context import RequestContext, ScopeKind
from pocketpaw_ee.cloud._core.realtime.events import (
    AgentCreated,
    AgentDeleted,
    AgentScopeUpdated,
    AgentUpdated,
)
from pocketpaw_ee.cloud.agents import service as agents_service
from pocketpaw_ee.cloud.agents.dto import (
    CreateAgentRequest,
    UpdateAgentRequest,
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


def _create_body(slug: str = "buddy", name: str = "Buddy") -> CreateAgentRequest:
    # soul_enabled=False keeps create() from kicking the eager-soul path
    # which would import pocketpaw.agents.pool — irrelevant to the emit test.
    return CreateAgentRequest(name=name, slug=slug, soul_enabled=False)


async def test_create_emits_agent_created(recording_bus) -> None:
    agent = await agents_service.create(_ctx(), "w1", _create_body())

    created = [e for e in recording_bus.events if isinstance(e, AgentCreated)]
    assert len(created) == 1
    ev = created[0]
    assert ev.type == "agent.created"
    assert ev.data["agent_id"] == agent.id
    assert ev.data["workspace_id"] == "w1"
    assert ev.data["owner_id"] == "u1"
    assert ev.data["name"] == "Buddy"
    assert ev.data["slug"] == "buddy"


async def test_update_emits_agent_updated(recording_bus) -> None:
    agent = await agents_service.create(_ctx(), "w1", _create_body())
    recording_bus.events.clear()

    await agents_service.update(_ctx(), agent.id, UpdateAgentRequest(name="Renamed"))

    updates = [e for e in recording_bus.events if isinstance(e, AgentUpdated)]
    assert len(updates) == 1
    ev = updates[0]
    assert ev.type == "agent.updated"
    assert ev.data["agent_id"] == agent.id
    assert ev.data["workspace_id"] == "w1"
    assert ev.data["name"] == "Renamed"


async def test_delete_emits_agent_deleted(recording_bus) -> None:
    agent = await agents_service.create(_ctx(), "w1", _create_body())
    recording_bus.events.clear()

    await agents_service.delete(_ctx(), agent.id)

    dels = [e for e in recording_bus.events if isinstance(e, AgentDeleted)]
    assert len(dels) == 1
    ev = dels[0]
    assert ev.type == "agent.deleted"
    assert ev.data["agent_id"] == agent.id
    assert ev.data["workspace_id"] == "w1"


async def test_set_scopes_emits_agent_scope_updated(recording_bus) -> None:
    agent = await agents_service.create(_ctx(), "w1", _create_body())
    recording_bus.events.clear()

    await agents_service.set_scopes(agent.id, ["org:sales:*"])

    scoped = [e for e in recording_bus.events if isinstance(e, AgentScopeUpdated)]
    assert len(scoped) == 1
    ev = scoped[0]
    assert ev.type == "agent.scope_updated"
    assert ev.data["agent_id"] == agent.id
    assert ev.data["workspace_id"] == "w1"
    assert ev.data["scopes"] == ["org:sales:*"]


async def test_seed_default_agent_emits_agent_created_when_new(recording_bus) -> None:
    """``seed_default_agent`` is idempotent — only emits on first insert."""
    _, created = await agents_service.seed_default_agent("w1", "u1")
    assert created is True

    events_after_first = [e for e in recording_bus.events if isinstance(e, AgentCreated)]
    assert len(events_after_first) == 1
    ev = events_after_first[0]
    assert ev.data["workspace_id"] == "w1"
    assert ev.data["slug"] == "pocketpaw"
    assert ev.data["owner_id"] == "u1"

    recording_bus.events.clear()

    # Second call — no insert, no emit.
    _, created_again = await agents_service.seed_default_agent("w1", "u1")
    assert created_again is False
    assert not any(isinstance(e, AgentCreated) for e in recording_bus.events)
