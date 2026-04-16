# ee/widget/router.py — REST surface for the widget journal projection.
# Created: 2026-04-16 (feat/widget-journal-projection) — Wave 3 / Org
# Architecture RFC, Phase 3. Carries the read-side intent of held PRs
# #941 (graduation state per widget) and #942 (co-occurrence
# suggestions) onto the journal-backed projection.
#
# Reads hit the in-memory projection; writes happen via
# WidgetJournalStore from pocketpaw callsites (dashboard widget
# clicks, scheduled graduation scans). Nothing in this router writes
# widget interactions — the dashboard does that directly when a user
# touches a widget.
#
# Store cache follows the same pattern as ee/retrieval/router.py: one
# warmed store per Journal id, bootstrap on first request, incremental
# apply on every subsequent write.

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from soul_protocol.engine.journal import Journal

from ee.journal_dep import get_journal
from ee.widget.policy import (
    DEFAULT_ARCHIVE_DAYS,
    DEFAULT_COOCCURRENCE_THRESHOLD,
    DEFAULT_PIN_THRESHOLD,
    DEFAULT_WINDOW_DAYS,
    WidgetGraduationDecision,
    scan_for_cooccurrences,
    scan_for_widget_graduations,
)
from ee.widget.store import WidgetJournalStore

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Widgets"])


# ---------------------------------------------------------------------------
# Response envelopes.
# ---------------------------------------------------------------------------


class WidgetUsageEntry(BaseModel):
    widget_name: str
    surface: str
    count: int
    promoting_count: int
    unique_actors: int
    last_interaction: str
    scope: list[str]
    pocket_id: str | None


class WidgetUsageResponse(BaseModel):
    entries: list[WidgetUsageEntry]
    total: int
    window_days: int


class CooccurrenceEntry(BaseModel):
    signature: str
    widget_a: str
    widget_b: str
    count: int
    pocket_id: str | None
    scope: list[str]
    last_seen: str


class CooccurrenceResponse(BaseModel):
    entries: list[CooccurrenceEntry]
    total: int
    min_count: int


class GraduationStateEntry(BaseModel):
    widget_name: str
    surface: str
    current_tier: str
    previous_tier: str | None
    confidence: float
    interactions_in_window: int
    window_days: int
    pocket_id: str | None
    scope: list[str]
    reason: str
    applied_at: str
    seq: int


class GraduationStateResponse(BaseModel):
    entries: list[GraduationStateEntry]
    total: int


class GraduationScanRequest(BaseModel):
    window_days: int = DEFAULT_WINDOW_DAYS
    pin_threshold: int = DEFAULT_PIN_THRESHOLD
    archive_days: int = DEFAULT_ARCHIVE_DAYS
    pocket_id: str | None = None
    scope: str | None = None


class GraduationScanResponse(BaseModel):
    decisions: list[WidgetGraduationDecision]
    scanned_widgets: int
    window_days: int
    dry_run: bool
    generated_at: str


# ---------------------------------------------------------------------------
# Store cache — one warmed store per Journal id.
# ---------------------------------------------------------------------------


_STORE_CACHE: dict[int, WidgetJournalStore] = {}


def _get_store(journal: Journal) -> WidgetJournalStore:
    key = id(journal)
    cached = _STORE_CACHE.get(key)
    if cached is not None:
        return cached
    store = WidgetJournalStore(journal)
    store.bootstrap()
    _STORE_CACHE[key] = store
    return store


def reset_store_cache() -> None:
    """Drop every cached store — for tests that need a clean projection."""

    _STORE_CACHE.clear()


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/widgets/usage", response_model=WidgetUsageResponse)
async def widget_usage(
    scope: str | None = Query(None, description="Filter to widgets tagged with this scope"),
    pocket_id: str | None = Query(None),
    window_days: int = Query(DEFAULT_WINDOW_DAYS, ge=1, le=365),
    journal: Journal = Depends(get_journal),
) -> WidgetUsageResponse:
    """Per-widget usage roll-up over the last ``window_days``.

    Supersedes the held PR #941 ``GET /api/v1/widgets/log`` endpoint.
    The projection is the source of truth; this endpoint serves an
    in-memory fold of the ``widget.interaction.recorded`` stream.
    """

    store = _get_store(journal)
    rows = store.projection.usage(
        window_days=window_days,
        scope=scope,
        pocket_id=pocket_id,
    )
    entries = [
        WidgetUsageEntry(
            widget_name=r.widget_name,
            surface=r.surface,
            count=r.count,
            promoting_count=r.promoting_count,
            unique_actors=r.unique_actors,
            last_interaction=r.last_interaction.isoformat(),
            scope=list(r.scope),
            pocket_id=r.pocket_id,
        )
        for r in rows
    ]
    return WidgetUsageResponse(
        entries=entries,
        total=len(entries),
        window_days=window_days,
    )


@router.get("/widgets/cooccurrence", response_model=CooccurrenceResponse)
async def widget_cooccurrence(
    min_count: int = Query(
        DEFAULT_COOCCURRENCE_THRESHOLD,
        ge=1,
        description="Minimum co-occurrence count to include",
    ),
    pocket_id: str | None = Query(None),
    journal: Journal = Depends(get_journal),
) -> CooccurrenceResponse:
    """Top co-occurring widget pairs.

    Supersedes the read side of held PR #942's co-occurrence
    detector. Ordering is count-desc, then last-seen-desc.
    """

    store = _get_store(journal)
    rows = store.projection.cooccurrences(
        min_count=min_count,
        pocket_id=pocket_id,
    )
    entries = [
        CooccurrenceEntry(
            signature=r.signature,
            widget_a=r.widget_a,
            widget_b=r.widget_b,
            count=r.count,
            pocket_id=r.pocket_id,
            scope=list(r.scope),
            last_seen=r.last_seen.isoformat(),
        )
        for r in rows
    ]
    return CooccurrenceResponse(
        entries=entries,
        total=len(entries),
        min_count=min_count,
    )


@router.get("/widgets/graduation/state", response_model=GraduationStateResponse)
async def widget_graduation_state(
    widget_name: str | None = Query(None),
    surface: str | None = Query(None),
    journal: Journal = Depends(get_journal),
) -> GraduationStateResponse:
    """Current graduation state — most-recent ``widget.graduated``
    event per (widget_name, surface) pair. Omitting both filters
    returns the full set.
    """

    store = _get_store(journal)
    rows = store.projection.graduation_state(
        widget_name=widget_name,
        surface=surface,
    )
    entries = [
        GraduationStateEntry(
            widget_name=r.widget_name,
            surface=r.surface,
            current_tier=r.current_tier,
            previous_tier=r.previous_tier,
            confidence=r.confidence,
            interactions_in_window=r.interactions_in_window,
            window_days=r.window_days,
            pocket_id=r.pocket_id,
            scope=list(r.scope),
            reason=r.reason,
            applied_at=r.applied_at.isoformat(),
            seq=r.seq,
        )
        for r in rows
    ]
    return GraduationStateResponse(entries=entries, total=len(entries))


@router.post("/widgets/graduation/scan", response_model=GraduationScanResponse)
async def run_widget_graduation_scan(
    req: GraduationScanRequest | None = None,
    journal: Journal = Depends(get_journal),
) -> GraduationScanResponse:
    """Dry-run the graduation policy over the projection and return
    the proposed decisions. Does NOT emit events — apply is a
    separate step that callers opt into explicitly, matching #941's
    dry-run-by-default contract.
    """

    req = req or GraduationScanRequest()
    store = _get_store(journal)
    report = scan_for_widget_graduations(
        store.projection,
        window_days=req.window_days,
        pin_threshold=req.pin_threshold,
        archive_days=req.archive_days,
        pocket_id=req.pocket_id,
        scope=req.scope,
        dry_run=True,
    )
    return GraduationScanResponse(
        decisions=list(report.decisions),
        scanned_widgets=report.scanned_widgets,
        window_days=report.window_days,
        dry_run=report.dry_run,
        generated_at=report.generated_at.isoformat(),
    )


# ---------------------------------------------------------------------------
# Helpers — expose the scan helper to downstream code without importing
# every router symbol.
# ---------------------------------------------------------------------------


def _scan_cooccurrences_via_router(
    journal: Journal,
    *,
    threshold: int = DEFAULT_COOCCURRENCE_THRESHOLD,
) -> Any:
    """Thin facade for callers that want a one-shot scan without going
    through HTTP (a CLI command, for instance). Not registered as a
    route; kept here so tests and tools share one path.
    """

    store = _get_store(journal)
    return scan_for_cooccurrences(store.projection, threshold=threshold)
