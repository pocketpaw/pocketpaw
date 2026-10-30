# router.py — FastAPI router for the herdr cockpit telemetry surface (HR-10a).
#
# Created: 2026-07-24 (feat/herdr-cockpit-sse) — the thin HTTP shell over
# ``herdr_cockpit.service``. Two read-only routes, both ADMIN-gated:
#   * GET /cockpit/stream  — text/event-stream; emits one ``cockpit.snapshot``
#     frame every POLL_INTERVAL_S with the live pane "dots".
#   * GET /cockpit/pane/{pane_id}/preview — on-demand pane scrollback (JSON).
#
# Discipline (ee/cloud 4-file rules): no business logic and no Beanie doc live
# here — the fail-open telemetry logic is in service.py. Mounted under /api/v1
# from ee/pocketpaw_ee/cloud/__init__.py. Mirrors the SSE shape of
# chat/runs/router.py and the ADMIN-gate shape of automations_status/router.py.

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse

from pocketpaw.agents.herdr_runtime import HerdrRuntime
from pocketpaw_ee.cloud._core.deps import require_action_any_workspace
from pocketpaw_ee.cloud.herdr_cockpit import service
from pocketpaw_ee.cloud.herdr_cockpit.dto import PanePreviewOut
from pocketpaw_ee.cloud.license import require_license

logger = logging.getLogger(__name__)

# How often the stream re-polls HerdrRuntime and emits a snapshot frame. Small
# and named so the cadence is one edit away; the poll is a direct adapter read
# (v1) — bus/Redis push is a later optimization, explicitly out of HR-10a scope.
POLL_INTERVAL_S = 1.5

# ---------------------------------------------------------------------------
# Auth: ADMIN-only (v1 safety). RATIONALE — herdr panes on a box are NOT
# paw-workspace-scoped: herdr mints its own opaque workspace ids (PaneRef.
# workspace_id ≠ a paw workspace id), so a member-visible cockpit on a SHARED
# box could leak other tenants' panes. Gating to a workspace ADMIN plus the
# default-off ``herdr_runtime_enabled`` flag keeps v1 safe on the dedicated-box
# (Track-A) deployment, where the workspace admin is the box operator.
#
# TODO(track-b): multi-tenant enablement MUST first scope panes to the caller's
# workspace (map herdr's pane workspace_id → the paw workspace, filter the
# snapshot, and authorize the preview per-pane) BEFORE this may be exposed to
# non-admins or on a multi-tenant shared box. Do not relax the ADMIN gate until
# that pane→tenant scoping exists.
# ---------------------------------------------------------------------------
_COCKPIT_ADMIN = Depends(require_action_any_workspace("cockpit.read"))

router = APIRouter(
    prefix="/cockpit",
    tags=["Herdr Cockpit"],
    dependencies=[Depends(require_license)],
)


def get_herdr_runtime() -> HerdrRuntime:
    """Provide a HerdrRuntime built from live settings.

    A FastAPI dependency so tests inject a fake via
    ``app.dependency_overrides[get_herdr_runtime]``. Construction is cheap (reads
    settings + resolves the binary; no subprocess), so a fresh instance per
    request/connection is fine — the adapter is stateless per call.
    """
    from pocketpaw.config import get_settings

    return HerdrRuntime(get_settings())


def _sse(event: str, data: dict) -> bytes:
    """Encode one named Server-Sent-Event frame."""
    return f"event: {event}\ndata: {json.dumps(data)}\n\n".encode()


@router.get("/stream", dependencies=[_COCKPIT_ADMIN])
async def stream_cockpit(
    runtime: HerdrRuntime = Depends(get_herdr_runtime),
) -> StreamingResponse:
    """Live SSE stream of herdr pane telemetry.

    Emits one ``cockpit.snapshot`` frame every ``POLL_INTERVAL_S``. Each frame's
    ``data`` is a ``CockpitSnapshot`` (``ts``, ``herdr_available``, ``panes``).
    The generator never raises — ``build_snapshot`` absorbs ``HerdrUnavailable``
    and emits a fail-open frame (``herdr_available=False``, empty panes), so the
    stream stays alive whether or not herdr is running. The frame cadence itself
    acts as the proxy keep-alive, so no separate heartbeat is needed.
    """

    async def gen() -> AsyncIterator[bytes]:
        while True:
            snapshot = await service.build_snapshot(runtime)
            yield _sse("cockpit.snapshot", snapshot.model_dump())
            await asyncio.sleep(POLL_INTERVAL_S)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/pane/{pane_id}/preview", response_model=PanePreviewOut, dependencies=[_COCKPIT_ADMIN])
async def preview_pane(
    pane_id: str,
    lines: int = Query(default=service.PREVIEW_DEFAULT_LINES, ge=1),
    runtime: HerdrRuntime = Depends(get_herdr_runtime),
) -> PanePreviewOut:
    """On-demand scrollback preview for one pane.

    ``lines`` is clamped to a sane maximum in the service (a too-large value is
    capped, not rejected). Fails open to ``text=""`` when herdr is unavailable or
    the pane cannot be read — never a 500.
    """
    return await service.read_preview(runtime, pane_id, lines)
