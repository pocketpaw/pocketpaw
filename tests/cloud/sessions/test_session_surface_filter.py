"""Regression: session bleed across /chat, /pockets, /files surfaces.

Bug recap (parent diagnosis): three frontend chat surfaces (``/chat``,
``/pockets`` pocket-creation mode, ``/files``) all hit
``POST /sessions`` followed by ``POST /cloud/chat/session/{mongo_id}/agent``.
The resulting ``Session`` rows are indistinguishable on
``pocket=None`` + ``context_type="session"``, so the ``/chat`` sidebar
filter ``(s) => !s.pocket`` lists every session-scope row regardless of
which surface created it.

Fix: stamp the originating surface on the ``Session`` row. Backwards
compatible — legacy rows keep ``surface=None`` and continue to appear in
unfiltered listings.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from ee.cloud._core.context import RequestContext, ScopeKind
from ee.cloud.sessions import service as sessions_service
from ee.cloud.sessions.dto import CreateSessionRequest

pytestmark = pytest.mark.usefixtures("mongo_db")


def _ctx(user_id: str = "u1", workspace_id: str | None = "w1") -> RequestContext:
    return RequestContext(
        user_id=user_id,
        workspace_id=workspace_id,
        request_id="r",
        scope=ScopeKind.NONE,
        started_at=datetime.now(UTC),
    )


async def test_create_persists_surface_field() -> None:
    """The DTO accepts ``surface`` and the domain mirrors it."""
    s = await sessions_service.create(
        _ctx(),
        "w1",
        CreateSessionRequest(title="from chat", surface="chat"),
    )
    assert s.surface == "chat"

    # Refetch to confirm the value round-trips through Mongo.
    refetched = await sessions_service.get(_ctx(), s.id)
    assert refetched.surface == "chat"


async def test_create_without_surface_defaults_to_none() -> None:
    """Backwards compatibility: legacy callers omitting ``surface`` get None."""
    s = await sessions_service.create(_ctx(), "w1", CreateSessionRequest(title="legacy"))
    assert s.surface is None


async def test_list_for_owner_filters_by_surface() -> None:
    """``surface="chat"`` returns only chat-originated rows; legacy ``surface=None``
    rows are excluded from the filtered listing (they originated elsewhere /
    pre-migration and shouldn't pollute the /chat sidebar)."""
    await sessions_service.create(
        _ctx(), "w1", CreateSessionRequest(title="from chat", surface="chat")
    )
    await sessions_service.create(
        _ctx(), "w1", CreateSessionRequest(title="from files", surface="files")
    )
    await sessions_service.create(
        _ctx(),
        "w1",
        CreateSessionRequest(title="from pockets", surface="pocket_creation"),
    )
    await sessions_service.create(
        _ctx(),
        "w1",
        CreateSessionRequest(title="legacy"),  # surface=None
    )

    chat_only = await sessions_service.list_for_owner(_ctx(), "w1", surface="chat")
    titles = {s.title for s in chat_only}
    assert titles == {"from chat"}, (
        "surface=chat filter must return only sessions stamped with surface=chat"
    )


async def test_list_for_owner_without_filter_returns_all() -> None:
    """No filter passed → preserve legacy behavior: every row (including
    legacy ``surface=None`` rows) is returned. This is critical so the
    migration is non-disruptive for callers that haven't adopted the param."""
    await sessions_service.create(
        _ctx(), "w1", CreateSessionRequest(title="from chat", surface="chat")
    )
    await sessions_service.create(
        _ctx(), "w1", CreateSessionRequest(title="from files", surface="files")
    )
    await sessions_service.create(
        _ctx(),
        "w1",
        CreateSessionRequest(title="from pockets", surface="pocket_creation"),
    )
    await sessions_service.create(
        _ctx(),
        "w1",
        CreateSessionRequest(title="legacy"),  # surface=None
    )

    all_sessions = await sessions_service.list_for_owner(_ctx(), "w1")
    assert len(all_sessions) == 4
    titles = {s.title for s in all_sessions}
    assert titles == {"from chat", "from files", "from pockets", "legacy"}
