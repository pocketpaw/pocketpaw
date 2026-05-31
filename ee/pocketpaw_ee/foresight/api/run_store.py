# ee/pocketpaw_ee/foresight/api/run_store.py
# Modified: 2026-05-25 (feat/foresight-v07-cloud-mount) — PR 7. SUPERSEDED.
#   The cloud router no longer uses this store; ``ee.cloud.foresight.service``
#   persists runs to the ``foresight_runs`` Mongo collection via Beanie
#   instead. Kept in-tree as a documented deprecation breadcrumb so any
#   v0.1 caller pinning the old import path fails loudly rather than
#   silently regressing to ephemeral storage. Remove after one release
#   cycle once no external callers reference it.
# Created: 2026-05-25 (feat/foresight-v01-scaffold) — RFC 08 v0.1 scaffold.
#
# In-memory run store — the v0.1 stand-in for the projected_decisions
# + foresight_runs Mongo collections RFC §7.7 + §13.1 specify. The
# v0.1 router wrote a RunRecord on POST /scenarios and read it on
# GET /runs/{id}; tests injected a fresh store via the singleton
# resolver.
#
# PR 7 SUPERSEDED this with Beanie-backed persistence at
# ``ee.cloud.foresight.service``. The 4-file cloud shape ships in the
# same PR: domain.py (value objects), dto.py (request/response),
# service.py (Beanie writes + event emission), router.py (thin endpoints).
# The router is mounted from ``mount_cloud``; this in-memory store is
# no longer wired into the request path.

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from threading import RLock
from typing import Any
from uuid import UUID, uuid4


@dataclass
class RunRecord:
    """One scenario run, as persisted by the v0.1 in-memory store.

    The RFC §7.7 ProjectedDecision schema (run_id, sim_tick,
    projection_confidence) is NOT enforced here — v0.1 emits a single
    coarse-grained record per run with the scenario name, request body,
    and the RunResult wire dict. v1.0 fans this out into a run
    document + per-tick aggregate rows + projected_decisions rows.
    """

    id: UUID
    scenario_name: str
    status: str  # "queued" | "running" | "complete" | "failed"
    created_at: datetime
    request: dict[str, Any]
    result: dict[str, Any] | None = None
    error: str | None = None


class RunStore:
    """Thread-safe in-memory run store.

    The lock protects against concurrent POST + GET (FastAPI's async
    handlers can interleave on the event loop, and the store is a
    shared singleton). Reads return *copies* of the RunRecord wire
    dict so callers can't mutate the store by mutating the response.
    """

    def __init__(self) -> None:
        self._lock = RLock()
        self._runs: dict[UUID, RunRecord] = {}

    def create(self, *, scenario_name: str, request: dict[str, Any]) -> RunRecord:
        record = RunRecord(
            id=uuid4(),
            scenario_name=scenario_name,
            status="queued",
            created_at=datetime.now(UTC),
            request=dict(request),
        )
        with self._lock:
            self._runs[record.id] = record
        return record

    def mark_running(self, run_id: UUID) -> None:
        with self._lock:
            if run_id in self._runs:
                self._runs[run_id].status = "running"

    def mark_complete(self, run_id: UUID, result: dict[str, Any]) -> None:
        with self._lock:
            if run_id in self._runs:
                self._runs[run_id].status = "complete"
                self._runs[run_id].result = dict(result)

    def mark_failed(self, run_id: UUID, error: str) -> None:
        with self._lock:
            if run_id in self._runs:
                self._runs[run_id].status = "failed"
                self._runs[run_id].error = error

    def get(self, run_id: UUID) -> RunRecord | None:
        with self._lock:
            return self._runs.get(run_id)

    def all(self) -> list[RunRecord]:
        with self._lock:
            return list(self._runs.values())

    def clear(self) -> None:
        """Reset state. Tests use this between runs."""
        with self._lock:
            self._runs.clear()


def record_to_wire(record: RunRecord) -> dict[str, Any]:
    """Convert a RunRecord to a JSON-serializable wire dict.

    Kept separate from the dataclass so the wire shape can evolve
    without changing storage. v1.0 will swap this for
    ``ForesightRunResponse.model_validate(record)``.
    """
    return {
        "id": str(record.id),
        "scenario_name": record.scenario_name,
        "status": record.status,
        "created_at": record.created_at.isoformat(),
        "request": dict(record.request),
        "result": dict(record.result) if record.result else None,
        "error": record.error,
    }


# --- singleton resolver ----------------------------------------------

_STORE: RunStore | None = None


def get_run_store() -> RunStore:
    """Return the process-wide run store, creating it on first call.

    The router depends on this via ``Depends(get_run_store)`` so tests
    can override it with ``app.dependency_overrides[get_run_store] = ...``
    to inject a fresh per-test store.
    """
    global _STORE
    if _STORE is None:
        _STORE = RunStore()
    return _STORE


def reset_run_store() -> None:
    """Tear down the singleton. Tests call this in teardown when they
    want the next test to construct a fresh store via ``get_run_store``.
    """
    global _STORE
    _STORE = None
