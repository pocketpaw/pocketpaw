"""A teammate who can read a pocket's thread can also watch its live run.

Created: 2026-09-27 (fix/run-stream-session-readers). Pocket and Paw Site threads
became readable by everyone who can read the pocket (#2244), but the run stream
still 404'd anyone but the run's author, so a teammate opening a thread mid-turn
saw the history and never the reply streaming in. The stream now admits the same
readers the thread history does. Stopping a run stays with its author.
"""

from __future__ import annotations

from datetime import UTC, datetime

import fakeredis.aioredis
import pytest
from pocketpaw_ee.cloud._core.context import RequestContext, ScopeKind
from pocketpaw_ee.cloud.chat.runs import service as run_service
from pocketpaw_ee.cloud.chat.runs.domain import RunSpec
from pocketpaw_ee.cloud.chat.runs.redis_stream import RedisStreamTransport
from pocketpaw_ee.cloud.models.pocket import Pocket as _PocketDoc
from pocketpaw_ee.cloud.sessions import service as sessions_service
from pocketpaw_ee.cloud.sessions.dto import CreateSessionRequest

pytestmark = pytest.mark.asyncio

_WS = "w1"
_OWNER = "u_owner"
_TEAMMATE = "u1"  # runs_app_client identifies as u1 / w1


def _ctx(user_id: str) -> RequestContext:
    return RequestContext(
        user_id=user_id,
        workspace_id=_WS,
        request_id="r",
        scope=ScopeKind.NONE,
        started_at=datetime.now(UTC),
    )


@pytest.fixture
def members(monkeypatch: pytest.MonkeyPatch) -> set[str]:
    """Workspace membership the session read gate consults. Tests edit the set."""
    who = {_OWNER, _TEAMMATE}

    async def _is_member(workspace_id: str, user_id: str) -> bool:
        return workspace_id == _WS and user_id in who

    monkeypatch.setattr(sessions_service, "_is_workspace_member", _is_member, raising=False)
    return who


@pytest.fixture
def transport(monkeypatch: pytest.MonkeyPatch) -> RedisStreamTransport:
    t = RedisStreamTransport(fakeredis.aioredis.FakeRedis(decode_responses=True))
    monkeypatch.setattr("pocketpaw_ee.cloud.chat.runs.router.get_stream_transport", lambda: t)
    return t


async def _owner_run_on_pocket_thread(visibility: str = "workspace") -> str:
    pocket = _PocketDoc(
        workspace=_WS, name="Site", owner=_OWNER, type="site", visibility=visibility
    )
    await pocket.insert()
    session = await sessions_service.create(
        _ctx(_OWNER), _WS, CreateSessionRequest(title="t", pocket_id=str(pocket.id))
    )
    await run_service.create_run(
        RunSpec(
            run_id="owner-run",
            workspace_id=_WS,
            context_type="session",
            scope_id=session.id,
            session_key=f"cloud:session:{session.id}:a1",
            group=None,
            user_id=_OWNER,
            agent_id="a1",
            client_message_id="c-owner",
            user_message_id="m1",
            content="hi",
            history=[],
            intent=None,
        )
    )
    return "owner-run"


@pytest.mark.usefixtures("mongo_db", "members")
async def test_pocket_reader_streams_the_owners_run(runs_app_client, transport) -> None:
    run_id = await _owner_run_on_pocket_thread()
    await transport.append_event(run_id, "tool_start", {"tool": "read_file"})
    await transport.append_event(run_id, "chunk", {"content": "hi"})
    await transport.append_event(run_id, "stream_end", {"assistant_message_id": "m2"})

    resp = await runs_app_client.get(f"/cloud/chat/runs/{run_id}/stream?after=0")

    assert resp.status_code == 200
    assert "event: tool_start" in resp.text
    assert "event: stream_end" in resp.text


@pytest.mark.usefixtures("mongo_db", "members", "transport")
async def test_pocket_reader_cannot_stop_the_owners_run(runs_app_client) -> None:
    run_id = await _owner_run_on_pocket_thread()

    resp = await runs_app_client.post(f"/cloud/chat/runs/{run_id}/stop")

    assert resp.status_code == 404


@pytest.mark.usefixtures("mongo_db", "transport")
async def test_non_member_still_gets_404(runs_app_client, members) -> None:
    run_id = await _owner_run_on_pocket_thread()
    members.discard(_TEAMMATE)

    resp = await runs_app_client.get(f"/cloud/chat/runs/{run_id}/stream?after=0")

    assert resp.status_code == 404


@pytest.mark.usefixtures("mongo_db", "members", "transport")
async def test_private_pocket_run_stays_with_its_author(runs_app_client) -> None:
    run_id = await _owner_run_on_pocket_thread(visibility="private")

    resp = await runs_app_client.get(f"/cloud/chat/runs/{run_id}/stream?after=0")

    assert resp.status_code == 404
