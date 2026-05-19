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

from __future__ import annotations

import logging

from ee.cloud._core.errors import NotFound, ValidationError
from ee.cloud.meetings.domain import Meeting as MeetingDomain
from ee.cloud.meetings.dto import (
    CreateMeetingRequest,
    ListMeetingsRequest,
    MeetingDetailResponse,
    MeetingResponse,
    TranscriptResponse,
)
from ee.cloud.models.meeting import Meeting as _MeetingDoc
from ee.cloud.models.meeting import MeetingProviderCredentials as _CredsDoc
from ee.cloud.models.meeting import MeetingTranscript as _TranscriptDoc
from ee.cloud.shared.events import event_bus
from pocketpaw.clients.token_store import TokenStore
from pocketpaw.connectors.protocol import ActionResult

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Mapping helpers (rule §8 — same-file private helpers, not separate module)
# ---------------------------------------------------------------------------


def _doc_to_response(doc: _MeetingDoc, *, transcript_available: bool = False) -> MeetingResponse:
    return MeetingResponse(
        id=str(doc.id),
        provider=doc.provider,
        provider_meeting_id=doc.provider_meeting_id,
        title=doc.title,
        join_url=doc.join_url,
        organizer_email=doc.organizer_email,
        scheduled_start=doc.scheduled_start,
        scheduled_end=doc.scheduled_end,
        actual_start=doc.actual_start,
        actual_end=doc.actual_end,
        status=doc.status,
        participants=list(doc.participants),
        recording_file_ids=list(doc.recording_file_ids),
        transcript_available=transcript_available,
        created_at=doc.createdAt,
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


# ---------------------------------------------------------------------------
# Public API — meetings
# ---------------------------------------------------------------------------


async def list_meetings(workspace_id: str, body: ListMeetingsRequest) -> list[MeetingResponse]:
    """List meetings for this workspace, newest scheduled first.

    Read-only. Tenant filter on the Beanie query (rule §7). Returns a
    plain list — pagination via cursor lands in Phase 1.5 once we have
    real data volumes to size against.
    """
    body = ListMeetingsRequest.model_validate(body)
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
        docs = [d for d in docs if d.scheduled_start and d.scheduled_start >= body.since]
    if body.until:
        docs = [d for d in docs if d.scheduled_start and d.scheduled_start <= body.until]

    if not docs:
        return []

    # Bulk lookup: which meeting_ids have a transcript file?
    meeting_ids = [str(d.id) for d in docs]
    transcripts = await _TranscriptDoc.find(
        _TranscriptDoc.workspace == workspace_id,
        {"meeting_id": {"$in": meeting_ids}, "file_id": {"$ne": None}},
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
    has_file = transcript_doc is not None and transcript_doc.file_id is not None
    return _doc_to_detail(doc, transcript_available=has_file)


# ---------------------------------------------------------------------------
# Adapter factory — constructs a per-workspace ConnectorProtocol instance.
# Tests replace this via ``_set_adapter_factory`` to inject fakes without
# touching the token store / Zoom REST surface.
# ---------------------------------------------------------------------------


async def _build_adapter_default(workspace_id: str, provider: str):
    """Default factory: read creds from token blob, return native adapter.

    Raises ``NotFound`` when the workspace has not configured the
    provider, ``ValidationError`` when the on-disk token blob is missing
    or malformed (which means setup partially failed and the admin must
    re-paste).
    """
    creds_doc = await _CredsDoc.find_one(
        _CredsDoc.workspace == workspace_id,
        _CredsDoc.provider == provider,
    )
    if creds_doc is None or not creds_doc.enabled:
        raise NotFound("meeting_credentials", provider)

    service_name = f"workspace-{workspace_id}-{provider}"
    tokens = TokenStore().load(service_name)
    if tokens is None or not tokens.extra.get("client_id"):
        raise ValidationError(
            "meeting.credentials_incomplete",
            f"Token blob for {provider} is missing — re-run Settings → Integrations → Meetings.",
        )

    if provider == "zoom":
        from pocketpaw.connectors.adapters.zoom import ZoomConnector

        return ZoomConnector(
            service_name=service_name,
            client_id=tokens.extra["client_id"],
            client_secret=tokens.extra["client_secret"],
        )
    if provider == "google_meet":
        from pocketpaw.connectors.adapters.google_meet import GoogleMeetConnector

        return GoogleMeetConnector(
            service_name=service_name,
            client_id=tokens.extra["client_id"],
            client_secret=tokens.extra["client_secret"],
        )
    raise ValidationError("meeting.unknown_provider", f"Unsupported meetings provider: {provider}")


_adapter_factory = _build_adapter_default


def _set_adapter_factory(fn):
    """Test-only seam: swap the adapter factory globally.

    Pattern matches how ``ee.cloud.kb.knowledge_router`` exposes its
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
      4. Persist a ``Meeting`` row with provider-returned IDs + join URL.
      5. Emit ``meeting.scheduled``.
    """
    body = CreateMeetingRequest.model_validate(body)
    if not body.title.strip():
        raise ValidationError("meeting.empty_title", "title must not be empty or whitespace")

    adapter = await _adapter_factory(workspace_id, body.provider)

    params: dict = {
        "topic": body.title,
        "duration_minutes": body.duration_minutes,
    }
    if body.scheduled_start is not None:
        # Adapter expects an ISO string; both providers accept the "Z" form.
        params["start_time"] = body.scheduled_start.strftime("%Y-%m-%dT%H:%M:%SZ")

    result: ActionResult = await adapter.execute("meeting_create", params)
    if not result.success:
        raise ValidationError(
            "meeting.provider_error",
            result.error or f"{body.provider} rejected the create request",
        )

    provider_payload = result.data or {}
    provider_meeting_id = str(provider_payload.get("id") or provider_payload.get("name") or "")
    if not provider_meeting_id:
        raise ValidationError(
            "meeting.provider_no_id",
            f"{body.provider} did not return a meeting ID",
        )

    doc = _MeetingDoc(
        workspace=workspace_id,
        provider=body.provider,
        provider_meeting_id=provider_meeting_id,
        provider_space_id=provider_payload.get("space_name"),
        title=body.title,
        join_url=str(provider_payload.get("join_url") or provider_payload.get("meetingUri") or ""),
        organizer_email=provider_payload.get("host_email"),
        scheduled_start=body.scheduled_start,
        scheduled_end=None,
        status="scheduled",
        participants=[],
        recording_file_ids=[],
        raw_provider_payload=provider_payload,
        created_by_user_id=user_id,
    )
    await doc.insert()

    await event_bus.emit(
        "meeting.scheduled",
        {
            "workspace_id": workspace_id,
            "meeting_id": str(doc.id),
            "provider": body.provider,
            "created_by": user_id,
        },
    )
    return _doc_to_response(doc, transcript_available=False)


async def cancel_meeting(workspace_id: str, meeting_id: str) -> MeetingResponse:
    """Cancel a scheduled meeting via the provider, then mark the row cancelled.

    Meet has no native cancel — the adapter marks it cancelled locally
    and the join URL keeps working (documented limitation). Zoom
    actually deletes the meeting on its side.
    """
    detail = await get_meeting(workspace_id, meeting_id)
    adapter = await _adapter_factory(workspace_id, detail.provider)

    result: ActionResult = await adapter.execute(
        "meeting_cancel", {"meeting_id": detail.provider_meeting_id}
    )
    if not result.success:
        raise ValidationError(
            "meeting.provider_error",
            result.error or f"{detail.provider} rejected the cancel request",
        )

    # Fetch the Beanie doc (get_meeting returned a DTO) and patch status.
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
        # Mid-flight delete — extremely unlikely, but raise so the
        # client refetches instead of seeing stale data.
        raise NotFound("meeting", meeting_id)
    doc.status = "cancelled"
    await doc.save()

    await event_bus.emit(
        "meeting.cancelled",
        {
            "workspace_id": workspace_id,
            "meeting_id": str(doc.id),
            "provider": detail.provider,
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
    matches: list[_MeetingDoc] = []
    for d in docs:
        if since and d.scheduled_start and d.scheduled_start < since:
            continue
        if until and d.scheduled_start and d.scheduled_start > until:
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
        {"meeting_id": {"$in": ids}, "file_id": {"$ne": None}},
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
        {"meeting_id": {"$in": ids}, "file_id": {"$ne": None}},
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
         cache and re-fetch. Auto-heals rows written by earlier buggy
         code paths (e.g. before we corrected Vexa's transcript endpoint).
      3. **No row at all** → fetch from Vexa / provider REST, store.
      4. **Nothing yet from any source** → raise ``NotFound``; caller retries.

    On-demand fetch replaces the webhook ingestion path. Trade-off:
    first call pays ~5–30s latency; subsequent useful calls are instant.
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
      1. **Vexa bot recording** — if we sent a bot to this meeting, ask
         Vexa's ``/recordings`` for the captured transcript.
      2. **Provider native REST fallback** — Zoom/Meet REST API, useful
         when the host enabled in-meeting transcription themselves and
         no bot was needed (or as a fallback when Vexa is down).

    Returns the ``MeetingTranscript`` doc on success, ``None`` when
    no transcript exists yet from either source. Caller should retry.
    Raises ``NotFound`` if the meeting itself doesn't exist.
    """
    from datetime import UTC, datetime

    from ee.cloud.uploads.service import write_text_file

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

    # Layer 1 — Vexa bot recording. Only attempt when we previously
    # requested a bot for this meeting (raw_provider_payload has the
    # ``vexa`` correlation block).
    text = ""
    payload = meeting.raw_provider_payload or {}
    if payload.get("vexa"):
        try:
            from ee.cloud.meetings import bot_coordinator

            vexa_vtt = await bot_coordinator.fetch_transcript_vtt(workspace_id, str(meeting.id))
            if vexa_vtt:
                text = vexa_vtt
                logger.info("transcript source=vexa_bot for meeting=%s", meeting_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Vexa transcript fetch failed for meeting=%s — falling back to provider: %s",
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
        mime="text/plain",
    )

    # Upsert the MeetingTranscript row.
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
            entry_count=text.count("\n--> "),  # cheap VTT-cue count
            speaker_count=len({m.group(1) for m in _SPEAKER_RE.finditer(text)}),
            language=None,
            fetched_at=datetime.now(UTC),
            indexed_in_kb=False,
        )
        await transcript.insert()
    else:
        transcript.file_id = file_rec.id
        transcript.fetched_at = datetime.now(UTC)
        await transcript.save()

    # Flip the meeting to ``transcript_ready`` so the desktop client
    # can refresh badges off this without re-fetching the transcript.
    if meeting.status != "transcript_ready":
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
