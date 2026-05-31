# Tests for cloud user + workspace threading through the agent
# pocket-creation path.
# Created: 2026-04-22
# Updated: 2026-05-20 — OSS-EE split (Phase 3b). The user/workspace
#   resolution logic moved out of ``src/pocketpaw/agents/loop.py`` into
#   ``pocketpaw_ee.cloud.pockets.service.create_pocket_and_session``;
#   ``loop._create_pocket_and_session`` is now a thin provider shim. The
#   resolution tests below target the service function directly; the
#   ``_publish_pocket_event`` tests still cover the core loop shim.

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest


def _install_ee_cloud_stubs(monkeypatch, *, user, workspace_by_id=None, create_ret=None):
    """Patch the cloud model classes + ``create()`` service call that
    ``create_pocket_and_session`` reaches, so the resolution heuristics run
    without Mongo.

    ``workspace_by_id`` is a dict ``{oid_str: workspace_or_None}`` consulted
    by the stub ``Workspace.get`` — lets individual tests control whether
    the active_workspace lookup hits or misses.
    """
    from pocketpaw_ee.cloud.pockets import service as pockets_service

    workspace_by_id = workspace_by_id or {}

    # ── Fake User/Workspace/Session documents ──────────────────────────
    get_user = AsyncMock(return_value=user)
    find_user = AsyncMock(return_value=user)

    # Workspace.get(oid) — dict-driven so tests can control hit/miss.
    async def _ws_get(oid):
        return workspace_by_id.get(str(oid))

    get_ws = AsyncMock(side_effect=_ws_get)
    # first-owned / any-workspace fallbacks
    find_owned_ws = AsyncMock(return_value=None)

    fake_user = SimpleNamespace(get=get_user, find_one=find_user)

    class _WorkspaceStub:
        # Mimic the Beanie Document constant used in the call
        # (``Workspace.owner == user_id`` — evaluating at call time).
        owner = "owner"  # placeholder; find_one mock ignores the value

    _WorkspaceStub.get = get_ws
    _WorkspaceStub.find_one = find_owned_ws

    # Session(...) is instantiated inside the service — return a spy instance.
    session_insert = AsyncMock()

    class _SessionDoc:
        # ``Session.find_one(Session.sessionId == safe_key)`` — ``sessionId``
        # has to resolve to *something* that supports ``==``.
        sessionId = "sessionId"  # placeholder — find_one ignores the value

        def __init__(self, **kwargs):
            self.kwargs = kwargs

        async def insert(self):
            await session_insert(self.kwargs)

        async def save(self):  # used when an existing session is updated
            pass

    _SessionDoc.find_one = AsyncMock(return_value=None)  # type: ignore[attr-defined]

    # create(workspace_id, user_id, body) — the same-module pocket-create
    # service function create_pocket_and_session delegates to.
    pocket_create = AsyncMock(return_value=create_ret or {"_id": "pocket-xyz"})

    # Patch exactly what create_pocket_and_session reaches: the lazily
    # imported model classes, the same-module create() service fn, and
    # PydanticObjectId (the real one rejects the non-ObjectId test ids).
    monkeypatch.setattr("pocketpaw_ee.cloud.models.user.User", fake_user)
    monkeypatch.setattr("pocketpaw_ee.cloud.models.workspace.Workspace", _WorkspaceStub)
    monkeypatch.setattr("pocketpaw_ee.cloud.models.session.Session", _SessionDoc)
    monkeypatch.setattr(pockets_service, "create", pocket_create)
    monkeypatch.setattr(pockets_service, "PydanticObjectId", lambda s: s)

    return SimpleNamespace(
        service=pockets_service,
        pocket_create=pocket_create,
        find_user=find_user,
        get_user=get_user,
        get_ws=get_ws,
        find_owned_ws=find_owned_ws,
        session_insert=session_insert,
    )


def _mk_user(user_id="u1", active_workspace=None):
    return SimpleNamespace(id=user_id, active_workspace=active_workspace)


def _mk_workspace(ws_id):
    return SimpleNamespace(id=ws_id, owner="u1")


@pytest.mark.asyncio
async def test_explicit_user_and_workspace_are_used(monkeypatch):
    """When user_id + workspace_id are passed, the pocket is created against
    exactly those ids — no find_one fallback."""
    user = _mk_user(user_id="u-alice", active_workspace="ws-stale")
    ws_active = _mk_workspace("ws-active")

    stubs = _install_ee_cloud_stubs(
        monkeypatch,
        user=user,
        workspace_by_id={"ws-active": ws_active},
    )

    result = await stubs.service.create_pocket_and_session(
        {"title": "Dashboard", "metadata": {"category": "custom"}},
        "websocket:abc",
        user_id="u-alice",
        workspace_id="ws-active",
    )

    assert result == "pocket-xyz"
    # Workspace.get must have been called with the explicit id.
    stubs.get_ws.assert_awaited_with("ws-active")
    # create() must receive the explicit workspace_id + user_id.
    args = stubs.pocket_create.await_args.args
    assert args[0] == "ws-active"
    assert args[1] == "u-alice"
    # User.find_one must NOT have been consulted — explicit user_id wins.
    stubs.find_user.assert_not_awaited()


@pytest.mark.asyncio
async def test_only_user_id_falls_back_to_active_workspace(monkeypatch):
    """When workspace_id is missing but user_id is given, ``user.active_workspace``
    is used — not the legacy ``Workspace.find_one(owner=...)`` fallback."""
    user = _mk_user(user_id="u-alice", active_workspace="ws-active")
    ws_active = _mk_workspace("ws-active")

    stubs = _install_ee_cloud_stubs(
        monkeypatch,
        user=user,
        workspace_by_id={"ws-active": ws_active},
    )

    result = await stubs.service.create_pocket_and_session(
        {"title": "Dashboard"},
        "websocket:abc",
        user_id="u-alice",
    )

    assert result == "pocket-xyz"
    stubs.get_ws.assert_awaited_with("ws-active")
    # find_one (the legacy fallback) must never fire.
    stubs.find_owned_ws.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_ids_falls_back_to_first_user_and_owned_workspace(monkeypatch):
    """No context → legacy behaviour. Preserves single-user self-hosted."""
    user = _mk_user(user_id="u-first", active_workspace=None)
    ws_owned = _mk_workspace("ws-owned")

    stubs = _install_ee_cloud_stubs(
        monkeypatch,
        user=user,
        workspace_by_id={},  # no active_workspace — get() misses
    )
    stubs.find_owned_ws.return_value = ws_owned

    result = await stubs.service.create_pocket_and_session(
        {"title": "Dashboard"},
        "websocket:abc",
    )

    assert result == "pocket-xyz"
    stubs.find_user.assert_awaited()
    stubs.find_owned_ws.assert_awaited()
    args = stubs.pocket_create.await_args.args
    assert args[0] == "ws-owned"
    assert args[1] == "u-first"


@pytest.mark.asyncio
async def test_invalid_user_id_falls_back_cleanly(monkeypatch, caplog):
    """An unparseable user id logs a warning and falls back to
    ``User.find_one`` rather than raising."""
    user = _mk_user(user_id="u-fallback", active_workspace="ws-active")
    ws_active = _mk_workspace("ws-active")

    stubs = _install_ee_cloud_stubs(
        monkeypatch,
        user=user,
        workspace_by_id={"ws-active": ws_active},
    )

    # Force PydanticObjectId to raise on a specific bad id.
    def _bad_oid(s):
        if s == "not-an-oid":
            raise ValueError("bad oid")
        return s

    monkeypatch.setattr(stubs.service, "PydanticObjectId", _bad_oid)
    caplog.set_level(logging.WARNING, logger="pocketpaw_ee.cloud.pockets.service")

    result = await stubs.service.create_pocket_and_session(
        {"title": "Dashboard"},
        "websocket:abc",
        user_id="not-an-oid",
    )

    assert result == "pocket-xyz"
    assert any("Invalid cloud_user_id" in rec.message for rec in caplog.records)
    stubs.find_user.assert_awaited()  # fallback engaged
    args = stubs.pocket_create.await_args.args
    assert args[1] == "u-fallback"


@pytest.mark.asyncio
async def test_session_is_linked_with_correct_workspace(monkeypatch):
    """The Session document created alongside the pocket is keyed to the
    explicit workspace/user ids, not the legacy fallback."""
    user = _mk_user(user_id="u-alice", active_workspace=None)
    ws_active = _mk_workspace("ws-active")

    stubs = _install_ee_cloud_stubs(
        monkeypatch,
        user=user,
        workspace_by_id={"ws-active": ws_active},
    )

    await stubs.service.create_pocket_and_session(
        {"title": "Dashboard"},
        "websocket:abc",
        user_id="u-alice",
        workspace_id="ws-active",
    )

    # Session insert captured the expected workspace + owner.
    stubs.session_insert.assert_awaited()
    recorded = stubs.session_insert.await_args.args[0]
    assert recorded["workspace"] == "ws-active"
    assert recorded["owner"] == "u-alice"
    assert recorded["sessionId"] == "websocket_abc"
    assert recorded["pocket"] == "pocket-xyz"


@pytest.mark.asyncio
async def test_publish_pocket_event_forwards_metadata(monkeypatch):
    """``_publish_pocket_event`` pulls cloud_user_id + cloud_workspace_id out
    of the passed metadata dict and forwards them to the pocket creator."""
    from pocketpaw.agents import loop as loop_mod

    captured: dict = {}

    async def _fake_create(spec, session_key, user_id=None, workspace_id=None):
        captured["spec"] = spec
        captured["session_key"] = session_key
        captured["user_id"] = user_id
        captured["workspace_id"] = workspace_id
        return "pocket-123"

    monkeypatch.setattr(loop_mod, "_create_pocket_and_session", _fake_create)

    # Minimal bus stub — only publish_system is called.
    bus = MagicMock()
    bus.publish_system = AsyncMock()

    content = '{"pocket_event": "created", "spec": {"title": "Dashboard"}}'
    metadata = {
        "cloud_user_id": "u-alice",
        "cloud_workspace_id": "ws-active",
        "source": "rest_api",
    }

    await loop_mod._publish_pocket_event(bus, content, "websocket:abc", metadata)

    assert captured["user_id"] == "u-alice"
    assert captured["workspace_id"] == "ws-active"
    assert captured["session_key"] == "websocket:abc"
    bus.publish_system.assert_awaited_once()


@pytest.mark.asyncio
async def test_publish_pocket_event_without_metadata_uses_none(monkeypatch):
    """Default metadata (None) is safe — the creator sees user_id=None,
    workspace_id=None and hits the legacy fallback path."""
    from pocketpaw.agents import loop as loop_mod

    captured: dict = {}

    async def _fake_create(spec, session_key, user_id=None, workspace_id=None):
        captured["user_id"] = user_id
        captured["workspace_id"] = workspace_id
        return None

    monkeypatch.setattr(loop_mod, "_create_pocket_and_session", _fake_create)

    bus = MagicMock()
    bus.publish_system = AsyncMock()

    content = '{"pocket_event": "created", "spec": {"title": "x"}}'

    # No metadata kwarg passed — backwards-compatible call shape.
    await loop_mod._publish_pocket_event(bus, content, "websocket:abc")

    assert captured["user_id"] is None
    assert captured["workspace_id"] is None
