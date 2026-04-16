# ee/retrieval/router.py — REST surface for the retrieval + graduation projection.
# Created: 2026-04-16 (feat/retrieval-journal-projection) — Wave 3 / Org
# Architecture RFC, Phase 3. Carries the intent of #936's retrieval log
# endpoints + #937's graduation endpoints onto the journal-backed
# projection. Reads are the projection; writes happen either via
# soul-protocol's own RetrievalRouter (which emits ``retrieval.query``
# directly) or via ``RetrievalJournalStore.log_retrieval`` from a
# pocketpaw caller. Nothing in this router writes retrievals.
#
# The router owns a process-scoped store + projection warmed from the
# org journal on first request. That follows the fleet router's pattern:
# one ``Depends(get_journal)`` per request, the store itself is cached
# so the projection doesn't rebuild on every call.

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from soul_protocol.engine.journal import Journal

from ee.journal_dep import get_journal
from ee.retrieval.policy import (
    DEFAULT_EPISODIC_THRESHOLD,
    DEFAULT_SEMANTIC_THRESHOLD,
    DEFAULT_WINDOW_DAYS,
    GraduationDecision,
    scan_for_graduations,
)
from ee.retrieval.store import RetrievalJournalStore

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Retrieval"])


# ---------------------------------------------------------------------------
# Response envelopes — small Pydantic shells so the OpenAPI schema documents
# every field. Keeping them here (not in projection.py) matches how other
# ee/ routers segregate HTTP types from the pure-Python model layer.
# ---------------------------------------------------------------------------


class RetrievalEntryResponse(BaseModel):
    """One retrieval row as rendered by the REST surface."""

    request_id: str
    query: str
    actor_id: str
    actor_kind: str
    scope: list[str]
    correlation_id: str | None
    ts: str
    strategy: str
    sources_queried: list[str]
    sources_failed: list[dict[str, Any]]
    candidate_count: int
    candidates: list[dict[str, Any]]
    picked: list[str]
    latency_ms: int
    pocket_id: str | None
    trace_id: str | None
    seq: int


class RecentRetrievalsResponse(BaseModel):
    """Envelope for ``GET /retrieval/recent`` — leaves room for pagination
    metadata without breaking clients later.
    """

    entries: list[RetrievalEntryResponse]
    total: int


class GraduationStateResponse(BaseModel):
    memory_id: str
    current_tier: str
    previous_tier: str | None
    kind: str
    access_count: int
    window_days: int
    pocket_id: str | None
    scope: list[str]
    reason: str
    applied_at: str
    seq: int


class GraduationStateListResponse(BaseModel):
    entries: list[GraduationStateResponse]
    total: int


class ScanRequest(BaseModel):
    """Body for ``POST /graduation/scan``."""

    window_days: int = DEFAULT_WINDOW_DAYS
    episodic_threshold: int = DEFAULT_EPISODIC_THRESHOLD
    semantic_threshold: int = DEFAULT_SEMANTIC_THRESHOLD
    actor_id: str | None = None
    pocket_id: str | None = None
    scope: str | None = None


class ScanResponse(BaseModel):
    """Flat projection of GraduationReport for HTTP — dataclasses don't
    serialise directly through FastAPI without a response_model shim.
    """

    decisions: list[GraduationDecision]
    scanned_retrievals: int
    window_days: int
    dry_run: bool
    generated_at: str


# ---------------------------------------------------------------------------
# Store caching — the projection is expensive to rebuild, cheap to query.
# One instance per (Journal) keeps the rebuild cost to O(1) in the amortised
# case and O(events) on cold start. Tests can reset this via
# ``_cached_store.cache_clear()`` or by overriding ``get_journal``.
# ---------------------------------------------------------------------------


def _get_store(journal: Journal) -> RetrievalJournalStore:
    """Return a warmed store for ``journal`` — one instance per Journal id.

    First call on a given journal warms the projection with
    ``store.bootstrap()``. Subsequent calls return the cached instance
    unchanged; incremental apply() on every new write keeps it current.
    """

    key = id(journal)
    cached = _STORE_CACHE.get(key)
    if cached is not None:
        return cached
    store = RetrievalJournalStore(journal)
    store.bootstrap()
    _STORE_CACHE[key] = store
    return store


_STORE_CACHE: dict[int, RetrievalJournalStore] = {}


def reset_store_cache() -> None:
    """Drop every cached store — for tests that need a clean projection."""

    _STORE_CACHE.clear()


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/retrieval/recent", response_model=RecentRetrievalsResponse)
async def recent_retrievals(
    scope: str | None = Query(None, description="Filter to retrievals tagged with this scope"),
    actor_id: str | None = Query(None, description="Filter by actor id (e.g. 'user:priya')"),
    pocket_id: str | None = Query(None, description="Filter by pocket id"),
    limit: int = Query(20, ge=1, le=500),
    journal: Journal = Depends(get_journal),
) -> RecentRetrievalsResponse:
    """Return the most-recent retrievals from the projection — newest first.

    The journal's event log is the source of truth; this endpoint serves
    an in-memory fold of the ``retrieval.query`` stream. Identical data
    shape to what #936's GET /retrieval/log returned, minus the
    since/until time filters (use correlation_id for session-scoped
    lookups or rebuild with ``scope=...`` for tenant-scoped views).
    """

    store = _get_store(journal)
    rows = store.projection.recent_retrievals(
        scope=scope,
        actor_id=actor_id,
        pocket_id=pocket_id,
        limit=limit,
    )
    return RecentRetrievalsResponse(
        entries=[RetrievalEntryResponse(**_view_to_dict(r)) for r in rows],
        total=len(rows),
    )


@router.get("/retrieval/session/{correlation_id}", response_model=RecentRetrievalsResponse)
async def retrievals_in_session(
    correlation_id: str,
    journal: Journal = Depends(get_journal),
) -> RecentRetrievalsResponse:
    """All retrievals sharing one correlation_id — the "what did the
    agent ask during this run" view. Ordered oldest-first.

    Returns 404 when no retrievals match — lets a UI distinguish between
    "session didn't exist" and "session had nothing in the projection
    yet" with a straightforward status code.
    """

    store = _get_store(journal)
    rows = store.projection.retrievals_by_correlation(correlation_id)
    if not rows:
        raise HTTPException(status_code=404, detail="No retrievals for correlation_id")
    return RecentRetrievalsResponse(
        entries=[RetrievalEntryResponse(**_view_to_dict(r)) for r in rows],
        total=len(rows),
    )


@router.get("/graduation/state", response_model=GraduationStateListResponse)
async def graduation_state(
    memory_id: str | None = Query(None, description="Return state for one memory_id"),
    journal: Journal = Depends(get_journal),
) -> GraduationStateListResponse:
    """Current graduation state — most-recent ``graduation.applied`` event
    per memory_id. Omitting ``memory_id`` returns the full set.
    """

    store = _get_store(journal)
    rows = store.projection.graduation_state(memory_id=memory_id)
    return GraduationStateListResponse(
        entries=[
            GraduationStateResponse(
                memory_id=r.memory_id,
                current_tier=r.current_tier,
                previous_tier=r.previous_tier,
                kind=r.kind,
                access_count=r.access_count,
                window_days=r.window_days,
                pocket_id=r.pocket_id,
                scope=list(r.scope),
                reason=r.reason,
                applied_at=r.applied_at.isoformat(),
                seq=r.seq,
            )
            for r in rows
        ],
        total=len(rows),
    )


@router.post("/graduation/scan", response_model=ScanResponse)
async def run_graduation_scan(
    req: ScanRequest | None = None,
    journal: Journal = Depends(get_journal),
) -> ScanResponse:
    """Dry-run the graduation policy over the projection and return the
    proposed decisions. Does NOT emit events — the apply path is a
    separate step that callers opt into explicitly, matching #937's
    dry-run-by-default contract.
    """

    req = req or ScanRequest()
    store = _get_store(journal)
    report = scan_for_graduations(
        store.projection,
        window_days=req.window_days,
        episodic_threshold=req.episodic_threshold,
        semantic_threshold=req.semantic_threshold,
        actor_id=req.actor_id,
        pocket_id=req.pocket_id,
        scope=req.scope,
        dry_run=True,
    )
    return ScanResponse(
        decisions=list(report.decisions),
        scanned_retrievals=report.scanned_retrievals,
        window_days=report.window_days,
        dry_run=report.dry_run,
        generated_at=report.generated_at.isoformat(),
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _view_to_dict(view: Any) -> dict[str, Any]:
    """RetrievalView.as_dict() emits ISO-timestamp strings for ``ts`` so the
    Pydantic response model serialises cleanly. This wrapper exists so the
    router stays agnostic to whether the projection returned a dataclass
    or something else in the future.
    """

    return view.as_dict()
