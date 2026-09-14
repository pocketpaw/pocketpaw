"""Recording privileged operator actions.

Created: 2026-09-14 (feat/platform-authority-axis) — chunk 1 of the Paw Admin PRD.

Every privileged write under ``/api/v1/platform`` goes through here. It emits
TWO records for one action, because neither alone is sufficient:

  - a ``PlatformAuditEvent`` document — durable and queryable by target
    workspace, by actor, and in time order. This is what the console reads and
    what answers "what happened to this customer".
  - a ``log_privileged_action`` line on the process audit logger — what an
    external SIEM tails in real time. It cannot be queried by target, and it
    leaves the box, which is precisely the property the document lacks.

``log_privileged_action`` has existed since the RBAC spec landed, is exported
twice, and until now had zero callers — the spec's "always log privileged
actions" boundary was simply unmet. These are its first.

ORDERING. Use ``begin`` before the mutation and ``settle`` after it. See the
``status`` field on the model for why that way round rather than recording once
at the end.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import Request

from pocketpaw_ee.cloud.models.platform_audit import PlatformAuditEvent
from pocketpaw_ee.cloud.models.user import User
from pocketpaw_ee.guards.audit import log_privileged_action

logger = logging.getLogger(__name__)


def _client_ip(request: Request | None) -> str | None:
    """Best-effort caller IP.

    Prefers the leftmost ``X-Forwarded-For`` hop, since every deployment of this
    app sits behind a proxy and ``request.client`` would otherwise record the
    proxy for every operator. Untrusted input — it is evidence for a human
    reading the log, never an input to an authorization decision.
    """
    if request is None:
        return None
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        first = forwarded.split(",")[0].strip()
        if first:
            return first
    return request.client.host if request.client else None


async def begin(
    *,
    operator: User,
    action: str,
    reason: str,
    target_type: str = "",
    target_workspace: str | None = None,
    target_user: str | None = None,
    before: dict[str, Any] | None = None,
    request: Request | None = None,
) -> PlatformAuditEvent:
    """Record an action as ``attempted``, BEFORE performing it.

    Returns the document so the caller can hand it to ``settle``. A row that
    stays at ``attempted`` means the process died mid-action, which is a fact
    worth being able to see rather than an absence to guess at.

    ``reason`` is required all the way down: the model has no default for it, so
    a caller that omits one fails here rather than writing a row that looks
    complete and explains nothing.
    """
    event = PlatformAuditEvent(
        actor_id=str(operator.id),
        actor_email=operator.email or "",
        actor_platform_role=operator.platform_role or "",
        action=action,
        target_type=target_type,
        target_workspace=target_workspace,
        target_user=target_user,
        reason=reason,
        before=before or {},
        after={},
        status="attempted",
        ip=_client_ip(request),
        user_agent=(request.headers.get("user-agent") if request is not None else None),
    )
    await event.insert()
    return event


async def settle(
    event: PlatformAuditEvent,
    *,
    ok: bool,
    after: dict[str, Any] | None = None,
) -> PlatformAuditEvent:
    """Close out a ``begin`` record and emit the structured log line.

    The SIEM line is emitted HERE rather than in ``begin`` so it carries the
    real outcome. A monitoring rule that fires on every attempt, including ones
    that failed validation, trains its readers to ignore it.
    """
    event.status = "applied" if ok else "failed"
    if after:
        event.after = after
    await event.save()

    log_privileged_action(
        actor=event.actor_id,
        action=event.action,
        resource_id=event.target_workspace or event.target_user or "",
        workspace_id=event.target_workspace,
        status="success" if ok else "failure",
        platform_role=event.actor_platform_role,
        reason=event.reason,
        target_type=event.target_type,
        audit_id=str(event.id),
    )
    return event


async def record_read(
    *,
    operator: User,
    action: str,
    query: str,
    target_type: str = "",
    target_workspace: str | None = None,
    target_user: str | None = None,
    request: Request | None = None,
) -> PlatformAuditEvent:
    """Record a cross-tenant READ.

    Reads need a trail for the same reason writes do, and arguably more: the
    common abuse of an operator account is not moving money, it is looking at
    things. A support operator can enumerate every tenant and pull every
    member's email address, and ``request_logs`` records only the ROUTE
    TEMPLATE — so the query string, which is the part that says whose data was
    read, is otherwise written down nowhere.

    ``reason`` on a read row is MACHINE-GENERATED and describes the query,
    because a read has no operator-supplied justification to capture. That is a
    real difference from a write row, where the reason is a human's and is
    required. Both land in the same collection; ``action`` tells them apart, and
    the read actions all end in ``.read``.

    Volume: this writes one row per cross-tenant read, on a collection with no
    TTL. That is deliberate for now — an operator log that forgets is not a
    log — but it is the main reason the retention decision this PRD defers
    should not stay deferred forever.
    """
    return await record(
        operator=operator,
        action=action,
        reason=f"read: {query}" if query else "read",
        target_type=target_type,
        target_workspace=target_workspace,
        target_user=target_user,
        request=request,
    )


async def record(
    *,
    operator: User,
    action: str,
    reason: str,
    target_type: str = "",
    target_workspace: str | None = None,
    target_user: str | None = None,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    request: Request | None = None,
) -> PlatformAuditEvent:
    """One-shot record for an action that cannot partially fail.

    Convenience over ``begin`` + ``settle`` for actions with nothing to settle —
    a read that still warrants a trail, or a write whose effect is the document
    itself. Anything that moves money or changes entitlements should use the
    two-step form so a crash between the two is visible.
    """
    event = await begin(
        operator=operator,
        action=action,
        reason=reason,
        target_type=target_type,
        target_workspace=target_workspace,
        target_user=target_user,
        before=before,
        request=request,
    )
    return await settle(event, ok=True, after=after)


__all__ = ["begin", "record", "record_read", "settle"]
