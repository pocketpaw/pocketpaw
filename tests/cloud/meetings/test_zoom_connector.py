# Tests for src/pocketpaw/connectors/adapters/zoom.py — adapter dispatch.
# Uses an injected fake ZoomClient so we exercise the action surface
# without coupling to httpx / OAuth plumbing (already covered in
# tests/test_zoom_client.py and tests/test_oauth.py).

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from pocketpaw_ee.cloud.meetings.providers.recall.adapters.zoom import ZoomConnector

from pocketpaw.connectors.protocol import (
    ActionResult,
    ConnectorStatus,
    ExecutionMode,
    TrustLevel,
)


class _FakeZoomClient:
    """Records call kwargs and returns canned payloads, no network."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.create_response: dict[str, Any] = {
            "id": 1234567890,
            "join_url": "https://zoom.us/j/1234567890",
            "host_url": "https://zoom.us/s/1234567890?pwd=x",
        }
        self.list_response: dict[str, Any] = {"meetings": [{"id": "m1"}, {"id": "m2"}]}
        self.get_response: dict[str, Any] = {"id": "m1", "topic": "Standup"}
        self.recordings_response: dict[str, Any] = {
            "recording_files": [
                {"id": "r1", "file_type": "MP4", "download_url": "https://x/mp4"},
                {"id": "r2", "file_type": "TRANSCRIPT", "download_url": "https://x/vtt"},
            ]
        }
        self.transcript_text = "WEBVTT\n\n00:00.000 --> 00:01.000\nHello"
        self.token_ok = True

    async def _get_token(self) -> str:
        if not self.token_ok:
            raise RuntimeError("not connected")
        return "tok_xyz"

    async def create_meeting(self, **kw):
        self.calls.append(("create_meeting", kw))
        return self.create_response

    async def list_meetings(self, **kw):
        self.calls.append(("list_meetings", kw))
        return self.list_response

    async def get_meeting(self, meeting_id):
        self.calls.append(("get_meeting", {"meeting_id": meeting_id}))
        return self.get_response

    async def cancel_meeting(self, meeting_id, *, notify_hosts=True):
        self.calls.append(
            ("cancel_meeting", {"meeting_id": meeting_id, "notify_hosts": notify_hosts})
        )

    async def list_recordings(self, meeting_id):
        self.calls.append(("list_recordings", {"meeting_id": meeting_id}))
        return self.recordings_response

    async def download_transcript(self, url):
        self.calls.append(("download_transcript", {"url": url}))
        return self.transcript_text


@pytest.fixture
def connector():
    fake = _FakeZoomClient()
    c = ZoomConnector("workspace-w1-zoom", "cid", "csec", client=fake)
    return c, fake


# ---------------------------------------------------------------------------
# Identity + protocol surface
# ---------------------------------------------------------------------------


def test_metadata(connector):
    c, _ = connector
    assert c.name == "zoom"
    assert c.display_name == "Zoom"
    assert c.type == "communication"


async def test_actions_surface(connector):
    """Adapter exposes all 6 meeting actions, all CLOUD execution mode."""
    c, _ = connector
    schemas = await c.actions()
    names = {s.name for s in schemas}
    assert names == {
        "meeting_create",
        "meeting_list",
        "meeting_get",
        "meeting_cancel",
        "recording_list",
        "transcript_get",
    }
    assert all(s.execution_mode == ExecutionMode.CLOUD for s in schemas)
    # Writes are CONFIRM-level; reads are AUTO.
    by_name = {s.name: s for s in schemas}
    assert by_name["meeting_create"].trust_level == TrustLevel.CONFIRM
    assert by_name["meeting_cancel"].trust_level == TrustLevel.CONFIRM
    assert by_name["meeting_list"].trust_level == TrustLevel.AUTO


# ---------------------------------------------------------------------------
# Execute dispatch — happy paths
# ---------------------------------------------------------------------------


async def test_execute_meeting_create_instant(connector):
    c, fake = connector
    result = await c.execute("meeting_create", {"topic": "Standup", "duration_minutes": 15})
    assert isinstance(result, ActionResult)
    assert result.success is True
    assert result.data["join_url"].startswith("https://zoom.us")
    # Adapter passed kwargs through; start_time is None for instant.
    name, kw = fake.calls[-1]
    assert name == "create_meeting"
    assert kw["topic"] == "Standup"
    assert kw["duration_minutes"] == 15
    assert kw["start_time"] is None


async def test_execute_meeting_create_scheduled_parses_iso(connector):
    c, fake = connector
    result = await c.execute(
        "meeting_create",
        {"topic": "Planning", "start_time": "2026-06-01T14:30:00Z", "duration_minutes": 60},
    )
    assert result.success
    _, kw = fake.calls[-1]
    # Parser tolerates trailing Z (replaced with +00:00 internally).
    assert kw["start_time"] == datetime(2026, 6, 1, 14, 30, 0, tzinfo=UTC)


async def test_execute_meeting_list_returns_inner_meetings_array(connector):
    c, _ = connector
    result = await c.execute("meeting_list", {"meeting_type": "upcoming"})
    assert result.success
    # The wrapper {"meetings": [...]} is unwrapped to just the list.
    assert result.data == [{"id": "m1"}, {"id": "m2"}]
    assert result.records_affected == 2


async def test_execute_meeting_cancel_forwards_notify_flag(connector):
    c, fake = connector
    result = await c.execute("meeting_cancel", {"meeting_id": "m1", "notify_hosts": False})
    assert result.success
    _, kw = fake.calls[-1]
    assert kw == {"meeting_id": "m1", "notify_hosts": False}


async def test_execute_transcript_get_happy_path(connector):
    """transcript_get fetches the listing, picks the TRANSCRIPT file, downloads it."""
    c, fake = connector
    result = await c.execute("transcript_get", {"meeting_id": "m1"})
    assert result.success
    assert result.data.startswith("WEBVTT")
    # Two upstream calls: list_recordings then download_transcript.
    assert [n for (n, _) in fake.calls] == ["list_recordings", "download_transcript"]
    assert fake.calls[-1][1]["url"] == "https://x/vtt"


async def test_execute_transcript_get_no_transcript_file(connector):
    """No TRANSCRIPT file → empty string, no download attempted."""
    c, fake = connector
    fake.recordings_response = {
        "recording_files": [{"id": "r1", "file_type": "MP4", "download_url": "x"}]
    }
    result = await c.execute("transcript_get", {"meeting_id": "m1"})
    assert result.success
    assert result.data == ""
    assert result.records_affected == 0
    assert [n for (n, _) in fake.calls] == ["list_recordings"]


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


async def test_execute_unknown_action(connector):
    c, _ = connector
    result = await c.execute("nonsense", {})
    assert result.success is False
    assert "Unknown action" in result.error


async def test_execute_missing_required_param(connector):
    c, _ = connector
    # meeting_get requires meeting_id
    result = await c.execute("meeting_get", {})
    assert result.success is False
    assert "Missing required param: meeting_id" in result.error


async def test_execute_wraps_zoom_api_error(connector):
    """ZoomAPIError from the client becomes a non-success ActionResult."""
    from pocketpaw_ee.cloud.meetings.providers.recall.clients.zoom import ZoomAPIError

    c, fake = connector

    async def _raise(*a, **kw):
        raise ZoomAPIError(404, "Meeting does not exist", body={"code": 3001})

    fake.get_meeting = _raise  # type: ignore[assignment]
    result = await c.execute("meeting_get", {"meeting_id": "nope"})
    assert result.success is False
    assert "404" in result.error
    assert "does not exist" in result.error


# ---------------------------------------------------------------------------
# Health + connect
# ---------------------------------------------------------------------------


async def test_health_ok(connector):
    c, _ = connector
    h = await c.health()
    assert h.ok is True
    assert h.status == ConnectorStatus.CONNECTED


async def test_health_when_unauthenticated(connector):
    c, fake = connector
    fake.token_ok = False
    h = await c.health()
    assert h.ok is False
    assert h.status == ConnectorStatus.ERROR
    assert "not connected" in h.message


async def test_connect_returns_status(connector):
    c, _ = connector
    result = await c.connect("pocket-1", {})
    assert result.success
    assert result.connector_name == "zoom"
    assert result.status == ConnectorStatus.CONNECTED
