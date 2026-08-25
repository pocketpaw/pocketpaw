# router.py — Otherhand page-snapshot REST surface.
#
# Created: 2026-08-25 (feat/other-hand-surface, Otherhand v1) — one endpoint,
# pinned by section 2 of the frozen frontend/backend contract
# (``docs/design/drafts/2026-08-25-otherhand-contract.md``):
#
#   POST /api/v1/other-hand/pages/{page_id}/snapshot
#     { "png_base64": "<base64 PNG of the full 1240x1754 page>", "free_y": 820 }
#   200 -> { "path": "<absolute path the agent can Read>", "free_y": 820 }
#
# The route is THIN: it reads identity from the cloud deps and delegates to
# ``other_hand.service``, which owns the filesystem discipline. Auth matches the
# sibling workspace-scoped routers (studio): ``require_license`` on the router
# plus ``current_workspace_id``, which itself depends on ``current_active_user``,
# so an unauthenticated caller never reaches the handler. No new RBAC action is
# invented — writing your own page's snapshot is not a privileged operation, and
# a bare workspace scope is what the other per-workspace product surfaces use.
#
# Note the URL says ``other-hand`` (hyphen) while the ``SurfaceKind`` value is
# ``other_hand`` (underscore). Both are contract: the hyphen is the frontend
# route and this endpoint's path; the underscore is the wire value the client
# stamps as ``surface``. They are not required to match and deliberately are not
# renamed to.
#
# ``free_y`` is echoed back unchanged rather than stored. The backend has no
# opinion on it — it is the frontend's measurement of its own canvas, and it
# reaches the agent via the surface meta on the next chat turn, not from here.
# Echoing it keeps the client's snapshot-then-send sequence to one round-trip's
# worth of state.
#
# Errors propagate as ``CloudError`` so the central cloud error handler maps them
# to the JSON envelope; the router never raises ``HTTPException`` (entity rule 10).

"""FastAPI router for Otherhand page snapshots."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Path
from pydantic import BaseModel, Field

from pocketpaw_ee.cloud._core.deps import current_workspace_id
from pocketpaw_ee.cloud._core.errors import CloudError
from pocketpaw_ee.cloud.license import require_license
from pocketpaw_ee.cloud.other_hand import service as other_hand_service

router = APIRouter(
    prefix="/other-hand",
    tags=["Otherhand"],
    dependencies=[Depends(require_license)],
)


class SnapshotRequest(BaseModel):
    """Body for ``POST /other-hand/pages/{page_id}/snapshot``.

    * ``png_base64`` — the full page rendered to a PNG, base64-encoded. A
      ``data:image/png;base64,`` prefix is tolerated. Validated (size, base64,
      PNG magic) by the service before anything touches the disk.
    * ``free_y`` — the y coordinate, in the page's 1240x1754 logical space, below
      which the page is empty. Echoed back; the backend stores nothing. Bounded
      to the page so a nonsense value is rejected at the wire rather than
      reaching the agent as a coordinate it would dutifully draw at.
    """

    png_base64: str = Field(min_length=1)
    free_y: int = Field(ge=0, le=1754)


def _to_cloud_error(exc: other_hand_service.SnapshotError) -> CloudError:
    """Map a service ``SnapshotError`` onto the cloud error envelope.

    The service's code and message are already safe to show — neither carries a
    filesystem path, only a restatement of what the caller sent.
    """
    return CloudError(exc.status_code, exc.code, exc.message)


@router.post("/pages/{page_id}/snapshot")
async def put_page_snapshot(
    body: SnapshotRequest,
    page_id: str = Path(min_length=1, max_length=128),
    workspace_id: str = Depends(current_workspace_id),
) -> dict[str, Any]:
    """Persist the page snapshot; return the path the agent reads it from.

    Overwrites the page's previous snapshot — one live snapshot per page, no
    history in v1. Any workspace member may snapshot their own page.
    """
    try:
        path = other_hand_service.write_snapshot(workspace_id, page_id, body.png_base64)
    except other_hand_service.SnapshotError as exc:
        raise _to_cloud_error(exc) from exc
    return {"path": path, "free_y": body.free_y}


__all__ = ["router"]
