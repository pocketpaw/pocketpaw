"""Regression: home/widget OSS-path pocket chats must be stamped with the
workspace's default ``pocketpaw`` agent (and the pocket id) so they surface
in the PocketPaw DM room (``GET /sessions?agent_id=<pocketpaw-agent-id>`` ->
``list_by_agent`` filters on ``Session.agent``).

Before the fix, ``auto_create_pocket_session`` built the ``Session`` with no
``agent`` and no ``pocket``, so these chats never appeared in that room. These
tests assert the stamping at creation (OSS path) and verify the cloud
agent-chat path already stamps an agent.

New file — covers:
- OSS path stamps ``agent`` + ``pocket`` via ``auto_create_pocket_session``.
- OSS save path (``_resolve_or_create_session``) threads the pocket id from
  ``entry.metadata["pocket_context"]["id"]`` and the flat ``pocket_id`` fallback.
- Graceful degradation when no default agent exists.
- Cloud path (``ensure_for_agent_scope`` kind="pocket") already stamps agent.
"""

from __future__ import annotations

import uuid

from pocketpaw.memory.protocol import MemoryEntry, MemoryType


async def _seed_user_and_agent(workspace_id: str) -> str:
    """Insert a workspace user and the default ``pocketpaw`` agent; return
    the agent id."""
    from pocketpaw_ee.cloud.models.agent import Agent
    from pocketpaw_ee.cloud.models.user import User, WorkspaceMembership

    user = await User(
        email=f"u-{uuid.uuid4().hex[:6]}@example.com",
        hashed_password="x",
        is_active=True,
        is_verified=True,
        workspaces=[WorkspaceMembership(workspace=workspace_id, role="owner")],
    ).insert()
    agent = await Agent(
        workspace=workspace_id,
        slug="pocketpaw",
        name="PocketPaw",
        owner=str(user.id),
    ).insert()
    return str(agent.id)


class TestAutoCreatePocketSessionStamping:
    async def test_stamps_default_agent_and_pocket(self, beanie_memory_db):
        from pocketpaw_ee.cloud.sessions import service as sessions_service

        ws = f"ws-{uuid.uuid4().hex[:6]}"
        agent_id = await _seed_user_and_agent(ws)
        pocket_id = f"pocket-{uuid.uuid4().hex[:6]}"
        key = f"sess-{uuid.uuid4().hex[:8]}"

        doc = await sessions_service.auto_create_pocket_session(
            key, workspace_id=ws, pocket_id=pocket_id
        )

        assert doc is not None
        assert doc.context_type == "pocket"
        assert doc.agent == agent_id
        assert doc.pocket == pocket_id

    async def test_stamps_agent_without_pocket(self, beanie_memory_db):
        from pocketpaw_ee.cloud.sessions import service as sessions_service

        ws = f"ws-{uuid.uuid4().hex[:6]}"
        agent_id = await _seed_user_and_agent(ws)
        key = f"sess-{uuid.uuid4().hex[:8]}"

        doc = await sessions_service.auto_create_pocket_session(key, workspace_id=ws)

        assert doc is not None
        assert doc.agent == agent_id
        assert doc.pocket is None

    async def test_degrades_when_no_default_agent(self, beanie_memory_db):
        """A workspace user exists but no ``pocketpaw`` agent — the chat save
        must still succeed, just without an agent stamp."""
        from pocketpaw_ee.cloud.models.user import User, WorkspaceMembership
        from pocketpaw_ee.cloud.sessions import service as sessions_service

        ws = f"ws-{uuid.uuid4().hex[:6]}"
        await User(
            email=f"u-{uuid.uuid4().hex[:6]}@example.com",
            hashed_password="x",
            is_active=True,
            is_verified=True,
            workspaces=[WorkspaceMembership(workspace=ws, role="owner")],
        ).insert()
        key = f"sess-{uuid.uuid4().hex[:8]}"

        doc = await sessions_service.auto_create_pocket_session(key, workspace_id=ws)

        assert doc is not None
        assert doc.agent is None


class TestResolveOrCreateThreadsPocket:
    async def test_save_path_stamps_agent_and_pocket_from_metadata(self, store, beanie_memory_db):
        """A pocket chat save (no pre-existing Session) auto-creates a Session
        stamped with the default agent and the pocket id from
        ``metadata['pocket_context']['id']``."""
        from pocketpaw_ee.cloud.sessions import service as sessions_service

        ws = f"ws-{uuid.uuid4().hex[:6]}"
        agent_id = await _seed_user_and_agent(ws)
        pocket_id = f"pocket-{uuid.uuid4().hex[:6]}"
        key = f"sess-{uuid.uuid4().hex[:8]}"

        entry = MemoryEntry(
            id="",
            type=MemoryType.SESSION,
            content="hi from the home pocket",
            role="user",
            session_key=key,
            metadata={
                "workspace_id": ws,
                "source": "pocket_chat",
                "pocket_context": {"id": pocket_id},
            },
        )
        await store.save(entry)

        doc = await sessions_service.find_by_session_id(key)
        assert doc is not None
        assert doc.agent == agent_id
        assert doc.pocket == pocket_id

    async def test_save_path_uses_flat_pocket_id_fallback(self, store, beanie_memory_db):
        from pocketpaw_ee.cloud.sessions import service as sessions_service

        ws = f"ws-{uuid.uuid4().hex[:6]}"
        agent_id = await _seed_user_and_agent(ws)
        pocket_id = f"pocket-{uuid.uuid4().hex[:6]}"
        key = f"sess-{uuid.uuid4().hex[:8]}"

        entry = MemoryEntry(
            id="",
            type=MemoryType.SESSION,
            content="hi",
            role="user",
            session_key=key,
            metadata={"workspace_id": ws, "pocket_id": pocket_id},
        )
        await store.save(entry)

        doc = await sessions_service.find_by_session_id(key)
        assert doc is not None
        assert doc.agent == agent_id
        assert doc.pocket == pocket_id


class TestCloudPathAlreadyStamps:
    async def test_ensure_for_agent_scope_pocket_stamps_agent(self, beanie_memory_db):
        """The cloud agent-chat path (``/cloud/chat/pocket/{id}/agent``) resolves
        the default workspace agent and stamps it — the home pocket (no custom
        agent) lands on ``slug=='pocketpaw'``. No code change; assert only."""
        from pocketpaw_ee.cloud.sessions import service as sessions_service

        ws = f"ws-{uuid.uuid4().hex[:6]}"
        agent_id = await _seed_user_and_agent(ws)
        pocket_id = f"pocket-{uuid.uuid4().hex[:6]}"

        session_id = await sessions_service.ensure_for_agent_scope(
            kind="pocket",
            scope_id=pocket_id,
            workspace_id=ws,
            user_id="u1",
            target_agent_id=agent_id,
        )

        assert session_id is not None
        doc = await sessions_service.find_by_session_id(session_id)
        assert doc is not None
        assert doc.agent == agent_id
        assert doc.pocket == pocket_id


def _load_backfill_module():
    """Import ``scripts/backfill_pocket_session_agent.py`` by path — the
    scripts dir is not a package, so a normal import won't resolve."""
    import importlib.util
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[3]
    path = repo_root / "scripts" / "backfill_pocket_session_agent.py"
    spec = importlib.util.spec_from_file_location("backfill_pocket_session_agent", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestBackfill:
    async def test_stamps_orphaned_pocket_sessions(self, beanie_memory_db):
        from pocketpaw_ee.cloud.models.session import Session

        backfill_mod = _load_backfill_module()

        ws = f"ws-{uuid.uuid4().hex[:6]}"
        agent_id = await _seed_user_and_agent(ws)

        orphan = await Session(
            sessionId=f"sess-{uuid.uuid4().hex[:8]}",
            context_type="pocket",
            workspace=ws,
            owner="u1",
            title="Chat",
        ).insert()
        assert orphan.agent is None

        updated = await backfill_mod.backfill(dry_run=False)
        assert updated == 1

        refetched = await Session.find_one(Session.sessionId == orphan.sessionId)
        assert refetched is not None
        assert refetched.agent == agent_id

    async def test_is_idempotent_and_dry_run_writes_nothing(self, beanie_memory_db):
        from pocketpaw_ee.cloud.models.session import Session

        backfill_mod = _load_backfill_module()

        ws = f"ws-{uuid.uuid4().hex[:6]}"
        agent_id = await _seed_user_and_agent(ws)
        orphan = await Session(
            sessionId=f"sess-{uuid.uuid4().hex[:8]}",
            context_type="pocket",
            workspace=ws,
            owner="u1",
        ).insert()

        # Dry run reports one candidate but writes nothing.
        assert await backfill_mod.backfill(dry_run=True) == 1
        still_orphan = await Session.find_one(Session.sessionId == orphan.sessionId)
        assert still_orphan is not None and still_orphan.agent is None

        # First apply stamps it; a second apply finds nothing to do.
        assert await backfill_mod.backfill(dry_run=False) == 1
        assert await backfill_mod.backfill(dry_run=False) == 0

        stamped = await Session.find_one(Session.sessionId == orphan.sessionId)
        assert stamped is not None and stamped.agent == agent_id

    async def test_skips_session_when_no_default_agent(self, beanie_memory_db):
        from pocketpaw_ee.cloud.models.session import Session

        backfill_mod = _load_backfill_module()

        ws = f"ws-{uuid.uuid4().hex[:6]}"  # no pocketpaw agent seeded
        orphan = await Session(
            sessionId=f"sess-{uuid.uuid4().hex[:8]}",
            context_type="pocket",
            workspace=ws,
            owner="u1",
        ).insert()

        assert await backfill_mod.backfill(dry_run=False) == 0
        unchanged = await Session.find_one(Session.sessionId == orphan.sessionId)
        assert unchanged is not None and unchanged.agent is None
