"""Canonical error hierarchy for ee/cloud.

Routers must never raise `HTTPException`; services raise these
`CloudError` subclasses and `_core.http.cloud_error_handler` maps them
to JSON responses.

Re-exports remain accessible via `ee.cloud.shared.errors` (a shim) for
the transition period; new code should import from this module.

Changed 2026-06-30 (feat/billing-quota-enforcement, chunk 2): added
`QuotaExceeded` (402, `credits.quota_exceeded`) — the monthly-credit-cap
sibling of `InsufficientCredits`. Same 402 status (the client can't spend
right now) but a distinct code so the UI can prompt a plan upgrade / top-up
rather than a balance refill; it carries the effective `ceiling` and the
`spent` figure that crossed it.

FL-2 (file-version spine, port of dewani12's #1193) added
``PreconditionFailed`` (412) for stale ``If-Match`` optimistic-concurrency
failures on the file-version write path.

Changed 2026-07-08 (feat/billing-cancel-downgrade): added
``NoActiveSubscription`` (402, ``billing.no_active_subscription``) — raised by
the subscription-cancel path when a workspace has no ``active`` subscription to
cancel (only historical / already-cancelled rows, or never subscribed). 402 (the
same money-error family as ``InsufficientCredits`` / ``QuotaExceeded``) so the
client renders a "not subscribed" state rather than a 404-style missing resource.

Changed 2026-07-08 (feat/billing-smb-caps): added ``PocketLimitError`` (402,
``billing.pocket_limit``) and ``ConnectorLimitError`` (402,
``billing.connector_limit``) — the pocket-create and connector-enable siblings of
``SeatLimitError``. Same 402 money-adjacent family; distinct codes so the UI can
prompt a plan upgrade. Both enforce at create/enable time only (never retroactive)
and only when ``billing_enforced`` is on.

Changed 2026-08-26 (feat/site-plans-as-addons): ``NoActiveSubscription`` now takes
an optional ``message``. It gained a SECOND caller — the site add-on rail, where a
workspace with no subscription cannot be sold a paid site because an add-on has
nothing to attach to — and the hardcoded "no active subscription to cancel"
described an action that buyer never attempted. Status, code and default message
are unchanged, so the cancel path and any client keyed on the code are untouched.

Changed 2026-08-15 (fix/sites-custom-domain-entitlement): added
``CustomDomainNotEntitled`` (402, ``billing.custom_domain_not_entitled``) — the
domain-attach sibling of the two above, and the first of this family keyed to a
PER-SITE plan rather than the workspace one. Not a count limit: it reports a
capability the site's tier does not resell, or a paid tier whose subscription is
not paying. Attach-time only, gated on ``billing_enforced``.
"""

from __future__ import annotations


class CloudError(Exception):
    """Base cloud error with status_code, code (machine-readable),
    message (human-readable)."""

    def __init__(self, status_code: int, code: str, message: str) -> None:
        self.status_code = status_code
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")

    def to_dict(self) -> dict:
        """Return a JSON-serializable error envelope."""
        return {"error": {"code": self.code, "message": self.message}}


class NotFound(CloudError):
    """Resource not found (404)."""

    def __init__(self, resource: str, resource_id: str = "") -> None:
        code = f"{resource}.not_found"
        if resource_id:
            message = f"{resource} '{resource_id}' not found"
        else:
            message = f"{resource} not found"
        super().__init__(404, code, message)


class Forbidden(CloudError):
    """Access denied (403)."""

    def __init__(self, code: str, message: str = "Access denied") -> None:
        super().__init__(403, code, message)


class ConflictError(CloudError):
    """Resource conflict (409)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(409, code, message)


class PreconditionFailed(CloudError):
    """A conditional request precondition failed (412).

    Standard HTTP ``If-Match`` optimistic-concurrency semantics: the caller
    supplied an ``If-Match`` etag (here the file's ``content_version``) that no
    longer matches the current resource, so the write is refused. Distinct from
    ``ConflictError`` (409, a generic resource conflict) — a stale ``If-Match``
    is precisely a 412. Added by FL-2 (file-version spine, port of #1193) so a
    stale-etag PUT/revert returns 412.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(412, code, message)


class ValidationError(CloudError):
    """Validation failure (422)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(422, code, message)


class BadRequest(CloudError):
    """Malformed / untrusted request (400).

    Distinct from ``ValidationError`` (422, a well-formed request that fails a
    field-level rule): a 400 is for a request that can't be trusted at all — a
    webhook whose signature doesn't verify, a body that isn't parseable. Used by
    the billing webhook so an unverifiable Dodo delivery returns 400, not 422.
    """

    def __init__(self, code: str, message: str = "Bad request") -> None:
        super().__init__(400, code, message)


class SeatLimitError(CloudError):
    """Seat/billing limit reached (402)."""

    def __init__(self, seats: int) -> None:
        super().__init__(402, "billing.seat_limit", f"Seat limit of {seats} reached")


class PocketLimitError(CloudError):
    """Workspace hit its plan's pocket cap (402).

    Sibling of ``SeatLimitError`` for the pocket-create seam: the workspace holds
    its plan's ``max_pockets`` and a new create would exceed it. 402 (same
    money-adjacent family) with a distinct ``billing.pocket_limit`` code so the UI
    can prompt a plan upgrade. Enforced at CREATE time only — never removes an
    existing pocket — and only when ``billing_enforced`` is on.
    """

    def __init__(self, limit: int) -> None:
        super().__init__(402, "billing.pocket_limit", f"Pocket limit of {limit} reached")


class ConnectorLimitError(CloudError):
    """Workspace hit its plan's connector cap (402).

    Sibling of ``SeatLimitError`` for the connector-enable seam: the workspace
    already has its plan's ``max_connectors`` enabled and enabling another would
    exceed it. 402 with a distinct ``billing.connector_limit`` code so the UI can
    prompt a plan upgrade. Enforced at ENABLE time only — never disables an
    already-enabled connector — and only when ``billing_enforced`` is on.
    """

    def __init__(self, limit: int) -> None:
        super().__init__(402, "billing.connector_limit", f"Connector limit of {limit} reached")


class CustomDomainNotEntitled(CloudError):
    """A site tried to attach a custom domain its per-site plan does not grant (402).

    Sibling of ``PocketLimitError`` / ``ConnectorLimitError`` for the domain-attach
    seam, and the same money-adjacent 402 family, with a distinct
    ``billing.custom_domain_not_entitled`` code so the UI prompts a per-SITE plan
    upgrade rather than a workspace one.

    The distinction from its siblings is that this is not a COUNT limit: there is no
    ceiling to report, only a capability the site's tier either grants or does not.
    ``CustomDomainLimitError`` below is the count sibling, and since 2026-08-21 it
    is the one that normally fires — the free floor grants one domained site, so a
    tier granting NONE only exists if the catalog is edited to say so, or if a
    resolve lands on a tier whose allowance is 0. The class stays because that is
    exactly the fail-closed case worth having a distinct code for.
    Two different failures land here and the message separates them, because the
    remedies differ — a free site upgrades its plan, while a paid site whose
    subscription lapsed fixes its billing and keeps the tier it already chose.

    Enforced at ATTACH time only — never retroactive. A downgrade does not rip a
    live domain off a deployed site, and re-adding an already-connected domain
    (the only self-service repair for a missing Worker route) stays reachable. The
    spec's period-end detach is a different lane. Gated on ``billing_enforced``,
    so OSS / self-host never sees it.
    """

    def __init__(self, *, plan_tier: str, subscription_active: bool) -> None:
        if subscription_active:
            detail = (
                f"the {plan_tier} plan does not include a custom domain — "
                "upgrade this site's plan to connect one"
            )
        else:
            detail = (
                f"this site is on {plan_tier} without an active subscription — "
                "renew it to connect a custom domain"
            )
        super().__init__(402, "billing.custom_domain_not_entitled", f"Custom domain: {detail}")


class CustomDomainLimitError(CloudError):
    """Workspace hit its plan's cap on how many SITES may carry a custom domain (402).

    The COUNT sibling of ``CustomDomainNotEntitled``, and a separate class because
    the remedies differ: that one means "this site's subscription is not paying",
    this one means "you have used the allowance, upgrade a site to add another".
    A UI that collapses them tells a paying customer to renew a subscription that
    never lapsed.

    **The unit is the SITE, not the hostname.** A workspace on the free floor gets
    one site carrying custom domains; apex and ``www`` both sit on that site and
    spend one allowance between them. ``scope="site"`` reports the OTHER cap — how
    many hostnames one floor-tier site may carry — which exists only because the
    site-unit cap leaves that number unbounded.

    Enforced at ATTACH time only, never retroactive: an existing domain is never
    detached, and re-adding an already-connected hostname (the only self-service
    repair for a missing Worker route) never reaches this check. Gated on
    ``billing_enforced``, so OSS / self-host never sees it.
    """

    def __init__(self, *, limit: int, scope: str = "workspace") -> None:
        if scope == "site":
            detail = (
                f"this site already has {limit} custom hostnames, which is the limit "
                "on the free plan — upgrade it to connect more"
            )
        else:
            noun = "site" if limit == 1 else "sites"
            detail = (
                f"custom domains are included on {limit} {noun} on your plan, and "
                f"{'that one is' if limit == 1 else 'those are'} already in use — "
                "upgrade a site's plan to connect another"
            )
        super().__init__(402, "billing.custom_domain_limit", f"Custom domain: {detail}")


class CallLimitError(CloudError):
    """Workspace hit its plan's daily LiveKit call-time cap (402).

    Sibling of ``SeatLimitError`` for the LiveKit room-create seam: Free has no
    call minutes at all (``max_call_seconds_per_day`` == 0), and a paid tier
    blocks a NEW call once today's cumulative call time would exceed its daily
    budget. 402 with a distinct ``billing.call_limit`` code so the UI can prompt
    a plan upgrade. Enforced at CALL-START time only — an already-running call is
    force-ended at its budget deadline rather than removed.
    """

    def __init__(self, limit_seconds: int | None) -> None:
        if limit_seconds == 0:
            label = "no call minutes on your plan"
        elif limit_seconds is not None:
            label = f"daily call limit of {limit_seconds // 60} minutes reached"
        else:  # pragma: no cover - uncapped plans never raise
            label = "daily call limit reached"
        super().__init__(402, "billing.call_limit", f"Call limit: {label}")


def _human_bytes(n: int) -> str:
    """Format a byte count the way storage products do (GB for this scale)."""
    if n % 1_000_000_000 == 0:
        return f"{n // 1_000_000_000} GB"
    if n % 1_000_000 == 0:
        return f"{n // 1_000_000} MB"
    if n % 1_000 == 0:
        return f"{n // 1_000} KB"
    return f"{n} bytes"


class StorageLimitError(CloudError):
    """Workspace hit its plan's S3 storage cap (402).

    Sibling of ``SeatLimitError`` for the UPLOAD seam: the sum of the workspace's
    live ``FileUpload`` blob sizes (the Files → Knowledge Base store) is at its
    plan's ``max_storage_bytes`` and the new upload would push it over. 402 with
    a distinct ``billing.storage_limit`` code so the UI can prompt a plan
    upgrade. Enforced at UPLOAD time only — never deletes an existing blob — and
    only when ``billing_enforced`` is on.
    """

    def __init__(self, limit_bytes: int | None) -> None:
        if limit_bytes is None:  # pragma: no cover - uncapped plans never raise
            label = "storage limit reached"
        else:
            label = f"storage limit of {_human_bytes(limit_bytes)} reached"
        super().__init__(402, "billing.storage_limit", f"Storage limit: {label}")


class InsufficientCredits(CloudError):
    """Credit wallet has too few credits for the requested debit (402)."""

    def __init__(self, requested: int, available: int) -> None:
        super().__init__(
            402,
            "credits.insufficient",
            f"Insufficient credits: requested {requested}, available {available}",
        )


class QuotaExceeded(CloudError):
    """Workspace hit its monthly credit ceiling for the period (402).

    Distinct from ``InsufficientCredits`` (the wallet is empty): the wallet may
    still hold credits, but the workspace has spent up to its plan's monthly cap
    (the ``monthly_ceiling`` entitlement, extended by any purchased top-ups in the
    period). Same 402 status as ``InsufficientCredits`` so the client treats both
    as "can't spend right now", but a distinct machine code so the UI can prompt a
    plan upgrade / top-up rather than just a balance refill. ``ceiling`` is the
    EFFECTIVE cap (plan ceiling + period top-ups) and ``spent`` is the
    month-to-date spend that met or crossed it.
    """

    def __init__(self, ceiling: int, spent: int) -> None:
        self.ceiling = ceiling
        self.spent = spent
        super().__init__(
            402,
            "credits.quota_exceeded",
            f"Monthly credit quota exceeded: spent {spent} of {ceiling} this month",
        )


class NoActiveSubscription(CloudError):
    """Workspace has no active recurring subscription to act on (402).

    Raised by the cancel path when a workspace asks to cancel but has no ``active``
    subscription row — only historical / already-cancelled rows, or it never
    subscribed. 402 (the same family as ``InsufficientCredits`` / ``QuotaExceeded``,
    "you can't do the money action right now") so the client prompts a
    not-subscribed state rather than treating it as a 404-style missing resource.

    ``message`` is overridable because there are now TWO callers and the default
    names only one of them. The site add-on rail raises this when a workspace with
    no subscription tries to buy a paid site — the same 402, the same code, and a
    client that reads the code keeps working — but telling that buyer there is
    "no active subscription to cancel" describes an action they did not attempt.
    The default is unchanged, so the cancel path and every existing test are
    untouched.
    """

    def __init__(
        self, message: str = "No active subscription to cancel for this workspace"
    ) -> None:
        super().__init__(
            402,
            "billing.no_active_subscription",
            message,
        )


class RateLimited(CloudError):
    """Rate limit exceeded (429)."""

    def __init__(self, code: str, message: str = "Rate limit exceeded") -> None:
        super().__init__(429, code, message)


class Internal(CloudError):
    """Unexpected internal error (500). Use sparingly — prefer specific codes."""

    def __init__(self, code: str = "internal", message: str = "Internal server error") -> None:
        super().__init__(500, code, message)


def with_cause(error: CloudError, cause: BaseException) -> CloudError:
    """Attach an underlying exception for log context. Returns the same error
    so it can be used fluently: `raise with_cause(NotFound(...), exc)`.

    The cause is stored on `__cause__` (Python's standard exception-chaining
    slot). The `to_dict()` envelope sent to clients still contains only
    `code` and `message`; the cause is not leaked.
    """
    error.__cause__ = cause
    return error


__all__ = [
    "BadRequest",
    "CallLimitError",
    "CloudError",
    "ConflictError",
    "ConnectorLimitError",
    "Forbidden",
    "InsufficientCredits",
    "Internal",
    "NoActiveSubscription",
    "NotFound",
    "PocketLimitError",
    "PreconditionFailed",
    "QuotaExceeded",
    "RateLimited",
    "SeatLimitError",
    "StorageLimitError",
    "ValidationError",
    "with_cause",
]
