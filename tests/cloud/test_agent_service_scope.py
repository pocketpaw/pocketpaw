"""ScopeContext resolver tests — dm/group/pocket dispatch + target agent.

Uses AsyncMock-substituted Beanie finders so tests stay unit-scoped.
The real Mongo path is exercised by the router integration tests.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from pocketpaw_ee.cloud.chat.agent_service import (
    InvalidScope,
    ScopeKind,
    resolve_scope_context,
)
from pocketpaw_ee.cloud.shared.errors import CloudError, NotFound


@pytest.mark.asyncio
async def test_resolve_dm_with_agent_peer_picks_that_agent():
    group = SimpleNamespace(
        id="g1",
        type="dm",
        members=["u_caller", "u_peer"],
        agents=[SimpleNamespace(agent="agent_peer_1", respond_mode="auto")],
        archived=False,
        workspace="w1",
    )
    with patch("pocketpaw_ee.cloud.chat.agent_service._get_group", AsyncMock(return_value=group)):
        ctx = await resolve_scope_context(
            scope="dm", scope_id="g1", user_id="u_caller", agent_id_hint=None
        )
    assert ctx.kind == ScopeKind.DM
    assert ctx.scope_id == "g1"
    assert ctx.target_agent_id == "agent_peer_1"
    assert ctx.workspace_id == "w1"
    assert ctx.members == ["u_caller", "u_peer"]


@pytest.mark.asyncio
async def test_resolve_group_requires_agent_id_when_multiple_agents():
    group = SimpleNamespace(
        id="g1",
        type="private",
        members=["u_caller", "u_other"],
        agents=[
            SimpleNamespace(agent="a1", respond_mode="auto"),
            SimpleNamespace(agent="a2", respond_mode="auto"),
        ],
        archived=False,
        workspace="w1",
    )
    with patch("pocketpaw_ee.cloud.chat.agent_service._get_group", AsyncMock(return_value=group)):
        with pytest.raises(CloudError):
            await resolve_scope_context(
                scope="group", scope_id="g1", user_id="u_caller", agent_id_hint=None
            )


@pytest.mark.asyncio
async def test_resolve_group_defaults_to_sole_agent():
    group = SimpleNamespace(
        id="g1",
        type="private",
        members=["u_caller"],
        agents=[SimpleNamespace(agent="only_one", respond_mode="auto")],
        archived=False,
        workspace="w1",
    )
    with patch("pocketpaw_ee.cloud.chat.agent_service._get_group", AsyncMock(return_value=group)):
        ctx = await resolve_scope_context(
            scope="group", scope_id="g1", user_id="u_caller", agent_id_hint=None
        )
    assert ctx.target_agent_id == "only_one"


@pytest.mark.asyncio
async def test_resolve_rejects_non_member():
    group = SimpleNamespace(
        id="g1",
        type="private",
        members=["u_other"],
        agents=[SimpleNamespace(agent="a1", respond_mode="auto")],
        archived=False,
        workspace="w1",
    )
    with patch("pocketpaw_ee.cloud.chat.agent_service._get_group", AsyncMock(return_value=group)):
        with pytest.raises(CloudError):
            await resolve_scope_context(
                scope="group", scope_id="g1", user_id="u_caller", agent_id_hint=None
            )


@pytest.mark.asyncio
async def test_resolve_pocket_uses_first_agent_when_no_hint():
    pocket = SimpleNamespace(
        id="p1",
        workspace="w1",
        owner="u_caller",
        team=["u_caller"],
        agents=["agent_primary", "agent_secondary"],
        tool_specs=[{"kind": "builtin", "id": "web_fetch"}],
        visibility="workspace",
        shared_with=[],
    )
    with patch("pocketpaw_ee.cloud.chat.agent_service._get_pocket", AsyncMock(return_value=pocket)):
        ctx = await resolve_scope_context(
            scope="pocket", scope_id="p1", user_id="u_caller", agent_id_hint=None
        )
    assert ctx.kind == ScopeKind.POCKET
    assert ctx.target_agent_id == "agent_primary"
    assert ctx.pocket_tool_specs == [{"kind": "builtin", "id": "web_fetch"}]


@pytest.mark.asyncio
async def test_resolve_home_pocket_populates_backend_summary():
    """A type='home' pocket scope must carry the non-secret backend summary
    on the resolved ScopeContext so the (sync) prompt builder can render the
    configured base_url into HOME_POCKET_PROMPT. Mirrors how the
    pocket_specialist fetches `get_pocket_backend`."""
    pocket = SimpleNamespace(
        id="home-1",
        type="home",
        workspace="w1",
        owner="u_caller",
        team=["u_caller"],
        agents=["agent_primary"],
        tool_specs=[],
        visibility="workspace",
        shared_with=[],
    )
    summary = {"configured": True, "base_url": "https://api.acme.test", "auth_type": "bearer"}
    with (
        patch("pocketpaw_ee.cloud.chat.agent_service._get_pocket", AsyncMock(return_value=pocket)),
        patch(
            "pocketpaw_ee.cloud.pockets.service.get_pocket_backend",
            AsyncMock(return_value=summary),
        ),
    ):
        ctx = await resolve_scope_context(
            scope="pocket", scope_id="home-1", user_id="u_caller", agent_id_hint=None
        )
    assert ctx.pocket_type == "home"
    assert ctx.backend_summary == summary


@pytest.mark.asyncio
async def test_resolve_non_home_pocket_skips_backend_summary():
    """A normal (non-home) pocket scope does NOT pay the backend-summary read —
    only the home agent inlines it into a static prompt; ordinary pockets get
    the summary lazily via get_pocket when the specialist needs it."""
    pocket = SimpleNamespace(
        id="p1",
        type="custom",
        workspace="w1",
        owner="u_caller",
        team=["u_caller"],
        agents=["agent_primary"],
        tool_specs=[],
        visibility="workspace",
        shared_with=[],
    )
    backend_mock = AsyncMock(return_value={"configured": True})
    with (
        patch("pocketpaw_ee.cloud.chat.agent_service._get_pocket", AsyncMock(return_value=pocket)),
        patch("pocketpaw_ee.cloud.pockets.service.get_pocket_backend", backend_mock),
    ):
        ctx = await resolve_scope_context(
            scope="pocket", scope_id="p1", user_id="u_caller", agent_id_hint=None
        )
    assert ctx.backend_summary is None
    backend_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_resolve_unknown_scope_raises():
    with pytest.raises(InvalidScope):
        await resolve_scope_context(scope="nope", scope_id="x", user_id="u", agent_id_hint=None)


@pytest.mark.asyncio
async def test_resolve_group_not_found_raises_notfound():
    with patch("pocketpaw_ee.cloud.chat.agent_service._get_group", AsyncMock(return_value=None)):
        with pytest.raises(NotFound):
            await resolve_scope_context(
                scope="group", scope_id="missing", user_id="u", agent_id_hint=None
            )


@pytest.mark.asyncio
async def test_resolve_rejects_dm_route_for_non_dm_group():
    group = SimpleNamespace(
        id="g1",
        type="private",
        members=["u_caller"],
        agents=[SimpleNamespace(agent="a1", respond_mode="auto")],
        archived=False,
        workspace="w1",
    )
    with patch("pocketpaw_ee.cloud.chat.agent_service._get_group", AsyncMock(return_value=group)):
        with pytest.raises(CloudError):
            await resolve_scope_context(
                scope="dm", scope_id="g1", user_id="u_caller", agent_id_hint=None
            )


@pytest.mark.asyncio
async def test_resolve_rejects_group_route_for_dm_group():
    group = SimpleNamespace(
        id="g1",
        type="dm",
        members=["u_caller", "u_peer"],
        agents=[SimpleNamespace(agent="a1", respond_mode="auto")],
        archived=False,
        workspace="w1",
    )
    with patch("pocketpaw_ee.cloud.chat.agent_service._get_group", AsyncMock(return_value=group)):
        with pytest.raises(CloudError):
            await resolve_scope_context(
                scope="group", scope_id="g1", user_id="u_caller", agent_id_hint=None
            )


@pytest.mark.asyncio
async def test_resolve_rejects_archived_group():
    group = SimpleNamespace(
        id="g1",
        type="private",
        members=["u_caller"],
        agents=[SimpleNamespace(agent="a1", respond_mode="auto")],
        archived=True,
        workspace="w1",
    )
    with patch("pocketpaw_ee.cloud.chat.agent_service._get_group", AsyncMock(return_value=group)):
        with pytest.raises(CloudError):
            await resolve_scope_context(
                scope="group", scope_id="g1", user_id="u_caller", agent_id_hint=None
            )


@pytest.mark.asyncio
async def test_resolve_pocket_falls_back_to_workspace_default_agent():
    pocket = SimpleNamespace(
        id="p1",
        workspace="w1",
        owner="u_caller",
        team=["u_caller"],
        agents=[],
        tool_specs=[],
        visibility="workspace",
        shared_with=[],
    )
    with (
        patch("pocketpaw_ee.cloud.chat.agent_service._get_pocket", AsyncMock(return_value=pocket)),
        patch(
            "pocketpaw_ee.cloud.chat.agent_service._get_default_workspace_agent_id",
            AsyncMock(return_value="agent_default_pp"),
        ),
    ):
        ctx = await resolve_scope_context(
            scope="pocket", scope_id="p1", user_id="u_caller", agent_id_hint=None
        )
    assert ctx.target_agent_id == "agent_default_pp"
    assert ctx.agent_ids_in_scope == ["agent_default_pp"]


@pytest.mark.asyncio
async def test_resolve_pocket_no_agents_and_no_default_raises():
    pocket = SimpleNamespace(
        id="p1",
        workspace="w1",
        owner="u_caller",
        team=["u_caller"],
        agents=[],
        tool_specs=[],
        visibility="workspace",
        shared_with=[],
    )
    with (
        patch("pocketpaw_ee.cloud.chat.agent_service._get_pocket", AsyncMock(return_value=pocket)),
        patch(
            "pocketpaw_ee.cloud.chat.agent_service._get_default_workspace_agent_id",
            AsyncMock(return_value=None),
        ),
    ):
        with pytest.raises(CloudError):
            await resolve_scope_context(
                scope="pocket", scope_id="p1", user_id="u_caller", agent_id_hint=None
            )


@pytest.mark.asyncio
async def test_resolve_pocket_dedupes_members_across_team_and_shared():
    pocket = SimpleNamespace(
        id="p1",
        workspace="w1",
        owner="u_owner",
        team=["u_owner", "u_alice"],  # owner duplicated intentionally
        shared_with=["u_alice", "u_bob"],  # alice duplicated across lists
        agents=["agent_primary"],
        tool_specs=[],
        visibility="workspace",
    )
    with patch("pocketpaw_ee.cloud.chat.agent_service._get_pocket", AsyncMock(return_value=pocket)):
        ctx = await resolve_scope_context(
            scope="pocket", scope_id="p1", user_id="u_owner", agent_id_hint=None
        )
    assert ctx.members == ["u_owner", "u_alice", "u_bob"]


def test_session_kind_value():
    assert ScopeKind.SESSION.value == "session"


def test_scopekind_accepts_session_string():
    assert ScopeKind("session") is ScopeKind.SESSION


@pytest.mark.asyncio
async def test_session_scope_happy_path():
    from pocketpaw_ee.cloud.models.session import Session

    fake = Session.model_construct(
        id="s1",
        sessionId="websocket_abc",
        workspace="w1",
        owner="u1",
        agent="a1",
        pocket=None,
        deleted_at=None,
    )
    with patch("pocketpaw_ee.cloud.chat.agent_service._get_session", AsyncMock(return_value=fake)):
        ctx = await resolve_scope_context(
            scope="session", scope_id="s1", user_id="u1", agent_id_hint=None
        )
    assert ctx.kind is ScopeKind.SESSION
    assert ctx.scope_id == "s1"
    assert ctx.workspace_id == "w1"
    assert ctx.target_agent_id == "a1"
    assert ctx.members == ["u1"]


@pytest.mark.asyncio
async def test_session_scope_not_found():
    with patch("pocketpaw_ee.cloud.chat.agent_service._get_session", AsyncMock(return_value=None)):
        with pytest.raises(NotFound):
            await resolve_scope_context(
                scope="session", scope_id="missing", user_id="u1", agent_id_hint=None
            )


@pytest.mark.asyncio
async def test_session_scope_deleted_treated_as_not_found():
    from datetime import UTC, datetime

    from pocketpaw_ee.cloud.models.session import Session

    fake = Session.model_construct(
        id="s1",
        sessionId="ws",
        workspace="w1",
        owner="u1",
        agent="a1",
        pocket=None,
        deleted_at=datetime.now(UTC),
    )
    with patch("pocketpaw_ee.cloud.chat.agent_service._get_session", AsyncMock(return_value=fake)):
        with pytest.raises(NotFound):
            await resolve_scope_context(
                scope="session", scope_id="s1", user_id="u1", agent_id_hint=None
            )


@pytest.mark.asyncio
async def test_session_scope_wrong_owner_forbidden():
    from pocketpaw_ee.cloud.models.session import Session

    fake = Session.model_construct(
        id="s1",
        sessionId="ws",
        workspace="w1",
        owner="other",
        agent="a1",
        pocket=None,
        deleted_at=None,
    )
    with patch("pocketpaw_ee.cloud.chat.agent_service._get_session", AsyncMock(return_value=fake)):
        with pytest.raises(CloudError) as exc:
            await resolve_scope_context(
                scope="session", scope_id="s1", user_id="u1", agent_id_hint=None
            )
    assert exc.value.code == "session.forbidden"


@pytest.mark.asyncio
async def test_session_scope_agent_id_hint_overrides():
    from pocketpaw_ee.cloud.models.session import Session

    fake = Session.model_construct(
        id="s1",
        sessionId="ws",
        workspace="w1",
        owner="u1",
        agent="a1",
        pocket=None,
        deleted_at=None,
    )
    with patch("pocketpaw_ee.cloud.chat.agent_service._get_session", AsyncMock(return_value=fake)):
        ctx = await resolve_scope_context(
            scope="session", scope_id="s1", user_id="u1", agent_id_hint="a2"
        )
    assert ctx.target_agent_id == "a2"


@pytest.mark.asyncio
async def test_session_scope_no_agent_errors():
    from pocketpaw_ee.cloud.models.session import Session

    fake = Session.model_construct(
        id="s1",
        sessionId="ws",
        workspace="w1",
        owner="u1",
        agent=None,
        pocket=None,
        deleted_at=None,
    )
    with patch("pocketpaw_ee.cloud.chat.agent_service._get_session", AsyncMock(return_value=fake)):
        with pytest.raises(CloudError) as exc:
            await resolve_scope_context(
                scope="session", scope_id="s1", user_id="u1", agent_id_hint=None
            )
    assert exc.value.code == "session.no_agent"
