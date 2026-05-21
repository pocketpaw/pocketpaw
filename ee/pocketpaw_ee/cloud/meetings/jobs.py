# Meetings — scheduled jobs.
# Created: 2026-05-19. Phase 2 ships a single batch job:
#
#   run_transcript_sync_pass(...) — walks recently-ended meetings whose
#   transcripts haven't been fetched yet, calls the provider, persists
#   the blob, indexes into KB. Idempotent and cheap; safe to run on a
#   nightly cron AND on first-of-day startup.
#
# Recall.ai pushes `transcript.done` to our webhook (meetings/webhooks.py),
# so in the common case transcripts land without this batch running. The
# batch is the safety net: it catches webhooks missed during a deploy,
# meetings transcribed by the provider with no bot dispatched, and keeps
# cross-meeting search ('what did we discuss with Acme last week?')
# hitting cached/indexed transcripts instead of fanning out per-meeting
# fetches on every query.

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from pocketpaw_ee.cloud.meetings import service as meetings_service
from pocketpaw_ee.cloud.models.meeting import Meeting as _MeetingDoc

logger = logging.getLogger(__name__)


# Meet deletes transcript entries 30 days after the conference ends.
# Cap the lookback so we never try to fetch something the provider
# already purged — saves a guaranteed-failed API call per stale meeting.
_DEFAULT_LOOKBACK_DAYS = 7
_RETENTION_FLOOR_DAYS = 28  # Stay safely under Meet's 30-day window.


@dataclass(frozen=True)
class TranscriptSyncReport:
    """Per-workspace pass summary — surfaced in logs / admin dashboard."""

    workspace_id: str
    candidates: int
    fetched: int
    not_ready: int
    failed: int


async def run_transcript_sync_pass(
    *,
    lookback_days: int = _DEFAULT_LOOKBACK_DAYS,
    workspace_ids: list[str] | None = None,
) -> list[TranscriptSyncReport]:
    """One pass of the transcript-sync job.

    For each workspace with at least one enabled meetings provider, walk
    meetings that ended in the lookback window and don't have a stored
    transcript yet. Call the provider for each; on success the blob
    lands via ``EEUploadService`` and the KB indexer ingests it via the
    existing ``FileReady`` event.

    Bounded by ``min(lookback_days, _RETENTION_FLOOR_DAYS)`` to avoid
    fetching meetings whose provider-side transcripts have expired.

    Returns a per-workspace report so the caller (cron logs, dashboard,
    alerting) can see what moved.
    """
    effective_lookback = min(max(lookback_days, 1), _RETENTION_FLOOR_DAYS)
    cutoff = datetime.now(UTC) - timedelta(days=effective_lookback)

    if workspace_ids is None:
        # Single-account env creds — any workspace can hold meetings.
        # Discover the active set from the Meeting collection itself.
        meetings = await _MeetingDoc.find_all().to_list()
        workspace_ids = sorted({m.workspace for m in meetings})

    reports: list[TranscriptSyncReport] = []
    for ws_id in workspace_ids:
        report = await _sync_workspace(ws_id, cutoff=cutoff)
        reports.append(report)
        if report.fetched or report.failed:
            logger.info(
                "transcript sync ws=%s candidates=%d fetched=%d not_ready=%d failed=%d",
                ws_id,
                report.candidates,
                report.fetched,
                report.not_ready,
                report.failed,
            )
    return reports


async def _sync_workspace(workspace_id: str, *, cutoff: datetime) -> TranscriptSyncReport:
    """Per-workspace pass — finds candidates and fetches each in sequence.

    Sequential by design: provider APIs rate-limit per app, and the
    polling cadence is generous enough (default daily) that fan-out
    isn't worth the rate-limit risk. If a workspace has hundreds of
    meetings in a single pass, the job runs longer; that's fine.
    """
    candidates = await _MeetingDoc.find(
        _MeetingDoc.workspace == workspace_id,
        {
            "status": {"$in": ["ended", "scheduled"]},
            "$or": [
                {"actual_end": {"$gte": cutoff}},
                {"scheduled_start": {"$gte": cutoff}},
            ],
        },
    ).to_list()

    fetched = 0
    not_ready = 0
    failed = 0
    for meeting in candidates:
        try:
            result = await meetings_service.fetch_and_store_transcript(
                workspace_id, str(meeting.id)
            )
            if result is None:
                not_ready += 1
            else:
                fetched += 1
        except Exception:  # noqa: BLE001
            logger.warning("transcript sync failed for meeting=%s", meeting.id, exc_info=True)
            failed += 1

    return TranscriptSyncReport(
        workspace_id=workspace_id,
        candidates=len(candidates),
        fetched=fetched,
        not_ready=not_ready,
        failed=failed,
    )
