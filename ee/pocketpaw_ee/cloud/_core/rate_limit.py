"""Rate-limit Depends factories for cloud routes.

Layered on the OSS in-memory ``RateLimiter`` (no new dependency). The dashboard
middleware already enforces a coarse per-IP limit on every request; the deps
here add finer per-(actor, resource) or per-IP buckets for routes that need
them: workspace invites, the social exchange code, slug checks, meeting lookup
and knocks, the public Discover and partner reads, the public AI check. New
public routes build theirs with ``per_ip_limit`` instead of copying a function.

Client address rule (``client_ip``): the RIGHTMOST ``X-Forwarded-For`` entry,
which is the hop the single trusted proxy (Traefik) appended; the leftmost is
caller-chosen and keying on it is a bypass. Values are validated as IPs so a
junk header never becomes a bucket key. One exception, OPT-IN PER LIMITER
(``trusted_header_ok=True``): when ``Settings.public_web_key``
(``POCKETPAW_PUBLIC_WEB_KEY``) is set and a request carries ``X-Paw-Web-Key``
equal to it (constant-time compare) plus a valid ``X-Paw-Client-IP``, that IP
is the address. That is how the paw-web Worker, whose every request would
otherwise share one Cloudflare egress bucket, passes the visitor through. Only
the limiters on routes the Worker fronts opt in (the public partner reads and
apply, the public Discover reads); the auth exchange, meetings, the AI check
and everything else never read the header, so a leaked key cannot pick their
buckets. Rotation: set the new key on the Worker and here, redeploy both; there
is no dual-key window.

Buckets are per-process. A multi-instance backend needs the Redis-backed
limiter; a single-instance deploy is the assumption today.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from hmac import compare_digest
from ipaddress import ip_address

from fastapi import Depends, Request

from pocketpaw.security.rate_limiter import RateLimiter
from pocketpaw_ee.cloud._core.context import RequestContext, request_context
from pocketpaw_ee.cloud._core.deps import current_user_id
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


# 30 address-availability checks per minute per user. A rename field checks as the
# owner types, so the burst is generous; the refill still stops a script walking the
# namespace to learn which site addresses exist.
_slug_check_limiter = RateLimiter(rate=30.0 / 60.0, capacity=30)


# 30 meeting-code lookups per minute per IP. The lookup is public (the /m/<code>
# page calls it before sign-in); a person opening links needs a handful, a script
# guessing codes gets 30 tries a minute out of ~4e13.
_meeting_lookup_limiter = RateLimiter(rate=30.0 / 60.0, capacity=30)

# 60 public Discover reads per minute per IP. The index is browsed before sign-in;
# a person paging and opening cards needs a few dozen, a scraper gets 60 a minute.
_discover_public_limiter = RateLimiter(rate=60.0 / 60.0, capacity=60)

# 5 free AI checks per hour per IP. Each check is six paid engine calls, so a person
# trying a few spellings of their business fits; a script does not (the daily spend
# cap in ai_visibility is the backstop).
_ai_check_public_limiter = RateLimiter(rate=5.0 / 3600.0, capacity=5)

# 10 Discover reports per hour per user, across every listing. Three reporters
# hide a listing, so a person reporting what they see needs a handful; a sock
# account sweeping the index gets 10 an hour.
_discover_report_limiter = RateLimiter(rate=10.0 / 3600.0, capacity=10)

# Public partner directory / profile reads: same shape as the Discover reads.
_partner_public_limiter = RateLimiter(rate=60.0 / 60.0, capacity=60)
# Partner applications: each one lands a proposal in front of an operator. A
# person applies once; a script gets 5 an hour.
_partner_apply_limiter = RateLimiter(rate=5.0 / 3600.0, capacity=5)

# Knocks (a guest asking to join). Per IP: a guest knocks once, maybe again after
# a denial. Per code: every knock puts a card in front of the people in the call,
# so one meeting can't be flooded by many addresses either.
_meeting_knock_ip_limiter = RateLimiter(rate=10.0 / 60.0, capacity=10)
_meeting_knock_code_limiter = RateLimiter(rate=30.0 / 60.0, capacity=30)
# The waiting screen polls every 2s (30/min); 120 leaves room for a few guests
# behind one NAT.
_meeting_knock_poll_limiter = RateLimiter(rate=120.0 / 60.0, capacity=120)


def _client_ip(request: Request, *, trusted_header_ok: bool = False) -> str:
    """Best available client address for rate-limit bucketing.

    With ``trusted_header_ok`` (only the limiters on Worker-fronted public
    routes pass it), a request carrying the shared key and a valid
    ``X-Paw-Client-IP`` is bucketed on that address. Everything else uses the
    RIGHTMOST ``X-Forwarded-For`` entry: a proxy APPENDS the address it saw, so
    only the last element is trustworthy and keying on the first let an
    attacker pick a fresh bucket per request. Exactly one trusted proxy is
    assumed (Traefik for a single backend); a second hop wants real
    trusted-proxy configuration, tracked in the operability audit.
    """
    if trusted_header_ok:
        trusted = _trusted_client_ip(request)
        if trusted:
            return trusted
    peer = request.client.host if request.client else "unknown"
    forwarded = request.headers.get("x-forwarded-for", "")
    if not forwarded:
        return peer
    candidate = forwarded.rsplit(",", 1)[-1].strip()
    if not candidate:
        return peer
    try:
        return str(ip_address(candidate))
    except ValueError:
        return peer


def _trusted_client_ip(request: Request) -> str | None:
    """``X-Paw-Client-IP`` when the request proves it came from the public site."""
    from pocketpaw.config import get_settings

    key = str(getattr(get_settings(), "public_web_key", None) or "").strip()
    if not key:
        return None
    sent = request.headers.get("x-paw-web-key", "")
    if not compare_digest(sent.encode(), key.encode()):
        return None
    try:
        return str(ip_address(request.headers.get("x-paw-client-ip", "").strip()))
    except ValueError:
        return None


def client_ip(request: Request, *, trusted_header_ok: bool = False) -> str:
    """Public name for ``_client_ip`` (rightmost XFF; the Worker header only on opt-in)."""
    return _client_ip(request, trusted_header_ok=trusted_header_ok)


def per_ip_limit(
    limiter: RateLimiter,
    *,
    prefix: str,
    code: str,
    message: str,
    trusted_header_ok: bool = False,
) -> Callable[[Request], Awaitable[None]]:
    """A per-IP ``Depends`` over ``limiter``: 429 ``code`` when the bucket is empty.
    ``trusted_header_ok`` only for a route the paw-web Worker fronts."""

    async def dep(request: Request) -> None:
        addr = _client_ip(request, trusted_header_ok=trusted_header_ok)
        if not limiter.check(f"{prefix}:{addr}").allowed:
            raise RateLimited(code, message)

    dep.__name__ = f"rate_limit_{prefix.replace('-', '_')}"
    return dep


rate_limit_partner_public = per_ip_limit(
    _partner_public_limiter,
    prefix="partner-public",
    code="partners.rate_limited",
    message="Too many requests - wait a moment and try again.",
    trusted_header_ok=True,
)
rate_limit_partner_apply = per_ip_limit(
    _partner_apply_limiter,
    prefix="partner-apply",
    code="partners.apply_rate_limited",
    message="Too many applications from here - try again in an hour.",
    trusted_header_ok=True,
)


async def rate_limit_social_exchange(request: Request) -> None:
    """Per-IP bucket guarding POST /auth/social/exchange."""
    client = _client_ip(request)
    if not _social_exchange_limiter.check(f"social-exchange:{client}").allowed:
        raise RateLimited(
            "social.exchange_rate_limited",
            "Too many attempts - try again shortly.",
        )


async def rate_limit_meeting_lookup(request: Request) -> None:
    """Per-IP bucket guarding GET /meetings/by-code/{code} (unauthenticated)."""
    if not _meeting_lookup_limiter.check(f"meeting-lookup:{_client_ip(request)}").allowed:
        raise RateLimited(
            "meetings.lookup_rate_limited",
            "Too many meeting lookups - wait a moment and try again.",
        )


async def rate_limit_discover_public(request: Request) -> None:
    """Per-IP bucket guarding the public Discover reads (unauthenticated; paw-web fronts them)."""
    addr = _client_ip(request, trusted_header_ok=True)
    if not _discover_public_limiter.check(f"discover-public:{addr}").allowed:
        raise RateLimited(
            "discover.rate_limited",
            "Too many requests - wait a moment and try again.",
        )


async def rate_limit_ai_check_public(request: Request) -> None:
    """Per-IP bucket guarding the public POST /tools/ai-check (5/hour)."""
    if not _ai_check_public_limiter.check(f"ai-check-public:{_client_ip(request)}").allowed:
        raise RateLimited(
            "tools.ai_check.rate_limited",
            "Too many checks from here - try again in an hour.",
        )


async def rate_limit_discover_report(user_id: str = Depends(current_user_id)) -> None:
    """Per-user bucket guarding POST /discover/{id}/report (10/hour)."""
    if not _discover_report_limiter.check(f"discover-report:{user_id}").allowed:
        raise RateLimited(
            "discover.report_rate_limited",
            "Too many reports - try again later.",
        )


def _knock_limited() -> RateLimited:
    return RateLimited(
        "meetings.knock_rate_limited",
        "Too many requests to join - wait a moment and try again.",
    )


async def rate_limit_meeting_knock(request: Request) -> None:
    """Per-IP and per-code buckets guarding POST /meetings/by-code/{code}/knock."""
    if not _meeting_knock_ip_limiter.check(f"meeting-knock:{_client_ip(request)}").allowed:
        raise _knock_limited()
    # One bucket per code however it's spelled; capped so junk paths can't mint
    # unbounded keys (the per-IP check above already ran).
    code = str(request.path_params.get("code", "")).replace("-", "").strip().lower()[:16]
    if not _meeting_knock_code_limiter.check(f"meeting-knock-code:{code}").allowed:
        raise _knock_limited()


async def rate_limit_meeting_knock_poll(request: Request) -> None:
    """Per-IP bucket guarding the guest's knock status poll and cancel."""
    if not _meeting_knock_poll_limiter.check(f"meeting-knock-poll:{_client_ip(request)}").allowed:
        raise _knock_limited()


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


async def rate_limit_slug_check(ctx: RequestContext = Depends(request_context)) -> None:
    """Per-user bucket guarding GET /sites/slug-available (VS-4)."""
    if not _slug_check_limiter.check(f"slug-check:{ctx.user_id}").allowed:
        raise RateLimited(
            "sites.slug_check_rate_limited",
            "Too many address checks - wait a moment and try again.",
        )


__all__ = [
    "client_ip",
    "consume_invite_create_tokens",
    "rate_limit_ai_check_public",
    "rate_limit_discover_public",
    "rate_limit_discover_report",
    "rate_limit_invite_create",
    "rate_limit_invite_resend",
    "rate_limit_meeting_knock",
    "rate_limit_meeting_knock_poll",
    "rate_limit_meeting_lookup",
    "rate_limit_partner_apply",
    "rate_limit_partner_public",
    "rate_limit_slug_check",
    "per_ip_limit",
]
