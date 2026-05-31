"""RecallProvider — MeetingProvider implementation for source="recall".

External Zoom/Meet/Teams meetings captured by a Recall.ai bot. This is the
working reference implementation alongside (and contrasted with) the
LiveKit provider that lives in a sibling package.

Methods are thin wrappers over the existing Recall helpers:
  * ``create`` calls the Zoom/Meet adapter via ``meetings.service._adapter_factory``
    and returns the raw provider response. The service layer persists the
    MeetingDoc.
  * ``cancel`` calls the adapter's ``meeting_cancel`` action.
  * ``start`` / ``end`` are no-ops — Recall meetings have no platform-managed
    lifecycle (the third-party call starts/ends when participants
    join/leave). They're here to satisfy the MeetingProvider protocol.
  * ``request_recording`` / ``stop_recording`` (SupportsRecording) call
    Recall.ai's ``request_bot_for_meeting`` / ``stop_bot``.
  * ``fetch_transcript`` (SupportsTranscript) pulls the captured transcript
    from Recall via the existing client.

The LiveKit engineer reads this file as the canonical example when
implementing their own provider in ``providers/livekit/provider.py``.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from pocketpaw.connectors.protocol import ActionResult
from pocketpaw_ee.cloud._core.errors import NotFound, ValidationError
from pocketpaw_ee.cloud.meetings.dto import CreateMeetingRequest
from pocketpaw_ee.cloud.meetings.providers.base import (
    ProviderCreateResult,
    ProviderStartResult,
    RecordingRef,
    TranscriptArtefact,
)
from pocketpaw_ee.cloud.models.meeting import Meeting as _MeetingDoc

logger = logging.getLogger(__name__)


class RecallProvider:
    """MeetingProvider + SupportsRecording + SupportsTranscript for Recall."""

    name = "recall"

    # ----- MeetingProvider -----

    async def create(self, ctx: Any, body: CreateMeetingRequest) -> ProviderCreateResult:
        """Schedule the underlying Zoom/Meet meeting via its adapter.

        The service layer persists the MeetingDoc using the returned
        provider_payload + join_url. We do NOT persist here — keeping
        provider methods side-effect-free against our DB makes them
        idempotent and easy to test.
        """
        body = CreateMeetingRequest.model_validate(body)
        if not body.provider:
            raise ValidationError(
                "meeting.recall_provider_required",
                "source='recall' requires provider to be 'zoom' or 'google_meet'.",
            )

        # Late import — _adapter_factory is on the service, which imports
        # this provider indirectly when the registry is populated.
        from pocketpaw_ee.cloud.meetings.service import _adapter_factory

        workspace_id = _workspace_of(ctx)
        adapter = await _adapter_factory(workspace_id, body.provider)

        params: dict[str, Any] = {
            "topic": body.title,
            "duration_minutes": body.duration_minutes,
        }
        if body.scheduled_start is not None:
            params["start_time"] = body.scheduled_start.strftime("%Y-%m-%dT%H:%M:%SZ")

        result: ActionResult = await adapter.execute("meeting_create", params)
        if not result.success:
            raise ValidationError(
                "meeting.provider_error",
                result.error or f"{body.provider} rejected the create request",
            )

        payload = result.data or {}
        join_url = str(payload.get("join_url") or payload.get("meetingUri") or "") or None

        return ProviderCreateResult(provider_payload=payload, join_url=join_url)

    async def start(self, ctx: Any, meeting: Any) -> ProviderStartResult:  # noqa: ARG002
        """No-op for Recall. The third-party call starts when humans join.

        Implemented to satisfy the protocol so callers can dispatch
        uniformly across sources without a hasattr check.
        """
        return ProviderStartResult()

    async def cancel(self, ctx: Any, meeting: Any) -> None:
        """Cancel the underlying Zoom/Meet meeting via its adapter."""
        from pocketpaw_ee.cloud.meetings.service import _adapter_factory

        workspace_id = _workspace_of(ctx)
        provider = _provider_of(meeting)
        if provider is None:
            # Likely a livekit meeting misrouted here — no-op.
            return None

        adapter = await _adapter_factory(workspace_id, provider)
        result: ActionResult = await adapter.execute(
            "meeting_cancel", {"meeting_id": _provider_meeting_id_of(meeting)}
        )
        if not result.success:
            raise ValidationError(
                "meeting.provider_error",
                result.error or f"{provider} cancel failed",
            )

    async def end(self, ctx: Any, meeting: Any) -> None:  # noqa: ARG002
        """No-op for Recall. The third-party call ends when humans leave."""
        return None

    # ----- SupportsRecording -----

    async def request_recording(self, ctx: Any, meeting: Any) -> RecordingRef:
        """Dispatch a Recall.ai bot to capture the meeting."""
        from pocketpaw_ee.cloud.meetings.providers.recall import client as recall_client

        workspace_id = _workspace_of(ctx)
        meeting_id = _id_of(meeting)
        payload = await recall_client.request_bot_for_meeting(workspace_id, meeting_id)
        return RecordingRef(
            provider="recall",
            external_id=str(payload.get("bot_id") or ""),
            status="recording",
            started_at=datetime.now(UTC),
            file_id=None,  # Filled later by the recording.done webhook.
        )

    async def stop_recording(self, ctx: Any, meeting: Any) -> None:
        """Tell an active Recall bot to leave the meeting."""
        from pocketpaw_ee.cloud.meetings.providers.recall import client as recall_client

        workspace_id = _workspace_of(ctx)
        meeting_id = _id_of(meeting)
        await recall_client.stop_bot(workspace_id, meeting_id)

    # ----- SupportsTranscript -----

    async def fetch_transcript(self, ctx: Any, meeting: Any) -> TranscriptArtefact | None:
        """Pull the captured transcript from Recall as a TranscriptArtefact.

        Returns None when no transcript is ready yet (no bot dispatched,
        bot still recording, or Recall still transcribing — caller polls).
        """
        from pocketpaw_ee.cloud.meetings.providers.recall import client as recall_client

        workspace_id = _workspace_of(ctx)
        meeting_id = _id_of(meeting)
        try:
            vtt = await recall_client.fetch_transcript_vtt(workspace_id, meeting_id)
        except NotFound:
            return None
        if not vtt:
            return None

        # Light counts derived from the VTT. The service layer's
        # fetch_and_store_transcript path computes richer stats; this
        # provider method intentionally stays light so the LiveKit engineer
        # sees a clean shape.
        entry_count = vtt.count("\n--> ") + vtt.count(" --> ")
        speaker_count = len({line for line in vtt.splitlines() if line.startswith("<v ")})
        return TranscriptArtefact(
            vtt=vtt,
            entry_count=entry_count,
            speaker_count=speaker_count,
            language=None,
        )


# ---------------------------------------------------------------------------
# Context shape adapters — RequestContext today, dict tomorrow, anything
# duck-typed in tests. Keeping these as small helpers means the provider
# methods don't fork on the call site's preferred shape.
# ---------------------------------------------------------------------------


def _workspace_of(ctx: Any) -> str:
    """Pull ``workspace_id`` off a RequestContext / dict / dataclass."""
    ws = getattr(ctx, "workspace_id", None)
    if ws is None and isinstance(ctx, dict):
        ws = ctx.get("workspace_id")
    if not ws:
        raise ValidationError(
            "meeting.no_workspace",
            "RecallProvider requires ctx.workspace_id to be set.",
        )
    return str(ws)


def _id_of(meeting: Any) -> str:
    """Pull a meeting id off a Meeting domain object, a MeetingDoc, or a dict."""
    mid = getattr(meeting, "id", None)
    if mid is None and isinstance(meeting, dict):
        mid = meeting.get("id") or meeting.get("meeting_id")
    return str(mid or "")


def _provider_of(meeting: Any) -> str | None:
    """Pull the external provider (zoom|google_meet) off a meeting."""
    p = getattr(meeting, "provider", None)
    if p is None and isinstance(meeting, dict):
        p = meeting.get("provider")
    return p if p in ("zoom", "google_meet") else None


def _provider_meeting_id_of(meeting: Any) -> str:
    """Pull the external provider's meeting id (zoom meeting id / Meet name)."""
    pmid = getattr(meeting, "provider_meeting_id", None)
    if pmid is None and isinstance(meeting, _MeetingDoc):
        pmid = meeting.provider_meeting_id
    if pmid is None and isinstance(meeting, dict):
        pmid = meeting.get("provider_meeting_id")
    return str(pmid or "")


__all__ = ["RecallProvider"]
