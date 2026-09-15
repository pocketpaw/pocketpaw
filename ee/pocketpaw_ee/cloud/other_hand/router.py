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
# Updated 2026-09-15 (feat/otherhand-page-store): the page STORE landed beside
# the snapshot endpoint — GET/PUT ``/other-hand/pages`` persist the ink model
# itself, so a page survives a reload and follows the user to another browser.
# The request/response models moved out of this file into ``dto.py`` at the same
# time (CLAUDE.md's touch-time rule; the entity had no 4-file shape before) and
# are re-exported here so existing importers are unaffected.
#
# Errors propagate as ``CloudError`` so the central cloud error handler maps them
# to the JSON envelope; the router never raises ``HTTPException`` (entity rule 10).

"""FastAPI router for Otherhand pages, snapshots and illustrations."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Path, Query

from pocketpaw_ee.cloud._core.deps import current_user_id, current_workspace_id
from pocketpaw_ee.cloud._core.errors import CloudError
from pocketpaw_ee.cloud.license import require_license
from pocketpaw_ee.cloud.other_hand import service as other_hand_service
from pocketpaw_ee.cloud.other_hand.dto import (
    PAGE_ID_MAX,
    IllustrateRequest,
    SnapshotRequest,
    UpsertPageRequest,
)

router = APIRouter(
    prefix="/other-hand",
    tags=["Otherhand"],
    dependencies=[Depends(require_license)],
)


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
        path = other_hand_service.write_snapshot(workspace_id, page_id, body.png_base64, body.kind)
    except other_hand_service.SnapshotError as exc:
        raise _to_cloud_error(exc) from exc
    return {"path": path, "free_y": body.free_y}


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
    from pocketpaw_ee.cloud.other_hand import illustration_credentials as creds
    from pocketpaw_ee.cloud.other_hand.svg_to_ink import Box

    # Who pays, and whether this may happen at all. The same module the agent's
    # ``illustrate`` tool asks, because these are money rules and two copies of
    # a money rule drift. In short: the workspace's own fal key wins and is
    # never capped by us; without one, an account gets the platform's key under
    # the daily ceiling and a guest is refused — a guest can mint a fresh
    # workspace for a fresh ceiling, so the ceiling alone left an unbounded bill
    # attached to a signup form that asks for nothing.
    grant = await creds.resolve(
        workspace_id,
        is_guest=await guest_budget.load_guest(user_id) is not None,
    )
    if isinstance(grant, creds.IllustrationRefusal):
        if grant.guest_gate:
            from pocketpaw_ee.cloud._core.errors import GuestIllustrateForbidden

            raise GuestIllustrateForbidden()
        # No generator anywhere. An empty op list, not an error: the page
        # carries on and there is nothing useful to tell the user about a
        # credential the operator has not set.
        return {"ops": []}

    # A pressed button authorises ONE generation; it does not cap how many.
    # Scripted, the same button is a loop, so the ceiling has to live here and
    # not in the UI. Claimed BEFORE the paid call and fail-closed — and only on
    # the platform's key, because a workspace spending its own money has no
    # reason to be inside our quota.
    if not grant.byok:
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
            api_key=grant.api_key,
            # Budget claimed above; this flag is the generator's own gate.
            allowed=True,
        )
    except illustrator.IllustrateError as exc:
        # The first refused generation is where a bad stored key becomes
        # visible — there is no save-time check for a fal credential — so stamp
        # the row rather than letting the panel show green over a dead key.
        if grant.byok and _looks_like_auth(exc):
            from pocketpaw_ee.cloud.byok import service as byok_service

            await byok_service.record_image_auth_failure(workspace_id, str(exc))
        raise CloudError(502, "other_hand.illustrate_failed", str(exc)) from exc
    return {"ops": ops}


def _looks_like_auth(exc: Exception) -> bool:
    """Whether a failed generation blames the CREDENTIAL rather than the prompt.

    fal's errors arrive as text through ``IllustrateError``, so this reads the
    message. Deliberately narrow: a false positive marks a good key as broken,
    which sends the user to re-paste a credential that was fine.
    """
    text = str(exc).lower()
    return "401" in text or "403" in text or "unauthorized" in text or "forbidden" in text


# ── Page store ─────────────────────────────────────────────────────────────
#
# Three routes, one resource. The path segment is the CLIENT-MINTED page id —
# the same value the snapshot endpoint above overwrites by, so a page has one id
# everywhere and an existing localStorage page maps 1:1 when the frontend
# switches over.
#
# No DELETE. "New page" in the UI mints a fresh id and leaves the old ink where
# it is, and clearing a page is a PUT with ``strokes: []`` — so a DELETE would be
# a fourth way to say something two routes already say. Add one when a real
# "forget this page" affordance exists to call it.
#
# Auth matches the snapshot endpoint: ``require_license`` on the router plus
# ``current_workspace_id`` (which depends on ``current_active_user``). Tenancy is
# the workspace; any member may read or write their workspace's pages, exactly
# as they may already snapshot them. No new RBAC action is invented.


@router.get("/pages")
async def list_pages(
    workspace_id: str = Depends(current_workspace_id),
    user_id: str = Depends(current_user_id),
    limit: int = Query(default=100, ge=1, le=500),
) -> dict[str, Any]:
    """The workspace's pages, newest first, without their ink."""
    return await other_hand_service.list_pages(workspace_id, user_id, limit)


@router.get("/pages/{page_id}")
async def get_page(
    page_id: str = Path(min_length=1, max_length=PAGE_ID_MAX),
    workspace_id: str = Depends(current_workspace_id),
    user_id: str = Depends(current_user_id),
) -> dict[str, Any]:
    """One page, ink included. 404 when the workspace has no such page."""
    return await other_hand_service.get_page(workspace_id, user_id, page_id)


@router.put("/pages/{page_id}")
async def upsert_page(
    body: UpsertPageRequest,
    page_id: str = Path(min_length=1, max_length=PAGE_ID_MAX),
    workspace_id: str = Depends(current_workspace_id),
    user_id: str = Depends(current_user_id),
) -> dict[str, Any]:
    """Save a page. Returns meta only — never an echo of the ink just sent.

    409 ``other_hand.page_conflict`` when ``base_rev`` does not match the stored
    ``rev``; the error body carries the server's current page so the client can
    reload from the refusal.
    """
    return await other_hand_service.upsert_page(workspace_id, user_id, page_id, body)


__all__ = ["IllustrateRequest", "SnapshotRequest", "router"]
