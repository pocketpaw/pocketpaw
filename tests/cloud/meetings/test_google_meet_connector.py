# Tests for src/pocketpaw/connectors/adapters/google_meet.py — adapter dispatch.

from __future__ import annotations

from typing import Any

import pytest
from pocketpaw_ee.cloud.meetings.providers.recall.adapters.google_meet import GoogleMeetConnector

from pocketpaw.connectors.protocol import (
    ConnectorStatus,
    ExecutionMode,
)


class _FakeMeetClient:
    def __init__(self):
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.create_response = {
            "name": "spaces/abc",
            "meetingUri": "https://meet.google.com/xyz-pdq",
            "meetingCode": "xyz-pdq",
        }
        self.records_response = {
            "conferenceRecords": [
                {"name": "conferenceRecords/r1"},
                {"name": "conferenceRecords/r2"},
            ]
        }
        self.get_record_response = {"name": "conferenceRecords/r1"}
        self.list_transcripts_response = {
            "transcripts": [{"name": "conferenceRecords/r1/transcripts/t1"}]
        }
        self.list_entries_response = {
            "transcriptEntries": [
                {
                    "participant": "alice",
                    "startTime": "2026-05-19T10:00:00Z",
                    "endTime": "2026-05-19T10:00:05Z",
                    "text": "hello world",
                },
            ],
            "nextPageToken": None,
        }
        self.end_active_raises = None

    async def _get_token(self) -> str:
        return "tok"

    async def create_space(self, **kw):
        self.calls.append(("create_space", kw))
        return self.create_response

    async def list_conference_records(self, **kw):
        self.calls.append(("list_conference_records", kw))
        return self.records_response

    async def get_conference_record(self, name):
        self.calls.append(("get_conference_record", {"name": name}))
        return self.get_record_response

    async def end_active_conference(self, name):
        self.calls.append(("end_active_conference", {"name": name}))
        if self.end_active_raises is not None:
            raise self.end_active_raises

    async def list_transcripts(self, name):
        self.calls.append(("list_transcripts", {"name": name}))
        return self.list_transcripts_response

    async def list_transcript_entries(self, name, **kw):
        self.calls.append(("list_transcript_entries", {"name": name, **kw}))
        return self.list_entries_response


@pytest.fixture
def connector():
    fake = _FakeMeetClient()
    c = GoogleMeetConnector("workspace-w1-google_meet", "cid", "csec", client=fake)
    return c, fake


def test_metadata(connector):
    c, _ = connector
    assert c.name == "google_meet"
    assert c.display_name == "Google Meet"


async def test_actions_surface_mirrors_zoom_names(connector):
    """Action names line up with Zoom for cross-provider tool consistency."""
    c, _ = connector
    schemas = await c.actions()
    names = {s.name for s in schemas}
    # No recording_list — Meet's recordings are Drive files; we surface
    # them only through transcript_get + the eventual Drive opt-in.
    assert names == {
        "meeting_create",
        "meeting_list",
        "meeting_get",
        "meeting_cancel",
        "transcript_get",
    }
    assert all(s.execution_mode == ExecutionMode.CLOUD for s in schemas)


async def test_execute_meeting_create_normalizes_to_zoom_shape(connector):
    """create_space's response is normalized so the service layer can stay provider-agnostic."""
    c, fake = connector
    result = await c.execute("meeting_create", {"topic": "Standup"})
    assert result.success
    # Normalized fields the service.create_meeting mapper reads:
    assert result.data["id"] == "spaces/abc"
    assert result.data["join_url"] == "https://meet.google.com/xyz-pdq"
    assert result.data["space_name"] == "spaces/abc"
    # access_type defaults to OPEN so the anonymous recording bot can join
    # without a host admitting it from the lobby.
    assert fake.calls[0] == ("create_space", {"access_type": "OPEN"})


async def test_execute_meeting_list_returns_records(connector):
    c, _ = connector
    result = await c.execute("meeting_list", {"page_size": 5})
    assert result.success
    assert len(result.data) == 2
    assert result.data[0]["name"].startswith("conferenceRecords/")


async def test_execute_meeting_cancel_swallows_404(connector):
    """No active conference → API 404 → adapter still returns success."""
    from pocketpaw_ee.cloud.meetings.providers.recall.clients.google_meet import GoogleMeetAPIError

    c, fake = connector
    fake.end_active_raises = GoogleMeetAPIError(404, "not running")
    result = await c.execute("meeting_cancel", {"meeting_id": "spaces/abc"})
    assert result.success is True


async def test_execute_meeting_cancel_raises_real_error(connector):
    from pocketpaw_ee.cloud.meetings.providers.recall.clients.google_meet import GoogleMeetAPIError

    c, fake = connector
    fake.end_active_raises = GoogleMeetAPIError(500, "internal")
    result = await c.execute("meeting_cancel", {"meeting_id": "spaces/abc"})
    assert result.success is False
    assert "500" in result.error


async def test_execute_transcript_get_passes_conference_record_through(connector):
    """When given a conferenceRecord name directly, we skip the space → records lookup."""
    c, fake = connector
    result = await c.execute("transcript_get", {"meeting_id": "conferenceRecords/r1"})
    assert result.success
    assert result.data.startswith("WEBVTT")
    assert "alice" in result.data
    assert "hello world" in result.data
    # No list_conference_records call — caller already had the right name.
    assert all(name != "list_conference_records" for name, _ in fake.calls)


async def test_execute_transcript_get_translates_space_to_records(connector):
    """Space name in → resolves to its conferenceRecords → fetches transcripts from each."""
    c, fake = connector
    result = await c.execute("transcript_get", {"meeting_id": "spaces/abc"})
    assert result.success
    assert result.data.startswith("WEBVTT")
    # First upstream call MUST be the space→records lookup.
    assert fake.calls[0][0] == "list_conference_records"
    assert fake.calls[0][1]["filter_"] == 'space.name="spaces/abc"'
    # Then list_transcripts gets the conference record from the lookup result.
    list_tx_calls = [c for c in fake.calls if c[0] == "list_transcripts"]
    assert list_tx_calls and list_tx_calls[0][1]["name"].startswith("conferenceRecords/")


async def test_execute_transcript_get_bare_id_treated_as_space(connector):
    """Bare ID (no prefix) is normalized to spaces/{id} for the lookup."""
    c, fake = connector
    await c.execute("transcript_get", {"meeting_id": "abc"})
    assert fake.calls[0][1]["filter_"] == 'space.name="spaces/abc"'


async def test_execute_transcript_get_space_with_no_conferences_yet(connector):
    """Space exists but nobody joined → no conferenceRecords → empty result, not error."""
    c, fake = connector
    fake.records_response = {"conferenceRecords": []}
    result = await c.execute("transcript_get", {"meeting_id": "spaces/abc"})
    assert result.success
    assert result.data == ""
    assert result.records_affected == 0
    # We bailed early — never attempted list_transcripts.
    assert all(name != "list_transcripts" for name, _ in fake.calls)


async def test_execute_transcript_get_no_transcripts_on_record(connector):
    """Conference happened but transcription wasn't enabled → empty, not error."""
    c, fake = connector
    fake.list_transcripts_response = {"transcripts": []}
    result = await c.execute("transcript_get", {"meeting_id": "conferenceRecords/r1"})
    assert result.success
    assert result.data == ""
    assert result.records_affected == 0


async def test_execute_transcript_get_transcripts_with_no_entries(connector):
    """Transcript sessions exist but yielded zero entries → still empty."""
    c, fake = connector
    fake.list_entries_response = {"transcriptEntries": [], "nextPageToken": None}
    result = await c.execute("transcript_get", {"meeting_id": "conferenceRecords/r1"})
    assert result.success
    assert result.data == ""
    assert result.records_affected == 0


async def test_execute_unknown_action(connector):
    c, _ = connector
    result = await c.execute("nope", {})
    assert result.success is False
    assert "Unknown action" in result.error


async def test_health_ok(connector):
    c, _ = connector
    h = await c.health()
    assert h.ok is True
    assert h.status == ConnectorStatus.CONNECTED
