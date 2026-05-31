# tests/cloud/surface/test_tenancy_guards.py — Surface handler tenancy guards.
#
# Created: 2026-05-24 — Follow-up tests for PR #1209 review M1+M2+M3.
# The pocket, pocket_widget, agent, and activity surface handlers all
# consume services that historically gate by user-or-id without a
# workspace filter, so a user who belongs to multiple workspaces could
# stamp a B-workspace id in an A-workspace chat and the preamble would
# echo B's data inside A's context. The guards added in this PR reject
# every cross-workspace stamp; these tests pin that behaviour so a
# future refactor can't quietly walk it back.
#
# Test shape:
#   - Seed ONE user_id (the same human signed in across both workspaces).
#   - Create the artifact (pocket / agent) in W2 owned by that user.
#   - Drive the matching surface handler with workspace_id=W1 and a meta
#     pointing at the W2 artifact.
#   - Assert the preamble does NOT carry the W2 identifying data
#     (name, slug, widgets) — it falls through to the unavailable /
#     placeholder path the handler already uses for missing artifacts.

from __future__ import annotations

import pytest
from pocketpaw_ee.cloud._core.context import RequestContext, ScopeKind
from pocketpaw_ee.cloud.agents import service as agents_service
from pocketpaw_ee.cloud.agents.dto import CreateAgentRequest
from pocketpaw_ee.cloud.models.user import User as _UserDoc
from pocketpaw_ee.cloud.pockets import service as pockets_service
from pocketpaw_ee.cloud.pockets.dto import CreatePocketRequest
from pocketpaw_ee.cloud.surface.domain import SurfaceMeta
from pocketpaw_ee.cloud.surface.handlers import activity as activity_handler
from pocketpaw_ee.cloud.surface.handlers import agent as agent_handler
from pocketpaw_ee.cloud.surface.handlers import pocket as pocket_handler
from pocketpaw_ee.cloud.surface.handlers import pocket_widget as pocket_widget_handler

pytestmark = pytest.mark.usefixtures("mongo_db")

W1 = "ws-tenancy-a"
W2 = "ws-tenancy-b"


async def _seed_user(email: str) -> str:
    """Insert a user the test can attribute pockets / agents to.

    The user's ``active_workspace`` is W1 — the workspace they're
    "chatting from" in every test below. The cross-workspace stamp
    pretends to be a route on W2 instead.
    """
    doc = _UserDoc(
        email=email,
        hashed_password="x",
        is_active=True,
        is_verified=True,
        full_name="Tenancy Owner",
        active_workspace=W1,
    )
    await doc.insert()
    return str(doc.id)


def _ctx(*, user_id: str, workspace_id: str) -> RequestContext:
    """Build a minimal RequestContext for agents_service.create.

    The agent service signature still takes a RequestContext but uses
    only ``user_id`` / ``workspace_id`` for the create path the test
    exercises; the rest of the fields are filler.
    """
    from datetime import UTC, datetime

    return RequestContext(
        user_id=user_id,
        workspace_id=workspace_id,
        request_id="r-tenancy",
        scope=ScopeKind.WORKSPACE,
        started_at=datetime.now(UTC),
    )


# ---------------------------------------------------------------------------
# M1 — pocket handler
# ---------------------------------------------------------------------------


async def test_pocket_handler_rejects_cross_workspace_id() -> None:
    """A pocket owned by the user in W2 must not surface in a W1 chat.

    Setup: same user is the pocket's owner — so ``pockets_service.get``
    would return the pocket fine on its access check. The guard rejects
    on the workspace mismatch instead, and the handler falls through to
    the unavailable-snapshot path.
    """
    user_id = await _seed_user("owner@pocket-tenancy.test")
    pocket = await pockets_service.create(
        W2,
        user_id,
        CreatePocketRequest(name="Secret W2 Pocket"),
    )

    preamble = await pocket_handler.build_preamble(
        W1, user_id, SurfaceMeta(pocket_id=pocket["_id"])
    )

    # The route tag is still rendered (the agent knows it's on a pocket
    # surface) but the W2 data MUST NOT appear.
    assert '<surface kind="pocket"' in preamble
    assert "Secret W2 Pocket" not in preamble
    # The widget count / current-pocket tag never gets emitted on the
    # unavailable path — assert we didn't accidentally render it.
    assert "<current-pocket" not in preamble
    assert "widgets=" not in preamble
    # And the standard unavailable marker is what the user sees.
    assert "unavailable" in preamble.lower()


# ---------------------------------------------------------------------------
# M1 — pocket_widget handler
# ---------------------------------------------------------------------------


async def test_pocket_widget_handler_rejects_cross_workspace_id() -> None:
    """Same guard, plus the focus block must drop too.

    The pocket_widget handler delegates the base preamble to
    pocket_handler (already guarded) but renders the client-supplied
    ``widget_id`` / ``focus_node_id`` independently. The fix in
    pocket_widget.py suppresses the focus block when the underlying
    pocket fetch would be cross-workspace, so no W2 identifiers leak.
    """
    user_id = await _seed_user("owner@widget-tenancy.test")
    pocket = await pockets_service.create(
        W2,
        user_id,
        CreatePocketRequest(name="Secret W2 Widget Pocket"),
    )
    leaky_widget_id = "leaky-widget-id-from-w2"
    leaky_focus_id = "leaky-focus-node-from-w2"

    preamble = await pocket_widget_handler.build_preamble(
        W1,
        user_id,
        SurfaceMeta(
            pocket_id=pocket["_id"],
            widget_id=leaky_widget_id,
            focus_node_id=leaky_focus_id,
        ),
    )

    # Base preamble's tenancy guard still holds.
    assert "Secret W2 Widget Pocket" not in preamble
    assert "<current-pocket" not in preamble
    # The focus block carrying client-supplied W2 identifiers must NOT
    # appear either.
    assert "<widget-focus" not in preamble
    assert leaky_widget_id not in preamble
    assert leaky_focus_id not in preamble


# ---------------------------------------------------------------------------
# M2 — agent handler
# ---------------------------------------------------------------------------


async def test_agent_handler_rejects_cross_workspace_agent_id() -> None:
    """An agent owned by W2 must not be rendered in a W1 chat.

    ``agents_service.get(agent_id)`` is workspace-agnostic, so the call
    succeeds on the id alone. The guard added to the handler compares
    the returned domain object's ``workspace_id`` to the chat's.
    """
    user_id = await _seed_user("owner@agent-tenancy.test")
    agent = await agents_service.create(
        _ctx(user_id=user_id, workspace_id=W2),
        W2,
        CreateAgentRequest(name="Secret W2 Agent", slug="secret-w2-agent"),
    )

    preamble = await agent_handler.build_preamble(W1, user_id, SurfaceMeta(agent_id=agent.id))

    # Surface tag still present so the agent knows it's on /agents/[id].
    assert '<surface kind="agent"' in preamble
    # The W2 agent's name and slug must NOT appear.
    assert "Secret W2 Agent" not in preamble
    assert "secret-w2-agent" not in preamble
    # The current-agent tag never gets emitted on the unavailable path.
    assert "<current-agent" not in preamble
    assert "unavailable" in preamble.lower()


# ---------------------------------------------------------------------------
# M3 — activity handler
# ---------------------------------------------------------------------------


async def test_activity_handler_returns_placeholder_when_buffer_not_workspace_scoped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the buffer can't be filtered by workspace, render a placeholder.

    The handler now requires a workspace-aware read path on the buffer.
    Simulate a buffer without ``get_recent`` (e.g. an older deploy or a
    future refactor that drops the API) and assert the handler emits the
    placeholder list rather than guessing.
    """

    class _UnscopedBuffer:
        """Stand-in buffer with no workspace-aware reader."""

        # No get_recent attribute on purpose.
        events = ["should-not-appear"]

    monkeypatch.setattr(
        "pocketpaw_ee.cloud.activity.buffer.get_buffer",
        lambda: _UnscopedBuffer(),
    )

    preamble = await activity_handler.build_preamble(W1, "u-irrelevant", SurfaceMeta())

    # Surface block still emits — the chat path needs a usable preamble.
    assert '<surface kind="activity"' in preamble
    # The placeholder appears, not the would-be event list.
    assert "per-workspace scope required" in preamble.lower()
    assert "should-not-appear" not in preamble


async def test_activity_handler_uses_workspace_scoped_buffer() -> None:
    """When the buffer IS workspace-scoped, only W1 events appear.

    Seeds two events on the real singleton — one for W1 and one for W2 —
    then calls the handler for W1 and confirms the W2 event is filtered
    out by ``Buffer.get_recent``'s workspace key.
    """
    import time

    from pocketpaw_ee.cloud.activity.buffer import ActivityEvent, get_buffer

    buf = get_buffer()
    buf.reset()
    now = time.time()
    buf.push(
        ActivityEvent(
            workspace_id=W1,
            kind="thinking",
            agent_id="a-w1",
            summary="w1-event-marker",
            pocket_id=None,
            ts=now,
        )
    )
    buf.push(
        ActivityEvent(
            workspace_id=W2,
            kind="thinking",
            agent_id="a-w2",
            summary="w2-event-marker",
            pocket_id=None,
            ts=now,
        )
    )

    preamble = await activity_handler.build_preamble(W1, "u-irrelevant", SurfaceMeta())

    assert '<surface kind="activity"' in preamble
    assert "w1-event-marker" in preamble
    assert "w2-event-marker" not in preamble

    # Clean up so we don't leak state to neighbouring tests.
    buf.reset()
