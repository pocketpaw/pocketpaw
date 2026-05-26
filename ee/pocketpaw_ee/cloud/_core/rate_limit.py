"""Rate-limit Depends factories for cloud routes.

Layered on top of the OSS in-memory limiter from
``pocketpaw.security.rate_limiter`` — no new dependency. The dashboard's
middleware already enforces a per-IP api_limiter on every request; these
deps add a finer-grained per-(actor, resource) bucket so abuse from a
single authenticated actor is bounded even when the IP bucket isn't.

In-memory backing is per-process. A multi-instance backend needs the
Redis-backed Wave 3 limiter; until then a single-instance deploy is the
assumption.
"""

from __future__ import annotations

from fastapi import Depends

from pocketpaw.security.rate_limiter import RateLimiter
from pocketpaw_ee.cloud._core.context import RequestContext, request_context
from pocketpaw_ee.cloud._core.errors import RateLimited

# 50 invites per workspace per actor per day. Burst capped at 50, refill at
# 50/day so a single bad admin can't email-bomb a workspace's domain.
_invite_create_limiter = RateLimiter(rate=50.0 / 86400.0, capacity=50)


async def rate_limit_invite_create(
    workspace_id: str,
    ctx: RequestContext = Depends(request_context),
) -> None:
    """Per-(actor, workspace) bucket guarding POST /workspaces/{id}/invites.

    Raises ``RateLimited`` (CloudError → 429) when the bucket is empty.
    """
    key = f"invite-create:{ctx.user_id}:{workspace_id}"
    info = _invite_create_limiter.check(key)
    if not info.allowed:
        raise RateLimited(
            "workspace.invite_rate_limited",
            "Too many invites created — try again later.",
        )


__all__ = ["rate_limit_invite_create"]
