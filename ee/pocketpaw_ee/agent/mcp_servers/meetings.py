# meetings.py — in-process MCP server exposing the meetings entity.
# Created: 2026-05-19 — wires pocketpaw_ee.cloud.meetings.service into the
#   cloud chat agent so the LLM can schedule, list, cancel, search, and pull
#   transcripts from Zoom + Google Meet natively. Mirrors the sibling
#   ``tasks.py`` MCP server pattern.
#
# Tools registered (namespaced ``mcp__pocketpaw_meetings__*`` by the SDK):
#   - schedule_meeting        — create a meeting via the configured provider
#   - list_meetings           — list workspace meetings (filter by provider/status)
#   - cancel_meeting          — cancel a scheduled meeting
#   - search_meetings         — cross-provider search by title/participant
#   - find_meeting_transcript — get transcript metadata for a meeting
#
# Identity (workspace + user) flows in from the per-stream ContextVars
# set by ``pocketpaw_ee.cloud.chat.agent_router._run_agent_stream``. When invoked
# outside a chat stream the tools return a structured error.

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

SERVER_NAME = "pocketpaw_meetings"
SCHEDULE_TOOL_ID = f"mcp__{SERVER_NAME}__schedule_meeting"
LIST_TOOL_ID = f"mcp__{SERVER_NAME}__list_meetings"
CANCEL_TOOL_ID = f"mcp__{SERVER_NAME}__cancel_meeting"
SEARCH_TOOL_ID = f"mcp__{SERVER_NAME}__search_meetings"
TRANSCRIPT_TOOL_ID = f"mcp__{SERVER_NAME}__find_meeting_transcript"
SEND_BOT_TOOL_ID = f"mcp__{SERVER_NAME}__send_bot_to_meeting"
CHECK_BOT_TOOL_ID = f"mcp__{SERVER_NAME}__check_meeting_bot"

MEETING_TOOL_IDS = (
    SCHEDULE_TOOL_ID,
    LIST_TOOL_ID,
    CANCEL_TOOL_ID,
    SEARCH_TOOL_ID,
    TRANSCRIPT_TOOL_ID,
    SEND_BOT_TOOL_ID,
    CHECK_BOT_TOOL_ID,
)


def _error_response(message: str) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": f"Error: {message}"}],
        "is_error": True,
    }


def _success_response(body: Any) -> dict[str, Any]:
    return {
        "content": [
            {
                "type": "text",
                "text": json.dumps(body, separators=(",", ":"), default=str),
            }
        ]
    }


def _identity() -> tuple[str | None, str | None]:
    """Resolve (workspace_id, user_id) from the chat stream's ContextVars."""
    try:
        from pocketpaw_ee.cloud.chat.agent_service import current_user_id, current_workspace_id

        return current_workspace_id(), current_user_id()
    except Exception:
        return None, None


def _parse_iso_opt(value: Any):
    """Tolerant ISO 8601 parser — returns None for empty/None, raises for malformed."""
    from datetime import datetime

    if value in (None, ""):
        return None
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


# ---------------------------------------------------------------------------
# Tool handlers — thin wrappers over pocketpaw_ee.cloud.meetings.service
# ---------------------------------------------------------------------------


async def _schedule_meeting_handler(args: dict) -> dict:
    workspace_id, user_id = _identity()
    if not workspace_id or not user_id:
        return _error_response(
            "no active workspace — schedule_meeting can only be called "
            "from inside a cloud SSE chat stream"
        )

    provider = args.get("provider")
    if provider not in ("zoom", "google_meet"):
        return _error_response("provider must be 'zoom' or 'google_meet'")
    title = args.get("title")
    if not isinstance(title, str) or not title.strip():
        return _error_response("title is required (non-empty string)")

    from pocketpaw_ee.cloud._core.errors import CloudError
    from pocketpaw_ee.cloud.meetings import service as ms
    from pocketpaw_ee.cloud.meetings.dto import CreateMeetingRequest

    try:
        body = CreateMeetingRequest(
            provider=provider,  # type: ignore[arg-type]
            title=title.strip(),
            scheduled_start=_parse_iso_opt(args.get("scheduled_start")),
            duration_minutes=int(args.get("duration_minutes") or 30),
        )
    except (ValueError, TypeError) as exc:
        return _error_response(f"invalid arguments: {exc}")

    try:
        response = await ms.create_meeting(workspace_id, user_id, body)
    except CloudError as exc:
        return _error_response(f"{exc.code}: {exc.message}")
    except Exception as exc:  # noqa: BLE001
        logger.warning("schedule_meeting failed", exc_info=True)
        return _error_response(f"schedule_meeting failed: {exc}")

    return _success_response({"ok": True, "meeting": response.model_dump()})


async def _list_meetings_handler(args: dict) -> dict:
    workspace_id, _ = _identity()
    if not workspace_id:
        return _error_response("no active workspace")

    from pocketpaw_ee.cloud.meetings import service as ms
    from pocketpaw_ee.cloud.meetings.dto import ListMeetingsRequest

    try:
        body = ListMeetingsRequest(
            provider=args.get("provider") or None,
            status=args.get("status") or None,
            since=_parse_iso_opt(args.get("since")),
            until=_parse_iso_opt(args.get("until")),
            limit=int(args.get("limit") or 25),
        )
    except (ValueError, TypeError) as exc:
        return _error_response(f"invalid arguments: {exc}")

    try:
        rows = await ms.list_meetings(workspace_id, body)
    except Exception as exc:  # noqa: BLE001
        logger.warning("list_meetings failed", exc_info=True)
        return _error_response(f"list_meetings failed: {exc}")

    return _success_response({"meetings": [r.model_dump() for r in rows], "count": len(rows)})


async def _cancel_meeting_handler(args: dict) -> dict:
    workspace_id, _ = _identity()
    if not workspace_id:
        return _error_response("no active workspace")

    meeting_id = args.get("meeting_id")
    if not isinstance(meeting_id, str) or not meeting_id:
        return _error_response("meeting_id is required (string)")

    from pocketpaw_ee.cloud._core.errors import CloudError
    from pocketpaw_ee.cloud.meetings import service as ms

    try:
        response = await ms.cancel_meeting(workspace_id, meeting_id)
    except CloudError as exc:
        return _error_response(f"{exc.code}: {exc.message}")
    except Exception as exc:  # noqa: BLE001
        logger.warning("cancel_meeting failed", exc_info=True)
        return _error_response(f"cancel_meeting failed: {exc}")

    return _success_response({"ok": True, "meeting": response.model_dump()})


async def _search_meetings_handler(args: dict) -> dict:
    workspace_id, _ = _identity()
    if not workspace_id:
        return _error_response("no active workspace")

    query = args.get("query")
    if not isinstance(query, str) or not query.strip():
        return _error_response("query is required (non-empty string)")

    from pocketpaw_ee.cloud.meetings import service as ms

    try:
        rows = await ms.search_meetings(
            workspace_id,
            query=query.strip(),
            since=_parse_iso_opt(args.get("since")),
            until=_parse_iso_opt(args.get("until")),
            limit=int(args.get("limit") or 20),
        )
    except (ValueError, TypeError) as exc:
        return _error_response(f"invalid arguments: {exc}")
    except Exception as exc:  # noqa: BLE001
        logger.warning("search_meetings failed", exc_info=True)
        return _error_response(f"search_meetings failed: {exc}")

    return _success_response({"meetings": [r.model_dump() for r in rows], "count": len(rows)})


async def _send_bot_handler(args: dict) -> dict:
    """Dispatch a Recall.ai bot to a meeting to capture audio + transcript.

    Two-step flow that pairs with ``schedule_meeting``:
      1. Agent calls ``schedule_meeting`` → user gets the join URL.
      2. Agent calls ``send_bot_to_meeting`` → Recall.ai sends a bot to
         the meeting URL; it records and transcribes the call.

    Recall.ai must be configured (``RECALL_API_KEY``). The transcript is
    pushed back via webhook and is also fetchable on demand.
    """
    workspace_id, _ = _identity()
    if not workspace_id:
        return _error_response("no active workspace")

    meeting_id = args.get("meeting_id")
    if not isinstance(meeting_id, str) or not meeting_id:
        return _error_response("meeting_id is required (string)")

    from pocketpaw_ee.cloud._core.errors import CloudError
    from pocketpaw_ee.cloud.meetings.providers.recall import client as recall_client

    try:
        payload = await recall_client.request_bot_for_meeting(workspace_id, meeting_id)
    except CloudError as exc:
        return _error_response(f"{exc.code}: {exc.message}")
    except Exception as exc:  # noqa: BLE001
        logger.warning("send_bot_to_meeting failed", exc_info=True)
        return _error_response(f"send_bot_to_meeting failed: {exc}")

    return _success_response(
        {
            "ok": True,
            "bot_id": payload.get("bot_id", ""),
            "status": payload.get("status", "queued"),
            "meeting_id": meeting_id,
        }
    )


async def _find_transcript_handler(args: dict) -> dict:
    """Return the transcript OR — when it isn't ready — explain why.

    "Transcript not ready" hides several distinct states that the agent
    has to communicate differently to the user:

      * No bot was ever dispatched → suggest sending one.
      * Bot is in the waiting room → ask user to admit it.
      * Bot is recording right now → tell user to retry after the call.
      * Meeting ended → async transcription with Deepgram/etc. is still
        running, give an ETA.
      * Bot failed (denied / fatal) → escalate honestly.

    We always fall through to a bot-status read on the not-ready path so
    the agent gets structured context to respond from, instead of a bare
    "transcript not found" that produces vague "empty transcript" replies.
    """
    workspace_id, _ = _identity()
    if not workspace_id:
        return _error_response("no active workspace")

    meeting_id = args.get("meeting_id")
    if not isinstance(meeting_id, str) or not meeting_id:
        return _error_response("meeting_id is required (string)")

    from pocketpaw_ee.cloud._core.errors import CloudError, NotFound
    from pocketpaw_ee.cloud.meetings import service as ms

    try:
        response = await ms.get_transcript(workspace_id, meeting_id)
    except NotFound:
        # Transcript not ready — fetch bot status to explain why.
        return await _transcript_not_ready_response(workspace_id, meeting_id)
    except CloudError as exc:
        return _error_response(f"{exc.code}: {exc.message}")
    except Exception as exc:  # noqa: BLE001
        logger.warning("find_meeting_transcript failed", exc_info=True)
        return _error_response(f"find_meeting_transcript failed: {exc}")

    body = response.model_dump()
    body["ready"] = True
    body["state"] = "ready"
    return _success_response(body)


# Map a Recall bot status to a coarse transcript state + ETA hint.
# Used when the transcript isn't on disk yet so the agent can choose
# the right thing to say instead of guessing.
_BOT_STATUS_TO_TRANSCRIPT_STATE = {
    None: ("no_bot", "No recording bot was dispatched to this meeting."),
    "ready": ("bot_starting", "The bot is starting up."),
    "joining_call": ("bot_joining", "The bot is joining the call."),
    "in_waiting_room": (
        "bot_waiting_admission",
        "The bot is in the meeting's waiting room and needs to be admitted by a host.",
    ),
    "in_call_not_recording": (
        "bot_in_call_not_recording",
        "The bot joined the call but isn't recording yet.",
    ),
    "recording_permission_allowed": (
        "bot_recording",
        "The bot is currently recording the call. The transcript will be ready a few "
        "minutes after the meeting ends.",
    ),
    "in_call_recording": (
        "bot_recording",
        "The bot is currently recording the call. The transcript will be ready a few "
        "minutes after the meeting ends.",
    ),
    "recording_permission_denied": (
        "bot_recording_denied",
        "The bot was denied recording permission and won't produce a transcript.",
    ),
    "call_ended": (
        "transcribing",
        "The meeting ended. Transcription is running (~1–3 minutes for a short call, "
        "longer for hour-plus recordings). Retry shortly.",
    ),
    "done": (
        "transcribing",
        "The meeting ended. Transcription is running (~1–3 minutes for a short call, "
        "longer for hour-plus recordings). Retry shortly.",
    ),
    "fatal": (
        "bot_failed",
        "The bot failed permanently — no transcript will be produced. "
        "Check the Recall dashboard for the failure reason.",
    ),
}


async def _transcript_not_ready_response(workspace_id: str, meeting_id: str) -> dict:
    """Build a structured 'not ready' response with bot context."""
    from pocketpaw_ee.cloud._core.errors import CloudError
    from pocketpaw_ee.cloud.meetings import service as ms

    bot_status: dict[str, Any] = {}
    try:
        bot_status = await ms.get_bot_status(workspace_id, meeting_id)
    except CloudError as exc:
        # Meeting itself unknown — surface that directly.
        return _error_response(f"{exc.code}: {exc.message}")
    except Exception as exc:  # noqa: BLE001
        logger.warning("transcript not-ready bot lookup failed: %s", exc)

    raw_status = bot_status.get("status") if bot_status.get("has_bot") else None
    state, message = _BOT_STATUS_TO_TRANSCRIPT_STATE.get(
        raw_status,
        ("unknown", f"Transcript is not ready. Bot status: {raw_status or 'unknown'}."),
    )
    return _success_response(
        {
            "ready": False,
            "state": state,
            "message": message,
            "meeting_id": meeting_id,
            "bot": bot_status or {"has_bot": False},
        }
    )


async def _check_bot_handler(args: dict) -> dict:
    workspace_id, _ = _identity()
    if not workspace_id:
        return _error_response("no active workspace")

    meeting_id = args.get("meeting_id")
    if not isinstance(meeting_id, str) or not meeting_id:
        return _error_response("meeting_id is required (string)")

    from pocketpaw_ee.cloud._core.errors import CloudError
    from pocketpaw_ee.cloud.meetings import service as ms

    try:
        status = await ms.get_bot_status(workspace_id, meeting_id)
    except CloudError as exc:
        return _error_response(f"{exc.code}: {exc.message}")
    except Exception as exc:  # noqa: BLE001
        logger.warning("check_meeting_bot failed", exc_info=True)
        return _error_response(f"check_meeting_bot failed: {exc}")

    return _success_response(status)


# ---------------------------------------------------------------------------
# Server factory
# ---------------------------------------------------------------------------


def build_meetings_context_server() -> tuple[str, Any] | None:
    """Build the in-process SDK MCP server for meetings, or return ``None``
    if the Claude Agent SDK isn't installed.

    Matches the shape returned by ``build_tasks_context_server`` so the
    claude_sdk backend's registration loop treats both identically.
    """

    try:
        from claude_agent_sdk import create_sdk_mcp_server, tool
    except ImportError:
        logger.debug("claude_agent_sdk not installed; pocketpaw_meetings MCP disabled")
        return None

    @tool(
        "schedule_meeting",
        (
            "Schedule a Zoom or Google Meet meeting on behalf of the user. "
            "``provider`` must be 'zoom' or 'google_meet' (the user's workspace "
            "must have configured that provider's credentials in Settings → "
            "Integrations → Meetings first). ``scheduled_start`` is ISO 8601 "
            "UTC ('2026-06-01T14:30:00Z'); omit for an instant meeting. "
            "Returns the created meeting including the join URL."
        ),
        {
            "provider": str,
            "title": str,
            "scheduled_start": str,
            "duration_minutes": int,
        },
    )
    async def schedule_meeting(args):  # type: ignore[no-untyped-def]
        return await _schedule_meeting_handler(args)

    @tool(
        "list_meetings",
        (
            "List meetings in the current workspace, newest scheduled first. "
            "Optional filters: ``provider`` ('zoom' | 'google_meet'), "
            "``status`` ('scheduled' | 'in_progress' | 'ended' | 'cancelled'), "
            "``since`` / ``until`` (ISO 8601 bounds on scheduled_start), "
            "``limit`` (default 25, max 200)."
        ),
        {
            "provider": str,
            "status": str,
            "since": str,
            "until": str,
            "limit": int,
        },
    )
    async def list_meetings(args):  # type: ignore[no-untyped-def]
        return await _list_meetings_handler(args)

    @tool(
        "cancel_meeting",
        (
            "Cancel a scheduled meeting by its ID. Zoom actually cancels on "
            "their side and notifies attendees; Google Meet marks it cancelled "
            "locally but the join URL stays live (Meet API limitation)."
        ),
        {"meeting_id": str},
    )
    async def cancel_meeting(args):  # type: ignore[no-untyped-def]
        return await _cancel_meeting_handler(args)

    @tool(
        "search_meetings",
        (
            "Search meetings across all providers by title, organizer email, "
            "or participant name/email. Use this for questions like 'what did "
            "we discuss with Acme last week?'. Optional date bounds ``since`` "
            "and ``until`` (ISO 8601)."
        ),
        {"query": str, "since": str, "until": str, "limit": int},
    )
    async def search_meetings(args):  # type: ignore[no-untyped-def]
        return await _search_meetings_handler(args)

    @tool(
        "find_meeting_transcript",
        (
            "Get a meeting's transcript or — when it isn't ready yet — find "
            "out exactly why. Always inspect the ``ready`` and ``state`` "
            "fields before responding to the user.\n\n"
            "When ``ready`` is true: ``file_id`` points at the stored "
            "transcript blob (fetch via the standard files API). Use "
            "``entry_count`` and ``language`` for context.\n\n"
            "When ``ready`` is false, ``state`` is one of: ``no_bot`` (no "
            "recording bot was sent — ask the user if they want to send "
            "one), ``bot_joining`` / ``bot_starting`` (bot is on its way), "
            "``bot_waiting_admission`` (bot is in the lobby — a host must "
            "admit it), ``bot_in_call_not_recording`` (joined but not "
            "recording yet), ``bot_recording`` (call is in progress — "
            "transcript will be ready a few minutes after it ends), "
            "``transcribing`` (call ended, async transcription is running "
            "— retry in 1–3 minutes), ``bot_recording_denied`` or "
            "``bot_failed`` (no transcript will ever appear). The "
            "``message`` field is the human-readable summary — relay it to "
            "the user instead of inventing your own status text."
        ),
        {"meeting_id": str},
    )
    async def find_meeting_transcript(args):  # type: ignore[no-untyped-def]
        return await _find_transcript_handler(args)

    @tool(
        "send_bot_to_meeting",
        (
            "Send a Recall.ai bot to a meeting to capture audio and "
            "produce a transcript. Use this AFTER ``schedule_meeting`` "
            "when the user wants the meeting recorded / transcribed — "
            "e.g. 'schedule a Zoom for 3pm AND record it', or 'send the "
            "bot to my next meeting'. The bot joins the meeting URL, "
            "records the call, and the transcript becomes available via "
            "``find_meeting_transcript`` once the meeting ends. Returns "
            "the ``bot_id`` for tracking and the bot's current ``status``."
        ),
        {"meeting_id": str},
    )
    async def send_bot_to_meeting(args):  # type: ignore[no-untyped-def]
        return await _send_bot_handler(args)

    @tool(
        "check_meeting_bot",
        (
            "Check where the recording bot is for a meeting — whether it has "
            "joined, is still waiting in the lobby to be admitted, is "
            "recording, or has finished. Use this whenever the user asks "
            "'where is the bot', 'did the bot join', or 'is it recording'. "
            "Returns the bot's live status plus a human-readable summary. "
            "If the status is 'in_waiting_room', someone in the meeting must "
            "admit the bot."
        ),
        {"meeting_id": str},
    )
    async def check_meeting_bot(args):  # type: ignore[no-untyped-def]
        return await _check_bot_handler(args)

    server = create_sdk_mcp_server(
        name=SERVER_NAME,
        version="1.0.0",
        tools=[
            schedule_meeting,
            list_meetings,
            cancel_meeting,
            search_meetings,
            find_meeting_transcript,
            send_bot_to_meeting,
            check_meeting_bot,
        ],
    )
    return SERVER_NAME, server


__all__ = [
    "CANCEL_TOOL_ID",
    "CHECK_BOT_TOOL_ID",
    "LIST_TOOL_ID",
    "MEETING_TOOL_IDS",
    "SCHEDULE_TOOL_ID",
    "SEARCH_TOOL_ID",
    "SEND_BOT_TOOL_ID",
    "SERVER_NAME",
    "TRANSCRIPT_TOOL_ID",
    "build_meetings_context_server",
]
