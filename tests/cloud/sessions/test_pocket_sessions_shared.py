"""Pocket (and Paw Site) conversations are visible to everyone who can read the pocket.

Created: 2026-09-25 (fix/shared-pocket-chat-visibility). Reproduces the report
"the chat of the sites is only visible to the user who did it": the site builder
recovers its rail from ``GET /pockets/<id>/sessions`` and each thread's
``/history``, and both filtered on ``owner == caller``, so a teammate opening a
workspace site saw an empty rail. Reads are now shared with anyone who can read
the pocket; writes (rename, delete, sending a turn) stay owner-only.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pocketpaw_ee.cloud._core.context import RequestContext, ScopeKind
from pocketpaw_ee.cloud.models.pocket import Pocket as _PocketDoc
from pocketpaw_ee.cloud.sessions import service as sessions_service
from pocketpaw_ee.cloud.sessions.dto import CreateSessionRequest, UpdateSessionRequest
from pocketpaw_ee.cloud.shared.errors import Forbidden, NotFound

pytestmark = pytest.mark.usefixtures("mongo_db")

_WS = "w1"
_OWNER = "u_owner"
_TEAMMATE = "u_teammate"


def _ctx(user_id: str, workspace_id: str | None = _WS) -> RequestContext:
    return RequestContext(
        user_id=user_id,
        workspace_id=workspace_id,
        request_id="r",
        scope=ScopeKind.NONE,
        started_at=datetime.now(UTC),
    )


async def _pocket(**overrides) -> str:
    fields = {
        "workspace": _WS,
        "name": "Marketing site",
        "owner": _OWNER,
        "type": "site",
        "visibility": "workspace",
    }
    fields.update(overrides)
    doc = _PocketDoc(**fields)
    await doc.insert()
    return str(doc.id)


async def _owner_thread(pocket_id: str) -> str:
    s = await sessions_service.create(
        _ctx(_OWNER), _WS, CreateSessionRequest(title="built the site", pocket_id=pocket_id)
    )
    return s.id


@pytest.fixture
def teammate_is_member(monkeypatch: pytest.MonkeyPatch) -> None:
    """Both users belong to ``_WS``; nobody belongs to any other workspace."""

    async def _is_member(workspace_id: str, user_id: str) -> bool:
        return workspace_id == _WS and user_id in (_OWNER, _TEAMMATE)

    monkeypatch.setattr(sessions_service, "_is_workspace_member", _is_member, raising=False)


# --- the reported bug --------------------------------------------------------


@pytest.mark.usefixtures("teammate_is_member")
async def test_teammate_sees_the_owners_thread_on_a_workspace_pocket() -> None:
    pid = await _pocket()
    sid = await _owner_thread(pid)

    rows = await sessions_service.list_for_pocket(_ctx(_TEAMMATE), pid)

    assert [r.id for r in rows] == [sid]
    assert rows[0].owner == _OWNER


@pytest.mark.usefixtures("teammate_is_member")
async def test_teammate_can_read_the_owners_thread_history() -> None:
    pid = await _pocket()
    sid = await _owner_thread(pid)

    history = await sessions_service.get_history(sid, _TEAMMATE)
    assert history["messages"] == []  # readable (no Forbidden); empty only because none were written
    assert (await sessions_service.get(_ctx(_TEAMMATE), sid)).id == sid


@pytest.mark.usefixtures("teammate_is_member")
async def test_explicit_share_on_a_private_pocket_also_grants_the_read() -> None:
    pid = await _pocket(visibility="private", shared_with=[_TEAMMATE])
    sid = await _owner_thread(pid)

    rows = await sessions_service.list_for_pocket(_ctx(_TEAMMATE), pid)
    assert [r.id for r in rows] == [sid]


# --- what must stay closed ---------------------------------------------------


@pytest.mark.usefixtures("teammate_is_member")
async def test_private_pocket_threads_stay_with_their_owner() -> None:
    pid = await _pocket(visibility="private")
    sid = await _owner_thread(pid)

    assert await sessions_service.list_for_pocket(_ctx(_TEAMMATE), pid) == []
    with pytest.raises((Forbidden, NotFound)):
        await sessions_service.get_history(sid, _TEAMMATE)


@pytest.mark.usefixtures("teammate_is_member")
async def test_a_non_member_of_the_pockets_workspace_sees_nothing() -> None:
    pid = await _pocket()
    sid = await _owner_thread(pid)

    assert await sessions_service.list_for_pocket(_ctx("u_outsider", "w_other"), pid) == []
    with pytest.raises((Forbidden, NotFound)):
        await sessions_service.get_history(sid, "u_outsider")


@pytest.mark.usefixtures("teammate_is_member")
async def test_a_plain_thread_with_no_pocket_stays_private() -> None:
    s = await sessions_service.create(_ctx(_OWNER), _WS, CreateSessionRequest(title="dm"))

    with pytest.raises(Forbidden):
        await sessions_service.get_history(s.id, _TEAMMATE)


@pytest.mark.usefixtures("teammate_is_member")
async def test_teammate_cannot_rename_or_delete_the_owners_thread() -> None:
    pid = await _pocket()
    sid = await _owner_thread(pid)

    with pytest.raises(Forbidden):
        await sessions_service.update(_ctx(_TEAMMATE), sid, UpdateSessionRequest(title="mine now"))
    with pytest.raises(Forbidden):
        await sessions_service.delete(_ctx(_TEAMMATE), sid)


@pytest.mark.usefixtures("teammate_is_member")
async def test_the_owner_still_sees_their_own_thread() -> None:
    pid = await _pocket(visibility="private")
    sid = await _owner_thread(pid)

    rows = await sessions_service.list_for_pocket(_ctx(_OWNER), pid)
    assert [r.id for r in rows] == [sid]
