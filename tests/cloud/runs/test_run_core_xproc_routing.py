"""When ``xproc.is_worker()`` is True, broadcasts from run_core must go
through the cross-process bridge instead of the local WS manager."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from pocketpaw_ee.cloud._core.realtime import xproc
from pocketpaw_ee.cloud.chat.runs import run_core
from pocketpaw_ee.cloud.chat.runs.run_core import (
    _broadcast_agent_typing,
    _broadcast_message_new,
)

pytestmark = pytest.mark.asyncio


class _Ctx:
    """Minimal stand-in for ScopeContext — only the fields the broadcasts use."""

    def __init__(self, *, members=("u1", "u2"), user_id="u1"):
        self.scope_id = "s1"
        self.user_id = user_id
        self.target_agent_id = "a1"
        self.members = list(members)
        self.kind = type("K", (), {"value": "session"})()


@pytest.fixture
def fake_manager(monkeypatch):
    """Replace the WS manager so we can assert it's NOT called in worker mode."""
    import pocketpaw_ee.cloud.chat.ws as ws_mod

    fake = AsyncMock()
    fake.broadcast_to_group = AsyncMock()
    monkeypatch.setattr(ws_mod, "manager", fake)
    return fake


@pytest.fixture(autouse=True)
def _reset_xproc():
    xproc._reset_for_tests()
    yield
    xproc._reset_for_tests()


# --- agent.typing ----------------------------------------------------------


async def test_typing_in_web_mode_calls_local_manager(fake_manager):
    await _broadcast_agent_typing(_Ctx(), active=True)

    fake_manager.broadcast_to_group.assert_awaited_once()
    args = fake_manager.broadcast_to_group.await_args.args
    assert args[0] == "s1"
    assert args[1] == ["u2"]  # excludes the caller


async def test_typing_in_worker_mode_publishes_via_xproc(fake_manager, monkeypatch):
    xproc.set_role("worker")
    published: list[dict] = []

    async def _capture(**kwargs):
        published.append(kwargs)

    monkeypatch.setattr(run_core.xproc, "publish_ws_envelope", _capture)

    await _broadcast_agent_typing(_Ctx(), active=True)

    fake_manager.broadcast_to_group.assert_not_called()
    assert len(published) == 1
    env = published[0]
    assert env["scope_id"] == "s1"
    assert env["recipients"] == ["u2"]
    assert env["ws_type"] == "agent.typing"
    assert env["ws_data"]["active"] is True
    assert env["ws_data"]["agent_id"] == "a1"


async def test_typing_no_others_skips_in_both_modes(fake_manager, monkeypatch):
    """No recipients → no broadcast in either mode."""
    published: list[dict] = []

    async def _capture(**kwargs):
        published.append(kwargs)

    monkeypatch.setattr(run_core.xproc, "publish_ws_envelope", _capture)

    # Web mode
    await _broadcast_agent_typing(_Ctx(members=("u1",), user_id="u1"), active=True)
    fake_manager.broadcast_to_group.assert_not_called()

    xproc.set_role("worker")
    await _broadcast_agent_typing(_Ctx(members=("u1",), user_id="u1"), active=True)
    assert published == []


# --- message.new ----------------------------------------------------------


async def test_message_new_in_web_mode_calls_local_manager(fake_manager):
    from datetime import UTC, datetime

    await _broadcast_message_new(
        _Ctx(),
        "msg-1",
        "hello",
        [],
        datetime.now(UTC),
    )

    fake_manager.broadcast_to_group.assert_awaited_once()


async def test_message_new_in_worker_mode_publishes_via_xproc(fake_manager, monkeypatch):
    from datetime import UTC, datetime

    xproc.set_role("worker")
    published: list[dict] = []

    async def _capture(**kwargs):
        published.append(kwargs)

    monkeypatch.setattr(run_core.xproc, "publish_ws_envelope", _capture)

    created = datetime.now(UTC)
    await _broadcast_message_new(_Ctx(), "msg-1", "hello", [], created)

    fake_manager.broadcast_to_group.assert_not_called()
    assert len(published) == 1
    env = published[0]
    assert env["scope_id"] == "s1"
    # Recipients include the caller for message.new (existing behaviour).
    assert set(env["recipients"]) == {"u1", "u2"}
    assert env["ws_type"] == "message.new"
    assert env["ws_data"]["id"] == "msg-1"
    assert env["ws_data"]["content"] == "hello"
    assert env["ws_data"]["sender_type"] == "agent"
    assert env["ws_data"]["created_at"] == created.isoformat()


# --- emit() routing -------------------------------------------------------


async def test_emit_in_web_mode_uses_local_bus(monkeypatch):
    from pocketpaw_ee.cloud._core.realtime import emit as emit_mod
    from pocketpaw_ee.cloud._core.realtime.events import GroupCreated

    bus = AsyncMock()
    bus.publish = AsyncMock()
    monkeypatch.setattr(emit_mod, "get_bus", lambda: bus)

    await emit_mod.emit(GroupCreated(data={"id": "g1"}))

    bus.publish.assert_awaited_once()


async def test_emit_in_worker_mode_publishes_via_xproc(monkeypatch):
    from pocketpaw_ee.cloud._core.realtime import emit as emit_mod
    from pocketpaw_ee.cloud._core.realtime.events import GroupCreated

    xproc.set_role("worker")
    bus = AsyncMock()
    bus.publish = AsyncMock()
    monkeypatch.setattr(emit_mod, "get_bus", lambda: bus)

    published: list = []

    async def _capture(event):
        published.append(event)

    monkeypatch.setattr(emit_mod.xproc, "publish_bus_envelope", _capture)

    evt = GroupCreated(data={"id": "g1"})
    await emit_mod.emit(evt)

    bus.publish.assert_not_called()
    assert published == [evt]
