# Tests for cloud user + active_workspace threading through the chat endpoints.
# Created: 2026-04-22
#
# The chat router populates ``InboundMessage.metadata`` with
# ``cloud_user_id`` + ``cloud_workspace_id`` when ``resolve_cloud_context``
# returns a non-None user.
#
# We exercise the chat module at two levels:
#   1. ``resolve_cloud_context`` — the FastAPI dep itself, unit-level.
#   2. ``_build_inbound_message`` / ``_send_message`` — direct calls that
#      capture the InboundMessage published to the bus so we can assert
#      on metadata without standing up TestClient + streaming machinery.

from __future__ import annotations

from types import SimpleNamespace

import pytest


@pytest.fixture
def stub_bus(monkeypatch):
    """Replace the message bus so publish_inbound captures the message
    instead of routing it through the real loop."""
    captured: dict = {}

    class _StubBus:
        async def publish_inbound(self, msg):
            captured["msg"] = msg

    from pocketpaw import bus as bus_mod

    monkeypatch.setattr(bus_mod, "get_message_bus", lambda: _StubBus())
    return captured


@pytest.fixture(autouse=True)
def stub_resolver(monkeypatch):
    """Short-circuit media resolution so _build_inbound_message doesn't
    attempt to read real files for our synthetic requests."""
    from pocketpaw.uploads import resolver as resolver_mod

    async def _noop(urls):
        return []

    monkeypatch.setattr(resolver_mod, "resolve_media_with_records", _noop)
    yield


@pytest.mark.asyncio
async def test_resolve_cloud_context_returns_none_pair_for_null_user():
    """``resolve_cloud_context`` yields ``(None, None)`` when no user is
    authenticated. Works whether or not ee.cloud is mounted — both
    branches of the top-level try/except define a dep with this contract."""
    import inspect

    from pocketpaw.api.v1.chat import resolve_cloud_context

    sig = inspect.signature(resolve_cloud_context)
    if "user" in sig.parameters:
        result = await resolve_cloud_context(user=None)  # type: ignore[call-arg]
    else:
        result = await resolve_cloud_context()
    assert result == (None, None)


@pytest.mark.asyncio
async def test_resolve_cloud_context_returns_ids_for_authenticated_user():
    """With a resolved user, the dep yields (user_id, active_workspace)."""
    import inspect

    from pocketpaw.api.v1.chat import resolve_cloud_context

    user = SimpleNamespace(id="u-alice", active_workspace="ws-active")

    sig = inspect.signature(resolve_cloud_context)
    if "user" in sig.parameters:
        uid, wsid = await resolve_cloud_context(user=user)  # type: ignore[call-arg]
        assert uid == "u-alice"
        assert wsid == "ws-active"
    else:
        # ee.cloud not mounted in this environment — the dep is zero-arg.
        # In that case the only contract is (None, None), which is what
        # the no-auth case covers. Skip this assertion cleanly.
        pytest.skip("pocketpaw_ee.cloud not mounted — nothing to auth against")


@pytest.mark.asyncio
async def test_build_inbound_message_writes_cloud_keys_when_present():
    """Given a non-empty cloud_ctx, the built message carries both ids
    in metadata."""
    from pocketpaw.api.v1.chat import _build_inbound_message
    from pocketpaw.api.v1.schemas.chat import ChatRequest

    req = ChatRequest(content="hi", session_id=None, media=[])
    _, msg = await _build_inbound_message(req, cloud_ctx=("u-alice", "ws-active"))

    assert msg.metadata["cloud_user_id"] == "u-alice"
    assert msg.metadata["cloud_workspace_id"] == "ws-active"
    assert msg.metadata["source"] == "rest_api"


@pytest.mark.asyncio
async def test_build_inbound_message_omits_missing_keys():
    """Partial cloud_ctx (only user_id, no workspace) writes only the
    populated key — the missing key is absent from the dict so the
    downstream fallback branches can trigger."""
    from pocketpaw.api.v1.chat import _build_inbound_message
    from pocketpaw.api.v1.schemas.chat import ChatRequest

    req = ChatRequest(content="hi", session_id=None, media=[])
    _, msg = await _build_inbound_message(req, cloud_ctx=("u-alice", None))

    assert msg.metadata["cloud_user_id"] == "u-alice"
    assert "cloud_workspace_id" not in msg.metadata


@pytest.mark.asyncio
async def test_build_inbound_message_default_cloud_ctx_is_empty():
    """No cloud_ctx (default) leaves metadata without cloud_* keys so
    non-cloud callers (CLI, Telegram, Discord) behave as before."""
    from pocketpaw.api.v1.chat import _build_inbound_message
    from pocketpaw.api.v1.schemas.chat import ChatRequest

    req = ChatRequest(content="hi", session_id=None, media=[])
    _, msg = await _build_inbound_message(req)

    assert "cloud_user_id" not in msg.metadata
    assert "cloud_workspace_id" not in msg.metadata
    assert msg.metadata["source"] == "rest_api"


@pytest.mark.asyncio
async def test_send_message_propagates_cloud_ctx_to_bus(stub_bus):
    """_send_message threads cloud_ctx through to the InboundMessage
    published on the bus. This is the contract the agent loop reads."""
    from pocketpaw.api.v1.chat import _send_message
    from pocketpaw.api.v1.schemas.chat import ChatRequest

    req = ChatRequest(content="make me a dashboard", session_id=None, media=[])
    await _send_message(req, cloud_ctx=("u-alice", "ws-active"))

    msg = stub_bus["msg"]
    assert msg.content == "make me a dashboard"
    assert msg.metadata["cloud_user_id"] == "u-alice"
    assert msg.metadata["cloud_workspace_id"] == "ws-active"


@pytest.mark.asyncio
async def test_send_message_without_cloud_ctx_publishes_clean_metadata(stub_bus):
    """_send_message with the default cloud_ctx leaves metadata without
    cloud_* keys — the non-cloud, backwards-compatible path."""
    from pocketpaw.api.v1.chat import _send_message
    from pocketpaw.api.v1.schemas.chat import ChatRequest

    req = ChatRequest(content="hi", session_id=None, media=[])
    await _send_message(req)

    msg = stub_bus["msg"]
    assert "cloud_user_id" not in msg.metadata
    assert "cloud_workspace_id" not in msg.metadata
