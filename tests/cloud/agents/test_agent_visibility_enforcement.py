# tests/cloud/agents/test_agent_visibility_enforcement.py
# Created: 2026-07-15 (fix/agent-visibility-enforcement, ASG-7) — reproduce-first
# security tests proving that agent ``visibility`` is enforced on the READ and
# ATTACH paths, not just on mutation.
#
# The product promise is "private means private": an agent set to
# ``visibility="private"`` must be invisible to everyone except its owner —
# on config reads, knowledge reads, group attach and pocket attach.
#
# Gap A (READ) — before this fix any licensed user could read ANY agent's full
#   config / knowledge by id via ``get_for_viewer`` (was: ``get``), the list
#   endpoint, and the three knowledge-read endpoints.
# Gap B (ATTACH) — before this fix a group admin or pocket editor could attach
#   another user's PRIVATE agent, and every member then ran it.
#
# Policy under test (mirrors the DM predicate in
# ``chat.group_service.get_or_create_agent_dm``): owner always; else
# same-workspace AND visibility=="workspace"; else visibility=="public".
# A denied read/attach surfaces as ``NotFound`` (404) so a leaked id never
# confirms a private agent's existence.

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pocketpaw_ee.cloud._core.context import RequestContext, ScopeKind
from pocketpaw_ee.cloud._core.errors import NotFound
from pocketpaw_ee.cloud.agents import service as agents_service
from pocketpaw_ee.cloud.agents.dto import CreateAgentRequest

pytestmark = pytest.mark.usefixtures("mongo_db")

WS = "w1"
OWNER = "u_owner"
OTHER = "u_other"


def _ctx(user_id: str, workspace_id: str | None = WS) -> RequestContext:
    return RequestContext(
        user_id=user_id,
        workspace_id=workspace_id,
        request_id="r",
        scope=ScopeKind.NONE,
        started_at=datetime.now(UTC),
    )


async def _make_agent(
    *, owner: str = OWNER, slug: str, visibility: str, workspace: str = WS
) -> str:
    """Create an agent owned by ``owner`` with the given visibility; return id.

    soul_enabled=False keeps create() off the eager-soul pool path.
    """
    body = CreateAgentRequest(
        name=slug.title(),
        slug=slug,
        visibility=visibility,
        soul_enabled=False,
    )
    agent = await agents_service.create(_ctx(owner, workspace), workspace, body)
    return agent.id


# ---------------------------------------------------------------------------
# Predicate helpers — can_read_agent / can_use_agent
# ---------------------------------------------------------------------------


async def test_can_read_predicate_matrix() -> None:
    from beanie import PydanticObjectId
    from pocketpaw_ee.cloud.models.agent import Agent as _AgentDoc

    priv = await _make_agent(slug="priv", visibility="private")
    ws = await _make_agent(slug="wsvis", visibility="workspace")
    pub = await _make_agent(slug="pub", visibility="public")

    priv_doc = await _AgentDoc.get(PydanticObjectId(priv))
    ws_doc = await _AgentDoc.get(PydanticObjectId(ws))
    pub_doc = await _AgentDoc.get(PydanticObjectId(pub))

    can_read = agents_service.can_read_agent
    # Owner may read all three.
    assert can_read(priv_doc, WS, OWNER) is True
    assert can_read(ws_doc, WS, OWNER) is True
    assert can_read(pub_doc, WS, OWNER) is True
    # Non-owner, same workspace: private no, workspace yes, public yes.
    assert can_read(priv_doc, WS, OTHER) is False
    assert can_read(ws_doc, WS, OTHER) is True
    assert can_read(pub_doc, WS, OTHER) is True
    # Cross-workspace viewer: only public leaks across.
    assert can_read(priv_doc, "w2", OTHER) is False
    assert can_read(ws_doc, "w2", OTHER) is False
    assert can_read(pub_doc, "w2", OTHER) is True
    # can_use mirrors can_read.
    assert agents_service.can_use_agent(priv_doc, WS, OTHER) is False
    assert agents_service.can_use_agent(ws_doc, WS, OTHER) is True


# ---------------------------------------------------------------------------
# Gap A #1 — get_for_viewer denies non-owner read of a private agent
# ---------------------------------------------------------------------------


async def test_get_for_viewer_denies_nonowner_private() -> None:
    agent_id = await _make_agent(slug="secret", visibility="private")

    with pytest.raises(NotFound):
        await agents_service.get_for_viewer(agent_id, WS, OTHER)


async def test_get_for_viewer_owner_allowed_private() -> None:
    """POSITIVE control: the owner still reads their own private agent."""
    agent_id = await _make_agent(slug="mine", visibility="private")

    got = await agents_service.get_for_viewer(agent_id, WS, OWNER)
    assert got.id == agent_id


async def test_get_for_viewer_member_allowed_workspace() -> None:
    """POSITIVE control: any workspace member reads a workspace-visible agent."""
    agent_id = await _make_agent(slug="shared", visibility="workspace")

    got = await agents_service.get_for_viewer(agent_id, WS, OTHER)
    assert got.id == agent_id


async def test_get_for_viewer_denies_cross_workspace_workspace_vis() -> None:
    """A workspace-visible agent does not leak to a viewer in a DIFFERENT
    workspace — NotFound, not the agent."""
    agent_id = await _make_agent(slug="wsonly", visibility="workspace")

    with pytest.raises(NotFound):
        await agents_service.get_for_viewer(agent_id, "w2", OTHER)


# ---------------------------------------------------------------------------
# Gap A #2 — list_agents excludes other users' private agents for a viewer
# ---------------------------------------------------------------------------


async def test_list_agents_viewer_excludes_others_private() -> None:
    priv = await _make_agent(slug="priv", visibility="private")
    ws = await _make_agent(slug="wsvis", visibility="workspace")
    pub = await _make_agent(slug="pub", visibility="public")
    own_priv = await _make_agent(owner=OTHER, slug="mine", visibility="private")

    listed = await agents_service.list_agents(WS, viewer_user_id=OTHER)
    ids = {a.id for a in listed}

    # Another user's private agent is NOT visible.
    assert priv not in ids
    # Workspace-visible + public + the viewer's own private agent ARE.
    assert ws in ids
    assert pub in ids
    assert own_priv in ids


async def test_list_agents_no_viewer_is_unfiltered() -> None:
    """Internal callers (planner / kb aggregation) pass no viewer and still
    get every workspace agent — the gate is opt-in via ``viewer_user_id``."""
    priv = await _make_agent(slug="priv", visibility="private")
    ws = await _make_agent(slug="wsvis", visibility="workspace")

    listed = await agents_service.list_agents(WS)
    ids = {a.id for a in listed}
    assert priv in ids
    assert ws in ids


# ---------------------------------------------------------------------------
# Gap A #3 — knowledge reads are gated (HTTP layer)
# ---------------------------------------------------------------------------


def _agents_app(viewer_user_id: str, workspace_id: str = WS) -> FastAPI:
    """A FastAPI app with ONLY the agents router mounted, auth/license
    overridden to a fixed viewer. Used to exercise the read endpoints."""
    from pocketpaw_ee.cloud._core.http import add_error_handler
    from pocketpaw_ee.cloud.agents.router import router as agents_router
    from pocketpaw_ee.cloud.license import require_license
    from pocketpaw_ee.cloud.shared.deps import current_user_id, current_workspace_id

    app = FastAPI()
    add_error_handler(app)
    app.include_router(agents_router)
    app.dependency_overrides[current_user_id] = lambda: viewer_user_id
    app.dependency_overrides[current_workspace_id] = lambda: workspace_id
    app.dependency_overrides[require_license] = lambda: None
    return app


def _client(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://t")


# Sentinel patches so a leak (missing gate) is provable WITHOUT invoking kb-go.
# If the gate is present the endpoint 404s BEFORE these are ever called.
_KB = "pocketpaw_ee.cloud.agents.knowledge.KnowledgeService"


async def test_knowledge_list_denied_for_nonowner_private() -> None:
    agent_id = await _make_agent(slug="secret", visibility="private")

    with patch(f"{_KB}.list_articles", return_value=[{"id": "leak"}]):
        async with _client(_agents_app(OTHER)) as client:
            resp = await client.get(f"/agents/{agent_id}/knowledge")
    assert resp.status_code == 404


async def test_knowledge_search_denied_for_nonowner_private() -> None:
    agent_id = await _make_agent(slug="secret2", visibility="private")

    with patch(f"{_KB}.search", return_value=["leak"]):
        async with _client(_agents_app(OTHER)) as client:
            resp = await client.get(f"/agents/{agent_id}/knowledge/search", params={"q": "x"})
    assert resp.status_code == 404


async def test_knowledge_article_denied_for_nonowner_private() -> None:
    agent_id = await _make_agent(slug="secret3", visibility="private")

    with patch(f"{_KB}.get_article", return_value={"content": "leak"}):
        async with _client(_agents_app(OTHER)) as client:
            resp = await client.get(f"/agents/{agent_id}/knowledge/art1")
    assert resp.status_code == 404


async def test_knowledge_list_allowed_for_owner() -> None:
    """POSITIVE control: the owner still reads their own agent's knowledge."""
    agent_id = await _make_agent(slug="mine2", visibility="private")

    with patch(f"{_KB}.list_articles", return_value=[{"id": "a1"}]):
        async with _client(_agents_app(OWNER)) as client:
            resp = await client.get(f"/agents/{agent_id}/knowledge")
    assert resp.status_code == 200
    assert resp.json() == {"items": [{"id": "a1"}]}


async def test_knowledge_list_allowed_for_workspace_member() -> None:
    """POSITIVE control: a workspace-visible agent's KB is readable by any
    workspace member (view-only surfaces still work)."""
    agent_id = await _make_agent(slug="sharedkb", visibility="workspace")

    with patch(f"{_KB}.list_articles", return_value=[{"id": "a1"}]):
        async with _client(_agents_app(OTHER)) as client:
            resp = await client.get(f"/agents/{agent_id}/knowledge")
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Gap A — HTTP get / list endpoints apply the gate
# ---------------------------------------------------------------------------


async def test_http_get_agent_denies_nonowner_private() -> None:
    agent_id = await _make_agent(slug="secret4", visibility="private")

    async with _client(_agents_app(OTHER)) as client:
        resp = await client.get(f"/agents/{agent_id}")
    assert resp.status_code == 404


async def test_http_get_agent_owner_ok() -> None:
    agent_id = await _make_agent(slug="mine3", visibility="private")

    async with _client(_agents_app(OWNER)) as client:
        resp = await client.get(f"/agents/{agent_id}")
    assert resp.status_code == 200
    assert resp.json()["_id"] == agent_id


async def test_http_list_agents_excludes_others_private() -> None:
    priv = await _make_agent(slug="priv", visibility="private")
    ws = await _make_agent(slug="wsvis", visibility="workspace")

    async with _client(_agents_app(OTHER)) as client:
        resp = await client.get("/agents")
    assert resp.status_code == 200
    ids = {row["_id"] for row in resp.json()}
    assert priv not in ids
    assert ws in ids


# ---------------------------------------------------------------------------
# Gap B #4 — group add_agent rejects another user's private agent
# ---------------------------------------------------------------------------


async def _make_group(owner: str = OTHER, workspace: str = WS) -> str:
    from pocketpaw_ee.cloud.chat import group_service
    from pocketpaw_ee.cloud.chat.schemas import CreateGroupRequest

    resp = await group_service.create_group(
        workspace, owner, CreateGroupRequest(name="G", type="private")
    )
    return resp["_id"] if "_id" in resp else resp["id"]


async def test_group_add_agent_denies_foreign_private() -> None:
    from pocketpaw_ee.cloud.chat import group_service
    from pocketpaw_ee.cloud.chat.schemas import AddGroupAgentRequest

    # Group owned by OTHER (the admin doing the attach).
    group_id = await _make_group(owner=OTHER)
    # Agent owned by OWNER, private, same workspace.
    agent_id = await _make_agent(owner=OWNER, slug="secretgrp", visibility="private")

    with pytest.raises(NotFound):
        await group_service.add_agent(group_id, OTHER, AddGroupAgentRequest(agent_id=agent_id))


async def test_group_add_agent_allows_workspace_visible() -> None:
    """POSITIVE control: a workspace-visible agent can still be attached."""
    from pocketpaw_ee.cloud.chat import group_service
    from pocketpaw_ee.cloud.chat.schemas import AddGroupAgentRequest

    group_id = await _make_group(owner=OTHER)
    agent_id = await _make_agent(owner=OWNER, slug="sharedgrp", visibility="workspace")

    # Must NOT raise.
    await group_service.add_agent(group_id, OTHER, AddGroupAgentRequest(agent_id=agent_id))
    group = await group_service.get_group(group_id, OTHER)
    attached = {a.get("agent") or a.get("agent_id") for a in group.get("agents", [])}
    assert agent_id in attached


# ---------------------------------------------------------------------------
# Gap B #5 — pocket add_agent rejects another user's private agent
# ---------------------------------------------------------------------------


async def _make_pocket(owner: str, workspace: str = WS, visibility: str = "private") -> str:
    from pocketpaw_ee.cloud.models.pocket import Pocket as _PocketDoc

    doc = _PocketDoc(workspace=workspace, name="P", owner=owner, visibility=visibility)
    await doc.insert()
    return str(doc.id)


async def test_pocket_add_agent_denies_foreign_private() -> None:
    from pocketpaw_ee.cloud.pockets import service as pockets_service

    # Pocket owned by OTHER (the editor doing the attach).
    pocket_id = await _make_pocket(owner=OTHER)
    # Agent owned by OWNER, private, same workspace.
    agent_id = await _make_agent(owner=OWNER, slug="secretpkt", visibility="private")

    with pytest.raises(NotFound):
        await pockets_service.add_agent(pocket_id, OTHER, agent_id)


async def test_pocket_add_agent_allows_workspace_visible() -> None:
    """POSITIVE control: a workspace-visible agent can still be attached."""
    from pocketpaw_ee.cloud.pockets import service as pockets_service

    pocket_id = await _make_pocket(owner=OTHER)
    agent_id = await _make_agent(owner=OWNER, slug="sharedpkt", visibility="workspace")

    result = await pockets_service.add_agent(pocket_id, OTHER, agent_id)
    # The agent id lands in the pocket's agent list.
    assert agent_id in [
        a if isinstance(a, str) else (a.get("_id") or a.get("id")) for a in result.get("agents", [])
    ]
