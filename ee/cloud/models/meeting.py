# Meeting Beanie documents — per-workspace meeting state, transcripts, and BYO provider creds.
# Created: 2026-05-19 — Native meetings integration (Google Meet + Zoom).
# See docs/plans/2026-05-19-meetings-integration-design.md.
#
# Three documents:
#   * MeetingProviderCredentials — per-workspace BYO creds + webhook secret.
#     Token bytes themselves stay in src/pocketpaw/clients/token_store.py;
#     only the *reference* + webhook config + validation state live here.
#   * Meeting — one row per provider meeting we know about.
#   * MeetingTranscript — one row per transcript session. Transcript entries
#     live in the .vtt/.txt blob (referenced via file_id), NOT here.

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from beanie import Indexed
from pydantic import Field

from ee.cloud.models.base import TimestampedDocument

# ---------------------------------------------------------------------------
# Per-workspace per-provider BYO credentials
# ---------------------------------------------------------------------------


class MeetingProviderCredentials(TimestampedDocument):
    """BYO credentials for one provider in one workspace.

    Tenancy: ``workspace`` + ``provider`` are uniquely paired (enforced
    at the service layer). ``credentials_ref`` is the filename of the
    on-disk token blob managed by ``TokenStore`` — Mongo never holds
    secret bytes.

    Phase 2 dropped webhook ingestion in favor of on-demand fetch +
    nightly batch (see ``ee/cloud/meetings/jobs.py`` and the Phase 2
    update in docs/plans/2026-05-19-meetings-integration-design.md).
    No webhook secret or Pub/Sub subscription is stored.
    """

    workspace: Indexed(str)  # type: ignore[valid-type]
    provider: Literal["google_meet", "zoom"]
    credentials_ref: str
    enabled: bool = True
    last_validated_at: datetime | None = None
    last_error: str = ""

    class Settings(TimestampedDocument.Settings):
        name = "meeting_provider_credentials"
        indexes = [
            [("workspace", 1), ("provider", 1)],
        ]


# ---------------------------------------------------------------------------
# Meeting
# ---------------------------------------------------------------------------


MeetingStatus = Literal[
    "scheduled",
    "in_progress",
    "ended",
    "transcript_ready",
    "failed",
    "cancelled",
]


class Meeting(TimestampedDocument):
    """One meeting we know about, in one workspace.

    ``provider_meeting_id`` is the provider's primary ID (Zoom meeting ID
    as a string, or Meet ``conferenceRecords/{name}``); paired with
    ``provider`` it is globally unique. ``provider_space_id`` is the Meet
    ``spaces/{space}`` resource (the persistent "room" — separate from
    each conference instance); null for Zoom.

    ``recording_file_ids`` holds ``FileUpload.file_id`` strings — not
    Mongo ObjectIds — to match the existing files convention.

    ``participants`` is a best-effort snapshot of attendee data from
    the provider; shape varies by provider so it stays as ``list[dict]``
    rather than a typed sub-document.
    """

    workspace: Indexed(str)  # type: ignore[valid-type]
    provider: Literal["google_meet", "zoom"]
    provider_meeting_id: str
    provider_space_id: str | None = None
    title: str | None = None
    join_url: str
    organizer_email: str | None = None
    scheduled_start: datetime | None = None
    scheduled_end: datetime | None = None
    actual_start: datetime | None = None
    actual_end: datetime | None = None
    status: MeetingStatus = "scheduled"
    participants: list[dict[str, Any]] = Field(default_factory=list)
    recording_file_ids: list[str] = Field(default_factory=list)
    raw_provider_payload: dict[str, Any] = Field(default_factory=dict)
    # ``None`` = ingested from webhook (we didn't create it).
    created_by_user_id: str | None = None

    class Settings(TimestampedDocument.Settings):
        name = "meetings"
        indexes = [
            [("workspace", 1), ("status", 1)],
            [("workspace", 1), ("scheduled_start", -1)],
            [("provider", 1), ("provider_meeting_id", 1)],
        ]


# ---------------------------------------------------------------------------
# Meeting transcript
# ---------------------------------------------------------------------------


class MeetingTranscript(TimestampedDocument):
    """One transcript session for one meeting.

    Most meetings have exactly one transcript; Meet can produce multiple
    if recording is stopped and restarted mid-conference. ``file_id``
    references the stored ``.vtt`` / ``.txt`` blob in the uploads
    pipeline — transcript *entries* live in that file, never in Mongo.

    ``indexed_in_kb`` is the join field for the KB indexer's listener
    so we know whether a transcript has been ingested.

    Retention invariant: Google Meet deletes transcript entries from its
    REST API 30 days after the conference ends. The polling fallback
    job uses this window.
    """

    workspace: Indexed(str)  # type: ignore[valid-type]
    meeting_id: Indexed(str)  # type: ignore[valid-type]  # Meeting._id as str
    provider_transcript_id: str
    file_id: str | None = None  # FileUpload.file_id
    entry_count: int = 0
    speaker_count: int = 0
    language: str | None = None
    fetched_at: datetime | None = None
    indexed_in_kb: bool = False

    class Settings(TimestampedDocument.Settings):
        name = "meeting_transcripts"
        indexes = [
            [("workspace", 1), ("indexed_in_kb", 1)],
        ]
