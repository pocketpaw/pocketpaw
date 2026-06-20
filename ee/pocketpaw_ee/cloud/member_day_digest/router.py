# router.py — Gated GET surface over the member-day digest (Phase B chunk 6).
# Created: 2026-06-08 — VIP Onboarding Phase B chunk 6 (the intent board's
#   read API). Exposes ``GET /api/v1/member-day-digest`` → the AUTHENTICATED
#   caller's OWN ``MemberDayDigest`` so the frontend intent board can render
#   the structured "your day" shape the chunk-5 service already produces.
#
# Per-member isolation (the load-bearing invariant)
# --------------------------------------------------
# ``member_id`` is the authenticated principal (``ctx.user_id``) and NOTHING
# else — there is NO ``member_id`` query/body param, so a caller can ONLY ever
# get their OWN digest. ``workspace_id`` likewise comes from the active-
# workspace context, never the wire. This is the same "tenancy from auth, never
# the wire" stance as ``outcomes/router.py`` and the chat-path briefing gate the
# digest already sits behind: member B can structurally never fetch member A's.
#
# Thin by design (cloud rule §5/§10): parse nothing from the wire, delegate to
# ``member_day_digest.service.member_day_digest``, return the structured DTO.
# Never raises ``HTTPException`` — a missing active workspace is a 400 CloudError
# that ``_core.http`` maps to the standard JSON envelope.

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends

from pocketpaw_ee.cloud._core.context import RequestContext, request_context
from pocketpaw_ee.cloud._core.errors import CloudError
from pocketpaw_ee.cloud.license import require_license
from pocketpaw_ee.cloud.member_day_digest.dto import MemberDayDigest
from pocketpaw_ee.cloud.member_day_digest.service import member_day_digest
from pocketpaw_ee.cloud.shared.deps import require_action_any_workspace

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/member-day-digest",
    tags=["Member Day Digest"],
    dependencies=[Depends(require_license)],
)


@router.get(
    "",
    response_model=MemberDayDigest,
    dependencies=[Depends(require_action_any_workspace("session.read_own"))],
)
async def get_member_day_digest(
    ctx: RequestContext = Depends(request_context),
) -> MemberDayDigest:
    """Return the AUTHENTICATED caller's OWN structured "your day" digest.

    The digest (upcoming calendar + unread/top mail) is built for
    ``ctx.user_id`` — the authenticated principal — and ONLY them. There is no
    ``member_id`` parameter: a caller cannot request another member's digest,
    which is the per-member isolation guarantee carried to the REST door. A
    member with no connected accounts gets an EMPTY digest (never an error).
    """
    # Tenancy from auth context, never the wire. A missing active workspace is
    # a setup error (400), not a denial — surface it before touching the
    # service so a tenant-less call can never collapse into a cross-member read.
    if not ctx.workspace_id:
        raise CloudError(
            400,
            "member_day_digest.no_active_workspace",
            "No active workspace. Create or join a workspace first.",
        )

    # ``member_id`` IS the principal (``ctx.user_id``) — the structural per-
    # member isolation. The service keys its per-user Gmail/Calendar clients on
    # this id, so a second principal resolves a different OAuth-token bucket.
    return await member_day_digest(workspace_id=ctx.workspace_id, member_id=ctx.user_id)


__all__ = ["router"]
