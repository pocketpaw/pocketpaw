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
from pocketpaw_ee.cloud._core.context import RequestContext, ScopeKind
from pocketpaw_ee.cloud.sessions import service as sessions_service
from pocketpaw_ee.cloud.sessions.dto import CreateSessionRequest

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


async def _seed_one_per_surface() -> None:
    await sessions_service.create(
        _ctx(), "w1", CreateSessionRequest(title="from chat", surface="chat")
    )
    await sessions_service.create(
        _ctx(), "w1", CreateSessionRequest(title="from files", surface="files")
    )
    await sessions_service.create(
        _ctx(), "w1", CreateSessionRequest(title="from pockets", surface="pocket_creation")
    )
    await sessions_service.create(
        _ctx(), "w1", CreateSessionRequest(title="from foresight", surface="foresight")
    )
    await sessions_service.create(
        _ctx(),
        "w1",
        CreateSessionRequest(title="legacy"),  # surface=None
    )


async def test_list_for_owner_chat_includes_legacy_null() -> None:
    """Legacy-null-as-chat: ``surface="chat"`` returns chat-stamped rows AND
    legacy ``surface=None`` rows (they predate the split and belong to the
    /chat sidebar), but nothing from files / pockets / foresight."""
    await _seed_one_per_surface()

    chat_only = await sessions_service.list_for_owner(_ctx(), "w1", surface="chat")
    titles = {s.title for s in chat_only}
    assert titles == {"from chat", "legacy"}, (
        "surface=chat must include legacy null rows but exclude other surfaces"
    )


async def test_list_for_owner_files_excludes_legacy_null() -> None:
    """Non-chat surfaces match their tag exactly — legacy nulls never bleed in."""
    await _seed_one_per_surface()

    files_only = await sessions_service.list_for_owner(_ctx(), "w1", surface="files")
    assert {s.title for s in files_only} == {"from files"}


async def test_list_for_owner_foresight_excludes_chat_and_null() -> None:
    """Foresight is a first-class surface — isolated from chat + legacy rows."""
    await _seed_one_per_surface()

    foresight_only = await sessions_service.list_for_owner(_ctx(), "w1", surface="foresight")
    assert {s.title for s in foresight_only} == {"from foresight"}


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


async def test_list_for_owner_page_walks_all_rows_without_dupes() -> None:
    """Keyset pagination returns every chat row exactly once across pages."""
    for i in range(5):
        await sessions_service.create(
            _ctx(), "w1", CreateSessionRequest(title=f"chat-{i}", surface="chat")
        )

    seen: list[str] = []
    cursor: str | None = None
    pages = 0
    while True:
        rows, cursor = await sessions_service.list_for_owner_page(
            _ctx(), "w1", surface="chat", cursor=cursor, limit=2
        )
        seen.extend(s.title for s in rows)
        pages += 1
        if cursor is None:
            break
        assert pages < 10, "pagination did not terminate"

    assert sorted(seen) == ["chat-0", "chat-1", "chat-2", "chat-3", "chat-4"]
    assert len(seen) == len(set(seen)), "no row should appear on two pages"


async def test_list_for_owner_page_is_surface_scoped() -> None:
    """The paginated lister honours the same surface scoping as list_for_owner."""
    await _seed_one_per_surface()

    rows, cursor = await sessions_service.list_for_owner_page(
        _ctx(), "w1", surface="foresight", limit=50
    )
    assert {s.title for s in rows} == {"from foresight"}
    assert cursor is None

    chat_rows, _ = await sessions_service.list_for_owner_page(
        _ctx(), "w1", surface="chat", limit=50
    )
    assert {s.title for s in chat_rows} == {"from chat", "legacy"}
