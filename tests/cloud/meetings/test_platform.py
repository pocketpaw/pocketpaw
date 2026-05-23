"""Phase 1 platform tests — protocol + registry + events.

These cover the unified meetings platform itself (the parts added in the
entrypoint PR), not the Recall implementation that was folded in. Recall
behaviour is covered by the existing 160 tests; this file pins the
platform contract.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pocketpaw_ee.cloud.meetings import events
from pocketpaw_ee.cloud.meetings.providers import base


@pytest.fixture(autouse=True)
def _clean_registry():
    """Every test in THIS module starts with an empty registry and
    restores whatever was registered before — so the autoloaded
    RecallProvider survives for sibling test files (e.g.
    test_recall_provider.py)."""
    snapshot = dict(base._REGISTRY)
    base._clear_registry_for_tests()
    yield
    base._REGISTRY.update(snapshot)


class _StubProvider:
    """Minimal MeetingProvider implementation — covers the protocol surface."""

    name = "stub"

    async def create(self, ctx, body):  # noqa: ARG002
        return base.ProviderCreateResult(
            provider_payload={"created": True}, join_url="https://stub/x"
        )

    async def start(self, ctx, meeting):  # noqa: ARG002
        return base.ProviderStartResult(provider_payload_updates={"started": True})

    async def cancel(self, ctx, meeting):  # noqa: ARG002
        return None

    async def end(self, ctx, meeting):  # noqa: ARG002
        return None


def test_registry_resolves_registered_provider():
    """register(p) then resolve(p.name) returns the same instance."""
    provider = _StubProvider()
    base.register(provider)
    assert base.resolve("stub") is provider
    assert base.registered_sources() == ["stub"]


def test_registry_raises_for_unregistered_source():
    """resolve() on an unknown source raises ProviderNotRegistered with a
    structured CloudError code so the API surface returns a useful 503."""
    with pytest.raises(base.ProviderNotRegistered) as exc:
        base.resolve("livekit")
    assert exc.value.code == "meeting.provider_not_registered"
    assert exc.value.status_code == 503
    assert "livekit" in exc.value.message


def test_runtime_checkable_protocol():
    """Provider classes that match the shape pass isinstance() checks —
    proves the protocol is actually runtime_checkable so service.py can
    use isinstance() to detect optional capabilities."""
    provider = _StubProvider()
    assert isinstance(provider, base.MeetingProvider)
    # No request_recording → does NOT satisfy SupportsRecording.
    assert not isinstance(provider, base.SupportsRecording)


def test_event_types_pin_meeting_namespace():
    """All meeting events live under the `meeting.*` namespace so a single
    audience filter in the realtime bus can fan them out together."""
    pairs = [
        (events.MeetingScheduled, "meeting.scheduled"),
        (events.MeetingStarted, "meeting.started"),
        (events.MeetingEnded, "meeting.ended"),
        (events.MeetingCancelled, "meeting.cancelled"),
        (events.MeetingReminder, "meeting.reminder"),
        (events.MeetingRecordingReady, "meeting.recording_ready"),
        (events.MeetingTranscriptReady, "meeting.transcript_ready"),
    ]
    for cls, expected_type in pairs:
        ev = cls(data={"workspace_id": "w1", "meeting_id": "m1"})
        assert ev.type == expected_type, f"{cls.__name__} expected type={expected_type}"
        assert isinstance(ev.ts, datetime)
        assert ev.ts.tzinfo is UTC
        assert ev.data["workspace_id"] == "w1"
