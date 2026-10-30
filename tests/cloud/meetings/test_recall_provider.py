"""RecallProvider — proves Recall is plugged into the MeetingProvider
abstraction (not just sitting under providers/recall/).

These tests pin the concrete Recall implementation against the protocol
contract. Tests for the underlying Recall behaviour (bot dispatch,
transcript fetch, webhooks) live in their own files; here we only
verify that the provider wires correctly into the platform.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pocketpaw_ee.cloud.meetings.dto import CreateMeetingRequest
from pocketpaw_ee.cloud.meetings.providers import base
from pocketpaw_ee.cloud.meetings.providers.recall.provider import RecallProvider


@pytest.fixture
def recall_provider():
    """A fresh RecallProvider instance — same shape as the registered one."""
    return RecallProvider()


@pytest.fixture
def ctx():
    """Minimal request context — RecallProvider just reads workspace_id."""
    return SimpleNamespace(workspace_id="ws-alpha", user_id="user-1")


# ---------------------------------------------------------------------------
# Registration + protocol shape
# ---------------------------------------------------------------------------


def test_recall_provider_registered_at_import():
    """Importing providers.recall side-effect-registers the provider so
    `base.resolve("recall")` succeeds with no extra wiring."""
    import pocketpaw_ee.cloud.meetings.providers.recall  # noqa: F401

    assert "recall" in base.registered_sources()
    p = base.resolve("recall")
    assert p.name == "recall"
    assert isinstance(p, RecallProvider)


def test_recall_provider_satisfies_all_protocols(recall_provider):
    """RecallProvider implements MeetingProvider + both optional
    capability sub-protocols — same shape the LiveKit engineer's provider
    will need for parity."""
    assert isinstance(recall_provider, base.MeetingProvider)
    assert isinstance(recall_provider, base.SupportsRecording)
    assert isinstance(recall_provider, base.SupportsTranscript)


# ---------------------------------------------------------------------------
# MeetingProvider lifecycle
# ---------------------------------------------------------------------------


async def test_create_calls_adapter_and_returns_provider_result(recall_provider, ctx, monkeypatch):
    """create() resolves the Zoom/Meet adapter, runs `meeting_create`, and
    returns a ProviderCreateResult — no DB writes, no emit (those are the
    service layer's job)."""
    from pocketpaw.connectors.protocol import ActionResult

    fake_adapter = AsyncMock()
    fake_adapter.execute.return_value = ActionResult(
        success=True,
        data={"id": "zoom-mtg-1", "join_url": "https://zoom.us/j/123"},
        records_affected=1,
    )

    async def fake_factory(workspace_id, provider):
        assert workspace_id == "ws-alpha"
        assert provider == "zoom"
        return fake_adapter

    monkeypatch.setattr("pocketpaw_ee.cloud.meetings.service._adapter_factory", fake_factory)

    body = CreateMeetingRequest(source="recall", provider="zoom", title="Standup")
    result = await recall_provider.create(ctx, body)

    fake_adapter.execute.assert_called_once()
    call_args = fake_adapter.execute.call_args
    assert call_args[0][0] == "meeting_create"
    assert call_args[0][1]["topic"] == "Standup"

    assert result.provider_payload["id"] == "zoom-mtg-1"
    assert result.join_url == "https://zoom.us/j/123"


async def test_create_rejects_recall_without_external_provider(recall_provider, ctx):
    """source='recall' + provider=None is invalid — we capture *external*
    meetings, so we need to know which (zoom|google_meet)."""
    from pocketpaw_ee.cloud._core.errors import ValidationError

    body = CreateMeetingRequest(source="recall", provider=None, title="Test")
    with pytest.raises(ValidationError) as exc:
        await recall_provider.create(ctx, body)
    assert exc.value.code == "meeting.recall_provider_required"


async def test_start_and_end_are_noops(recall_provider, ctx):
    """Recall meetings have no platform-managed start/end — the third-party
    call manages its own lifecycle. These exist only to satisfy the protocol."""
    meeting = SimpleNamespace(id="m1", provider="zoom", provider_meeting_id="z1")
    start_result = await recall_provider.start(ctx, meeting)
    assert start_result.provider_payload_updates == {}
    assert start_result.join_url is None
    assert await recall_provider.end(ctx, meeting) is None


# ---------------------------------------------------------------------------
# SupportsRecording — dispatches a Recall.ai bot
# ---------------------------------------------------------------------------


async def test_request_recording_dispatches_bot_and_returns_ref(recall_provider, ctx, monkeypatch):
    """request_recording delegates to recall_client and returns a
    RecordingRef with status='recording'; file_id is None because the
    artefact lands later via the recording.done webhook."""
    fake = AsyncMock(return_value={"bot_id": "bot-xyz", "status": "joining_call"})
    monkeypatch.setattr(
        "pocketpaw_ee.cloud.meetings.providers.recall.client.request_bot_for_meeting",
        fake,
    )

    meeting = SimpleNamespace(id="m1", provider="zoom", provider_meeting_id="z1")
    ref = await recall_provider.request_recording(ctx, meeting)

    fake.assert_called_once_with("ws-alpha", "m1")
    assert ref.provider == "recall"
    assert ref.external_id == "bot-xyz"
    assert ref.status == "recording"
    assert ref.file_id is None
    assert isinstance(ref.started_at, datetime)
    assert ref.started_at.tzinfo is UTC


# ---------------------------------------------------------------------------
# SupportsTranscript — derives entry/speaker counts from VTT
# ---------------------------------------------------------------------------


async def test_fetch_transcript_returns_artefact_when_ready(recall_provider, ctx, monkeypatch):
    """fetch_transcript wraps the VTT into a TranscriptArtefact, including
    light entry/speaker counts derived from the VTT text. The service layer
    can compute richer stats — this is just the minimum the protocol asks for."""
    vtt = (
        "WEBVTT\n\n"
        "00:00:00.000 --> 00:00:02.000\n<v Alice>hi there</v>\n\n"
        "00:00:02.000 --> 00:00:04.000\n<v Bob>hello</v>\n"
    )

    async def fake_fetch(workspace_id, meeting_id):
        assert workspace_id == "ws-alpha"
        assert meeting_id == "m1"
        return vtt

    monkeypatch.setattr(
        "pocketpaw_ee.cloud.meetings.providers.recall.client.fetch_transcript_vtt",
        fake_fetch,
    )

    meeting = SimpleNamespace(id="m1")
    artefact = await recall_provider.fetch_transcript(ctx, meeting)

    assert artefact is not None
    assert artefact.vtt == vtt
    assert artefact.entry_count == 2
    assert artefact.speaker_count == 2


async def test_fetch_transcript_returns_none_when_not_ready(recall_provider, ctx, monkeypatch):
    """When the bot is still recording / Recall is still transcribing,
    recall_client returns None and the provider passes it through so
    callers can poll."""

    async def fake_fetch(workspace_id, meeting_id):  # noqa: ARG001
        return None

    monkeypatch.setattr(
        "pocketpaw_ee.cloud.meetings.providers.recall.client.fetch_transcript_vtt",
        fake_fetch,
    )

    meeting = SimpleNamespace(id="m1")
    assert await recall_provider.fetch_transcript(ctx, meeting) is None
