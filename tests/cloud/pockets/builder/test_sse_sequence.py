# Pockets builder — SSE sequence contract test.
#
# Created 2026-05-01.  This is the load-bearing test: the frontend's
# event consumer depends on the exact sequence emitted when a pocket
# create flow runs.  Mocks ``run_intent_from_message`` and the agent
# pool / persistence helpers so the test exercises ``_run_agent_stream``
# end-to-end without any network or Mongo calls.

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import patch

import pytest

from ee.cloud.chat import agent_router as router_mod
from ee.cloud.chat.agent_router import _run_agent_stream
from ee.cloud.chat.agent_schemas import CloudAgentChatRequest
from ee.cloud.chat.agent_service import ScopeContext, ScopeKind
from ee.cloud.pockets.builder.domain import BuilderEvent


def _ctx() -> ScopeContext:
    return ScopeContext(
        kind=ScopeKind.SESSION,
        scope_id="sess-1",
        workspace_id="ws-1",
        user_id="u-1",
        members=["u-1"],
        target_agent_id="agent-1",
        agent_ids_in_scope=["agent-1"],
    )


def _body() -> CloudAgentChatRequest:
    return CloudAgentChatRequest(content="build me a stripe research pocket")


class _FakeInstance:
    agent_name = "stub"
    backend = type("StubBackend", (), {})()


class _FakePool:
    """Pool stub — ``get`` returns a fake instance; ``run`` and ``observe``
    are async no-ops.  ``run`` returns an empty async iterator so if the
    builder dispatch DOESN'T short-circuit, the test sees no chunks but
    also no crash."""

    async def get(self, _agent_id: str) -> Any:
        return _FakeInstance()

    async def run(self, *_args: Any, **_kwargs: Any) -> AsyncIterator[Any]:
        if False:  # pragma: no cover - kept to satisfy the async generator shape
            yield None
        return

    async def observe(self, *_args: Any, **_kwargs: Any) -> None:
        return None


async def _builder_create_sequence(*_args: Any, **_kwargs: Any) -> AsyncIterator[BuilderEvent]:
    """Stand-in for ``run_intent_from_message`` that emits a successful
    create flow.  Mirrors the real service's event sequence so the test
    locks the contract the frontend depends on."""
    yield BuilderEvent("intent.detected", {"intent": "pocket_create", "confidence": 0.95})
    yield BuilderEvent("spec.building", {})
    yield BuilderEvent(
        "pocket.created",
        {"pocket_id": "p-1", "pocket": {"_id": "p-1", "name": "Stripe"}},
    )
    yield BuilderEvent(
        "chunk", {"content": "Built Stripe — a research pocket.", "type": "text"}
    )


@pytest.mark.asyncio
async def test_agent_router_emits_builder_sequence_for_pocket_create(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Patch the pool, the persist helper, and the broadcast helper so we
    # don't need Mongo / WebSocket plumbing.
    monkeypatch.setattr(router_mod, "get_agent_pool", lambda: _FakePool())

    class _FakeAssistantMsg:
        id = "msg-1"
        createdAt = None

    async def _fake_persist(*_args: Any, **_kwargs: Any) -> Any:
        return _FakeAssistantMsg()

    async def _fake_broadcast(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(router_mod, "_persist_assistant_message", _fake_persist)
    monkeypatch.setattr(router_mod, "_broadcast_agent_typing", _fake_broadcast)
    monkeypatch.setattr(router_mod, "_broadcast_message_new", _fake_broadcast)

    # The builder import lives inside ``_dispatch_builder`` (lazy) so we
    # patch the underlying symbol where it lives in the builder package.
    with patch(
        "ee.cloud.pockets.builder.run_intent_from_message",
        side_effect=_builder_create_sequence,
    ):
        events: list[tuple[str, dict[str, Any]]] = []
        async for name, data in _run_agent_stream(
            _ctx(),
            user_message_id="msg-user-1",
            body=_body(),
            cancel_event=asyncio.Event(),
        ):
            events.append((name, data))

    names = [n for n, _ in events]

    # Required ordering: stream_start MUST come first; the four builder
    # events MUST appear in order; stream_end MUST be last.  Other side-
    # channel events (like ``agent.typing``) are tolerated between them.
    assert names[0] == "stream_start", f"unexpected first event: {names}"
    assert names[-1] == "stream_end", f"unexpected last event: {names}"
    expected_middle = [
        "intent.detected",
        "spec.building",
        "pocket.created",
        "chunk",
    ]
    middle_idx = [names.index(n) for n in expected_middle]
    assert middle_idx == sorted(middle_idx), (
        f"builder events out of order: {names}"
    )
    assert all(n in names for n in expected_middle), (
        f"builder events missing: {names}"
    )

    # The stream_end payload reflects the persisted assistant message id
    # because the builder's chunk text was accumulated and persisted.
    end_payload = events[-1][1]
    assert end_payload.get("assistant_message_id") == "msg-1"
    assert end_payload.get("cancelled") is False
