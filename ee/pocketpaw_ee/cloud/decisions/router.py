# router.py — FastAPI router skeleton for the decision-graph entity.
# Created: 2026-05-25 (RFC 07 Slice 1) — SKELETON only. Slice 1 ships the
#   in-process Python API; Slice 2 wires the real REST endpoints:
#
#     GET  /api/v1/decisions/:id
#     GET  /api/v1/decisions
#     GET  /api/v1/decisions/:id/trace
#     GET  /api/v1/decisions/:id/downstream
#     GET  /api/v1/decisions/:id/timeline
#     POST /api/v1/decisions/explain
#
#   Slice 1 ships ONE placeholder route — `GET /api/v1/decisions/_ping`
#   — that returns the projection cursor + row count. It exists so
#   operators can smoke-test that the projection bootstrap fired without
#   waiting on Slice 2. Thin: never `raise HTTPException`; CloudError →
#   JSON is the contract.
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from pocketpaw_ee.cloud.decisions.service import get_decision_graph
from pocketpaw_ee.cloud.license import require_license

router = APIRouter(
    prefix="/decisions",
    tags=["Decisions"],
    dependencies=[Depends(require_license)],
)


@router.get("/_ping")
async def decisions_ping() -> dict[str, Any]:
    """Smoke endpoint — returns projection cursor + row count.

    Slice 1-only. Slice 2 replaces this with the real REST surface.
    Intentionally unauthenticated beyond the license gate so an
    operator can curl it.
    """
    graph = get_decision_graph()
    return {
        "status": "ok",
        "cursor": graph.projection.cursor,
        "decisions": graph.store.count(),
    }
