"""Unified meeting events — emitted regardless of source.

These events are source-agnostic (``source: "recall" | "livekit"`` lives
in ``data``). Subscribers — notifications fan-out, KB indexer, the chat
agent activity ticker — bind on event type and don't need to know which
provider produced the meeting.

The events here intentionally mirror the existing ``ee.cloud._core.realtime``
``Event`` shape so they ride the same bus + audience resolver. They are
defined in this module (rather than in ``_core.realtime.events``) so the
meetings module stays the single owner of its event vocabulary.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from pocketpaw_ee.cloud._core.realtime.events import Event


@dataclass
class MeetingScheduled(Event):
    """Fired when a meeting row is created with a future ``scheduled_start``.

    ``data``: ``{workspace_id, meeting_id, source, scheduled_start,
    organizer_user_id, participant_user_ids}``. Drives the
    ``meeting_scheduled`` notification fan-out.
    """

    EVENT_TYPE: ClassVar[str] = "meeting.scheduled"


@dataclass
class MeetingStarted(Event):
    """Fired when a meeting transitions to ``active`` — either explicitly
    via ``start_meeting`` or implicitly by the reminder loop at the exact
    scheduled time.

    ``data``: ``{workspace_id, meeting_id, source, join_url,
    participant_user_ids}``. Drives the ``meeting_started`` notification
    (deep-link join) and the in-app toast.
    """

    EVENT_TYPE: ClassVar[str] = "meeting.started"


@dataclass
class MeetingEnded(Event):
    """Fired when the meeting ends — provider lifecycle complete, no more
    live events expected. Recordings + transcripts may still arrive later
    via their own events.

    ``data``: ``{workspace_id, meeting_id, source, actual_end}``. Used by
    the KB indexer to mark the meeting closed; no notification fan-out
    (users already left the call).
    """

    EVENT_TYPE: ClassVar[str] = "meeting.ended"


@dataclass
class MeetingCancelled(Event):
    """Fired when a scheduled meeting is cancelled before it starts.

    ``data``: ``{workspace_id, meeting_id, source, cancelled_by_user_id,
    participant_user_ids}``. Drives the ``meeting_cancelled``
    notification.
    """

    EVENT_TYPE: ClassVar[str] = "meeting.cancelled"


@dataclass
class MeetingReminder(Event):
    """Fired by the scheduling reminder loop ~5 minutes before
    ``scheduled_start``. Distinct from ``Scheduled`` so the notification
    bridge can use a different template + suppress duplicates.

    ``data``: ``{workspace_id, meeting_id, source, scheduled_start,
    participant_user_ids}``.
    """

    EVENT_TYPE: ClassVar[str] = "meeting.reminder"


@dataclass
class MeetingRecordingReady(Event):
    """Fired when a recording artefact lands and is attached to the
    meeting — typically from a provider webhook (Recall ``recording.done``,
    LiveKit ``egress_ended``).

    ``data``: ``{workspace_id, meeting_id, source, file_id, duration_seconds}``.
    KB indexer subscribes to attach the recording's audio/video. UI
    subscribes to refresh the meeting detail view.
    """

    EVENT_TYPE: ClassVar[str] = "meeting.recording_ready"


@dataclass
class MeetingTranscriptReady(Event):
    """Fired when a transcript artefact lands — Recall ``transcript.done``
    or post-call LiveKit transcription (follow-up).

    ``data``: ``{workspace_id, meeting_id, source, file_id, entry_count,
    speaker_count, language}``. KB indexer ingests the VTT into the
    workspace knowledge base. UI surfaces it in the meeting detail.

    Distinct from ``RecordingReady`` so subscribers that only care about
    one don't have to match on a kind field.
    """

    EVENT_TYPE: ClassVar[str] = "meeting.transcript_ready"


__all__ = [
    "MeetingCancelled",
    "MeetingEnded",
    "MeetingRecordingReady",
    "MeetingReminder",
    "MeetingScheduled",
    "MeetingStarted",
    "MeetingTranscriptReady",
]
