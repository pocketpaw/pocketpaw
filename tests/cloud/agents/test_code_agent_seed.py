# tests/cloud/agents/test_code_agent_seed.py
# Created 2026-07-24 (CX-3, feat/code-agent-exclusive-tools) — pins the dedicated
# ``/code`` agent seed and the backend-authoritative routing that makes the
# classic pocket repro die.
#
# Three properties carry the weight here:
#   1. ``seed_code_agent`` inserts a slug-"code" agent whose config is
#      ``tool_mode="exclusive"`` + ``tools=_CODE_FILE_TOOL_IDS`` and carries NO
#      create-pocket / widget skill; it is idempotent.
#   2. THE HEADLINE — the seeded agent's OWN config, sourced via the CX-2
#      ``_agent_tool_policy`` and driven through the REAL ``_build_options``
#      allowlist computation, yields an effective MCP surface of EXACTLY the four
#      file ids and ZERO pocket / widget / atlas / planner ids. This is the
#      "build an employee management app…" repro dying at the allowlist level.
#   3. ``_get_code_agent_id`` lazy-seeds on miss, and a CODE-surface session
#      resolution routes to slug "code" (lazy-seeding), not the default
#      "pocketpaw" agent.
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from pocketpaw_ee.cloud._core.realtime.events import AgentCreated
from pocketpaw_ee.cloud.agents import service as agents_service
from pocketpaw_ee.cloud.chat import agent_service
from pocketpaw_ee.cloud.chat.runs import run_core
from pocketpaw_ee.cloud.surface.surface_registry import _CODE_FILE_TOOL_IDS
from pocketpaw_ee.cloud.surface.system_prompts import CODE_SYSTEM_PROMPT

pytestmark = pytest.mark.usefixtures("mongo_db")


# ── 1. the seed ─────────────────────────────────────────────────────────────


async def test_seed_code_agent_inserts_exclusive_file_tool_agent(recording_bus) -> None:
    doc, created = await agents_service.seed_code_agent("w1", "u1")

    assert created is True
    assert doc is not None
    assert doc.slug == "code"
    assert doc.visibility == "workspace"
    # exclusive file-tool policy — this is what CX-1/CX-2 read at run time.
    assert doc.config.tool_mode == "exclusive"
    assert set(doc.config.tools) == set(_CODE_FILE_TOOL_IDS)
    # persona reuses the CODE surface prompt verbatim (no drift).
    assert doc.config.system_prompt == CODE_SYSTEM_PROMPT
    # NO create-pocket / pocket / widget skill — the CODE surface ships none.
    assert doc.config.skill_refs == []
    assert not any("pocket" in s.lower() or "widget" in s.lower() for s in doc.config.skill_refs)

    created_events = [e for e in recording_bus.events if isinstance(e, AgentCreated)]
    assert len(created_events) == 1
    assert created_events[0].data["slug"] == "code"

    # idempotent — a second call inserts nothing and returns the existing row.
    recording_bus.events.clear()
    again, created_again = await agents_service.seed_code_agent("w1", "u1")
    assert created_again is False
    assert str(again.id) == str(doc.id)
    assert not any(isinstance(e, AgentCreated) for e in recording_bus.events)


# ── 2. THE HEADLINE — the pocket repro dies at the allowlist level ───────────


async def test_seeded_code_agent_config_drives_exclusive_allowlist(monkeypatch) -> None:
    """The seeded agent's OWN config, resolved through CX-2 ``_agent_tool_policy``
    and driven through the REAL ``_build_options`` computation with a candidate
    pool that INCLUDES the universal grant ids, yields exactly the four file ids
    and nothing that could author a pocket. Presence + absence together are the
    point — this is the assertion that "build me an employee management app,
    with components and a nice design" can no longer reach a pocket tool."""
    from pocketpaw.agents.claude_sdk import POCKET_CREATION_GRANT, ClaudeSDKBackend
    from pocketpaw.agents.sdk_mcp_atlas import ATLAS_TOOL_IDS
    from pocketpaw.agents.sdk_mcp_widgets import WIDGET_TOOL_IDS
    from pocketpaw.config import get_settings

    doc, _ = await agents_service.seed_code_agent("w1", "u1")

    # Source the exclusive policy FROM the seeded agent's config (the CX-2 seam).
    exclusive, tools = run_core._agent_tool_policy(doc)
    assert exclusive is True
    assert tools == frozenset(_CODE_FILE_TOOL_IDS)

    widget_id = next(iter(WIDGET_TOOL_IDS))
    atlas_id = next(iter(ATLAS_TOOL_IDS))
    planner_id = "mcp__pocketpaw_pocket_planner__plan_pocket"
    assert planner_id in POCKET_CREATION_GRANT

    backend = ClaudeSDKBackend(get_settings())
    # A candidate pool that WOULD grant pocket/widget/atlas on a default turn, so
    # the suppression is provable rather than vacuous.
    pool = [*sorted(_CODE_FILE_TOOL_IDS), widget_id, atlas_id, planner_id]
    monkeypatch.setattr(backend, "_collect_mcp_tool_ids", lambda: list(pool))

    built = await backend._build_options(
        "build me an employee management app, with components and a nice design",
        system_prompt=doc.config.system_prompt,
        history=None,
        session_key=None,
        deny_mcp_tool_ids=frozenset(),
        allow_sdk_tools=frozenset(),
        allow_mcp_tool_ids=tools,
        skill_names=frozenset(),
        stderr_sink=[],
        exclusive_mcp_tools=exclusive,
    )
    effective = {t for t in built.options_kwargs["allowed_tools"] if t.startswith("mcp__")}

    assert effective == set(_CODE_FILE_TOOL_IDS)
    # the pocket repro dies: zero pocket / widget / atlas / planner ids survive.
    assert not [t for t in effective if t.startswith("mcp__pocketpaw_pocket")]
    assert widget_id not in effective
    assert atlas_id not in effective
    assert planner_id not in effective


# ── 3. lazy-seed + CODE-surface routing ─────────────────────────────────────


async def test_get_code_agent_id_lazy_seeds_when_absent() -> None:
    """A workspace with no code agent still works on its FIRST /code turn: the
    helper seeds one and returns its id, and a second call returns the SAME id
    (no duplicate seed)."""
    # No code agent yet.
    from pocketpaw_ee.cloud.models.agent import Agent

    assert await Agent.find_one(Agent.workspace == "w1", Agent.slug == "code") is None

    first = await agent_service._get_code_agent_id("w1")
    assert first is not None

    seeded = await Agent.find_one(Agent.workspace == "w1", Agent.slug == "code")
    assert seeded is not None
    assert str(seeded.id) == first

    second = await agent_service._get_code_agent_id("w1")
    assert second == first  # idempotent — same id, no second row


async def test_code_surface_session_resolves_to_code_agent_not_default() -> None:
    """End to end: a session-scope /code turn with NO explicit agent hint and no
    pre-existing code agent LAZY-SEEDS one and resolves ``target`` to slug
    "code" — never the default "pocketpaw" agent."""
    from pocketpaw_ee.cloud.models.agent import Agent
    from pocketpaw_ee.cloud.models.session import Session

    # A default pocketpaw agent exists — the CODE override must still win.
    default_doc, _ = await agents_service.seed_default_agent("w1", "u1")

    fake_session = Session.model_construct(
        id="s1",
        sessionId="ws",
        workspace="w1",
        owner="u1",
        agent=None,
        pocket=None,
        deleted_at=None,
    )
    with patch(
        "pocketpaw_ee.cloud.chat.agent_service._get_session",
        AsyncMock(return_value=fake_session),
    ):
        ctx = await agent_service.resolve_scope_context(
            scope="session",
            scope_id="s1",
            user_id="u1",
            agent_id_hint=None,
            surface="code",
        )

    resolved = await Agent.get(ctx.target_agent_id)
    assert resolved is not None
    assert resolved.slug == "code"
    assert ctx.target_agent_id != str(default_doc.id)
