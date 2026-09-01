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

from typing import Any, Literal

from fastapi import APIRouter, Depends, Path
from pydantic import BaseModel, Field

from pocketpaw_ee.cloud._core.deps import current_user_id, current_workspace_id
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
    #: Upper bound lifted 2026-08-26: the paper GROWS downward (whole
    #: half-sheets as ink approaches the bottom), so free_y can exceed one
    #: A4 sheet. 30 sheets is far past any real page and still rejects a
    #: nonsense coordinate at the wire.
    free_y: int = Field(ge=0, le=52620)
    #: Which image this is. ``page`` (the default, and the only v1 value) is
    #: the notebook the agent draws on. ``book`` is the read-only source page
    #: shown beside it in book mode — the agent reads it and never draws on it.
    #: Defaulted so an older client that knows nothing about book mode keeps
    #: working unchanged.
    kind: Literal["page", "book", "mark"] = "page"


class IllustrateRequest(BaseModel):
    """Body for ``POST /other-hand/illustrate``.

    The endpoint IS the opt-in. It exists only because a person pressed a
    button, so reaching it is the authorisation — there is no path by which an
    ordinary turn arrives here. That matters because each call costs real money
    (a Recraft v4 pro generation) and, unlike LLM tokens, a user's own BYOK key
    does NOT cover it.

    ``x/y/w/h`` is where the drawing lands, in the page's 1240-wide logical
    space. The caller picks it because only the client knows where the page is
    empty; the box is bounded here so a nonsense rectangle is refused at the
    wire rather than becoming coordinates nobody can see.
    """

    prompt: str = Field(min_length=2, max_length=500)
    x: float = Field(ge=0, le=1240)
    #: The paper grows downward, so y follows the same 30-sheet bound the
    #: snapshot's free_y uses.
    y: float = Field(ge=0, le=52620)
    w: float = Field(gt=0, le=1240)
    h: float = Field(gt=0, le=1754)


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
        path = other_hand_service.write_snapshot(
            workspace_id, page_id, body.png_base64, body.kind
        )
    except other_hand_service.SnapshotError as exc:
        raise _to_cloud_error(exc) from exc
    return {"path": path, "free_y": body.free_y}


__all__ = ["router"]


@router.post("/illustrate")
async def illustrate(
    body: IllustrateRequest,
    workspace_id: str = Depends(current_workspace_id),
    user_id: str = Depends(current_user_id),
) -> dict[str, Any]:
    """Generate an illustration and return it as page-ops the caller can draw.

    Returns ``{"ops": [...]}`` — ``path`` ops in page space, ready to hand
    straight to the renderer. The drawing arrives as INK rather than a picture,
    which is the whole point: it uses the same pen, erases like ink, and counts
    toward free_y so the next turn will not write over it.

    An empty ``ops`` list means "could not illustrate" — no key configured, or
    nothing drawable came back. Deliberately not an error: the page should carry
    on, and the caller has nothing useful to tell the user about a missing
    generator.
    """
    from pocketpaw_ee.cloud.auth import guest_budget
    from pocketpaw_ee.cloud.other_hand import illustrate as illustrator
    from pocketpaw_ee.cloud.other_hand import illustration_budget
    from pocketpaw_ee.cloud.other_hand.svg_to_ink import Box
    from pocketpaw_ee.cloud.studio import fal_edit

    # Guests do not get to spend platform money on pictures. The budget below
    # is a cost CEILING, not an entitlement, and a guest can mint a fresh
    # workspace to get a fresh ceiling, so the ceiling alone left an unbounded
    # bill attached to a signup form that asks for nothing. Refused BEFORE the
    # budget is claimed, so a refusal costs the workspace nothing.
    if await guest_budget.load_guest(user_id) is not None:
        from pocketpaw_ee.cloud._core.errors import GuestIllustrateForbidden

        raise GuestIllustrateForbidden()

    # A pressed button authorises ONE generation; it does not cap how many.
    # Scripted, the same button is a loop, so the ceiling has to live here and
    # not in the UI. Claimed BEFORE the paid call and fail-closed, exactly like
    # the MCP tool path -- this route was the one caller that skipped it.
    allowed, spent, cap = await illustration_budget.try_spend(workspace_id)
    if not allowed:
        raise CloudError(
            429,
            "other_hand.illustration_limit",
            f"Today's illustration limit is used up ({spent}/{cap}).",
        )

    try:
        ops = await illustrator.illustrate_as_ops(
            body.prompt,
            Box(x=body.x, y=body.y, w=body.w, h=body.h),
            api_key=fal_edit.fal_api_key(),
            # Budget claimed above; this flag is the generator's own gate.
            allowed=True,
        )
    except illustrator.IllustrateError as exc:
        raise CloudError(502, "other_hand.illustrate_failed", str(exc)) from exc
    return {"ops": ops}
