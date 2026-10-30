# orchestrator.py — RetrievalRouter: scope-filtered, strategy-aware dispatch.
# Created: 2026-06-02 (feat/retrieval-rehome, #1327) — re-homed from
# soul-protocol's deleted ``engine/retrieval/router.py``. soul-protocol 0.4.0
# (#179) pruned the concrete retrieval orchestration out of the spec: the
# package now ships only the *vocabulary* (request/response models, the
# SourceAdapter + CredentialBroker protocols, the exception hierarchy) under
# ``soul_protocol.spec.retrieval``. The dispatcher itself is application-layer
# infrastructure, so it lives here in the consuming runtime.
#
# Adapted to the 0.4.0 surface:
#   * Imports of CandidateSource / RetrievalCandidate / RetrievalRequest /
#     RetrievalResult / SourceAdapter / the exceptions all come from
#     ``soul_protocol.spec.retrieval`` now (they used to be split across
#     engine/retrieval/{adapters,broker,exceptions}.py + spec/retrieval.py).
#   * ``scope_matches`` still lives at ``soul_protocol.engine.journal`` — the
#     0.4.0 prune only touched engine/retrieval, not the journal scope helpers.
#   * ``Credential`` is imported from spec.retrieval for type hints; the typed
#     ``DataRef`` content is opaque to the router (it never inspects
#     candidate.content), so the candidate-payload type change is transparent
#     here — the router merges by ``score`` and truncates, nothing else.
#
# This file is named ``orchestrator.py`` (not ``router.py``) on purpose: the
# package already has a ``router.py`` that is a FastAPI ``APIRouter`` for the
# retrieval-journal projection. Two different "routers" — keep them apart.
#
# Strategies:
#   * ``first``      — try sources in registration order, return the first
#                      that yields a non-empty list.
#   * ``parallel``   — fan out on a ThreadPoolExecutor, gather all, merge by
#                      score (None scores sink).
#   * ``sequential`` — try in order, accumulate until ``limit`` is reached.
#
# Scope enforcement: a source is a candidate iff its registered scopes overlap
# the request's scopes (bidirectional, via the journal's ``scope_matches``).
#
# Journal integration: if a Journal is attached, every dispatch writes a
# ``retrieval.query`` event tagged with the request's scope + actor, payload
# carrying the query text + the sources actually queried. Fire-and-forget —
# the query log is observability, not the auth trail (the broker's credential
# events are the fail-closed auth trail; see broker.py for the asymmetry).

from __future__ import annotations

import asyncio
import inspect
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout
from datetime import UTC, datetime
from uuid import uuid4

from soul_protocol.engine.journal import Journal, scope_matches
from soul_protocol.spec.journal import EventEntry
from soul_protocol.spec.retrieval import (
    CandidateSource,
    CredentialBroker,
    NoSourcesError,
    RetrievalCandidate,
    RetrievalRequest,
    RetrievalResult,
    SourceAdapter,
    SourceTimeoutError,
)


class RetrievalRouter:
    """Scope-filtered, strategy-aware dispatcher over registered sources.

    Usage::

        router = RetrievalRouter(journal=journal, broker=broker)
        router.register_source(CandidateSource(...), MyAdapter())
        result = router.dispatch(RetrievalRequest(...))
    """

    def __init__(
        self,
        *,
        journal: Journal | None = None,
        broker: CredentialBroker | None = None,
    ) -> None:
        self._journal = journal
        self._broker = broker
        self._sources: dict[str, tuple[CandidateSource, SourceAdapter]] = {}

    # -- registration -----------------------------------------------------

    def register_source(self, source: CandidateSource, adapter: SourceAdapter) -> None:
        self._sources[source.name] = (source, adapter)

    # -- dispatch ---------------------------------------------------------

    def dispatch(self, request: RetrievalRequest) -> RetrievalResult:
        request_id = uuid4()
        started = time.perf_counter()

        selected = self._select_sources(request)
        if not selected:
            raise NoSourcesError(
                f"no registered source matches scopes {request.scopes} "
                f"(sources filter: {request.sources})"
            )

        if request.strategy == "first":
            candidates, queried, failed = self._run_first(request, selected)
        elif request.strategy == "sequential":
            candidates, queried, failed = self._run_sequential(request, selected)
        else:
            candidates, queried, failed = self._run_parallel(request, selected)

        merged = _merge_and_truncate(candidates, request.limit)
        total_ms = (time.perf_counter() - started) * 1000.0

        result = RetrievalResult(
            request_id=request_id,
            candidates=merged,
            sources_queried=queried,
            sources_failed=failed,
            total_latency_ms=total_ms,
            trace=None,  # RetrievalTrace receipt is a future slice.
        )
        self._emit_query_event(request, result)
        return result

    async def adispatch(self, request: RetrievalRequest) -> RetrievalResult:
        """Async dispatch — prefers ``aquery`` per adapter, falls back to
        threading the sync ``query``.

        Adapters backed by async SDKs can participate in cooperative
        multitasking without bridging through ``asyncio.run``. Sync-only
        adapters keep working — the router wraps their ``query`` in
        ``asyncio.to_thread``.

        Strategy is parallel with per-source timeout (matching the default
        sync dispatch). ``first`` and ``sequential`` strategies fall back to
        the sync path — they serialize by design, so there's no async win.

        Cancellation note: on timeout, the async path (``aquery``) is
        cancelled via ``asyncio.wait_for`` and stops cooperatively. The sync
        fallback via ``to_thread`` does *not* stop the running thread — the
        wait just unblocks the caller while the adapter's sync ``query``
        keeps running to completion. Standard asyncio behavior; adapters that
        need hard cancellation should implement ``aquery`` natively.
        """
        if request.strategy in ("first", "sequential"):
            return await asyncio.to_thread(self.dispatch, request)

        request_id = uuid4()
        started = time.perf_counter()

        selected = self._select_sources(request)
        if not selected:
            raise NoSourcesError(
                f"no registered source matches scopes {request.scopes} "
                f"(sources filter: {request.sources})"
            )

        queried = [s.name for s, _ in selected]
        failed: list[tuple[str, str]] = []
        collected: list[RetrievalCandidate] = []

        async def _run_one(
            source: CandidateSource, adapter: SourceAdapter
        ) -> tuple[str, list[RetrievalCandidate] | None, str | None]:
            try:
                out = await asyncio.wait_for(
                    self._acall_adapter(request, source, adapter),
                    timeout=request.timeout_s,
                )
                return source.name, out, None
            except TimeoutError:
                return (
                    source.name,
                    None,
                    f"source {source.name} timed out after {request.timeout_s}s",
                )
            except Exception as e:
                return source.name, None, f"{type(e).__name__}: {e}"

        outcomes = await asyncio.gather(*(_run_one(s, a) for s, a in selected))
        for name, out, err in outcomes:
            if err is not None:
                failed.append((name, err))
            elif out is not None:
                collected.extend(out)

        merged = _merge_and_truncate(collected, request.limit)
        total_ms = (time.perf_counter() - started) * 1000.0

        result = RetrievalResult(
            request_id=request_id,
            candidates=merged,
            sources_queried=queried,
            sources_failed=failed,
            total_latency_ms=total_ms,
            trace=None,
        )
        self._emit_query_event(request, result)
        return result

    async def _acall_adapter(
        self,
        request: RetrievalRequest,
        source: CandidateSource,
        adapter: SourceAdapter,
    ) -> list[RetrievalCandidate]:
        """Async version of _call_adapter — prefer aquery, fall back to thread."""
        credential = None
        if source.kind == "dataref" and self._broker is not None:
            credential = self._broker.acquire(source.name, request.scopes)
            self._broker.ensure_usable(credential, request.scopes)
            self._broker.mark_used(credential)

        aquery = getattr(adapter, "aquery", None)
        if aquery is not None and inspect.iscoroutinefunction(aquery):
            return await aquery(request, credential)
        return await asyncio.to_thread(adapter.query, request, credential)

    # -- internals --------------------------------------------------------

    def _select_sources(
        self, request: RetrievalRequest
    ) -> list[tuple[CandidateSource, SourceAdapter]]:
        selected: list[tuple[CandidateSource, SourceAdapter]] = []
        for name, (source, adapter) in self._sources.items():
            if request.sources is not None and name not in request.sources:
                continue
            # Bidirectional overlap — a source registered for ``org:sales:*``
            # should match a request scoped to ``org:sales:leads`` AND vice
            # versa. ``scope_matches`` treats arg 2 as the pattern set, so we
            # run it both ways.
            if not (
                scope_matches(request.scopes, source.scopes)
                or scope_matches(source.scopes, request.scopes)
            ):
                continue
            selected.append((source, adapter))
        return selected

    def _call_adapter(
        self,
        request: RetrievalRequest,
        source: CandidateSource,
        adapter: SourceAdapter,
    ) -> list[RetrievalCandidate]:
        credential = None
        if source.kind == "dataref" and self._broker is not None:
            credential = self._broker.acquire(source.name, request.scopes)
            self._broker.ensure_usable(credential, request.scopes)
            self._broker.mark_used(credential)
        return adapter.query(request, credential)

    def _run_first(
        self,
        request: RetrievalRequest,
        selected: list[tuple[CandidateSource, SourceAdapter]],
    ) -> tuple[list[RetrievalCandidate], list[str], list[tuple[str, str]]]:
        queried: list[str] = []
        failed: list[tuple[str, str]] = []
        for source, adapter in selected:
            queried.append(source.name)
            try:
                out = _with_timeout(
                    lambda s=source, a=adapter: self._call_adapter(request, s, a),
                    request.timeout_s,
                    source.name,
                )
            except SourceTimeoutError as e:
                failed.append((source.name, str(e)))
                continue
            except Exception as e:  # pragma: no cover - defensive
                failed.append((source.name, f"{type(e).__name__}: {e}"))
                continue
            if out:
                return out, queried, failed
        return [], queried, failed

    def _run_sequential(
        self,
        request: RetrievalRequest,
        selected: list[tuple[CandidateSource, SourceAdapter]],
    ) -> tuple[list[RetrievalCandidate], list[str], list[tuple[str, str]]]:
        collected: list[RetrievalCandidate] = []
        queried: list[str] = []
        failed: list[tuple[str, str]] = []
        for source, adapter in selected:
            queried.append(source.name)
            try:
                out = _with_timeout(
                    lambda s=source, a=adapter: self._call_adapter(request, s, a),
                    request.timeout_s,
                    source.name,
                )
            except SourceTimeoutError as e:
                failed.append((source.name, str(e)))
                continue
            except Exception as e:
                failed.append((source.name, f"{type(e).__name__}: {e}"))
                continue
            collected.extend(out)
            if len(collected) >= request.limit:
                break
        return collected, queried, failed

    def _run_parallel(
        self,
        request: RetrievalRequest,
        selected: list[tuple[CandidateSource, SourceAdapter]],
    ) -> tuple[list[RetrievalCandidate], list[str], list[tuple[str, str]]]:
        queried = [s.name for s, _ in selected]
        failed: list[tuple[str, str]] = []
        collected: list[RetrievalCandidate] = []
        max_workers = max(1, len(selected))
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            future_to_name = {
                pool.submit(self._call_adapter, request, source, adapter): source.name
                for source, adapter in selected
            }
            for future, name in future_to_name.items():
                try:
                    out = future.result(timeout=request.timeout_s)
                except FuturesTimeout:
                    failed.append((name, f"source {name} timed out after {request.timeout_s}s"))
                    future.cancel()
                except Exception as e:
                    failed.append((name, f"{type(e).__name__}: {e}"))
                else:
                    collected.extend(out)
        return collected, queried, failed

    def _emit_query_event(self, request: RetrievalRequest, result: RetrievalResult) -> None:
        if self._journal is None:
            return
        entry = EventEntry(
            id=uuid4(),
            ts=datetime.now(UTC),
            actor=request.actor,
            action="retrieval.query",
            scope=list(request.scopes),
            correlation_id=request.correlation_id,
            payload={
                "request_id": str(result.request_id),
                "query": request.query,
                "strategy": request.strategy,
                "sources_queried": result.sources_queried,
                "sources_failed": [{"source": s, "reason": r} for s, r in result.sources_failed],
                "candidate_count": len(result.candidates),
                # point_in_time: record as ISO so downstream consumers can
                # replay the time-travel intent. Only present when the caller
                # asked for a historical snapshot.
                **(
                    {"point_in_time": request.point_in_time.isoformat()}
                    if request.point_in_time is not None
                    else {}
                ),
            },
        )
        try:
            self._journal.append(entry)
        except Exception:
            # Fire-and-forget. The retrieval.query event is a query log, not
            # an auth trail: losing it is an observability regression, not a
            # security one. Credential lifecycle events on the broker are
            # fail-closed precisely because they ARE the auth trail. The
            # asymmetry is deliberate — don't unify these two policies.
            pass


# -- helpers --------------------------------------------------------------


def _with_timeout(fn, timeout_s: float, source_name: str) -> list[RetrievalCandidate]:
    """Run ``fn`` on a helper thread with a wall-clock deadline.

    Used by the ``first`` and ``sequential`` strategies — ``parallel`` uses
    its own thread pool + futures timeout. One helper thread per call, in a
    ``with`` block so a wedged adapter never blocks interpreter shutdown.
    """
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(fn)
        try:
            return future.result(timeout=timeout_s)
        except FuturesTimeout as e:
            future.cancel()
            raise SourceTimeoutError(f"source {source_name} timed out after {timeout_s}s") from e


def _merge_and_truncate(
    candidates: list[RetrievalCandidate], limit: int
) -> list[RetrievalCandidate]:
    """Sort by score descending (None sinks), then truncate to ``limit``."""

    def key(c: RetrievalCandidate) -> tuple[int, float]:
        if c.score is None:
            return (1, 0.0)
        return (0, -c.score)

    ordered = sorted(candidates, key=key)
    return ordered[:limit]
