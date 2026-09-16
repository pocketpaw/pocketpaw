"""Cross-tenant wallet reads and operator adjustments.

Created 2026-09-16 (feat/platform-credits) — chunk 6 of the Paw Admin PRD, per
docs/design/drafts/2026-09-15-paw-admin-screen-credits.md §3 (the binding data
contract for this route set; every DTO and ordering rule below is taken from it
verbatim).

Four routes, mounted under the same ``/workspaces`` prefix as
``platform/workspaces.py``: a wallet read and a ledger-history read at
``platform.credits.read`` (SUPPORT), an adjustment and a reconcile at
``platform.credits.adjust`` (OPERATOR). Both action keys already exist in
``PLATFORM_ACTIONS`` (``ee/pocketpaw_ee/guards/platform.py``) — nothing new was
added there, per the spec.

MONEY IS MICRO-CREDITS END TO END. Every amount on this wire is
``*_micro`` (1_000_000 micro == 1 credit == $0.01). There is no
``balance_credits`` field anywhere in this module's DTOs, on purpose — see
the spec's §7: a whole-credit field on this screen is how an operator claws
back 1 credit intending to correct a 375,000-micro (0.375 credit) discrepancy,
silently over-refunding by 2.7x.

MUTATIONS GO THROUGH ``credits.service``, NEVER A DIRECT LEDGER WRITE (master
PRD C6). ``grant``/``debit`` own the exactly-once insert, the atomic
``$inc``/CAS, and the ``applied``/``conditional`` bookkeeping that
``reconcile`` depends on; a direct ``CreditLedgerEntry.insert()`` or a raw
``$inc`` on ``CreditBalance`` here would desynchronize the stored balance from
the ledger's own definition of it (sum of applied deltas) in a way the next
reconcile "repairs" toward the bad row, irreversibly.

DEVIATION FROM THE SPEC, DELIBERATE, DOCUMENTED HERE AS INSTRUCTED: §3 asks for
a ``DebitResult(balance, created)`` mirroring ``GrantResult``, so a claw-back's
replay could be read directly off the return value the way a grant's already
is. That was NOT bundled into this chunk. ``credits.service.debit`` is called
from ``billing/service.py`` (twice, one of which does
``return await credits_service.debit(...)``, propagating the bare-int return
type up a second layer of callers), ``llm_provisioning/service.py`` and
``metering/service.py`` — all revenue/metering hot paths well outside this
chunk's scope, and none of them were touched here. Changing ``debit``'s return
type would have required auditing and updating all of them in the same PR.

Instead, this route calls the pre-existing ``credits.service.is_recorded``
BEFORE calling ``debit`` and uses that as the ``replayed`` signal for a
claw-back. This is check-then-act and has a small, explicitly bounded race: a
genuinely concurrent duplicate request could see "not yet recorded" on both
sides of the check, in which case BOTH read ``replayed=False`` even though only
one of them can actually apply — the ledger's unique
``(workspace, idempotency_key)`` index still guarantees exactly one insert
lands; the other's call to ``debit`` transparently no-ops (catches
``DuplicateKeyError`` internally and returns the current balance). The race can
only ever produce a wrong ``replayed`` flag in the HTTP response, never a
double-applied movement. A grant's ``replayed`` flag has no such race — it
reads directly off ``GrantResult.created``, which is authoritative because the
service computes it from the insert's own success/failure.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel

from pocketpaw_ee.cloud._core.errors import CloudError, InsufficientCredits, ValidationError
from pocketpaw_ee.cloud._core.platform_deps import require_platform
from pocketpaw_ee.cloud.credits import service as credits_service
from pocketpaw_ee.cloud.credits.domain import LedgerEntry
from pocketpaw_ee.cloud.models.user import User
from pocketpaw_ee.cloud.platform import audit

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/workspaces", tags=["platform"])


# ---------------------------------------------------------------------------
# DTOs — field names and shapes are taken verbatim from the spec's §3.
# ---------------------------------------------------------------------------


class PlatformWalletOut(BaseModel):
    workspace_id: str
    balance_micro: int
    has_wallet: bool
    unapplied_count: int
    read_at: datetime


class PlatformLedgerEntryOut(BaseModel):
    id: str
    kind: str
    cause: str | None
    amount_delta_micro: int
    balance_after_micro: int
    applied: bool
    conditional: bool
    member_id: str | None
    ref: dict
    idempotency_key: str
    created_at: datetime | None


class PlatformLedgerPageOut(BaseModel):
    # ``items`` / ``next_cursor`` — matching the live ``WorkspacePageOut``, NOT
    # the tenant-side ``HistoryResponse``'s ``entries``. The console has one
    # shared pager; a second key name for the same concept is how one screen's
    # "next page" silently stops working.
    items: list[PlatformLedgerEntryOut]
    next_cursor: str | None


class PlatformAdjustIn(BaseModel):
    amount_delta_micro: int
    reason: str
    idempotency_key: str


class PlatformAdjustResultOut(BaseModel):
    workspace_id: str
    balance_micro: int
    amount_delta_micro: int
    cause: str
    ledger_entry_id: str
    effective_idempotency_key: str
    replayed: bool
    audit_event_id: str
    applied_at: datetime


class PlatformReconcileIn(BaseModel):
    reason: str


class PlatformReconcileOut(BaseModel):
    workspace_id: str
    balance_micro_before: int
    balance_micro_after: int
    redriven: int
    voided: int


def _ledger_row(entry: LedgerEntry) -> PlatformLedgerEntryOut:
    return PlatformLedgerEntryOut(
        id=entry.id,
        kind=entry.kind,
        cause=entry.cause,
        amount_delta_micro=entry.amount_delta_micro,
        balance_after_micro=entry.balance_after_micro,
        applied=entry.applied,
        conditional=entry.conditional,
        member_id=entry.member_id,
        ref=entry.ref,
        idempotency_key=entry.idempotency_key,
        created_at=entry.created_at,
    )


# ---------------------------------------------------------------------------
# Reads — platform.credits.read (SUPPORT)
# ---------------------------------------------------------------------------


@router.get("/{workspace_id}/credits", response_model=PlatformWalletOut)
async def get_wallet(
    workspace_id: str,
    request: Request,
    operator: Annotated[User, Depends(require_platform("platform.credits.read"))],
) -> PlatformWalletOut:
    """The wallet's exact balance, plus the two signals a balance alone hides.

    ``has_wallet`` tells "never had a wallet" apart from "spent everything" —
    both read as a balance of 0 without it. ``unapplied_count`` is the drift a
    balance cannot show on its own: a nonzero count means the ledger below and
    the number above may disagree until an operator runs reconcile.
    """
    balance_micro = await credits_service.balance_micro(workspace_id)
    has_wallet = await credits_service.wallet_exists(workspace_id)
    unapplied = await credits_service.unapplied_count(workspace_id)

    await audit.record_read(
        operator=operator,
        action="platform.credits.read",
        query=f"workspace={workspace_id}",
        target_type="wallet",
        target_workspace=workspace_id,
        request=request,
    )

    return PlatformWalletOut(
        workspace_id=workspace_id,
        balance_micro=balance_micro,
        has_wallet=has_wallet,
        unapplied_count=unapplied,
        read_at=datetime.now(UTC),
    )


@router.get("/{workspace_id}/credits/history", response_model=PlatformLedgerPageOut)
async def get_history(
    workspace_id: str,
    request: Request,
    operator: Annotated[User, Depends(require_platform("platform.credits.read"))],
    cursor: Annotated[str | None, Query(description="Opaque cursor from a previous page")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    cause: Annotated[str | None, Query(description="Exact-match filter, e.g. operator_grant")] = (
        None
    ),
) -> PlatformLedgerPageOut:
    """The ledger, newest first. Server-side ``cause`` filter — a client-side
    filter over one cursor page would show three rows out of fifty and call it
    the result."""
    # A malformed cursor raises ValidationError, which the central
    # cloud_error_handler maps to the standard envelope — no try/except here,
    # same convention as workspaces.py's search route.
    entries, next_cursor = await credits_service.history(
        workspace_id, limit=limit, cursor=cursor, cause=cause
    )

    await audit.record_read(
        operator=operator,
        action="platform.credits.read",
        query=f"workspace={workspace_id} cursor={cursor!r} cause={cause!r}",
        target_type="wallet_history",
        target_workspace=workspace_id,
        request=request,
    )

    return PlatformLedgerPageOut(
        items=[_ledger_row(e) for e in entries],
        next_cursor=next_cursor,
    )


# ---------------------------------------------------------------------------
# Writes — platform.credits.adjust (OPERATOR)
# ---------------------------------------------------------------------------


@router.post("/{workspace_id}/credits/adjust", response_model=PlatformAdjustResultOut)
async def adjust_credits(
    workspace_id: str,
    body: PlatformAdjustIn,
    request: Request,
    operator: Annotated[User, Depends(require_platform("platform.credits.adjust"))],
) -> PlatformAdjustResultOut:
    """A support correction, in exact micro-credits, going through the same
    service path a top-up or a metered spend does.

    Route internals, in order (master PRD C4, and the spec's route-internals
    section names this exact order):

      1. ``audit.begin`` — writes the row as ``attempted``, before anything
         moves, with the pre-read balance in ``before``.
      2. ``grant`` for a positive delta, ``debit(allow_negative=False)`` for a
         negative one. Never both, never a direct ledger write.
      3. ``audit.settle`` — closes the row to ``applied`` or ``failed``.

    Fixed values the route supplies and the operator cannot override:

      * ``cause`` is derived from the sign — ``operator_grant`` or
        ``operator_debit``. Chunk 9's revenue aggregates exclude
        ``operator_grant`` so a comped credit never reads as earnings.
      * ``allow_negative`` is hard-wired False (master PRD Decision 6) — overage
        handling belongs to the metering path only, never to an operator click.
      * ``member_id`` stays ``None``. A platform operator is not a member of the
        tenant; writing their id there would make them appear as one in every
        tenant-side ledger read. Their identity goes in ``ref`` instead.
      * The idempotency key is namespaced ``operator:{key}`` before it reaches
        the service — the ledger's key space is shared with three machine
        writers (``run:{run_id}``, ``litellm:{request_id}``, Dodo's raw
        ``event_id``), and an unnamespaced operator key that happened to match
        one would collide, be swallowed as a replay, and report a balance that
        never moved as a success.
    """
    reason = body.reason.strip()
    if not reason:
        raise ValidationError("platform.credits.invalid_reason", "reason is required")
    if body.amount_delta_micro == 0:
        raise ValidationError(
            "platform.credits.invalid_amount", "amount_delta_micro must be non-zero"
        )
    key = body.idempotency_key.strip() if body.idempotency_key else ""
    if not key:
        raise ValidationError("platform.credits.invalid_key", "idempotency_key is required")

    effective_key = f"operator:{key}"
    cause = "operator_grant" if body.amount_delta_micro > 0 else "operator_debit"

    before_balance = await credits_service.balance_micro(workspace_id)
    event = await audit.begin(
        operator=operator,
        action="platform.credits.adjust",
        reason=reason,
        target_type="workspace",
        target_workspace=workspace_id,
        before={"balance_micro": before_balance},
        request=request,
    )

    ref = {
        "reason": reason,
        "actor_id": str(operator.id),
        "actor_email": operator.email or "",
        "audit_event_id": str(event.id),
    }

    # See the module docstring for the race this pre-check accepts and why it
    # is safe: only the reported `replayed` flag can be wrong, never the money.
    already_recorded = await credits_service.is_recorded(workspace_id, effective_key)

    try:
        if body.amount_delta_micro > 0:
            result = await credits_service.grant(
                workspace_id,
                cause=cause,
                idempotency_key=effective_key,
                amount_micro=body.amount_delta_micro,
                ref=ref,
            )
            replayed = not result.created
        else:
            magnitude = -body.amount_delta_micro
            try:
                await credits_service.debit(
                    workspace_id,
                    cause=cause,
                    idempotency_key=effective_key,
                    amount_micro=magnitude,
                    ref=ref,
                    allow_negative=False,
                )
            except InsufficientCredits:
                # L9: InsufficientCredits truncates both figures through
                # micro_to_credits before raising, so its own message would
                # read "requested 0, available 0" for a sub-credit claw-back
                # against a sub-credit balance. Rebuild it with the exact
                # micro figures this route already has.
                available_micro = await credits_service.balance_micro(workspace_id)
                await audit.settle(
                    event,
                    ok=False,
                    after={
                        "error": "credits.insufficient",
                        "requested_micro": magnitude,
                        "available_micro": available_micro,
                    },
                )
                raise CloudError(
                    402,
                    "credits.insufficient",
                    f"Insufficient credits: requested {magnitude} micro, "
                    f"available {available_micro} micro",
                ) from None
            replayed = already_recorded
    except CloudError:
        raise
    except Exception:
        await audit.settle(event, ok=False, after={"error": "unexpected"})
        raise

    balance_after = await credits_service.balance_micro(workspace_id)
    entry = await credits_service.find_by_key(workspace_id, effective_key)
    ledger_entry_id = entry.id if entry is not None else ""

    await audit.settle(
        event,
        ok=True,
        after={"balance_micro": balance_after, "replayed": replayed},
    )

    return PlatformAdjustResultOut(
        workspace_id=workspace_id,
        balance_micro=balance_after,
        amount_delta_micro=body.amount_delta_micro,
        cause=cause,
        ledger_entry_id=ledger_entry_id,
        effective_idempotency_key=effective_key,
        replayed=replayed,
        audit_event_id=str(event.id),
        applied_at=datetime.now(UTC),
    )


@router.post("/{workspace_id}/credits/reconcile", response_model=PlatformReconcileOut)
async def reconcile_wallet(
    workspace_id: str,
    body: PlatformReconcileIn,
    request: Request,
    operator: Annotated[User, Depends(require_platform("platform.credits.adjust"))],
) -> PlatformReconcileOut:
    """Repair the crash-window drift between the ledger and the stored balance.

    Reuses ``platform.credits.adjust`` rather than a dedicated action key — per
    the spec, reconcile writes to the balance and the ledger, so it IS an
    adjustment, and a registry key with exactly one route behind it is coverage
    nobody actually tested.

    ``credits.service.reconcile`` returns its balance in WHOLE credits
    (``micro_to_credits`` on every exit path — its return value is not reused
    here for that reason). This route re-reads ``balance_micro`` after the
    repair instead, so the one endpoint whose entire job is making the stored
    balance exact does not itself report that balance through a truncating
    conversion.

    MANUAL RECOVERY TOOL, RUN QUIESCENT ONLY — quoting the service's own
    docstring, because the words belong on the screen this route feeds: "a
    concurrent grant or debit racing a reconcile can interleave with the
    re-drive / re-sum and corrupt the balance." This is not a refresh button.
    """
    reason = body.reason.strip()
    if not reason:
        raise ValidationError("platform.credits.invalid_reason", "reason is required")

    before_balance = await credits_service.balance_micro(workspace_id)
    event = await audit.begin(
        operator=operator,
        action="platform.credits.adjust",
        reason=reason,
        target_type="workspace",
        target_workspace=workspace_id,
        before={"balance_micro": before_balance},
        request=request,
    )

    try:
        result = await credits_service.reconcile(workspace_id)
    except Exception:
        await audit.settle(event, ok=False, after={"error": "unexpected"})
        raise

    balance_after = await credits_service.balance_micro(workspace_id)

    await audit.settle(
        event,
        ok=True,
        after={
            "balance_micro": balance_after,
            "redriven": result.redriven,
            "voided": result.voided,
        },
    )

    return PlatformReconcileOut(
        workspace_id=workspace_id,
        balance_micro_before=before_balance,
        balance_micro_after=balance_after,
        redriven=result.redriven,
        voided=result.voided,
    )


__all__ = ["router"]
