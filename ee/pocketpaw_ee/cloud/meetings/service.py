# Meetings — workspace-scoped business logic.
# Created: 2026-05-19. Module-level async API. Sole owner of writes to
# Meeting / MeetingTranscript Beanie docs. Provider calls are delegated
# to adapters under src/pocketpaw/connectors/adapters/ (wired in Phase 1.5).
#
# Cloud rules followed (per backend/CLAUDE.md ee/cloud Code Rules):
#   §2  Writes go through this service; routers never import models.
#   §5  Module-level async functions, not a class.
#   §6  Every request schema is re-validated at the service entry.
#   §7  Every read filters by workspace_id.
#   §9  Every write emits an event (or carries a ``# no-event`` justification).
#   §10 Errors via CloudError, never HTTPException.
#
# 2026-10-01 (feat/meetings-instant, MC-1): ``start_instant_meeting`` backs
# ``POST /meetings/instant`` — a hidden ``type="meeting"`` chat room, one Meeting
# row with a meeting code, and a live LiveKit call, all or nothing. Meeting codes
# are 10 letters from an alphabet without l/i/o, stored WITHOUT dashes and shown
# / linked as ``xxx-xxxx-xxx`` (a lookup should strip dashes and lowercase, so
# both forms resolve). ``MeetingResponse`` carries ``code``, ``link``,
# ``room_group_id``, ``access``, ``host_user_id`` and ``description``.
#
# 2026-10-01 (feat/meetings-by-code, MC-2): meetings for later + join by code.
#   * ``create_meeting`` with ``source="livekit"`` and no ``group_id`` makes a
#     hidden meeting room and a coded Meeting WITHOUT starting a call. Status is
#     ``scheduled``; a null ``scheduled_start`` means "for later, no date" (the
#     reminder/auto-start jobs skip null starts). A LiveKit meeting on an explicit
#     chat room keeps its old behaviour and also gets a code + link.
#   * ``link_expires_at``: undated (``scheduled_end`` is None, instant meetings
#     included) = 30 days from creation, pushed to 30 days after each join; dated
#     = ``scheduled_end``. ``list_meetings`` self-heals an expired undated
#     ``scheduled`` row to ``ended``.
#   * ``lookup_meeting_by_code`` (public) returns six fields; status is
#     ``not_started`` | ``live`` | ``ended`` from the row plus real room presence.
#   * ``join_meeting_by_code``: same-workspace members only; adds the caller to
#     a meeting room (never to a chat room — those still need membership) and
#     makes sure the call is running through ``create_room`` (budget gate).
#   * ``start_meeting_room_call`` backs ``POST /livekit/rooms`` for a hidden
#     meeting room (the call engine always calls it on join): same
#     ``_run_meeting_call`` as a by-code join, so no "Instant call" twin row.
#
# 2026-10-01 (feat/meetings-lobby, MC-3): ``update_meeting`` backs
# ``PATCH /meetings/{id}`` — host only (``host_user_id``, else the creator),
# ``access`` / ``title`` / ``description``, realtime ``meeting.updated`` to the
# meeting's room. The lobby itself lives in lobby_service.py.
#
# 2026-10-01 (feat/meetings-ics, MC-4): scheduling on meeting rooms.
#   * ``update_meeting`` also reschedules: ``scheduled_start`` /
#     ``duration_minutes`` on a LiveKit meeting that is ``scheduled`` and has not
#     started (else 409 ``meeting.not_reschedulable``). It recomputes
#     ``scheduled_end`` and ``link_expires_at``, replaces the reminder / auto-start
#     / auto-end jobs (old ones removed first, so a reminder that no longer fits
#     can't fire at the old time) and emits bus ``meeting.edited`` so the calendar
#     bridge re-syncs the in-app event. (Bus ``meeting.updated`` is the Recall
#     webhook upsert; the bridge deliberately doesn't listen to that.)
#   * ``get_joining_info`` / ``get_meeting_ics`` back ``GET /meetings/{id}/
#     joining-info`` and ``/ics``: open to the meeting's workspace, the same
#     visibility as ``GET /meetings/{id}`` (which already carries code + link).
#     ``MeetingResponse.joining_info`` carries the same text. Formatting is in ics.py.

from __future__ import annotations

import logging
import os
import secrets
from datetime import UTC, datetime, timedelta

from pymongo.errors import DuplicateKeyError

from pocketpaw.connectors.protocol import ActionResult
from pocketpaw_ee.cloud._core.errors import (
    CloudError,
    ConflictError,
    Forbidden,
    NotFound,
    ValidationError,
)
from pocketpaw_ee.cloud.meetings import ics
from pocketpaw_ee.cloud.meetings.domain import Meeting as MeetingDomain
from pocketpaw_ee.cloud.meetings.dto import (
    CreateMeetingRequest,
    JoinMeetingByCodeResponse,
    ListMeetingsRequest,
    MeetingDetailResponse,
    MeetingLookupResponse,
    MeetingResponse,
    StartInstantMeetingRequest,
    TranscriptResponse,
    UpdateMeetingRequest,
)
from pocketpaw_ee.cloud.models.meeting import Meeting as _MeetingDoc
from pocketpaw_ee.cloud.models.meeting import MeetingTranscript as _TranscriptDoc
from pocketpaw_ee.cloud.shared.events import event_bus

logger = logging.getLogger(__name__)


# Bump when the VTT → KB pipeline changes shape (extractor strips cue tags,
# mime is text/vtt, etc). The startup migration re-emits FileReady for
# every transcript whose stored ``kb_indexed_version`` is below this so
# kb-go re-ingests through the current cleaner. v1 (2026-05-26): added
# _vtt_to_plain in LocalExtractor + mime=text/vtt on writes.
TRANSCRIPT_KB_VERSION = 1


# ---------------------------------------------------------------------------
# Mapping helpers (rule §8 — same-file private helpers, not separate module)
# ---------------------------------------------------------------------------


# Meeting codes: 10 letters, no l / i / o (they read as 1 / 0 when typed from
# a screen). 23**10 ≈ 4e13 codes, so a collision on the unique index is rare;
# the insert retries a few times.
_CODE_ALPHABET = "abcdefghjkmnpqrstuvwxyz"
_CODE_LENGTH = 10
_CODE_ATTEMPTS = 5
INSTANT_MEETING_TITLE = "Instant meeting"
# How long an undated meeting's link lives, counted from creation and then
# from each join.
LINK_TTL = timedelta(days=30)


def _new_code() -> str:
    """A canonical (dash-less) meeting code."""
    return "".join(secrets.choice(_CODE_ALPHABET) for _ in range(_CODE_LENGTH))


def display_code(code: str) -> str:
    """``abcdefghjk`` → ``abc-defg-hjk``."""
    return f"{code[:3]}-{code[3:7]}-{code[7:]}"


def _frontend_base() -> str:
    return os.environ.get("POCKETPAW_FRONTEND_BASE_URL", "http://localhost:1420").rstrip("/")


def _meeting_link(code: str) -> str:
    """``<frontend base>/m/<display code>`` — same base the OAuth redirects use."""
    return f"{_frontend_base()}/m/{display_code(code)}"


def joining_info_for(doc: _MeetingDoc) -> str | None:
    """The paste-into-an-invite text; None for a meeting without a code."""
    if not doc.code:
        return None
    return ics.joining_info(
        title=doc.title,
        start=doc.scheduled_start,
        end=doc.scheduled_end,
        link=_meeting_link(doc.code),
        code=display_code(doc.code),
        description=doc.description,
    )


def _canonical_code(code: str) -> str | None:
    """``ABC-defg-HJK`` → ``abcdefghjk``; None when it can't be a meeting code."""
    c = code.replace("-", "").strip().lower()
    if len(c) != _CODE_LENGTH or any(ch not in _CODE_ALPHABET for ch in c):
        return None
    return c


def _room_of(doc: _MeetingDoc) -> str | None:
    """The chat group the meeting's call runs in (hidden room or chat room)."""
    return doc.room_group_id or (doc.raw_provider_payload or {}).get("group_id")


def _is_closed(doc: _MeetingDoc, now: datetime) -> bool:
    """Ended, cancelled, failed, or past its link expiry: no one can join."""
    if doc.status not in ("scheduled", "in_progress"):
        return True
    expires = _aware(doc.link_expires_at)
    return expires is not None and expires <= now


def _doc_to_response(doc: _MeetingDoc, *, transcript_available: bool = False) -> MeetingResponse:
    payload = doc.raw_provider_payload or {}
    return MeetingResponse(
        id=str(doc.id),
        source=getattr(doc, "source", "recall"),
        provider=doc.provider,
        provider_meeting_id=doc.provider_meeting_id,
        group_id=payload.get("group_id"),
        title=doc.title,
        join_url=doc.join_url,
        organizer_email=doc.organizer_email,
        scheduled_start=doc.scheduled_start,
        scheduled_end=doc.scheduled_end,
        duration_minutes=payload.get("duration_minutes", 30),
        actual_start=doc.actual_start,
        actual_end=doc.actual_end,
        status=doc.status,
        participants=list(doc.participants),
        recording_file_ids=list(doc.recording_file_ids),
        transcript_available=transcript_available,
        created_at=doc.createdAt,
        bot_status=doc.bot_status,
        bot_status_detail=doc.bot_status_detail,
        bot_status_at=doc.bot_status_at,
        auto_created_from_calendar=payload.get("auto_created_by") == "calendar_bridge",
        calendar_event_id=payload.get("calendar_event_id"),
        code=display_code(doc.code) if doc.code else None,
        link=_meeting_link(doc.code) if doc.code else None,
        room_group_id=doc.room_group_id,
        access=doc.access,
        host_user_id=doc.host_user_id,
        description=doc.description,
        joining_info=joining_info_for(doc),
    )


def _doc_to_detail(
    doc: _MeetingDoc, *, transcript_available: bool = False
) -> MeetingDetailResponse:
    base = _doc_to_response(doc, transcript_available=transcript_available).model_dump()
    return MeetingDetailResponse(**base, raw_provider_payload=dict(doc.raw_provider_payload))


def _doc_to_domain(doc: _MeetingDoc) -> MeetingDomain:
    return MeetingDomain(
        id=str(doc.id),
        workspace_id=doc.workspace,
        provider=doc.provider,
        provider_meeting_id=doc.provider_meeting_id,
        provider_space_id=doc.provider_space_id,
        title=doc.title,
        join_url=doc.join_url,
        organizer_email=doc.organizer_email,
        scheduled_start=doc.scheduled_start,
        scheduled_end=doc.scheduled_end,
        actual_start=doc.actual_start,
        actual_end=doc.actual_end,
        status=doc.status,
        participants=tuple(doc.participants),
        recording_file_ids=tuple(doc.recording_file_ids),
        created_by_user_id=doc.created_by_user_id,
        created_at=doc.createdAt,
        updated_at=doc.updatedAt,
    )


def _aware(dt: datetime | None) -> datetime | None:
    """Coerce a datetime to timezone-aware UTC.

    Mongo/Beanie hand back naive datetimes (stored as UTC), while request
    DTOs and the MCP tools parse ISO strings as tz-aware. Normalize both
    sides before comparing — otherwise Python raises "can't compare
    offset-naive and offset-aware datetimes".
    """
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


# ---------------------------------------------------------------------------
# Public API — meetings
# ---------------------------------------------------------------------------


async def list_meetings(workspace_id: str, body: ListMeetingsRequest) -> list[MeetingResponse]:
    """List meetings for this workspace, newest scheduled first.

    Read-only. Tenant filter on the Beanie query (rule §7). Returns a
    plain list — pagination via cursor lands in Phase 1.5 once we have
    real data volumes to size against.

    Before returning, hydrates stale meeting statuses:
      - ``scheduled`` → ``ended`` when ``scheduled_end + 5 min`` grace has passed
      - ``in_progress`` → ``ended`` when ``scheduled_end + 5 min`` grace has passed
    This self-heals any statuses that the APScheduler auto-end job missed
    (server restart, job scheduling bug, etc.).
    """
    body = ListMeetingsRequest.model_validate(body)

    # ── Hydration: auto-transition stale statuses ──────────────────────
    now_utc = datetime.now(UTC)
    grace = timedelta(minutes=5)

    # scheduled → ended (meeting never started)
    stale_scheduled = await _MeetingDoc.find(
        {
            "workspace": workspace_id,
            "status": "scheduled",
            "scheduled_end": {"$ne": None},
        }
    ).to_list()
    for doc in stale_scheduled:
        end = _aware(doc.scheduled_end)
        if end and end + grace < now_utc:
            doc.status = "ended"
            doc.actual_end = now_utc
            await doc.save()
            logger.info("Hydrated meeting %s: scheduled → ended (past scheduled_end)", doc.id)

    # in_progress → ended (meeting ran past scheduled_end + grace)
    stale_running = await _MeetingDoc.find(
        {
            "workspace": workspace_id,
            "status": "in_progress",
            "scheduled_end": {"$ne": None},
        }
    ).to_list()
    for doc in stale_running:
        end = _aware(doc.scheduled_end)
        if end and end + grace < now_utc:
            doc.status = "ended"
            doc.actual_end = now_utc
            await doc.save()
            logger.info("Hydrated meeting %s: in_progress → ended (past scheduled_end)", doc.id)

    # scheduled → ended (undated meeting whose link expired unused)
    expired_links = await _MeetingDoc.find(
        {
            "workspace": workspace_id,
            "status": "scheduled",
            "scheduled_end": None,
            "link_expires_at": {"$lt": now_utc.replace(tzinfo=None)},
        }
    ).to_list()
    for doc in expired_links:
        doc.status = "ended"
        await doc.save()
        logger.info("Hydrated meeting %s: scheduled → ended (link expired)", doc.id)

    # ── Main query ────────────────────────────────────────────────────
    query: dict = {"workspace": workspace_id}
    if body.provider:
        query["provider"] = body.provider
    if body.status:
        query["status"] = body.status

    docs = (
        await _MeetingDoc.find(query)
        .sort([("scheduled_start", -1), ("createdAt", -1)])
        .limit(body.limit)
        .to_list()
    )
    # Filter date range in Python — index doesn't cover both, and date
    # filtering on top of the cursor keeps the query plan simple.
    if body.since:
        since = _aware(body.since)
        docs = [d for d in docs if (sd := _aware(d.scheduled_start)) and sd >= since]
    if body.until:
        until = _aware(body.until)
        docs = [d for d in docs if (sd := _aware(d.scheduled_start)) and sd <= until]

    if not docs:
        return []

    # Bulk lookup: which meeting_ids have a USEFUL transcript file?
    # Match get_transcript's gate (file_id AND entry_count > 0) so the
    # panel's "ready" badge can't disagree with what the open action
    # returns — a stale row with 0 entries used to show "ready" and
    # then 404 on click.
    meeting_ids = [str(d.id) for d in docs]
    transcripts = await _TranscriptDoc.find(
        _TranscriptDoc.workspace == workspace_id,
        {
            "meeting_id": {"$in": meeting_ids},
            "file_id": {"$ne": None},
            "entry_count": {"$gt": 0},
        },
    ).to_list()
    have_transcript = {t.meeting_id for t in transcripts}

    return [_doc_to_response(d, transcript_available=str(d.id) in have_transcript) for d in docs]


async def get_meeting(workspace_id: str, meeting_id: str) -> MeetingDetailResponse:
    """One meeting's detail. Raises NotFound if unknown to this workspace."""
    doc = await _MeetingDoc.find_one(
        _MeetingDoc.workspace == workspace_id,
        {"_id": meeting_id} if False else _MeetingDoc.id == meeting_id,
    )
    # Beanie ObjectId coercion fallback — accept string IDs from URL.
    if doc is None:
        try:
            from beanie import PydanticObjectId

            doc = await _MeetingDoc.find_one(
                _MeetingDoc.workspace == workspace_id,
                _MeetingDoc.id == PydanticObjectId(meeting_id),
            )
        except Exception:
            doc = None
    if doc is None:
        raise NotFound("meeting", meeting_id)

    transcript_doc = await _TranscriptDoc.find_one(
        _TranscriptDoc.workspace == workspace_id,
        _TranscriptDoc.meeting_id == str(doc.id),
    )
    # "ready" means the open path will return content — same gate as
    # get_transcript's fast path. file_id alone isn't enough: a stale
    # row with 0 cues would show ready then 404 on click.
    has_file = (
        transcript_doc is not None
        and transcript_doc.file_id is not None
        and transcript_doc.entry_count > 0
    )
    return _doc_to_detail(doc, transcript_available=has_file)


# ---------------------------------------------------------------------------
# Adapter factory — constructs a ConnectorProtocol instance from stored or
# env credentials. Single-account model: the deployment configures ONE Zoom
# S2S app and ONE Google Cloud OAuth client, either through the Settings
# connector page (stored, encrypted) or via environment variables. Tests
# replace this via ``_set_adapter_factory`` to inject fakes.
# ---------------------------------------------------------------------------


async def _build_adapter_default(workspace_id: str, provider: str):
    """Default factory: build a provider adapter from stored or env credentials.

    Resolution order: the ``MeetingProviderCredentials`` store first (set
    via Settings → Meetings), then the ``ZOOM_*`` / ``GOOGLE_MEET_*``
    environment variables as a fallback. ``workspace_id`` is accepted for
    signature compatibility but unused — credentials are deployment-wide,
    not per-tenant. Raises ``ValidationError`` when neither source is set.
    """
    from pocketpaw_ee.cloud.meetings.providers.recall import credentials as creds_service

    stored = await creds_service.resolve(provider)

    if provider == "zoom":
        if stored is not None:
            account_id = stored["account_id"]
            client_id = stored["client_id"]
            client_secret = stored["client_secret"]
        else:
            account_id = os.environ.get("ZOOM_ACCOUNT_ID", "").strip()
            client_id = os.environ.get("ZOOM_CLIENT_ID", "").strip()
            client_secret = os.environ.get("ZOOM_CLIENT_SECRET", "").strip()
            if not (account_id and client_id and client_secret):
                raise ValidationError(
                    "meeting.zoom_not_configured",
                    "Zoom is not configured — connect it in Settings → Meetings, "
                    "or set ZOOM_ACCOUNT_ID, ZOOM_CLIENT_ID and ZOOM_CLIENT_SECRET.",
                )
        from pocketpaw_ee.cloud.meetings.providers.recall.adapters.zoom import ZoomConnector

        return ZoomConnector(account_id, client_id, client_secret)

    if provider == "google_meet":
        if stored is not None:
            client_id = stored["client_id"]
            client_secret = stored["client_secret"]
            refresh_token = stored["refresh_token"]
        else:
            client_id = os.environ.get("GOOGLE_MEET_CLIENT_ID", "").strip()
            client_secret = os.environ.get("GOOGLE_MEET_CLIENT_SECRET", "").strip()
            refresh_token = os.environ.get("GOOGLE_MEET_REFRESH_TOKEN", "").strip()
            if not (client_id and client_secret and refresh_token):
                raise ValidationError(
                    "meeting.google_meet_not_configured",
                    "Google Meet is not configured — connect it in Settings → "
                    "Meetings, or set GOOGLE_MEET_CLIENT_ID, GOOGLE_MEET_CLIENT_SECRET "
                    "and GOOGLE_MEET_REFRESH_TOKEN.",
                )
        from pocketpaw_ee.cloud.meetings.providers.recall.adapters.google_meet import (
            GoogleMeetConnector,
        )

        return GoogleMeetConnector(client_id, client_secret, refresh_token)

    raise ValidationError("meeting.unknown_provider", f"Unsupported meetings provider: {provider}")


_adapter_factory = _build_adapter_default


def _set_adapter_factory(fn):
    """Test-only seam: swap the adapter factory globally.

    Pattern matches how ``pocketpaw_ee.cloud.kb.knowledge_router`` exposes its
    ``_call_kb_list`` for monkeypatching — keeps the production path
    free of test indirection.
    """
    global _adapter_factory
    prev = _adapter_factory
    _adapter_factory = fn
    return prev


async def create_meeting(
    workspace_id: str,
    user_id: str,
    body: CreateMeetingRequest,
) -> MeetingResponse:
    """Create a meeting via the configured provider adapter.

    Flow:
      1. Validate input (rule §6) — non-empty title.
      2. Resolve the provider adapter for this workspace.
      3. Call ``adapter.execute("meeting_create", ...)`` — adapter
         wraps provider failures as ``ActionResult(success=False)``.
      4. Persist a ``Meeting`` row with provider-returned IDs + join URL
         (LiveKit rows also get a meeting code, link and link expiry).
      5. Emit ``meeting.scheduled``.

    LiveKit with no ``group_id`` is "create a meeting for later": a hidden
    meeting room is made first and removed again if anything after it fails.
    """
    body = CreateMeetingRequest.model_validate(body)
    if not body.title.strip():
        raise ValidationError("meeting.empty_title", "title must not be empty or whitespace")

    from pocketpaw_ee.cloud.chat import group_service

    # A LiveKit meeting with no chat room gets its own hidden meeting room (host
    # as only member); the provider then sees it as an ordinary group_id. No call
    # starts here — the first join, or the scheduled auto-start, starts it.
    meeting_room_id: str | None = None
    if body.source == "livekit" and not body.group_id:
        meeting_room_id = await group_service.create_meeting_room(
            workspace_id, user_id, body.title.strip()
        )
        body = body.model_copy(update={"group_id": meeting_room_id})
    try:
        doc = await _create_meeting_row(workspace_id, user_id, body, meeting_room_id)
    except Exception:
        if meeting_room_id:
            await group_service.delete_meeting_room(meeting_room_id)
        raise

    # Schedule APScheduler jobs for reminder (5 min before) + auto-start.
    from pocketpaw_ee.cloud.meetings.scheduling.reminders import (
        schedule_meeting_jobs,
    )

    schedule_meeting_jobs(doc)

    await event_bus.emit(
        "meeting.scheduled",
        {
            "workspace_id": workspace_id,
            "meeting_id": str(doc.id),
            "source": body.source,
            "provider": body.provider,
            "group_id": body.group_id,
            "created_by": user_id,
            # The notification bridge words the toast from these, and sends
            # none for a LiveKit meeting with no date.
            "title": body.title,
            "scheduled_start": body.scheduled_start.isoformat() if body.scheduled_start else None,
        },
    )
    # Also emit on the realtime bus so ALL connected clients (not just the
    # creator) receive the event and update their sidebar.
    try:
        from pocketpaw_ee.cloud._core.realtime.emit import emit as _emit_realtime
        from pocketpaw_ee.cloud.meetings.events import MeetingScheduled

        await _emit_realtime(
            MeetingScheduled(
                data={
                    "workspace_id": workspace_id,
                    "meeting_id": str(doc.id),
                    "source": body.source,
                    "group_id": body.group_id,
                }
            )
        )
    except Exception:
        logger.exception("Failed to emit realtime meeting.scheduled for %s", doc.id)

    return _doc_to_response(doc, transcript_available=False)


async def _create_meeting_row(
    workspace_id: str,
    user_id: str,
    body: CreateMeetingRequest,
    meeting_room_id: str | None,
) -> _MeetingDoc:
    """Provider ``create`` + insert the row. LiveKit rows get a code and link.

    The provider does the source-specific work (adapter call for Recall; room
    check for LiveKit) and returns the provider_payload + join_url; persistence
    stays here, the same for every source.
    """
    from types import SimpleNamespace

    from pocketpaw_ee.cloud.meetings.providers import base as providers_base

    provider_impl = providers_base.resolve(body.source)
    ctx = SimpleNamespace(workspace_id=workspace_id, user_id=user_id)
    provider_result = await provider_impl.create(ctx, body)

    provider_payload = provider_result.provider_payload or {}
    # Persist duration_minutes + group_id so the response can include them.
    if body.duration_minutes:
        provider_payload["duration_minutes"] = body.duration_minutes
    if body.group_id:
        provider_payload["group_id"] = body.group_id

    provider_meeting_id = str(provider_payload.get("id") or provider_payload.get("name") or "")
    if body.source == "recall" and not provider_meeting_id:
        # External providers MUST return an id we can correlate webhooks
        # against; LiveKit meetings don't (room name lives in payload).
        raise ValidationError(
            "meeting.provider_no_id",
            f"{body.provider} did not return a meeting ID",
        )

    # Compute scheduled_end from scheduled_start + duration_minutes so the
    # frontend can display duration without an extra field on the Beanie doc.
    scheduled_end: datetime | None = None
    if body.scheduled_start and body.duration_minutes:
        try:
            scheduled_end = body.scheduled_start.replace(
                second=0,
                microsecond=0,
            ) + timedelta(minutes=body.duration_minutes)
        except (ValueError, OverflowError):
            scheduled_end = None

    fields = {
        "workspace": workspace_id,
        "source": body.source,
        "provider": body.provider,
        "provider_meeting_id": provider_meeting_id,
        "provider_space_id": provider_payload.get("space_name"),
        "title": body.title,
        "description": (body.description or "").strip() or None,
        "join_url": provider_result.join_url
        or str(provider_payload.get("join_url") or provider_payload.get("meetingUri") or ""),
        "organizer_email": provider_payload.get("host_email"),
        "scheduled_start": body.scheduled_start,
        "scheduled_end": scheduled_end,
        "status": "scheduled",
        "participants": [],
        "recording_file_ids": [],
        "raw_provider_payload": provider_payload,
        "created_by_user_id": user_id,
    }
    if body.source != "livekit":
        doc = _MeetingDoc(**fields)  # Recall meetings have no code
        await doc.insert()
        return doc

    fields["host_user_id"] = user_id
    fields["link_expires_at"] = scheduled_end or datetime.now(UTC) + LINK_TTL
    if meeting_room_id:
        fields["room_group_id"] = meeting_room_id
    doc = await _insert_with_code(fields)
    if meeting_room_id:
        # A chat-room meeting keeps its old empty join_url (the room's own invite
        # flow); a meeting-room meeting is joined through its link.
        doc.join_url = _meeting_link(doc.code or "")
        await doc.save()
    return doc


async def _insert_with_code(fields: dict) -> _MeetingDoc:
    """Insert a Meeting with a fresh code, retrying on a unique-index collision."""
    for attempt in range(_CODE_ATTEMPTS):
        doc = _MeetingDoc(**fields, code=_new_code())
        try:
            await doc.insert()
            return doc
        except DuplicateKeyError:
            if attempt == _CODE_ATTEMPTS - 1:
                raise
            logger.info("Meeting code collision, retrying")
    raise AssertionError("unreachable")


async def start_instant_meeting(
    workspace_id: str, user_id: str, body: StartInstantMeetingRequest
) -> MeetingResponse:
    """Start a meeting now: hidden room + Meeting row + live call.

    The room is a ``type="meeting"`` chat group with the host as its only
    member, so the room-keyed call pieces (call-bot, notes, recording, in-call
    chat) work unchanged. ``create_room`` runs with ``record_meeting=False`` so
    this row is the call's only Meeting row, and it applies the plan's daily
    call budget: if it refuses (``CallLimitError``) or fails, the room and the
    row are deleted and the error propagates, so nothing is left behind.
    """
    from pocketpaw_ee.cloud.chat import group_service
    from pocketpaw_ee.cloud.livekit import service as livekit_service

    body = StartInstantMeetingRequest.model_validate(body)
    title = (body.title or "").strip() or INSTANT_MEETING_TITLE
    description = (body.description or "").strip() or None

    room_id = await group_service.create_meeting_room(workspace_id, user_id, title)
    room_name = livekit_service.room_name_for_group(room_id)
    now = datetime.now(UTC)
    doc: _MeetingDoc | None = None
    try:
        doc = await _insert_with_code(
            {
                "workspace": workspace_id,
                "source": "livekit",
                "provider_meeting_id": room_name,
                "title": title,
                "description": description,
                "join_url": "",
                "scheduled_start": now,
                "actual_start": now,
                "status": "in_progress",
                "room_group_id": room_id,
                # Back-compat: older readers take the room from the payload.
                "raw_provider_payload": {"group_id": room_id, "room_name": room_name},
                "created_by_user_id": user_id,
                "host_user_id": user_id,
                "access": "ask",
                "link_expires_at": now + LINK_TTL,
            }
        )
        result = await livekit_service.create_room(
            room_id, workspace_id, user_id, record_meeting=False
        )
    except Exception:
        if doc is not None:
            await doc.delete()  # no-event: never announced
        await group_service.delete_meeting_room(room_id)
        raise

    doc.join_url = _meeting_link(doc.code or "")
    doc.call_budget_deadline = result.get("call_budget_deadline")
    await doc.save()

    # Realtime only, like create_room's own instant-call path: the audience is
    # the room's members (just the host). The bus ``meeting.started`` would
    # notify the host about the meeting they just started.
    try:
        from pocketpaw_ee.cloud._core.realtime.emit import emit as _emit_realtime
        from pocketpaw_ee.cloud.meetings.events import MeetingStarted

        await _emit_realtime(
            MeetingStarted(
                data={
                    "workspace_id": workspace_id,
                    "meeting_id": str(doc.id),
                    "source": "livekit",
                    "group_id": room_id,
                    "join_url": doc.join_url,
                }
            )
        )
    except Exception:
        logger.exception("Failed to emit realtime meeting.started for %s", doc.id)

    return _doc_to_response(doc)


# ---------------------------------------------------------------------------
# Meeting codes — public lookup + member join
# ---------------------------------------------------------------------------


async def _find_by_code(code: str) -> _MeetingDoc:
    """The meeting with this code (any dash / case form). NotFound otherwise."""
    canonical = _canonical_code(code)
    doc = None
    if canonical:
        # global-read: the code is the one cross-workspace key a link carries;
        # the lookup returns no tenant data and the join checks the workspace.
        doc = await _MeetingDoc.find_one({"code": canonical})
    if doc is None:
        raise NotFound("meeting", code)
    return doc


async def _room_is_live(room_id: str | None) -> bool:
    """True while someone other than the call-bot is in the room's call."""
    if not room_id:
        return False
    from pocketpaw_ee.cloud.livekit import service as livekit_service

    try:
        info = await livekit_service.get_room_info(room_id)
    except Exception:
        # LiveKit unset or unreachable: report "not started" rather than fail
        # the join page; the join itself goes through create_room and errors there.
        # Debug, not warning: this runs on a public endpoint, 30 times a minute per IP.
        logger.debug("Room presence check failed for %s", room_id, exc_info=True)
        return False
    return bool(info and info.get("active"))


async def _host_name(user_id: str | None) -> str | None:
    """The host's display name, never their email."""
    if not user_id:
        return None
    from beanie import PydanticObjectId

    from pocketpaw_ee.cloud.models.user import User as _UserDoc

    try:
        user = await _UserDoc.get(PydanticObjectId(user_id))
    except Exception:
        return None
    return (user.full_name or None) if user else None


async def lookup_meeting_by_code(code: str) -> MeetingLookupResponse:
    """Public view of a meeting for the ``/m/<code>`` page. Read-only.

    404 for a code nobody has. A known code whose meeting is over, cancelled or
    past its link expiry reports ``status="ended"``.
    """
    doc = await _find_by_code(code)
    if _is_closed(doc, datetime.now(UTC)):
        status = "ended"
    else:
        status = "live" if await _room_is_live(_room_of(doc)) else "not_started"
    return MeetingLookupResponse(
        code=display_code(doc.code or ""),
        title=doc.title,
        scheduled_start=doc.scheduled_start,
        host_name=await _host_name(doc.host_user_id or doc.created_by_user_id),
        access=doc.access,
        status=status,
    )


async def _run_meeting_call(
    doc: _MeetingDoc, room_id: str, workspace_id: str, user_id: str
) -> dict:
    """Make sure the meeting's call is running and its own row says so.

    410 ``meeting.ended`` when the meeting is closed. ``create_room`` runs with
    ``record_meeting=False`` (so no "Instant call" twin row) and applies the
    daily budget to a NEW room (402 ``billing.call_limit``). The row then takes
    the room name and budget deadline, goes ``in_progress`` (``actual_start``
    kept if already set), and an undated meeting's link moves 30 days out.
    ``meeting.started`` (realtime) goes to the room only when the room is new.
    Returns ``create_room``'s result.
    """
    from pocketpaw_ee.cloud.livekit import service as livekit_service

    now = datetime.now(UTC)
    if _is_closed(doc, now):
        raise CloudError(410, "meeting.ended", "This meeting has ended.")

    result = await livekit_service.create_room(room_id, workspace_id, user_id, record_meeting=False)

    call_started = bool(result.get("is_new"))
    if call_started or doc.status != "in_progress":
        doc.status = "in_progress"
        doc.actual_start = doc.actual_start or now
        # end_room and the budget watchdog find the call's row by room name.
        doc.provider_meeting_id = result["room_name"]
        if result.get("call_budget_deadline") is not None:
            doc.call_budget_deadline = result["call_budget_deadline"]
    if doc.scheduled_end is None:
        doc.link_expires_at = now + LINK_TTL
    # no-event unless the call started (below): nothing listens for a
    # link-expiry refresh.
    await doc.save()

    if call_started:
        # Same realtime-only event the instant path sends; audience is the room.
        try:
            from pocketpaw_ee.cloud._core.realtime.emit import emit as _emit_realtime
            from pocketpaw_ee.cloud.meetings.events import MeetingStarted

            await _emit_realtime(
                MeetingStarted(
                    data={
                        "workspace_id": workspace_id,
                        "meeting_id": str(doc.id),
                        "source": "livekit",
                        "group_id": room_id,
                        "join_url": doc.join_url,
                    }
                )
            )
        except Exception:
            logger.exception("Failed to emit realtime meeting.started for %s", doc.id)
    return result


async def start_meeting_room_call(workspace_id: str, user_id: str, group_id: str) -> dict | None:
    """``POST /livekit/rooms`` for a hidden meeting room; None for any other room.

    The call engine always calls ``POST /livekit/rooms`` when joining, including
    a host re-joining after LiveKit closed the empty room and the instant flow
    right after it started. For a meeting room that must not insert an
    "Instant call" row, so it runs the meeting's own row through
    ``_run_meeting_call`` like a by-code join. The caller has already passed
    ``require_call_group`` (member of the room, same workspace).
    """
    from pocketpaw_ee.cloud.chat import group_service
    from pocketpaw_ee.cloud.livekit import service as livekit_service

    if not await group_service.is_meeting_room(group_id):
        return None
    doc = await _MeetingDoc.find_one({"workspace": workspace_id, "room_group_id": group_id})
    if doc is None:
        # Every meeting room is made with its row; if one is missing, still
        # don't mint an "Instant call" twin for a hidden room.
        return await livekit_service.create_room(
            group_id, workspace_id, user_id, record_meeting=False
        )
    return await _run_meeting_call(doc, group_id, workspace_id, user_id)


async def join_meeting_by_code(
    workspace_id: str, user_id: str, code: str
) -> JoinMeetingByCodeResponse:
    """Let a member of the meeting's workspace into its call.

    Order matters: workspace (403 ``livekit.room_forbidden``, same as
    ``require_call_group``) before state (410 ``meeting.ended``), so an outsider
    learns nothing about the meeting. A hidden meeting room takes the caller as
    a member; a chat-room meeting needs them to be a member already, so a code
    never grants a private room or DM. ``create_room`` then makes sure the call
    runs (idempotent; the daily budget gates a NEW room, 402
    ``billing.call_limit``) and only after that is membership added, so a
    refusal grants nothing. The frontend takes ``room_group_id`` / ``room_name``
    to the usual token/join flow.
    """
    from pocketpaw_ee.cloud.chat import group_service
    from pocketpaw_ee.cloud.livekit import service as livekit_service
    from pocketpaw_ee.guards.audit import log_denial

    doc = await _find_by_code(code)
    room_id = _room_of(doc)
    if doc.workspace != workspace_id or not room_id:
        log_denial(
            actor=user_id,
            action="group.view",
            code="livekit.room_forbidden",
            resource_id=str(doc.id),
            workspace_id=workspace_id,
        )
        raise Forbidden("livekit.room_forbidden", "You don't have access to this call.")

    meeting_room = await group_service.is_meeting_room(room_id)
    if not meeting_room:
        await livekit_service.require_call_group(room_id, user_id, workspace_id)

    result = await _run_meeting_call(doc, room_id, workspace_id, user_id)
    if meeting_room:
        await group_service.add_meeting_room_member(room_id, user_id)

    return JoinMeetingByCodeResponse(room_group_id=room_id, room_name=result["room_name"])


async def _get_doc(workspace_id: str, meeting_id: str) -> _MeetingDoc:
    """The meeting in this workspace; NotFound for another workspace or a bad id."""
    from beanie import PydanticObjectId

    try:
        oid = PydanticObjectId(meeting_id)
    except Exception:
        raise NotFound("meeting", meeting_id) from None
    doc = await _MeetingDoc.find_one({"_id": oid, "workspace": workspace_id})
    if doc is None:
        raise NotFound("meeting", meeting_id)
    return doc


def _reschedule(doc: _MeetingDoc, schedule: dict) -> None:
    """Apply ``scheduled_start`` / ``duration_minutes`` to the row (not saved yet)."""
    if doc.source != "livekit" or doc.status != "scheduled" or doc.actual_start is not None:
        raise ConflictError(
            "meeting.not_reschedulable",
            "Only a meeting that hasn't started can be moved.",
        )
    payload = dict(doc.raw_provider_payload or {})
    duration = schedule.get("duration_minutes") or payload.get("duration_minutes", 30)
    payload["duration_minutes"] = duration
    doc.raw_provider_payload = payload
    if "scheduled_start" in schedule:
        # An offset-aware value is fine: save() hands back naive UTC, as on create.
        doc.scheduled_start = schedule["scheduled_start"]
    if doc.scheduled_start is not None:
        # Same end rule as create: start (seconds dropped) + duration.
        doc.scheduled_end = doc.scheduled_start.replace(second=0, microsecond=0) + timedelta(
            minutes=duration
        )
        doc.link_expires_at = doc.scheduled_end


async def update_meeting(
    workspace_id: str, user_id: str, meeting_id: str, body: UpdateMeetingRequest
) -> MeetingResponse:
    """PATCH /meetings/{id}: the host changes access, title, description or time.

    404 outside this workspace, 403 ``meeting.host_only`` for anyone but the host
    (``host_user_id``; the creator for rows that predate it), 422
    ``meeting.empty_title`` for a blank title, 409 ``meeting.not_reschedulable``
    when ``scheduled_start`` / ``duration_minutes`` is sent for a meeting that has
    started, ended, been cancelled, or isn't a LiveKit meeting (moving a Zoom/Meet
    row here wouldn't move the real meeting). Fields left out are unchanged.
    """
    from pocketpaw_ee.cloud.meetings.scheduling.reminders import (
        schedule_meeting_jobs,
        unschedule_meeting_jobs,
    )

    body = UpdateMeetingRequest.model_validate(body)
    doc = await _get_doc(workspace_id, meeting_id)
    if (doc.host_user_id or doc.created_by_user_id) != user_id:
        raise Forbidden("meeting.host_only", "Only the meeting's host can change it.")

    changes = body.model_dump(exclude_unset=True, exclude_none=True)
    schedule = {k: changes.pop(k) for k in ("scheduled_start", "duration_minutes") if k in changes}
    if "title" in changes:
        changes["title"] = changes["title"].strip()
        if not changes["title"]:
            raise ValidationError("meeting.empty_title", "title must not be empty or whitespace")
    if "description" in changes:
        changes["description"] = changes["description"].strip() or None
    if schedule:
        _reschedule(doc, schedule)
    for field, value in changes.items():
        setattr(doc, field, value)
    await doc.save()

    if schedule:
        # Remove first: re-adding only replaces jobs that are re-added, and a
        # reminder whose time has passed is not re-added.
        unschedule_meeting_jobs(str(doc.id))
        schedule_meeting_jobs(doc)

    fields = sorted([*changes, *schedule])
    from pocketpaw_ee.cloud._core.realtime.emit import emit as _emit_realtime
    from pocketpaw_ee.cloud._core.realtime.events import MeetingUpdated

    await _emit_realtime(
        MeetingUpdated(
            data={
                "workspace_id": workspace_id,
                "meeting_id": str(doc.id),
                "group_id": _room_of(doc),
                "fields": fields,
            }
        )
    )
    # The calendar bridge re-syncs the in-app event from this.
    await event_bus.emit(
        "meeting.edited",
        {"workspace_id": workspace_id, "meeting_id": str(doc.id), "fields": fields},
    )
    return _doc_to_response(doc)


async def get_joining_info(workspace_id: str, meeting_id: str) -> str:
    """Joining-info text for any member of the meeting's workspace.

    404 outside the workspace; 409 ``meeting.no_link`` for a meeting with no code
    (Zoom/Meet rows).
    """
    info = joining_info_for(await _get_doc(workspace_id, meeting_id))
    if info is None:
        raise ConflictError("meeting.no_link", "This meeting has no meeting link.")
    return info


async def get_meeting_ics(workspace_id: str, meeting_id: str) -> tuple[str, str]:
    """``(filename, text/calendar body)`` for one meeting; same access as joining info.

    409 ``meeting.no_link`` without a code, 409 ``meeting.not_scheduled`` for a
    meeting with no date.
    """
    from urllib.parse import urlparse

    doc = await _get_doc(workspace_id, meeting_id)
    info = joining_info_for(doc)
    if info is None or not doc.code:
        raise ConflictError("meeting.no_link", "This meeting has no meeting link.")
    if doc.scheduled_start is None:
        raise ConflictError("meeting.not_scheduled", "This meeting has no date yet.")
    start = doc.scheduled_start
    end = doc.scheduled_end or start + timedelta(minutes=30)
    host = urlparse(_frontend_base()).hostname or "pocketpaw"
    body = ics.build_ics(
        uid=f"{doc.id}@{host}",
        title=doc.title or "Meeting",
        start=start,
        end=end,
        link=_meeting_link(doc.code),
        description=info,
        now=datetime.now(UTC),
    )
    return ics.filename(doc.title), body


async def cancel_meeting(workspace_id: str, meeting_id: str, user_id: str = "") -> MeetingResponse:
    """Cancel a scheduled meeting via the provider, then mark the row cancelled.

    Only the meeting creator can cancel. Meet has no native cancel — the
    adapter marks it cancelled locally and the join URL keeps working
    (documented limitation). Zoom actually deletes the meeting on its side.
    """
    # Fetch the Beanie doc directly so we can check created_by_user_id.
    doc = await _MeetingDoc.find_one(
        _MeetingDoc.workspace == workspace_id,
        {"_id": meeting_id} if False else _MeetingDoc.id == meeting_id,
    )
    if doc is None:
        try:
            from beanie import PydanticObjectId

            doc = await _MeetingDoc.find_one(
                _MeetingDoc.workspace == workspace_id,
                _MeetingDoc.id == PydanticObjectId(meeting_id),
            )
        except Exception:
            doc = None
    if doc is None:
        raise NotFound("meeting", meeting_id)

    # Permission check: only the creator can cancel.
    if doc.created_by_user_id and doc.created_by_user_id != user_id:
        from pocketpaw_ee.cloud._core.errors import Forbidden

        raise Forbidden("meeting.not_owner", "Only the meeting creator can cancel")

    # Dispatch the provider-specific cancel through the registry. For
    # Recall this hits the Zoom/Meet adapter via RecallProvider.cancel();
    # LiveKit's cancel is a no-op (nothing reserved server-side).
    from types import SimpleNamespace

    from pocketpaw_ee.cloud.meetings.providers import base as providers_base

    source = getattr(doc, "source", "recall") or "recall"
    provider_impl = providers_base.resolve(source)
    ctx = SimpleNamespace(workspace_id=workspace_id, user_id=user_id)
    await provider_impl.cancel(ctx, doc)

    # Patch status (doc already loaded above).
    doc.status = "cancelled"
    await doc.save()

    # Remove APScheduler jobs so reminders / auto-start don't fire.
    from pocketpaw_ee.cloud.meetings.scheduling.reminders import (
        unschedule_meeting_jobs,
    )

    unschedule_meeting_jobs(meeting_id)

    await event_bus.emit(
        "meeting.cancelled",
        {
            "workspace_id": workspace_id,
            "meeting_id": str(doc.id),
            "provider": doc.provider,
        },
    )
    return _doc_to_response(doc, transcript_available=False)


# ---------------------------------------------------------------------------
# Cross-provider aggregation — used by the meetings meta-connector
# (src/pocketpaw/connectors/adapters/meetings_aggregator.py)
# ---------------------------------------------------------------------------


async def search_meetings(
    workspace_id: str,
    *,
    query: str,
    since=None,
    until=None,
    limit: int = 20,
) -> list[MeetingResponse]:
    """Cross-provider meeting search for the agent.

    Phase 1.7 implements the simple substring match over Meeting.title +
    Meeting.participants. The KB-backed transcript search lands when
    Phase 2 ships transcript indexing (a transcript file's content is
    already indexable via the existing KB pipeline).
    """
    if not query.strip():
        return []
    docs = (
        await _MeetingDoc.find(_MeetingDoc.workspace == workspace_id)
        .sort([("scheduled_start", -1), ("createdAt", -1)])
        .to_list()
    )
    q = query.lower()
    since, until = _aware(since), _aware(until)
    matches: list[_MeetingDoc] = []
    for d in docs:
        sd = _aware(d.scheduled_start)
        if since and sd and sd < since:
            continue
        if until and sd and sd > until:
            continue
        haystack_parts: list[str] = [d.title or "", d.organizer_email or ""]
        haystack_parts.extend(str(p.get("email", "")) for p in d.participants)
        haystack_parts.extend(str(p.get("name", "")) for p in d.participants)
        haystack = " ".join(haystack_parts).lower()
        if q in haystack:
            matches.append(d)
            if len(matches) >= limit:
                break

    if not matches:
        return []
    ids = [str(d.id) for d in matches]
    transcripts = await _TranscriptDoc.find(
        _TranscriptDoc.workspace == workspace_id,
        {
            "meeting_id": {"$in": ids},
            "file_id": {"$ne": None},
            "entry_count": {"$gt": 0},
        },
    ).to_list()
    have_transcript = {t.meeting_id for t in transcripts}
    return [_doc_to_response(d, transcript_available=str(d.id) in have_transcript) for d in matches]


async def list_recent_meetings(workspace_id: str, *, limit: int = 10) -> list[MeetingResponse]:
    """Return the most-recently scheduled meetings across all providers."""
    docs = (
        await _MeetingDoc.find(_MeetingDoc.workspace == workspace_id)
        .sort([("scheduled_start", -1), ("createdAt", -1)])
        .limit(max(1, min(limit, 100)))
        .to_list()
    )
    if not docs:
        return []
    ids = [str(d.id) for d in docs]
    transcripts = await _TranscriptDoc.find(
        _TranscriptDoc.workspace == workspace_id,
        {
            "meeting_id": {"$in": ids},
            "file_id": {"$ne": None},
            "entry_count": {"$gt": 0},
        },
    ).to_list()
    have = {t.meeting_id for t in transcripts}
    return [_doc_to_response(d, transcript_available=str(d.id) in have) for d in docs]


# ---------------------------------------------------------------------------
# Transcripts
# ---------------------------------------------------------------------------


async def get_transcript(workspace_id: str, meeting_id: str) -> TranscriptResponse:
    """Return the transcript for a meeting, fetching on-demand if needed.

    Resolution order:
      1. **Useful cached row** — ``file_id`` set AND ``entry_count > 0``
         → return immediately (fast path).
      2. **Empty/stale cached row** — ``entry_count == 0`` → ignore the
         cache and re-fetch. Auto-heals rows written by an earlier pass
         that ran before the bot had captured any audio.
      3. **No row at all** → fetch from Recall.ai / provider REST, store.
      4. **Nothing yet from any source** → raise ``NotFound``; caller retries.

    On-demand fetch complements the Recall.ai webhook (webhooks.py):
    whichever fires first wins, and both land here. Trade-off on the
    on-demand path: the first call pays ~5–30s latency; subsequent
    useful calls are instant.
    """
    # Tenant filter (§7).
    doc = await _TranscriptDoc.find_one(
        _TranscriptDoc.workspace == workspace_id,
        _TranscriptDoc.meeting_id == meeting_id,
    )
    if doc is not None and doc.file_id and doc.entry_count > 0:
        return _transcript_response(doc)
    if doc is not None and doc.entry_count == 0:
        logger.info(
            "cached transcript for meeting=%s has 0 entries — retrying fetch",
            meeting_id,
        )

    # Fetch on-demand. ``fetch_and_store_transcript`` updates the row
    # if it exists, or inserts a new one.
    fetched = await fetch_and_store_transcript(workspace_id, meeting_id)
    if fetched is None:
        raise NotFound("meeting_transcript", meeting_id)
    return _transcript_response(fetched)


def _transcript_response(doc: _TranscriptDoc) -> TranscriptResponse:
    return TranscriptResponse(
        meeting_id=doc.meeting_id,
        file_id=doc.file_id,
        entry_count=doc.entry_count,
        speaker_count=doc.speaker_count,
        language=doc.language,
        fetched_at=doc.fetched_at,
        indexed_in_kb=doc.indexed_in_kb,
    )


async def fetch_and_store_transcript(workspace_id: str, meeting_id: str) -> _TranscriptDoc | None:
    """Fetch a transcript, persist the blob + row.

    Resolution order:
      1. **Recall.ai bot recording** — if we dispatched a bot to this
         meeting, pull the captured transcript from Recall.ai.
      2. **Provider native REST fallback** — Zoom/Meet REST API, useful
         when the host enabled in-meeting transcription themselves and
         no bot was needed (or as a fallback when Recall.ai is down).

    Returns the ``MeetingTranscript`` doc on success, ``None`` when
    no transcript exists yet from either source. Caller should retry.
    Raises ``NotFound`` if the meeting itself doesn't exist.
    """
    from datetime import UTC, datetime

    from pocketpaw_ee.cloud.uploads.service import write_text_file

    meeting = await _MeetingDoc.find_one(
        _MeetingDoc.workspace == workspace_id,
        _MeetingDoc.id == meeting_id,
    )
    if meeting is None:
        try:
            from beanie import PydanticObjectId

            meeting = await _MeetingDoc.find_one(
                _MeetingDoc.workspace == workspace_id,
                _MeetingDoc.id == PydanticObjectId(meeting_id),
            )
        except Exception:
            meeting = None
    if meeting is None:
        raise NotFound("meeting", meeting_id)

    # Layer 1 — Recall.ai bot recording. Only attempt when we previously
    # dispatched a bot for this meeting (raw_provider_payload has the
    # ``recall`` correlation block).
    text = ""
    payload = meeting.raw_provider_payload or {}
    recall_block = payload.get("recall") or {}
    transcript_id = recall_block.get("transcript_id")
    if transcript_id:
        # Async path — transcription was kicked off post-recording; the
        # transcript lives at Recall's /transcript/{id} endpoint.
        try:
            from pocketpaw_ee.cloud.meetings.providers.recall import client as recall_client

            async_vtt = await recall_client.fetch_async_transcript_vtt(str(transcript_id))
            if async_vtt:
                text = async_vtt
                logger.info("transcript source=recall_async for meeting=%s", meeting_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("async transcript fetch failed for meeting=%s: %s", meeting_id, exc)
    if not text and recall_block:
        # Realtime path — the transcript lives on the bot recording.
        try:
            from pocketpaw_ee.cloud.meetings.providers.recall import client as recall_client

            recall_vtt = await recall_client.fetch_transcript_vtt(workspace_id, str(meeting.id))
            if recall_vtt:
                text = recall_vtt
                logger.info("transcript source=recall_bot for meeting=%s", meeting_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Recall transcript fetch failed for meeting=%s — falling back to provider: %s",
                meeting_id,
                exc,
            )

    # Layer 2 — Provider REST fallback (Zoom/Meet native API).
    if not text:
        adapter = await _adapter_factory(workspace_id, meeting.provider)
        result: ActionResult = await adapter.execute(
            "transcript_get", {"meeting_id": meeting.provider_meeting_id}
        )
        if not result.success:
            logger.warning(
                "transcript fetch failed for %s/%s: %s",
                meeting.provider,
                meeting.provider_meeting_id,
                result.error,
            )
            return None
        text = result.data or ""
        if text:
            logger.info("transcript source=provider_rest for meeting=%s", meeting_id)
    if not text:
        # Neither source has anything yet.
        return None

    # Refuse to write a transcript with no cues. A bare ``WEBVTT``
    # header or a file with just speaker tags is not a real transcript
    # and would pollute the KB. Better to return None and let the
    # caller retry later when audio capture actually worked.
    cue_count = text.count("\n--> ") + text.count(" --> ")
    if cue_count == 0:
        logger.info(
            "Refusing to store empty transcript for meeting=%s "
            "(text len=%d, no cues found). Bot probably wasn't admitted "
            "or no audio was captured.",
            meeting_id,
            len(text),
        )
        return None

    # Land the blob in the uploads pipeline. mime=text/plain so the
    # KB indexer treats it as searchable text; filename keeps the .vtt
    # extension so users browsing Files see what it is.
    safe_title = (meeting.title or "meeting").replace("/", "-")[:80]
    filename = f"{safe_title}-transcript.vtt"
    file_rec = await write_text_file(
        workspace_id=workspace_id,
        owner_id=meeting.created_by_user_id or "system",
        folder_path="/transcripts",
        filename=filename,
        content=text,
        # text/vtt routes to the .vtt branch in LocalExtractor, which
        # strips the WEBVTT header, timestamp lines, and <v Speaker> cue
        # tags before KB ingest. Lying about the mime as text/plain dumps
        # raw VTT (mostly noise) into the workspace KB.
        mime="text/vtt",
    )

    # Derive cue + speaker counts from the freshly-written VTT so both
    # upsert branches stay in lockstep with the empty-guard above (which
    # uses the same "\n--> " or " --> " heuristic).
    new_entry_count = text.count("\n--> ") + text.count(" --> ")
    new_speaker_count = len({m.group(1) for m in _SPEAKER_RE.finditer(text)})

    # Upsert the MeetingTranscript row. The else-branch USED TO refresh
    # only file_id + fetched_at, leaving entry_count/speaker_count frozen
    # at whatever an earlier (possibly empty) pass wrote. That left rows
    # stuck at entry_count=0 forever — the meeting flipped to
    # transcript_ready, the card lit up, but the open-gate (entry_count>0)
    # kept saying "no transcript". Now both branches write all derived
    # fields so a successful refetch heals the row.
    transcript = await _TranscriptDoc.find_one(
        _TranscriptDoc.workspace == workspace_id,
        _TranscriptDoc.meeting_id == str(meeting.id),
    )
    if transcript is None:
        transcript = _TranscriptDoc(
            workspace=workspace_id,
            meeting_id=str(meeting.id),
            provider_transcript_id=meeting.provider_meeting_id,
            file_id=file_rec.id,
            entry_count=new_entry_count,
            speaker_count=new_speaker_count,
            language=None,
            fetched_at=datetime.now(UTC),
            indexed_in_kb=False,
            kb_indexed_version=TRANSCRIPT_KB_VERSION,
        )
        await transcript.insert()
    else:
        transcript.file_id = file_rec.id
        transcript.entry_count = new_entry_count
        transcript.speaker_count = new_speaker_count
        transcript.fetched_at = datetime.now(UTC)
        transcript.kb_indexed_version = TRANSCRIPT_KB_VERSION
        await transcript.save()

    # Flip the meeting to ``transcript_ready`` so the desktop client
    # can refresh badges off this without re-fetching the transcript.
    # Guard on the cue count so an empty fetch can't promote a meeting
    # whose detail panel will then say "no transcript".
    if new_entry_count > 0 and meeting.status != "transcript_ready":
        meeting.status = "transcript_ready"
        await meeting.save()

    await event_bus.emit(
        "meeting.transcript_ready",
        {
            "workspace_id": workspace_id,
            "meeting_id": str(meeting.id),
            "file_id": file_rec.id,
        },
    )
    return transcript


async def reindex_outdated_transcripts() -> dict:
    """Re-emit FileReady for every transcript indexed under an older KB pipeline.

    Idempotent. Walks all rows where ``kb_indexed_version <
    TRANSCRIPT_KB_VERSION`` and ``file_id`` is set, re-emits FileReady
    against the existing file (no Recall re-fetch), then bumps the
    version. The FileReady listener re-runs the extraction chain — which
    now routes ``text/vtt`` through ``_vtt_to_plain`` — and ingests the
    cleaned text into the same workspace KB scope.

    **Old noisy articles are NOT deleted.** kb-go (as of v0.1.0) has no
    delete-by-source or per-article rm primitive — only ``kb clear``,
    which would nuke the whole workspace scope. We accept the duplicates
    and rely on rank dominance: the cleaner article scores higher on
    real queries (no timestamp tokens, real speech vocabulary), so the
    noisy one rarely surfaces. If kb-go ever gains ``kb rm --source X``,
    add a pre-emit delete call here keyed off the file's filename.

    Called fire-and-forget from ``dashboard_lifecycle.startup_event`` so
    the dashboard never blocks on it. Safe to call repeatedly: the
    version gate makes the second call a no-op.

    Returns ``{"scanned": N, "republished": M, "skipped": K}`` so the
    boot log can show what happened without sampling Mongo by hand.
    """
    from pocketpaw_ee.cloud._core.realtime.bus import get_bus
    from pocketpaw_ee.cloud._core.realtime.emit import emit
    from pocketpaw_ee.cloud._core.realtime.events import FileReady
    from pocketpaw_ee.cloud.uploads.models import FileUpload as _FileDoc

    try:
        get_bus()
    except Exception as exc:  # noqa: BLE001
        logger.warning("transcript reindex skipped: bus not ready: %s", exc)
        return {"scanned": 0, "republished": 0, "skipped": 0}

    # global-read: deployment-wide migration, cross-tenant by design.
    docs = await _TranscriptDoc.find(
        {
            "kb_indexed_version": {"$lt": TRANSCRIPT_KB_VERSION},
            "file_id": {"$ne": None},
        }
    ).to_list()
    if not docs:
        return {"scanned": 0, "republished": 0, "skipped": 0}

    republished = 0
    skipped = 0
    for doc in docs:
        if not doc.file_id:
            skipped += 1
            continue
        # global-read: FileUpload is cross-workspace; we trust doc.workspace.
        file_doc = await _FileDoc.find_one(_FileDoc.file_id == doc.file_id)
        if file_doc is None:
            logger.info(
                "transcript reindex: file %s missing for meeting=%s — skipping",
                doc.file_id,
                doc.meeting_id,
            )
            skipped += 1
            continue
        try:
            await emit(
                FileReady(
                    data={
                        "workspace_id": doc.workspace,
                        "file_id": file_doc.file_id,
                        "filename": file_doc.filename,
                        # Force text/vtt so the extractor's mime-first
                        # routing hits _vtt_to_plain even when the row
                        # was originally written as text/plain.
                        "mime": "text/vtt",
                        "size": file_doc.size,
                        "storage_key": file_doc.storage_key,
                        "url": f"/api/v1/uploads/{file_doc.file_id}",
                    },
                ),
            )
        except Exception:
            logger.exception(
                "transcript reindex emit failed for meeting=%s file=%s",
                doc.meeting_id,
                doc.file_id,
            )
            skipped += 1
            continue
        doc.kb_indexed_version = TRANSCRIPT_KB_VERSION
        await doc.save()
        republished += 1

    logger.info(
        "transcript reindex: scanned=%d republished=%d skipped=%d (target version=%d)",
        len(docs),
        republished,
        skipped,
        TRANSCRIPT_KB_VERSION,
    )
    return {"scanned": len(docs), "republished": republished, "skipped": skipped}


async def ingest_transcript_for_recall_bot(bot_id: str) -> bool:
    """Webhook entry point — fetch + store the transcript for a Recall bot.

    The inbound Recall.ai webhook (``webhooks.py``) has no workspace
    context, so this resolves the meeting from the correlated ``bot_id``
    and derives the workspace from the matched row. Idempotent: safe for
    Recall to retry. Returns ``True`` when a transcript was stored,
    ``False`` when the bot is unknown or the transcript isn't ready yet.
    """
    if not bot_id:
        return False
    # global-read: inbound webhook is cross-tenant; the workspace is
    # derived from the matched meeting row, then enforced downstream.
    doc = await _MeetingDoc.find_one({"raw_provider_payload.recall.bot_id": bot_id})
    if doc is None:
        logger.info("Recall webhook for unknown bot=%s — ignoring", bot_id)
        return False
    result = await fetch_and_store_transcript(doc.workspace, str(doc.id))
    return result is not None


async def start_async_transcript(bot_id: str, recording_id: str) -> bool:
    """Webhook entry — kick off async transcription for a finished recording.

    Fired by the Recall ``recording.done`` webhook. A no-op unless the
    deployment is in async transcription mode. Resolves the meeting from
    the correlated ``bot_id``, calls Recall's ``create_transcript``, and
    stores the returned transcript id on the meeting so ``transcript.done``
    (and the on-demand path) can fetch it. Returns ``True`` when started.
    """
    if not bot_id or not recording_id:
        return False
    from pocketpaw_ee.cloud.meetings.providers.recall import client as recall_client
    from pocketpaw_ee.cloud.meetings.providers.recall import settings as meetings_settings

    resolved = await meetings_settings.resolve()
    if not meetings_settings.is_async_provider(resolved["provider"]):
        # Realtime mode — the transcript is produced on the bot itself,
        # not via create_transcript. recording.done is not our trigger.
        return False

    # global-read: inbound webhook is cross-tenant; resolved via bot_id.
    doc = await _MeetingDoc.find_one({"raw_provider_payload.recall.bot_id": bot_id})
    if doc is None:
        logger.info("Recall recording.done for unknown bot=%s — ignoring", bot_id)
        return False

    transcript_id = await recall_client.create_async_transcript(recording_id)
    recall_block = dict((doc.raw_provider_payload or {}).get("recall") or {})
    recall_block.update({"recording_id": recording_id, "transcript_id": transcript_id})
    doc.raw_provider_payload = {**(doc.raw_provider_payload or {}), "recall": recall_block}
    # no-event: provider-side correlation ids; the transcript itself emits later.
    await doc.save()
    logger.info(
        "started async transcript meeting=%s recording=%s transcript=%s",
        doc.id,
        recording_id,
        transcript_id,
    )
    return True


# ---------------------------------------------------------------------------
# Bot status tracking
# ---------------------------------------------------------------------------

_BOT_STATUS_SUMMARY = {
    "joining_call": "The bot is connecting to the meeting.",
    "in_waiting_room": (
        "The bot is in the lobby — someone in the meeting must admit "
        "'PocketPaw Bot' to let it into the call."
    ),
    "in_call_not_recording": "The bot is in the meeting but not recording yet.",
    "in_call_recording": "The bot is in the meeting and recording.",
    "recording_permission_allowed": "The bot is in the meeting and recording.",
    "recording_permission_denied": "The host denied the bot recording permission.",
    "call_ended": "The bot has left the meeting.",
    "done": "The bot has finished and left the meeting.",
    "fatal": "The bot hit a fatal error and could not join the meeting.",
}


def _bot_status_summary(status: str | None, sub_code: str | None) -> str:
    """Human-readable one-liner for a Recall bot status — surfaced to the agent."""
    if not status:
        return "No bot has been dispatched to this meeting yet."
    base = _BOT_STATUS_SUMMARY.get(status, f"Bot status: {status}.")
    return f"{base} ({sub_code})" if sub_code else base


async def _resolve_meeting_doc(workspace_id: str, meeting_id: str) -> _MeetingDoc:
    """Load a workspace-scoped Meeting doc, tolerating str or ObjectId ids."""
    doc = await _MeetingDoc.find_one(
        _MeetingDoc.workspace == workspace_id, _MeetingDoc.id == meeting_id
    )
    if doc is None:
        try:
            from beanie import PydanticObjectId

            doc = await _MeetingDoc.find_one(
                _MeetingDoc.workspace == workspace_id,
                _MeetingDoc.id == PydanticObjectId(meeting_id),
            )
        except Exception:
            doc = None
    if doc is None:
        raise NotFound("meeting", meeting_id)
    return doc


async def update_bot_status_for_recall_bot(
    bot_id: str, status: str, sub_code: str | None = None
) -> bool:
    """Webhook entry — persist a Recall ``bot.status_change`` onto the meeting.

    Cross-tenant lookup by the correlated ``bot_id`` (the webhook carries
    no workspace context). Returns ``True`` if a meeting matched.
    """
    if not bot_id or not status:
        return False
    from datetime import UTC, datetime

    # global-read: inbound webhook is cross-tenant; resolved via bot_id.
    doc = await _MeetingDoc.find_one({"raw_provider_payload.recall.bot_id": bot_id})
    if doc is None:
        logger.info("Recall status webhook for unknown bot=%s — ignoring", bot_id)
        return False
    doc.bot_status = status
    doc.bot_status_detail = sub_code
    doc.bot_status_at = datetime.now(UTC)
    await doc.save()
    await event_bus.emit(
        "meeting.bot_status",
        {
            "workspace_id": doc.workspace,
            "meeting_id": str(doc.id),
            "bot_status": status,
        },
    )
    return True


async def get_bot_status(workspace_id: str, meeting_id: str) -> dict:
    """Return the current Recall bot status for a meeting.

    Does one live Recall lookup and refreshes the persisted ``bot_status``
    on the meeting row — so the field self-heals in setups where the
    ``bot.status_change`` webhook isn't reachable (e.g. local dev).
    Raises ``NotFound`` if the meeting is unknown to the workspace.
    """
    from pocketpaw_ee.cloud.meetings.providers.recall import client as recall_client

    doc = await _resolve_meeting_doc(workspace_id, meeting_id)
    bot_id = str((doc.raw_provider_payload or {}).get("recall", {}).get("bot_id") or "")
    if not bot_id:
        return {
            "meeting_id": str(doc.id),
            "has_bot": False,
            "bot_id": None,
            "status": None,
            "status_detail": None,
            "status_at": None,
            "summary": _bot_status_summary(None, None),
        }

    try:
        live = await recall_client.get_bot_status(bot_id)
    except Exception as exc:  # noqa: BLE001 — fall back to the persisted value
        logger.warning("live bot status fetch failed for meeting=%s: %s", meeting_id, exc)
        live = None

    if live is not None:
        from datetime import UTC, datetime

        doc.bot_status = live["status"]
        doc.bot_status_detail = live.get("sub_code")
        doc.bot_status_at = datetime.now(UTC)
        # no-event: read-through refresh of a cached field, not a domain mutation.
        await doc.save()

    return {
        "meeting_id": str(doc.id),
        "has_bot": True,
        "bot_id": bot_id,
        "status": doc.bot_status,
        "status_detail": doc.bot_status_detail,
        "status_at": doc.bot_status_at,
        "summary": _bot_status_summary(doc.bot_status, doc.bot_status_detail),
    }


# Tiny helper for cheap speaker-counting in VTT.
import re as _re  # noqa: E402

_SPEAKER_RE = _re.compile(r"<v\s+([^>]+)>")


# ---------------------------------------------------------------------------
# Internal helpers used by listeners (Phase 2 — webhook ingestion path)
# ---------------------------------------------------------------------------


async def upsert_meeting_from_provider(
    workspace_id: str,
    *,
    provider: str,
    provider_meeting_id: str,
    patch: dict,
) -> MeetingDomain:
    """Idempotent upsert used by webhook listeners + adapter callbacks.

    Looks up by ``(provider, provider_meeting_id)`` (the unique index)
    and applies ``patch``. Emits ``meeting.scheduled`` on insert,
    ``meeting.updated`` on existing row update.

    Stubbed minimally for Phase 1.3 — fully exercised by Phase 1.5 +
    Phase 2.1 webhook handlers.
    """
    doc = await _MeetingDoc.find_one(
        _MeetingDoc.provider == provider,
        _MeetingDoc.provider_meeting_id == provider_meeting_id,
    )
    is_new = doc is None
    if doc is None:
        doc = _MeetingDoc(
            workspace=workspace_id,
            provider=provider,
            provider_meeting_id=provider_meeting_id,
            join_url=patch.get("join_url", ""),
            status=patch.get("status", "scheduled"),
        )
    # Apply known fields conservatively — never overwrite workspace.
    for field in (
        "title",
        "join_url",
        "organizer_email",
        "scheduled_start",
        "scheduled_end",
        "actual_start",
        "actual_end",
        "status",
        "participants",
        "recording_file_ids",
        "raw_provider_payload",
        "provider_space_id",
    ):
        if field in patch:
            setattr(doc, field, patch[field])
    if is_new:
        await doc.insert()
        await event_bus.emit(
            "meeting.scheduled",
            {"workspace_id": workspace_id, "meeting_id": str(doc.id), "provider": provider},
        )
    else:
        await doc.save()
        await event_bus.emit(
            "meeting.updated",
            {"workspace_id": workspace_id, "meeting_id": str(doc.id), "provider": provider},
        )
    return _doc_to_domain(doc)
