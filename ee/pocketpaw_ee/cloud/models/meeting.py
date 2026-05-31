# Meeting Beanie documents — per-workspace meeting state + transcripts.
# Created: 2026-05-19 — Native meetings integration (Google Meet + Zoom).
# See docs/plans/2026-05-19-meetings-integration-design.md.
#
# Two documents:
#   * Meeting — one row per provider meeting we know about.
#   * MeetingTranscript — one row per transcript session. Transcript entries
#     live in the .vtt/.txt blob (referenced via file_id), NOT here.
#
# Provider credentials live in MeetingProviderCredentials — one
# deployment-global row per provider, set via the Settings connector
# page, with secret values encrypted at rest (_core/crypto.py). The
# ZOOM_* / GOOGLE_MEET_* environment variables remain a fallback when no
# stored row exists.

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from beanie import Indexed
from pydantic import Field

from pocketpaw_ee.cloud.models.base import TimestampedDocument

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
    # source — which platform module owns this meeting's lifecycle.
    # "recall" = an external Zoom/Meet/Teams meeting we capture via a
    # Recall.ai bot. "livekit" = a native room hosted on our LiveKit
    # Cloud. Default "recall" for back-compat: every row that existed
    # before the unified meetings platform was a Recall meeting.
    source: Literal["recall", "livekit"] = "recall"
    # provider — Recall-specific external platform. Set when source="recall".
    # For source="livekit" leave unset (the provider IS LiveKit, named in
    # `source`). Optional so LiveKit-created rows can omit it.
    provider: Literal["google_meet", "zoom"] | None = None
    # External meeting id — Recall's view (Zoom meeting ID / Meet
    # `conferenceRecords/{name}`). For source="livekit" use the room name
    # or leave empty; nothing in the Recall paths reads it for LiveKit rows.
    provider_meeting_id: str = ""
    provider_space_id: str | None = None
    title: str | None = None
    # For LiveKit meetings this is the pocketpaw:// or web deep link that
    # opens the in-app call. For Recall meetings it's the third-party
    # Zoom/Meet join URL.
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
    # Recall.ai bot lifecycle status — the latest ``status_changes`` code
    # (``joining_call`` / ``in_waiting_room`` / ``in_call_recording`` /
    # ``done`` / ``fatal`` / …). Updated by the bot.status_change webhook
    # and by on-demand status checks. ``None`` until a bot is dispatched.
    bot_status: str | None = None
    bot_status_detail: str | None = None  # Recall sub_code, e.g. bot_kicked_from_call
    bot_status_at: datetime | None = None

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
    # Version of the KB-extraction pipeline that last ingested this
    # transcript. Bumped whenever the cleaner changes shape (e.g. the
    # 2026-05-26 VTT-cue-stripper). The startup migration walks rows
    # with version < TRANSCRIPT_KB_VERSION and re-emits FileReady so the
    # newer extractor re-ingests cleaned text. Idempotent — runs once
    # per row per upgrade.
    kb_indexed_version: int = 0

    class Settings(TimestampedDocument.Settings):
        name = "meeting_transcripts"
        indexes = [
            [("workspace", 1), ("indexed_in_kb", 1)],
            [("kb_indexed_version", 1)],
        ]


# ---------------------------------------------------------------------------
# Provider credentials
# ---------------------------------------------------------------------------


class MeetingProviderCredentials(TimestampedDocument):
    """Stored credentials for ONE meeting provider — deployment-global.

    Single-account model: exactly one row per provider for the whole
    deployment (``provider`` is the unique key — there is deliberately no
    ``workspace`` field). A workspace admin sets these via the
    Settings → Meetings connector page.

    Secret values (Zoom ``client_secret``; Google Meet ``client_secret``
    + ``refresh_token``) are stored in ``secret_enc`` — a Fernet token
    over a JSON dict (see _core/crypto.py). Non-secret values (Zoom
    ``account_id`` + ``client_id``; Meet ``client_id``) live in
    ``public_config`` in clear text so the UI can echo them back.

    ``enabled`` flips True only once the credentials are validated — Zoom
    via a live token grant, Google Meet once OAuth consent completes.
    ``pending_state`` holds the in-flight OAuth ``state`` nonce between
    the auth-url call and the consent callback.
    """

    provider: Indexed(str, unique=True)  # type: ignore[valid-type]  # "zoom" | "google_meet"
    enabled: bool = False
    public_config: dict[str, str] = Field(default_factory=dict)
    secret_enc: str = ""
    pending_state: str | None = None
    last_validated_at: datetime | None = None
    last_error: str = ""

    class Settings(TimestampedDocument.Settings):
        name = "meeting_provider_credentials"


# ---------------------------------------------------------------------------
# Deployment settings
# ---------------------------------------------------------------------------


class MeetingsSettings(TimestampedDocument):
    """Deployment-global meetings configuration — a singleton row.

    Holds the Recall.ai bot transcription choice. The service
    (meetings/settings.py) reads and upserts the single row; the
    ``RECALL_TRANSCRIPT_PROVIDER`` / ``RECALL_TRANSCRIPT_MODEL``
    environment variables are the fallback when no row exists.

    ``transcript_provider`` selects the path: a ``meeting_captions`` /
    ``*_streaming`` value transcribes live on the bot; an ``*_async``
    value records only, then transcribes post-call via Recall's
    ``create_transcript`` endpoint. ``transcript_model`` is the provider
    model name (e.g. Deepgram ``nova-3``) — used only by async providers
    that accept one.
    """

    transcript_provider: str = "meeting_captions"
    transcript_model: str = ""

    class Settings(TimestampedDocument.Settings):
        name = "meetings_settings"
