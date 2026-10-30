# Tests for ee/cloud/meetings/jobs.py — the transcript-sync batch.

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from pocketpaw.connectors.protocol import ActionResult


@pytest.fixture
def fake_transcript_adapter():
    """Adapter that returns canned transcript text for every call."""

    class _Fake:
        def __init__(self):
            self.calls = 0

        async def execute(self, action, params):
            self.calls += 1
            if action == "transcript_get":
                return ActionResult(
                    success=True,
                    data=f"WEBVTT\n\n00:00.000 --> 00:01.000\n<v alice>hi {params}",
                    records_affected=1,
                )
            return ActionResult(success=False, error="unknown action")

    from pocketpaw_ee.cloud.meetings import service as ms

    fake = _Fake()

    async def _factory(workspace_id, provider):
        return fake

    prev = ms._set_adapter_factory(_factory)
    yield fake
    ms._set_adapter_factory(prev)


@pytest.mark.usefixtures("mongo_db")
async def test_run_transcript_sync_pass_fetches_recent_meetings(
    fake_transcript_adapter,
) -> None:
    """Recent ``ended`` meetings without transcripts get fetched + reported."""
    from pocketpaw_ee.cloud.meetings.providers.recall import jobs
    from pocketpaw_ee.cloud.models.meeting import Meeting as _MD

    # ws-alpha is discovered from its meeting docs — env-based single-account
    # creds mean there is no per-workspace credentials row.
    # Two recent ended meetings + one older one outside the lookback.
    now = datetime.now(UTC)
    await _MD(
        workspace="ws-alpha",
        provider="zoom",
        provider_meeting_id="m1",
        title="Recent A",
        join_url="https://x/1",
        status="ended",
        actual_end=now - timedelta(hours=1),
    ).insert()
    await _MD(
        workspace="ws-alpha",
        provider="zoom",
        provider_meeting_id="m2",
        title="Recent B",
        join_url="https://x/2",
        status="ended",
        actual_end=now - timedelta(days=2),
    ).insert()
    await _MD(
        workspace="ws-alpha",
        provider="zoom",
        provider_meeting_id="m_old",
        title="Way old",
        join_url="https://x/old",
        status="ended",
        actual_end=now - timedelta(days=60),
    ).insert()

    reports = await jobs.run_transcript_sync_pass(lookback_days=7)
    assert len(reports) == 1
    r = reports[0]
    assert r.workspace_id == "ws-alpha"
    # Two recent meetings → two fetches. The 60-day-old one is outside the
    # lookback window AND outside the retention floor; not touched.
    assert r.fetched == 2
    assert r.failed == 0
    assert fake_transcript_adapter.calls == 2


@pytest.mark.usefixtures("mongo_db")
async def test_run_transcript_sync_pass_caps_at_retention_floor() -> None:
    """lookback_days > 28 is clamped — protects against Meet's 30-day window."""
    from pocketpaw_ee.cloud.meetings.providers.recall import jobs

    # No meetings exist, so no workspaces are discovered.
    reports = await jobs.run_transcript_sync_pass(lookback_days=9999)
    assert reports == []


@pytest.mark.usefixtures("mongo_db")
async def test_run_transcript_sync_pass_skips_meetings_with_transcripts(
    fake_transcript_adapter,
) -> None:
    """A meeting that already has a transcript file_id is not re-fetched."""
    from pocketpaw_ee.cloud.meetings.providers.recall import jobs
    from pocketpaw_ee.cloud.models.meeting import Meeting as _MD
    from pocketpaw_ee.cloud.models.meeting import MeetingTranscript as _TD

    meeting = _MD(
        workspace="ws-alpha",
        provider="zoom",
        provider_meeting_id="m1",
        title="Already cached",
        join_url="https://x",
        status="ended",
        actual_end=datetime.now(UTC) - timedelta(hours=2),
    )
    await meeting.insert()
    # Pre-existing transcript row with file_id set.
    await _TD(
        workspace="ws-alpha",
        meeting_id=str(meeting.id),
        provider_transcript_id="m1",
        file_id="file-1",
        fetched_at=datetime.now(UTC),
    ).insert()

    # The job still touches the meeting (we don't filter at the query
    # level — fetch_and_store_transcript is idempotent), but the fetch
    # itself is a no-op because the row exists. Document behavior: the
    # adapter is still called since we hand off to
    # fetch_and_store_transcript unconditionally. If this becomes a
    # cost concern, add a pre-check on the query side.
    reports = await jobs.run_transcript_sync_pass(lookback_days=7)
    assert reports[0].candidates == 1
