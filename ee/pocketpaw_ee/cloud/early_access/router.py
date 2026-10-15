# ee/pocketpaw_ee/cloud/early_access/router.py: the public early-access routes,
# mounted at ``/api/v1``. No sign-in: a visitor who meets paused signups has no
# account yet. Rate-limited per IP.

from __future__ import annotations

from fastapi import APIRouter, Depends, Response

from pocketpaw_ee.cloud._core.rate_limit import rate_limit_early_access_waitlist
from pocketpaw_ee.cloud.early_access import service
from pocketpaw_ee.cloud.early_access.dto import WaitlistJoinRequest

router = APIRouter(prefix="/early-access", tags=["early-access"])


@router.post("/waitlist", status_code=204, dependencies=[Depends(rate_limit_early_access_waitlist)])
async def join_waitlist(body: WaitlistJoinRequest) -> Response:
    """PUBLIC. Join the early-access waitlist. 204 whether or not the email was
    already on it; 429 ``early_access.waitlist_rate_limited`` past 5 an hour."""
    await service.join_waitlist(body)
    return Response(status_code=204)


__all__ = ["router"]
