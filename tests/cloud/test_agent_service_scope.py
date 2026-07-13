"""ScopeContext resolver tests — dm/group/pocket dispatch + target agent.

Uses AsyncMock-substituted Beanie finders so tests stay unit-scoped.
The real Mongo path is exercised by the router integration tests.

Updated: 2026-06-08 (VIP Onboarding Phase B) — added the session-user
isolation gate tests for ``_kb_scopes_for_context``. The centerpiece is the
leak-prevention matrix: member A's private ``user:{A}`` scope must NEVER
appear in member B's resolved scopes nor in any multi-member room — it is
emitted ONLY in A's own solo session. These are the RED-before / GREEN-after
tests for the gate that keeps one member's Gmail/calendar KB out of every
other member's agent context.

Updated: 2026-06-12 (fix/pocket-anchored-chat-context) — added the
``ScopeContext.pocket_summary`` population tests: ``_resolve_pocket`` and
``_resolve_session`` (when the session is pocket-anchored) must stash the
pocket's orientation data (name, description, type, template/pattern, ripple
summary) for ALL pocket types using the already-fetched Pocket doc — no
extra DB round-trips. Home keeps its backend_summary alongside the new
field; non-pocket scopes carry ``pocket_summary=None``.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from pocketpaw_ee.cloud.chat.agent_service import (
    InvalidScope,
    ScopeContext,
    ScopeKind,
    _kb_scopes_for_context,
    resolve_scope_context,
)
from pocketpaw_ee.cloud.shared.errors import CloudError, NotFound


def _ctx(
    *,
    kind: ScopeKind,
    user_id: str,
    members: list[str],
    workspace_id: str = "w1",
    pocket_id: str | None = None,
    target_agent_id: str = "a1",
) -> ScopeContext:
    """Minimal ScopeContext for the KB-scope gate matrix."""
    return ScopeContext(
        kind=kind,
        scope_id="scope-1",
        workspace_id=workspace_id,
        user_id=user_id,
        members=list(members),
        target_agent_id=target_agent_id,
        pocket_id=pocket_id,
    )


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


# ===========================================================================
# VIP Onboarding Phase B — session-user isolation gate for KB scopes.
#
# The gate decides whether ``_kb_scopes_for_context`` emits the member-private
# ``user:{member_id}`` scope. Rule: emit it ONLY when the room is the member's
# own solo context (exactly one member == the authenticated principal). NEVER
# in a multi-member room. This keeps one member's Gmail/calendar KB out of
# every other member's agent session.
# ===========================================================================


def test_solo_session_emits_member_private_user_scope():
    """A member's own solo session injects their private ``user:{id}`` scope,
    at the HEAD of the list (highest priority, ahead of workspace)."""
    ctx = _ctx(kind=ScopeKind.SESSION, user_id="memberA", members=["memberA"])
    scopes = _kb_scopes_for_context(ctx)
    assert scopes[0] == "user:memberA"
    assert "user:memberA" in scopes


def test_leak_prevention_member_A_scope_absent_from_member_B_session():
    """CENTERPIECE LEAK TEST: member A's private scope must NEVER surface in
    member B's resolved scopes. B's solo session yields ``user:B`` and never
    ``user:A``."""
    ctx_b = _ctx(kind=ScopeKind.SESSION, user_id="memberB", members=["memberB"])
    scopes_b = _kb_scopes_for_context(ctx_b)
    assert "user:memberA" not in scopes_b
    assert "user:memberB" in scopes_b


def test_leak_prevention_no_member_private_scope_in_multi_member_room():
    """CENTERPIECE LEAK TEST: a shared room (>1 member) emits NO member-private
    ``user:`` scope for ANY participant — not the caller's, not anyone's."""
    ctx = _ctx(
        kind=ScopeKind.GROUP,
        user_id="memberA",
        members=["memberA", "memberB", "memberC"],
        workspace_id="w1",
    )
    scopes = _kb_scopes_for_context(ctx)
    assert "user:memberA" not in scopes
    assert "user:memberB" not in scopes
    assert "user:memberC" not in scopes
    assert not any(s.startswith("user:") for s in scopes)
    # The shared workspace scope is still present — only the private tier is gated.
    assert "workspace:w1" in scopes


def test_leak_prevention_dm_with_peer_has_no_member_private_scope():
    """A 2-person DM is a shared room: no member-private ``user:`` scope, even
    though the caller is an authenticated member."""
    ctx = _ctx(
        kind=ScopeKind.DM,
        user_id="memberA",
        members=["memberA", "memberB"],
    )
    scopes = _kb_scopes_for_context(ctx)
    assert not any(s.startswith("user:") for s in scopes)


def test_member_private_scope_gated_off_when_principal_not_sole_member():
    """Defense-in-depth: even a single-other-member room (principal NOT in the
    member list, or a stale member set) must not leak a ``user:`` scope. The
    gate keys on ``members == [user_id]`` exactly."""
    # Principal is memberA but the sole listed member is someone else.
    ctx = _ctx(kind=ScopeKind.POCKET, user_id="memberA", members=["memberX"], pocket_id="p1")
    scopes = _kb_scopes_for_context(ctx)
    assert "user:memberA" not in scopes
    assert "user:memberX" not in scopes


def test_solo_pocket_owner_session_emits_user_scope_above_pocket():
    """A private pocket whose only member is the owner is a solo own-context:
    the user scope is emitted and outranks the pocket scope."""
    ctx = _ctx(
        kind=ScopeKind.POCKET,
        user_id="memberA",
        members=["memberA"],
        pocket_id="p1",
    )
    scopes = _kb_scopes_for_context(ctx)
    assert scopes[0] == "user:memberA"
    assert scopes.index("user:memberA") < scopes.index("pocket:p1")


def test_no_user_scope_regression_existing_ordering_preserved():
    """Regression guard: a multi-member room's NON-user scopes keep their exact
    pocket > agent > workspace ordering — the gate adds nothing when off."""
    ctx = ScopeContext(
        kind=ScopeKind.GROUP,
        scope_id="g1",
        workspace_id="w1",
        user_id="memberA",
        members=["memberA", "memberB"],
        target_agent_id="agentZ",
        pocket_id="p1",
    )
    scopes = _kb_scopes_for_context(ctx)
    assert scopes == ["pocket:p1", "agent:agentZ", "workspace:w1"]


# ===========================================================================
# fix/pocket-anchored-chat-context — ScopeContext.pocket_summary population.
#
# The resolvers already hold the fetched Pocket doc; they must stash its
# orientation data (name, description, type, template/pattern metadata, and
# the rippleSpec summary) for EVERY pocket-anchored scope, paying no extra
# DB read. This is what fixes "what's this pocket about?" → "an empty shell".
# ===========================================================================


def _template_pocket_ns(**overrides) -> SimpleNamespace:
    """A template-instantiated pocket: composed rippleSpec, empty legacy
    widgets[] — the exact shape the bug transcript hit."""
    base = dict(
        id="p_apps",
        type="custom",
        name="Applications",
        description="Triage queue for inbound applications",
        template_slug="applications-triage",
        pattern="dashboard",
        workspace="w1",
        owner="u_caller",
        team=["u_caller"],
        agents=["agent_primary"],
        tool_specs=[],
        visibility="workspace",
        shared_with=[],
        widgets=[],
        rippleSpec={
            "version": "1.0",
            "ui": {
                "id": "n_root0000",
                "type": "flex",
                "children": [
                    {"id": "n_header00", "type": "page-header"},
                    {"id": "n_grid0001", "type": "grid"},
                ],
            },
            "state": {"selected_id": None, "applications": []},
            "sources": {
                "applications": {
                    "method": "GET",
                    "path": "/applications?status=open",
                    "bind": "state.applications",
                }
            },
        },
    )
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.mark.asyncio
async def test_resolve_non_home_pocket_populates_pocket_summary():
    """The fix: a non-home pocket scope carries the pocket's summary data —
    name, description, template/pattern metadata, and the ripple summary —
    without any extra backend read."""
    backend_mock = AsyncMock(return_value={"configured": True})
    with (
        patch(
            "pocketpaw_ee.cloud.chat.agent_service._get_pocket",
            AsyncMock(return_value=_template_pocket_ns()),
        ),
        patch("pocketpaw_ee.cloud.pockets.service.get_pocket_backend", backend_mock),
    ):
        ctx = await resolve_scope_context(
            scope="pocket", scope_id="p_apps", user_id="u_caller", agent_id_hint=None
        )
    summary = ctx.pocket_summary
    assert summary is not None
    assert summary["name"] == "Applications"
    assert summary["description"] == "Triage queue for inbound applications"
    assert summary["type"] == "custom"
    assert summary["template_slug"] == "applications-triage"
    assert summary["pattern"] == "dashboard"
    ripple = summary["ripple"]
    assert ripple["has_ripple_spec"] is True
    assert ripple["ui_node_count"] == 2
    assert ripple["ui_node_types"] == ["page-header", "grid"]
    assert "selected_id" in ripple["state_keys"]
    assert ripple["sources"][0]["key"] == "applications"
    assert ripple["widgets_count"] == 0
    # Still no backend read for non-home pockets (existing rule unchanged).
    backend_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_resolve_home_pocket_keeps_backend_summary_and_gains_pocket_summary():
    """Home behavior is unchanged (backend summary still resolved) — the
    pocket summary is additive."""
    pocket = _template_pocket_ns(id="home-1", type="home", name="Home", template_slug=None)
    backend = {"configured": True, "base_url": "https://api.acme.test", "auth_type": "bearer"}
    with (
        patch("pocketpaw_ee.cloud.chat.agent_service._get_pocket", AsyncMock(return_value=pocket)),
        patch(
            "pocketpaw_ee.cloud.pockets.service.get_pocket_backend",
            AsyncMock(return_value=backend),
        ),
    ):
        ctx = await resolve_scope_context(
            scope="pocket", scope_id="home-1", user_id="u_caller", agent_id_hint=None
        )
    assert ctx.backend_summary == backend
    assert ctx.pocket_summary is not None
    assert ctx.pocket_summary["name"] == "Home"
    assert ctx.pocket_summary["type"] == "home"


@pytest.mark.asyncio
async def test_resolve_pocket_summary_degrades_without_ripple_spec():
    """A pocket with no rippleSpec still yields a summary (name/description/
    legacy widgets count) — has_ripple_spec is simply False."""
    pocket = _template_pocket_ns(
        rippleSpec=None, template_slug=None, pattern=None, widgets=["w1", "w2"]
    )
    with patch("pocketpaw_ee.cloud.chat.agent_service._get_pocket", AsyncMock(return_value=pocket)):
        ctx = await resolve_scope_context(
            scope="pocket", scope_id="p_apps", user_id="u_caller", agent_id_hint=None
        )
    assert ctx.pocket_summary is not None
    assert ctx.pocket_summary["ripple"]["has_ripple_spec"] is False
    assert ctx.pocket_summary["ripple"]["widgets_count"] == 2


@pytest.mark.asyncio
async def test_resolve_session_with_pocket_populates_pocket_summary():
    """Pocket chats commonly route through session scope (the frontend
    prefers it) — the same summary must ride along there."""
    from pocketpaw_ee.cloud.models.session import Session

    fake = Session.model_construct(
        id="s1",
        sessionId="ws",
        workspace="w1",
        owner="u_caller",
        agent="a1",
        pocket="p_apps",
        deleted_at=None,
    )
    with (
        patch("pocketpaw_ee.cloud.chat.agent_service._get_session", AsyncMock(return_value=fake)),
        patch(
            "pocketpaw_ee.cloud.chat.agent_service._get_pocket",
            AsyncMock(return_value=_template_pocket_ns()),
        ),
    ):
        ctx = await resolve_scope_context(
            scope="session", scope_id="s1", user_id="u_caller", agent_id_hint=None
        )
    assert ctx.pocket_summary is not None
    assert ctx.pocket_summary["name"] == "Applications"
    assert ctx.pocket_summary["ripple"]["ui_node_count"] == 2


@pytest.mark.asyncio
async def test_resolve_group_scope_has_no_pocket_summary():
    """Non-anchored scopes carry pocket_summary=None — behavior unchanged."""
    group = SimpleNamespace(
        id="g1",
        type="private",
        members=["u_caller"],
        agents=[SimpleNamespace(agent="a1", respond_mode="auto")],
        archived=False,
        workspace="w1",
    )
    with patch("pocketpaw_ee.cloud.chat.agent_service._get_group", AsyncMock(return_value=group)):
        ctx = await resolve_scope_context(
            scope="group", scope_id="g1", user_id="u_caller", agent_id_hint=None
        )
    assert ctx.pocket_summary is None
