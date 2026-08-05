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

from fastapi import Depends, Request

from pocketpaw.security.rate_limiter import RateLimiter
from pocketpaw_ee.cloud._core.context import RequestContext, request_context
from pocketpaw_ee.cloud._core.errors import RateLimited

# 50 invites per workspace per actor per day. Burst capped at 50, refill at
# 50/day so a single bad admin can't email-bomb a workspace's domain.
_invite_create_limiter = RateLimiter(rate=50.0 / 86400.0, capacity=50)

# 5 resends per 30 minutes per invite. Keyed on invite_id rather than actor
# so an admin can't sidestep by rotating between teammates.
_invite_resend_limiter = RateLimiter(rate=5.0 / 1800.0, capacity=5)


# 10 social exchange-code redemptions per minute per IP. The code is already
# single-use with a 60s TTL and 32 bytes of entropy, so this is defence in
# depth against someone spraying guesses rather than the primary control.
# Keyed on IP because the endpoint is UNAUTHENTICATED by nature - it is how a
# desktop client turns a callback into its first token, so there is no actor
# to key on yet.
_social_exchange_limiter = RateLimiter(rate=10.0 / 60.0, capacity=10)


async def rate_limit_social_exchange(request: Request) -> None:
    """Per-IP bucket guarding POST /auth/social/exchange."""
    client = request.client.host if request.client else "unknown"
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        # Left-most entry is the originating client when behind a proxy.
        client = forwarded.split(",")[0].strip() or client
    if not _social_exchange_limiter.check(f"social-exchange:{client}").allowed:
        raise RateLimited(
            "social.exchange_rate_limited",
            "Too many attempts - try again shortly.",
        )


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


def consume_invite_create_tokens(user_id: str, workspace_id: str, count: int) -> None:
    """Consume ``count`` tokens from the invite-create bucket for this
    (actor, workspace). Used by the bulk-invite route, where the batch
    size isn't known at Depends-resolution time so the limiter has to be
    checked manually inside the handler. Each email in the batch consumes
    one token, so the same 50/day cap covers batches too — a 100-email
    paste effectively spends two days of budget. That's intentional: bulk
    is for one-off onboarding, not steady-state invite traffic.

    Atomic: if the bucket can't cover the full batch we raise without
    consuming anything, so a failing batch doesn't silently drain the
    day's budget for the rest of the workspace.
    """
    key = f"invite-create:{user_id}:{workspace_id}"
    info = _invite_create_limiter.try_consume(key, count)
    if not info.allowed:
        raise RateLimited(
            "workspace.invite_rate_limited",
            "Too many invites created — try again later.",
        )


async def rate_limit_invite_resend(
    workspace_id: str,
    invite_id: str,
    ctx: RequestContext = Depends(request_context),
) -> None:
    """Per-invite bucket guarding POST /workspaces/{id}/invites/{invite_id}/resend.

    Keyed on invite_id rather than actor: the resend is meant to refresh a
    plaintext for the inviter's clipboard, not a re-mail blast surface.
    """
    key = f"invite-resend:{invite_id}"
    info = _invite_resend_limiter.check(key)
    if not info.allowed:
        raise RateLimited(
            "workspace.invite_resend_rate_limited",
            "Too many resends — wait before retrying.",
        )


__all__ = [
    "consume_invite_create_tokens",
    "rate_limit_invite_create",
    "rate_limit_invite_resend",
]
