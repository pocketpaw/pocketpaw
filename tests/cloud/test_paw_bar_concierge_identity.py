# tests/cloud/test_paw_bar_concierge_identity.py — the concierge answers to the
# name its owner gave it.
#
# An owner who sets ``Site.concierge_name`` (the setup wizard's "Name" step)
# expects the concierge to introduce itself by that name, on every surface:
#
#   * v2 runtime: the constant FRAME no longer claims a fixed identity ("You are
#     the concierge for this site"); it points the model at the <owner-settings>
#     block for its name, and the owner block tells it to introduce itself by it.
#     The frame stays constant: no owner text ever reaches it.
#   * legacy runtime: the dedicated agent is named after the concierge, a settings
#     PATCH of the name renames it (only while the name is still the generated
#     one), and the concierge run's instructions carry the owner block, so an
#     agent whose soul was born under the old name still hears the new one.
#   * widget header: the frame boot's ``agentName`` falls back to the concierge
#     name when the look editor's own ``agent_name`` is empty, on the public frame
#     and the owner's preview frame alike.

# The provisioning fixtures (``client``) are reused by importing them.
# ruff: noqa: F811

from __future__ import annotations

from types import SimpleNamespace

import pytest

from tests.cloud.test_paw_bar_agent_provisioning import (  # noqa: F401 — fixtures
    _VALID_KEY,
    _WS,
    _site,
    _widget,
    client,
)

# --------------------------------------------------------------------------- #
# v2: the frame and the owner block
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("frame_name", ["FRAME", "FRAME_DOC_CODE"])
def test_the_frame_takes_its_name_from_the_owner_settings(frame_name):
    from pocketpaw_ee.paw_bar import concierge_runtime

    frame = getattr(concierge_runtime, frame_name)
    # The bug: the frame hard-coded an identity the model introduced itself with.
    assert "You are the concierge for this site" not in frame
    lowered = frame.lower()
    assert "<owner-settings>" in frame
    assert "introduce yourself" in lowered
    # A generic fallback only when no name is set.
    assert "the site's assistant" in lowered
    # Rule 4 names the owner block: trusted for identity and manner, data otherwise.
    rule_4 = next(line for line in frame.splitlines() if line.startswith("4. "))
    assert "<owner-settings>" in rule_4


def test_the_frame_stays_a_constant_without_owner_text():
    from pocketpaw_ee.paw_bar import concierge_runtime

    for frame in (concierge_runtime.FRAME, concierge_runtime.FRAME_DOC_CODE):
        assert "Maya" not in frame
        assert "{" not in frame  # never a format string


def test_a_named_concierge_is_told_to_introduce_itself_by_that_name():
    from pocketpaw_ee.paw_bar import concierge_runtime
    from pocketpaw_ee.paw_bar.concierge_runtime import KnowledgeItem, build_prompt

    site = SimpleNamespace(concierge_name="Maya")
    kb = KnowledgeItem(id="hours", source="pocket:p", text="We open at 7:30am.", score=1.0)
    prompt = build_prompt([kb], SimpleNamespace(spec=None), [], "Who are you?", site=site)

    assert "Your name is «Maya»." in prompt
    assert "Introduce yourself as «Maya»" in prompt
    # Together, the frame and the data half never leave the generic line as the
    # model's only identity.
    assert "the concierge for this site" not in (concierge_runtime.FRAME + prompt)


def test_unsaved_playground_overrides_reach_the_owner_block():
    """The owner's Try it sends unsaved fields; ``render_owner_block`` takes any
    object with the ``concierge_*`` attributes, so an override name is rendered."""
    from pocketpaw_ee.paw_bar.concierge_prompt import render_owner_block

    saved = SimpleNamespace(concierge_name="Maya", concierge_tone="friendly")
    override = SimpleNamespace(**{**vars(saved), "concierge_name": "Juno"})
    block = render_owner_block(override)
    assert "«Juno»" in block and "«Maya»" not in block


# --------------------------------------------------------------------------- #
# Widget header: the frame boot's agentName
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_public_frame_header_falls_back_to_the_concierge_name(client):
    c, _store = client
    await _site(concierge_name="Maya")
    res = await c.get("/paw-bar/frame", params={"key": _VALID_KEY})
    assert res.status_code == 200, res.text
    assert '"agentName": "Maya"' in res.text


@pytest.mark.asyncio
async def test_public_frame_header_prefers_the_look_editors_name(client):
    c, _store = client
    await _site(concierge_name="Maya", concierge_appearance={"agent_name": "Brew Bot"})
    res = await c.get("/paw-bar/frame", params={"key": _VALID_KEY})
    assert res.status_code == 200, res.text
    assert '"agentName": "Brew Bot"' in res.text


@pytest.mark.asyncio
async def test_preview_frame_header_falls_back_to_the_concierge_name(client):
    c, store = client
    site = await _site(concierge_name="Maya")
    await store.create_widget(_widget())
    res = await c.get(f"/paw-bar/admin/site/{site.id}/preview-frame")
    assert res.status_code == 200, res.text
    assert '"agentName": "Maya"' in res.text


# --------------------------------------------------------------------------- #
# Legacy runtime: the dedicated agent and the run's instructions
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_named_concierge_provisions_an_agent_with_that_name(client):
    from pocketpaw_ee.cloud.agents import service as agents_service
    from pocketpaw_ee.paw_bar import agent_provisioning as ap

    _c, store = client
    site = await _site(concierge_name="Maya")
    widget = await store.create_widget(_widget())

    agent = await agents_service.get(await ap.ensure_site_agent(site, widget))
    assert agent.name == "Maya"
    assert "Maya" in agent.config.soul_persona
    assert "Brew & Co" in agent.config.soul_persona


@pytest.mark.asyncio
async def test_renaming_the_concierge_renames_its_dedicated_agent(client):
    from pocketpaw_ee.cloud.agents import service as agents_service
    from pocketpaw_ee.paw_bar import agent_provisioning as ap

    c, store = client
    site = await _site()
    widget = await store.create_widget(_widget())
    agent_id = await ap.ensure_site_agent(site, widget)
    assert (await agents_service.get(agent_id)).name == "Brew & Co Concierge"

    res = await c.patch(f"/paw-bar/admin/site/{site.id}/settings", json={"concierge_name": "Maya"})
    assert res.status_code == 200, res.text
    agent = await agents_service.get(agent_id)
    assert agent.name == "Maya"
    assert "Maya" in agent.config.soul_persona

    # Clearing the name goes back to the generated one.
    res = await c.patch(f"/paw-bar/admin/site/{site.id}/settings", json={"concierge_name": ""})
    assert res.status_code == 200, res.text
    agent = await agents_service.get(agent_id)
    assert agent.name == "Brew & Co Concierge"
    assert "Maya" not in agent.config.soul_persona


@pytest.mark.asyncio
async def test_renaming_the_concierge_leaves_a_hand_bound_agent_alone(client):
    from pocketpaw_ee.cloud.agents import service as agents_service
    from pocketpaw_ee.cloud.agents.dto import CreateAgentRequest

    c, store = client
    site = await _site()
    ctx = agents_service.legacy_ctx("user:maya", _WS)
    mine = await agents_service.create(
        ctx, _WS, CreateAgentRequest(name="Helper", slug="helper", persona="I help.")
    )
    await store.create_widget(_widget(agent_id=mine.id))

    res = await c.patch(f"/paw-bar/admin/site/{site.id}/settings", json={"concierge_name": "Maya"})
    assert res.status_code == 200, res.text
    agent = await agents_service.get(mine.id)
    assert agent.name == "Helper"
    assert agent.config.soul_persona == "I help."


@pytest.mark.asyncio
async def test_renaming_keeps_an_agent_name_the_owner_changed_by_hand(client):
    from pocketpaw_ee.cloud.agents import service as agents_service
    from pocketpaw_ee.cloud.agents.dto import UpdateAgentRequest
    from pocketpaw_ee.paw_bar import agent_provisioning as ap

    c, store = client
    site = await _site()
    widget = await store.create_widget(_widget())
    agent_id = await ap.ensure_site_agent(site, widget)
    ctx = agents_service.legacy_ctx("user:maya", _WS)
    await agents_service.update(ctx, agent_id, UpdateAgentRequest(name="Barista"))

    res = await c.patch(f"/paw-bar/admin/site/{site.id}/settings", json={"concierge_name": "Maya"})
    assert res.status_code == 200, res.text
    assert (await agents_service.get(agent_id)).name == "Barista"


async def _pocket_and_agent():
    from pocketpaw_ee.cloud.models.agent import Agent
    from pocketpaw_ee.cloud.models.pocket import Pocket

    pocket = Pocket(workspace=_WS, name="Shop", owner="user:maya", type="custom")
    await pocket.insert()
    agent = Agent(workspace=_WS, name="Brew & Co Concierge", slug="c", owner="user:maya")
    await agent.insert()
    return pocket, agent


@pytest.mark.asyncio
async def test_a_legacy_concierge_run_is_told_its_name(mongo_db):
    from pocketpaw_ee.cloud.chat.agent_service import (
        build_behavior_instructions,
        resolve_scope_context,
    )

    pocket, agent = await _pocket_and_agent()
    await _site(pocket_id=str(pocket.id), concierge_name="Maya", concierge_tone="friendly")

    ctx = await resolve_scope_context(
        scope="concierge",
        scope_id=str(pocket.id),
        user_id="cust-0001",
        agent_id_hint=str(agent.id),
        expected_workspace_id=_WS,
    )
    instructions = build_behavior_instructions(ctx)
    assert "<owner-settings>" in instructions
    assert "Your name is «Maya»." in instructions
    assert "Sound warm and upbeat." in instructions


@pytest.mark.asyncio
async def test_a_legacy_concierge_without_guided_fields_gets_no_owner_block(mongo_db):
    from pocketpaw_ee.cloud.chat.agent_service import (
        build_behavior_instructions,
        resolve_scope_context,
    )

    pocket, agent = await _pocket_and_agent()
    await _site(pocket_id=str(pocket.id))

    ctx = await resolve_scope_context(
        scope="concierge",
        scope_id=str(pocket.id),
        user_id="cust-0001",
        agent_id_hint=str(agent.id),
        expected_workspace_id=_WS,
    )
    assert "owner-settings" not in build_behavior_instructions(ctx)
