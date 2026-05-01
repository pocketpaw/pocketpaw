# Tests for ``ee.cloud.pockets.builder.service``.
#
# Created 2026-05-01.  Five cases — happy classify, none classify, spec
# validation, full create flow event sequence, and the intent-hint
# short-circuit that skips the classifier call.

from __future__ import annotations

from typing import Any

import pytest

from ee.cloud.pockets.builder import service as service_mod
from ee.cloud.pockets.builder.domain import (
    BuilderEvent,
    IntentKind,
    PocketSpec,
)
from ee.cloud.pockets.builder.dto import BuildRequest, IntentDetectionResult
from ee.cloud.pockets.builder.providers import ProviderError
from ee.cloud.pockets.builder.service import (
    build_pocket_spec,
    detect_intent,
    run_intent_from_message,
)


def _req(**overrides: Any) -> BuildRequest:
    base = {
        "user_message": "build me a stripe research pocket",
        "workspace_id": "ws1",
        "user_id": "u1",
        "session_mongo_id": "sess1",
        "provider": "anthropic",
    }
    base.update(overrides)
    return BuildRequest(**base)


@pytest.mark.asyncio
async def test_detect_intent_returns_create(fake_provider: Any) -> None:
    fake_provider.configure(
        [IntentDetectionResult(intent="pocket_create", confidence=0.92)]
    )
    out = await detect_intent(_req())
    assert out.intent == "pocket_create"
    assert out.confidence == pytest.approx(0.92)


@pytest.mark.asyncio
async def test_detect_intent_returns_none(fake_provider: Any) -> None:
    fake_provider.configure(
        [IntentDetectionResult(intent="none", confidence=0.31)]
    )
    out = await detect_intent(_req(user_message="what time is it?"))
    assert out.intent == "none"


@pytest.mark.asyncio
async def test_build_pocket_spec_validates(fake_provider: Any) -> None:
    spec = PocketSpec(
        name="Stripe Research",
        description="Q4 outlook",
        type="research",
        color="#0A84FF",
        ripple_spec={"version": "1.0", "ui": {"type": "text", "props": {}}},
    )
    fake_provider.configure([spec])
    out = await build_pocket_spec(_req())
    assert out.name == "Stripe Research"
    assert out.ripple_spec is not None


@pytest.mark.asyncio
async def test_run_intent_create_flow_yields_correct_events(
    fake_provider: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Stub the pocket service.agent_create call so we don't hit Mongo.
    async def _fake_agent_create(**kwargs: Any) -> Any:
        return ({"_id": "p1", **kwargs}, "p1", None)

    monkeypatch.setattr(
        "ee.cloud.pockets.builder.service.pockets_service.agent_create",
        _fake_agent_create,
    )

    spec = PocketSpec(
        name="Stripe Research",
        description="Q4 outlook",
        type="research",
        color="#0A84FF",
        ripple_spec={"version": "1.0", "ui": {"type": "text", "props": {}}},
    )
    fake_provider.configure(
        [
            IntentDetectionResult(intent="pocket_create", confidence=0.95),
            spec,
        ]
    )

    events: list[BuilderEvent] = []
    async for ev in run_intent_from_message(_req()):
        events.append(ev)

    names = [e.name for e in events]
    assert names == [
        "intent.detected",
        "spec.building",
        "pocket.created",
        "chunk",
    ], f"unexpected sequence: {names}"
    assert events[2].data.get("pocket_id") == "p1"
    assert events[3].data.get("type") == "text"
    assert "Stripe Research" in events[3].data.get("content", "")


@pytest.mark.asyncio
async def test_run_intent_skips_classify_when_intent_hint_provided(
    fake_provider: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _fake_agent_create(**kwargs: Any) -> Any:
        return ({"_id": "p2"}, "p2", None)

    monkeypatch.setattr(
        "ee.cloud.pockets.builder.service.pockets_service.agent_create",
        _fake_agent_create,
    )

    spec = PocketSpec(name="Hinted Pocket", color="#0A84FF")
    # Configure ONLY the spec builder return — no classifier entry.
    fake_provider.configure([spec])

    events: list[BuilderEvent] = []
    async for ev in run_intent_from_message(
        _req(intent_hint=IntentKind.CREATE.value)
    ):
        events.append(ev)

    # If the classifier ran, fake_provider would error out (only 1 item
    # configured but two would be popped).  Verify the call count.
    assert len(fake_provider.calls) == 1
    # The single call must be for the spec schema (PocketSpec), not the
    # classifier's IntentDetectionResult.
    assert fake_provider.calls[0]["schema"] is PocketSpec

    names = [e.name for e in events]
    assert names == [
        "intent.detected",
        "spec.building",
        "pocket.created",
        "chunk",
    ]


@pytest.mark.asyncio
async def test_run_intent_classifier_failure_falls_through_silently(
    fake_provider: Any,
) -> None:
    # Per design §8 exception clause: classifier failure with no hint
    # surfaces as an ``intent.detected(none)`` event so the SSE handler
    # falls through to the normal agent run.
    fake_provider.configure([ProviderError("api_error", "boom")])
    events = []
    async for ev in run_intent_from_message(_req()):
        events.append(ev)
    assert len(events) == 1
    assert events[0].name == "intent.detected"
    assert events[0].data["intent"] == "none"
