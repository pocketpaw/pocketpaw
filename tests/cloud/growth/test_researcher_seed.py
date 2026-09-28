# tests/cloud/growth/test_researcher_seed.py — the growth researcher agent
# exists in every workspace that runs a hunt.
#
# ``GROWTH_RESEARCHER_AGENT`` is only a definition. Discovery resolves the agent
# by slug per workspace, so a workspace nobody seeded fails every preview and
# every scheduled run with "no researcher agent is seeded in this workspace".
# These pin the three ways it gets there, mirroring the ``code`` agent:
#   1. ``seed_growth_researcher_agent`` inserts it idempotently, owned by the
#      workspace owner, with the read-only exclusive tool surface.
#   2. ``ensure_growth_researcher_agent_all_workspaces`` back-fills it at boot.
#   3. ``agent_research`` lazy-seeds on miss instead of refusing to run.
# The seed also narrows an existing row back to the pinned surface: for this
# agent a wider tool list is the hazard, so it is corrected, not unioned.

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from pocketpaw_ee.cloud._core.realtime.events import AgentCreated
from pocketpaw_ee.cloud.agents import service as agents_service
from pocketpaw_ee.cloud.growth.discovery import ResearchRequest
from pocketpaw_ee.cloud.growth.researcher import (
    GROWTH_RESEARCHER_PROMPT,
    GROWTH_RESEARCHER_SLUG,
    GROWTH_RESEARCHER_TOOLS,
    agent_research,
)
from pocketpaw_ee.cloud.models.agent import Agent, AgentConfig
from pocketpaw_ee.cloud.models.workspace import Workspace

pytestmark = pytest.mark.usefixtures("mongo_db")


async def _researcher(workspace_id: str) -> Agent | None:
    return await Agent.find_one(
        Agent.workspace == workspace_id, Agent.slug == GROWTH_RESEARCHER_SLUG
    )


class _FakePool:
    def __init__(self, answer: str) -> None:
        self.answer = answer
        self.calls: list[dict] = []

    async def run(self, **kwargs):
        self.calls.append(kwargs)
        yield SimpleNamespace(type="message", content=self.answer)


async def test_seed_inserts_read_only_exclusive_agent(recording_bus) -> None:
    doc, created = await agents_service.seed_growth_researcher_agent("w1", "u1")

    assert created is True
    assert doc is not None
    assert doc.slug == GROWTH_RESEARCHER_SLUG
    assert doc.owner == "u1"
    assert doc.config.tool_mode == "exclusive"
    assert list(doc.config.tools) == list(GROWTH_RESEARCHER_TOOLS)
    assert doc.config.system_prompt == GROWTH_RESEARCHER_PROMPT
    assert doc.config.soul_enabled is False
    assert doc.config.trust_level == 1

    created_events = [e for e in recording_bus.events if isinstance(e, AgentCreated)]
    assert [e.data["slug"] for e in created_events] == [GROWTH_RESEARCHER_SLUG]

    recording_bus.events.clear()
    again, created_again = await agents_service.seed_growth_researcher_agent("w1", "u1")
    assert created_again is False
    assert str(again.id) == str(doc.id)
    assert not any(isinstance(e, AgentCreated) for e in recording_bus.events)


async def test_seed_narrows_a_widened_existing_agent() -> None:
    widened = Agent(
        workspace="w1",
        name="Growth researcher",
        slug=GROWTH_RESEARCHER_SLUG,
        owner="u1",
        config=AgentConfig(
            tools=["WebSearch", "WebFetch", "growth_upsert_prospect", "Bash"],
            tool_mode="additive",
        ),
    )
    await widened.insert()

    doc, created = await agents_service.seed_growth_researcher_agent("w1", "u1")

    assert created is False
    stored = await _researcher("w1")
    assert stored is not None
    assert str(stored.id) == str(doc.id)
    assert stored.config.tool_mode == "exclusive"
    assert list(stored.config.tools) == list(GROWTH_RESEARCHER_TOOLS)


async def test_boot_backfill_seeds_every_workspace_once() -> None:
    a = Workspace(name="a", slug="a", owner="owner-a")
    b = Workspace(name="b", slug="b", owner="owner-b")
    await a.insert()
    await b.insert()

    assert await agents_service.ensure_growth_researcher_agent_all_workspaces() == 2
    assert await agents_service.ensure_growth_researcher_agent_all_workspaces() == 0

    seeded_a = await _researcher(str(a.id))
    assert seeded_a is not None and seeded_a.owner == "owner-a"


async def test_research_seeds_the_agent_instead_of_refusing_to_run() -> None:
    """The reported bug: a hunt preview in a workspace nobody seeded failed with
    "no researcher agent is seeded in this workspace". It must seed and run."""
    ws = Workspace(name="acme", slug="acme", owner="owner-1")
    await ws.insert()
    workspace_id = str(ws.id)
    assert await _researcher(workspace_id) is None

    pool = _FakePool(
        json.dumps({"companies": [{"domain": "getcore.me", "company": "CORE"}], "notes": ""})
    )
    request = ResearchRequest(
        workspace_id=workspace_id,
        icp_id="icp-1",
        criteria="Early-stage Postgres startups in India",
    )
    with patch("pocketpaw.agents.pool.get_agent_pool", return_value=pool):
        result = await agent_research(request)

    seeded = await _researcher(workspace_id)
    assert seeded is not None
    assert seeded.owner == "owner-1"
    assert seeded.config.tool_mode == "exclusive"
    assert [c["agent_id"] for c in pool.calls] == [str(seeded.id)]
    assert [c.domain for c in result.companies] == ["getcore.me"]
