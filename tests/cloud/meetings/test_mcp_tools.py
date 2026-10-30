# Tests for pocketpaw_ee/agent/mcp_servers/meetings.py — in-process MCP
# tools exposed to the cloud chat agent. Mirrors the existing
# sdk_mcp_tasks tests' approach: identity comes from the same
# ContextVars the chat router sets, handlers are tested directly.

from __future__ import annotations

import pytest
import pytest_asyncio
from pocketpaw_ee.agent.mcp_servers.meetings import (
    CHECK_BOT_TOOL_ID,
    MEETING_TOOL_IDS,
    SEND_BOT_TOOL_ID,
    SERVER_NAME,
    _cancel_meeting_handler,
    _check_bot_handler,
    _find_transcript_handler,
    _list_meetings_handler,
    _read_transcript_handler,
    _schedule_meeting_handler,
    _search_meetings_handler,
    _send_bot_handler,
)


@pytest_asyncio.fixture
async def chat_identity(monkeypatch, mongo_db):  # noqa: ARG001 — mongo_db forces Beanie init
    """Install workspace+user identity into the chat agent's ContextVars."""

    # The chat-stream identity ContextVars are set via tokens; bypass by
    # monkeypatching the read functions directly.
    monkeypatch.setattr(
        "pocketpaw_ee.cloud.chat.agent_service.current_workspace_id",
        lambda: "ws-alpha",
    )
    monkeypatch.setattr(
        "pocketpaw_ee.cloud.chat.agent_service.current_user_id",
        lambda: "user-1",
    )
    yield


@pytest.fixture
def fake_adapter(monkeypatch):
    """Swap the meetings adapter factory for a fake that always succeeds."""
    from pocketpaw_ee.cloud.meetings import service as ms

    from pocketpaw.connectors.protocol import ActionResult

    class _FakeAdapter:
        def __init__(self):
            self.calls = []

        async def execute(self, action, params):
            self.calls.append((action, params))
            if action == "meeting_create":
                return ActionResult(
                    success=True,
                    data={
                        "id": "fake-meeting-id-1",
                        "join_url": "https://example.com/join/abc",
                        "host_email": "host@example.com",
                    },
                    records_affected=1,
                )
            if action == "meeting_cancel":
                return ActionResult(success=True, records_affected=1)
            return ActionResult(success=False, error=f"unknown action: {action}")

    fake = _FakeAdapter()

    async def _factory(workspace_id, provider):
        return fake

    prev = ms._set_adapter_factory(_factory)
    yield fake
    ms._set_adapter_factory(prev)


# ---------------------------------------------------------------------------
# Tool-id allowlist contract
# ---------------------------------------------------------------------------


def test_tool_ids_are_namespaced():
    """All meeting tool ids must use the SDK's mcp__<server>__<tool> shape."""
    for tid in MEETING_TOOL_IDS:
        assert tid.startswith(f"mcp__{SERVER_NAME}__"), tid


# ---------------------------------------------------------------------------
# Identity gating
# ---------------------------------------------------------------------------


async def test_handler_errors_when_no_identity(monkeypatch):
    """Tools must refuse to run outside an active chat stream."""
    monkeypatch.setattr("pocketpaw_ee.cloud.chat.agent_service.current_workspace_id", lambda: None)
    monkeypatch.setattr("pocketpaw_ee.cloud.chat.agent_service.current_user_id", lambda: None)

    result = await _schedule_meeting_handler({"provider": "zoom", "title": "x"})
    assert result["is_error"] is True
    assert "no active workspace" in result["content"][0]["text"]


# ---------------------------------------------------------------------------
# schedule_meeting
# ---------------------------------------------------------------------------


async def test_schedule_meeting_validates_provider(chat_identity):
    result = await _schedule_meeting_handler({"provider": "skype", "title": "x"})
    assert result["is_error"] is True
    assert "provider must be" in result["content"][0]["text"]


async def test_schedule_meeting_requires_title(chat_identity):
    result = await _schedule_meeting_handler({"provider": "zoom", "title": "  "})
    assert result["is_error"] is True


async def test_schedule_meeting_end_to_end(chat_identity, fake_adapter):
    """Happy path: handler → ms.create_meeting → MeetingResponse JSON."""
    import json

    result = await _schedule_meeting_handler(
        {
            "provider": "zoom",
            "title": "Quarterly review",
            "duration_minutes": 45,
        }
    )
    assert result.get("is_error") is not True, result
    payload = json.loads(result["content"][0]["text"])
    assert payload["ok"] is True
    assert payload["meeting"]["title"] == "Quarterly review"
    assert payload["meeting"]["join_url"] == "https://example.com/join/abc"
    # Adapter was called with the right action + params.
    assert fake_adapter.calls[0][0] == "meeting_create"
    assert fake_adapter.calls[0][1]["topic"] == "Quarterly review"
    assert fake_adapter.calls[0][1]["duration_minutes"] == 45


async def test_schedule_meeting_surfaces_cloud_error(chat_identity, monkeypatch):
    """ValidationError from the service maps cleanly to the MCP error envelope."""
    from pocketpaw_ee.cloud._core.errors import ValidationError

    async def _broken_factory(workspace_id, provider):
        raise ValidationError("meeting.credentials_incomplete", "broken")

    from pocketpaw_ee.cloud.meetings import service as ms

    prev = ms._set_adapter_factory(_broken_factory)
    try:
        result = await _schedule_meeting_handler(
            {"provider": "zoom", "title": "Test", "duration_minutes": 30}
        )
    finally:
        ms._set_adapter_factory(prev)

    assert result["is_error"] is True
    assert "meeting.credentials_incomplete" in result["content"][0]["text"]


# ---------------------------------------------------------------------------
# list_meetings + search + transcript
# ---------------------------------------------------------------------------


async def test_list_meetings_empty_workspace(chat_identity):
    import json

    result = await _list_meetings_handler({})
    payload = json.loads(result["content"][0]["text"])
    assert payload == {"meetings": [], "count": 0}


async def test_list_meetings_returns_inserted_rows(chat_identity):
    """End-to-end list against Mongo: insert directly, then list via the handler."""
    import json

    from pocketpaw_ee.cloud.models.meeting import Meeting as _MD

    await _MD(
        workspace="ws-alpha",
        provider="zoom",
        provider_meeting_id="m1",
        title="Daily standup",
        join_url="https://zoom.us/j/1",
    ).insert()

    result = await _list_meetings_handler({"limit": 10})
    payload = json.loads(result["content"][0]["text"])
    assert payload["count"] == 1
    assert payload["meetings"][0]["title"] == "Daily standup"


async def test_search_meetings_requires_query(chat_identity):
    result = await _search_meetings_handler({"query": ""})
    assert result["is_error"] is True


async def test_search_meetings_matches(chat_identity):
    import json

    from pocketpaw_ee.cloud.models.meeting import Meeting as _MD

    await _MD(
        workspace="ws-alpha",
        provider="zoom",
        provider_meeting_id="m1",
        title="Acme sync",
        join_url="https://x/1",
    ).insert()
    await _MD(
        workspace="ws-alpha",
        provider="zoom",
        provider_meeting_id="m2",
        title="Internal",
        join_url="https://x/2",
    ).insert()

    result = await _search_meetings_handler({"query": "Acme"})
    payload = json.loads(result["content"][0]["text"])
    titles = [m["title"] for m in payload["meetings"]]
    assert "Acme sync" in titles
    assert "Internal" not in titles


async def test_find_transcript_404_when_meeting_missing(chat_identity):
    """No such meeting → meeting.not_found (more accurate than transcript.not_found)."""
    result = await _find_transcript_handler({"meeting_id": "nope"})
    assert result["is_error"] is True
    assert "meeting.not_found" in result["content"][0]["text"]


async def test_find_transcript_fetches_on_demand(chat_identity, monkeypatch):
    """No cached row → on-demand fetch via the adapter → cached row returned.

    Phase 2 behavior: get_transcript now fetches from the provider when
    no cached MeetingTranscript exists. Webhooks are no longer involved.
    """
    import json

    from pocketpaw_ee.cloud.meetings import service as ms
    from pocketpaw_ee.cloud.models.meeting import Meeting as _MD

    from pocketpaw.connectors.protocol import ActionResult

    # Insert a meeting row that has no transcript yet.
    meeting = _MD(
        workspace="ws-alpha",
        provider="zoom",
        provider_meeting_id="zoom-mtg-1",
        title="Phase2 demo",
        join_url="https://zoom.us/j/1",
    )
    await meeting.insert()

    # Fake adapter that returns a VTT-shaped transcript.
    class _FakeAdapter:
        async def execute(self, action, params):
            assert action == "transcript_get"
            assert params["meeting_id"] == "zoom-mtg-1"
            return ActionResult(
                success=True,
                data="WEBVTT\n\n00:00.000 --> 00:01.000\n<v alice>hello",
                records_affected=1,
            )

    async def _factory(workspace_id, provider):
        return _FakeAdapter()

    prev = ms._set_adapter_factory(_factory)
    try:
        result = await _find_transcript_handler({"meeting_id": str(meeting.id)})
    finally:
        ms._set_adapter_factory(prev)

    assert result.get("is_error") is not True, result
    payload = json.loads(result["content"][0]["text"])
    assert payload["meeting_id"] == str(meeting.id)
    assert payload["file_id"] is not None  # blob was uploaded
    assert payload["fetched_at"] is not None
    assert payload["speaker_count"] >= 1


async def test_find_transcript_returns_not_ready_when_provider_empty(chat_identity):
    """No transcript + no bot → structured ``{ready: false, state: no_bot}`` payload.

    The agent needs to know WHY the transcript isn't there. We return a
    success response (not an error) so the LLM can read the ``state`` and
    ``message`` fields and tell the user "no bot was dispatched" instead
    of "transcript is empty".
    """
    import json

    from pocketpaw_ee.cloud.meetings import service as ms
    from pocketpaw_ee.cloud.models.meeting import Meeting as _MD

    from pocketpaw.connectors.protocol import ActionResult

    meeting = _MD(
        workspace="ws-alpha",
        provider="zoom",
        provider_meeting_id="zoom-mtg-2",
        title="Just ended",
        join_url="https://zoom.us/j/2",
    )
    await meeting.insert()

    class _EmptyAdapter:
        async def execute(self, action, params):
            return ActionResult(success=True, data="", records_affected=0)

    async def _factory(workspace_id, provider):
        return _EmptyAdapter()

    prev = ms._set_adapter_factory(_factory)
    try:
        result = await _find_transcript_handler({"meeting_id": str(meeting.id)})
    finally:
        ms._set_adapter_factory(prev)

    assert result.get("is_error") is not True, result
    payload = json.loads(result["content"][0]["text"])
    assert payload["ready"] is False
    assert payload["state"] == "no_bot"
    assert "no recording bot" in payload["message"].lower()
    assert payload["bot"]["has_bot"] is False


async def test_read_transcript_returns_plain_speech(chat_identity):
    """read_meeting_transcript returns cleaned speech, and non-ASCII (Hindi)
    survives the store→read→decode round-trip intact (no mojibake)."""
    import json

    from pocketpaw_ee.cloud.meetings import service as ms
    from pocketpaw_ee.cloud.models.meeting import Meeting as _MD

    from pocketpaw.connectors.protocol import ActionResult

    meeting = _MD(
        workspace="ws-alpha",
        provider="zoom",
        provider_meeting_id="zoom-mtg-read",
        title="Hindi sync",
        join_url="https://zoom.us/j/9",
    )
    await meeting.insert()

    vtt = (
        "WEBVTT\n\n"
        "00:00:00.000 --> 00:00:02.000\n<v Rohit>इसमें आवाज़ नहीं जा रहा</v>\n\n"
        "00:00:02.000 --> 00:00:04.000\n<v Amritesh>हाँ बहुत slow है</v>"
    )

    class _FakeAdapter:
        async def execute(self, action, params):
            assert action == "transcript_get"
            return ActionResult(success=True, data=vtt, records_affected=1)

    async def _factory(workspace_id, provider):
        return _FakeAdapter()

    prev = ms._set_adapter_factory(_factory)
    try:
        # Prime the stored transcript blob (writes via write_text_file).
        await ms.get_transcript("ws-alpha", str(meeting.id))
        result = await _read_transcript_handler({"meeting_id": str(meeting.id)})
    finally:
        ms._set_adapter_factory(prev)

    assert result.get("is_error") is not True, result
    payload = json.loads(result["content"][0]["text"])
    assert payload["truncated"] is False
    assert "Rohit: इसमें आवाज़ नहीं जा रहा" in payload["text"]
    assert "Amritesh: हाँ बहुत slow है" in payload["text"]
    # The mojibake signature must NOT appear — proves clean UTF-8 round-trip.
    assert "à¤" not in payload["text"]


async def test_read_transcript_not_ready_when_no_transcript(chat_identity):
    """No transcript yet → structured not-ready payload, not an error."""
    import json

    from pocketpaw_ee.cloud.meetings import service as ms
    from pocketpaw_ee.cloud.models.meeting import Meeting as _MD

    from pocketpaw.connectors.protocol import ActionResult

    meeting = _MD(
        workspace="ws-alpha",
        provider="zoom",
        provider_meeting_id="zoom-mtg-empty-read",
        title="Nothing yet",
        join_url="https://zoom.us/j/10",
    )
    await meeting.insert()

    class _EmptyAdapter:
        async def execute(self, action, params):
            return ActionResult(success=True, data="", records_affected=0)

    async def _factory(workspace_id, provider):
        return _EmptyAdapter()

    prev = ms._set_adapter_factory(_factory)
    try:
        result = await _read_transcript_handler({"meeting_id": str(meeting.id)})
    finally:
        ms._set_adapter_factory(prev)

    assert result.get("is_error") is not True, result
    payload = json.loads(result["content"][0]["text"])
    assert payload["ready"] is False


async def test_read_transcript_requires_id(chat_identity):
    result = await _read_transcript_handler({})
    assert result["is_error"] is True


async def test_cancel_meeting_requires_id(chat_identity):
    result = await _cancel_meeting_handler({})
    assert result["is_error"] is True


# ---------------------------------------------------------------------------
# send_bot_to_meeting
# ---------------------------------------------------------------------------


def test_send_bot_tool_id_in_allowlist():
    """The send_bot tool must be in the global allowlist so the SDK exposes it."""
    assert SEND_BOT_TOOL_ID in MEETING_TOOL_IDS


async def test_send_bot_requires_meeting_id(chat_identity):
    result = await _send_bot_handler({})
    assert result["is_error"] is True
    assert "meeting_id is required" in result["content"][0]["text"]


async def test_send_bot_happy_path(chat_identity, monkeypatch):
    """Handler calls recall_client and returns {ok, bot_id, status}."""
    import json

    captured: dict = {}

    async def _fake_request_bot(workspace_id, meeting_id):
        captured["workspace_id"] = workspace_id
        captured["meeting_id"] = meeting_id
        return {"bot_id": "bot-abc-123", "meeting_id": meeting_id, "status": "queued"}

    monkeypatch.setattr(
        "pocketpaw_ee.cloud.meetings.providers.recall.client.request_bot_for_meeting",
        _fake_request_bot,
    )

    result = await _send_bot_handler({"meeting_id": "m1"})
    assert result.get("is_error") is not True, result
    payload = json.loads(result["content"][0]["text"])
    assert payload["ok"] is True
    assert payload["bot_id"] == "bot-abc-123"
    assert payload["status"] == "queued"
    assert payload["meeting_id"] == "m1"
    # Workspace flows from contextvars, not args.
    assert captured["workspace_id"] == "ws-alpha"


async def test_send_bot_surfaces_cloud_errors(chat_identity, monkeypatch):
    """Recall.ai failure (e.g. missing API key) surfaces as MCP error."""
    from pocketpaw_ee.cloud._core.errors import ValidationError

    async def _broken(workspace_id, meeting_id):
        raise ValidationError("meeting.bot_secret_missing", "bot service disabled")

    monkeypatch.setattr(
        "pocketpaw_ee.cloud.meetings.providers.recall.client.request_bot_for_meeting", _broken
    )

    result = await _send_bot_handler({"meeting_id": "m1"})
    assert result["is_error"] is True
    assert "meeting.bot_secret_missing" in result["content"][0]["text"]


# ---------------------------------------------------------------------------
# check_meeting_bot
# ---------------------------------------------------------------------------


def test_check_bot_tool_id_in_allowlist():
    """The check_meeting_bot tool must be in the global allowlist."""
    assert CHECK_BOT_TOOL_ID in MEETING_TOOL_IDS


async def test_check_bot_requires_meeting_id(chat_identity):
    result = await _check_bot_handler({})
    assert result["is_error"] is True
    assert "meeting_id is required" in result["content"][0]["text"]


async def test_check_bot_happy_path(chat_identity, monkeypatch):
    """Handler returns the service's bot-status payload to the agent."""
    import json

    captured: dict = {}

    async def _fake_get_bot_status(workspace_id, meeting_id):
        captured["workspace_id"] = workspace_id
        captured["meeting_id"] = meeting_id
        return {
            "meeting_id": meeting_id,
            "has_bot": True,
            "bot_id": "bot-abc",
            "status": "in_waiting_room",
            "status_detail": None,
            "status_at": None,
            "summary": "The bot is in the lobby — admit it.",
        }

    monkeypatch.setattr("pocketpaw_ee.cloud.meetings.service.get_bot_status", _fake_get_bot_status)

    result = await _check_bot_handler({"meeting_id": "m1"})
    assert result.get("is_error") is not True, result
    payload = json.loads(result["content"][0]["text"])
    assert payload["status"] == "in_waiting_room"
    assert payload["has_bot"] is True
    assert captured["workspace_id"] == "ws-alpha"
    assert captured["meeting_id"] == "m1"


async def test_list_meetings_handler_tz_aware_bounds_no_crash(chat_identity):
    """Regression: tz-aware since/until vs a naive stored scheduled_start.

    The MCP tool parses since/until as tz-aware; Mongo hands back naive
    datetimes. Comparing them used to raise 'can't compare offset-naive
    and offset-aware datetimes' and fail list_meetings outright.
    """
    import json
    from datetime import datetime

    from pocketpaw_ee.cloud.models.meeting import Meeting as _MD

    await _MD(
        workspace="ws-alpha",
        provider="zoom",
        provider_meeting_id="m1",
        title="standup",
        join_url="https://zoom.us/j/1",
        scheduled_start=datetime(2026, 5, 20, 9, 0),  # naive, as Mongo returns
    ).insert()

    result = await _list_meetings_handler(
        {"since": "2026-05-01T00:00:00Z", "until": "2026-06-01T00:00:00Z"}
    )
    assert result.get("is_error") is not True, result
    payload = json.loads(result["content"][0]["text"])
    assert payload["count"] == 1
    assert payload["meetings"][0]["title"] == "standup"
