# ee/pocketpaw_ee/cloud/early_access/router.py: the early-access routes, mounted
# at ``/api/v1``. The waitlist takes no sign-in (a visitor who meets paused
# signups has no account yet) and is rate-limited per IP. Notify-me needs a
# session and is idempotent, so it carries no limiter.

from __future__ import annotations

from fastapi import APIRouter, Depends, Response

from pocketpaw_ee.cloud._core.context import RequestContext, request_context
from pocketpaw_ee.cloud._core.rate_limit import rate_limit_early_access_waitlist
from pocketpaw_ee.cloud._core.time import iso_utc
from pocketpaw_ee.cloud.early_access import service
from pocketpaw_ee.cloud.early_access.dto import NotifyPlansResponse, WaitlistJoinRequest

router = APIRouter(prefix="/early-access", tags=["early-access"])


@router.post("/waitlist", status_code=204, dependencies=[Depends(rate_limit_early_access_waitlist)])
async def join_waitlist(body: WaitlistJoinRequest) -> Response:
    """PUBLIC. Join the early-access waitlist. 204 whether or not the email was
    already on it; 429 ``early_access.waitlist_rate_limited`` past 5 an hour."""
    await service.join_waitlist(body)
    return Response(status_code=204)


@router.post("/notify", response_model=NotifyPlansResponse)
async def notify_plans(ctx: RequestContext = Depends(request_context)) -> NotifyPlansResponse:
    """Signed-in user asks to hear when plans open. Sets
    ``User.notify_plans_at`` on the first call; every call returns that time."""
    return NotifyPlansResponse(notified_at=iso_utc(await service.notify_plans(ctx.user_id)))


__all__ = ["router"]
