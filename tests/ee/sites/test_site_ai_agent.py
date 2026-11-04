# tests/ee/sites/test_site_ai_agent.py: the /sites agent's site AI tools.
#
# get_site_ai_settings returns settings, budget, presets and the build guidance;
# set_site_ai_settings and upsert_site_ai_preset need ``sites.ai_manage`` (a member
# is denied and nothing changes), set pushes the "set a budget" SSE card, and the
# agent can't touch caps or transcripts. The tool ids ride the /sites allow-list.
from __future__ import annotations

import json

import pytest

from tests.ee.sites.site_ai_harness import WS, make_pocket, make_site

USER = "64b000000000000000000001"


class _Membership:
    def __init__(self, workspace: str, role: str) -> None:
        self.workspace = workspace
        self.role = role


class _User:
    def __init__(self, role: str) -> None:
        self.id = USER
        self.workspaces = [_Membership(WS, role)]


@pytest.fixture
def agent(monkeypatch):
    from pocketpaw_ee.agent.mcp_servers import sites_ai
    from pocketpaw_ee.cloud.chat import agent_service

    pushed: list[tuple[str, dict]] = []
    state = {"role": "admin"}

    async def _load_user(user_id):
        return _User(state["role"])

    monkeypatch.setattr(sites_ai, "_identity", lambda: (WS, USER))
    monkeypatch.setattr(sites_ai, "_load_user", _load_user)
    monkeypatch.setattr(
        agent_service, "push_sse_event", lambda name, data: pushed.append((name, data))
    )
    return sites_ai, pushed, state


def _body(resp: dict) -> dict:
    return json.loads(resp["content"][0]["text"])


@pytest.mark.asyncio
async def test_get_returns_settings_budget_presets_and_how_to(beanie_test_db, agent):
    sites_ai, _pushed, _state = agent
    pid = await make_pocket()
    await make_site(pid, plan_tier="staff")
    body = _body(await sites_ai._get_handler({"pocket_id": pid}))
    assert body["ok"] is True
    assert body["settings"]["plan_models"] == ["fast", "smart", "vision", "embed"]
    assert body["budget"]["daily_cap_micro"] > 0
    assert body["presets"] == []
    assert "pawAi(env)" in body["how_to"] and "budget_exhausted" in body["how_to"]
    assert "provider key" in body["how_to"]


@pytest.mark.asyncio
async def test_set_needs_ai_manage_and_pushes_the_budget_card(beanie_test_db, agent):
    from pocketpaw_ee.site_ai import service

    sites_ai, pushed, state = agent
    pid = await make_pocket()
    await make_site(pid, plan_tier="staff")

    state["role"] = "member"
    denied = _body(
        await sites_ai._set_handler(
            {"pocket_id": pid, "browser_access": True, "purpose": "A recipe helper"}
        )
    )
    assert denied["ok"] is False and denied["denied"] is True
    assert (await service.load_settings(WS, pid)).browser_access is False
    assert pushed == []

    state["role"] = "admin"
    ok = _body(
        await sites_ai._set_handler(
            {
                "pocket_id": pid,
                "browser_access": True,
                "allowed_models": ["fast", "smart"],
                "purpose": "A recipe helper",
            }
        )
    )
    assert ok["ok"] is True and ok["settings"]["browser_access"] is True
    assert ok["settings"]["allowed_models"] == ["fast", "smart"]
    assert pushed[0][0] == "site_ai_budget_requested"
    assert pushed[0][1]["pocket_id"] == pid and pushed[0][1]["purpose"] == "A recipe helper"
    assert "daily_cap_micro" in pushed[0][1]["budget"]

    # Money and transcripts are not the agent's: those keys are simply ignored.
    await sites_ai._set_handler(
        {"pocket_id": pid, "purpose": "x", "keep_transcripts": True, "daily_cap_micro": 1}
    )
    assert (await service.load_settings(WS, pid)).keep_transcripts is False
    assert (await service.load_budget(WS, pid)).daily_cap_micro != 1


@pytest.mark.asyncio
async def test_upsert_creates_then_updates_a_preset(beanie_test_db, agent):
    sites_ai, _pushed, state = agent
    pid = await make_pocket()
    created = _body(
        await sites_ai._upsert_handler(
            {"pocket_id": pid, "name": "Chef", "system_prompt": "Talk about food.", "model": "fast"}
        )
    )
    assert created["ok"] is True and created["preset"]["version"] == 1
    preset_id = created["preset"]["id"]
    updated = _body(
        await sites_ai._upsert_handler(
            {
                "pocket_id": pid,
                "preset_id": preset_id,
                "name": "Chef",
                "system_prompt": "Only soups.",
            }
        )
    )
    assert updated["preset"] == {"id": preset_id, "name": "Chef", "version": 2}
    bad = await sites_ai._upsert_handler(
        {"pocket_id": pid, "name": "x", "system_prompt": "y", "model": "gpt-9"}
    )
    assert bad.get("is_error") is True

    state["role"] = "member"
    denied = _body(
        await sites_ai._upsert_handler({"pocket_id": pid, "name": "x", "system_prompt": "y"})
    )
    assert denied["denied"] is True


@pytest.mark.asyncio
async def test_another_workspaces_pocket_is_not_found(beanie_test_db, agent):
    sites_ai, _pushed, _state = agent
    pid = await make_pocket(workspace="ws_elsewhere")
    resp = await sites_ai._get_handler({"pocket_id": pid})
    assert resp.get("is_error") is True and "not_found" in resp["content"][0]["text"]


def test_the_tools_ride_the_sites_allow_list_and_register():
    from pocketpaw_ee.agent.mcp_servers.sites import SITES_TOOL_IDS
    from pocketpaw_ee.agent.mcp_servers.sites_ai import SITE_AI_TOOL_IDS, make_site_ai_tools

    assert set(SITE_AI_TOOL_IDS) <= set(SITES_TOOL_IDS)
    names = []

    def _tool(name, description, schema):
        assert len(description) < 1_000
        assert schema["additionalProperties"] is False

        def wrap(fn):
            names.append(name)
            return fn

        return wrap

    make_site_ai_tools(_tool)
    assert [f"mcp__pocketpaw_sites_manager__{n}" for n in names] == list(SITE_AI_TOOL_IDS)
