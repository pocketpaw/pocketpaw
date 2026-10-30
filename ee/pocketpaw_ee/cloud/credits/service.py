# ee/pocketpaw_ee/cloud/credits/service.py: the credit ledger business logic.
# Sole owner of writes (and reads) of the CreditBalance and CreditLedgerEntry
# documents; no other module imports models.credit. Module-level async API, no
# class (EE cloud rule).
#
# Movements: grant / debit (idempotent, emit CreditMovement after each real
# movement, never on a replay), record_no_movement (zero-value "seen" marker),
# grant_early_access (one-time per-user grant into an owned workspace; it also
# stamps the claim fields on the User row) and grant_early_access_best_effort
# (the same, logged instead of raised, for sign-in and workspace create).
# Gates: check_balance and check_quota are pure, flag-free assertions that
# raise InsufficientCredits / QuotaExceeded (402); callers decide when to
# enforce. Reads: balance, month_to_date_spend, has_paid_credits, history,
# spend_by_model, sum_debits_by_cause, is_recorded, find_by_key, wallet_exists,
# unapplied_count. Repair: reconcile; boot check: verify_wallet_migrated.
#
# Atomicity without transactions (single-node Mongo, no replica set):
#   1. Insert the ledger entry FIRST. The unique (workspace, idempotency_key)
#      index makes a retried movement collide, so a duplicate key never
#      double-applies. The entry is the idempotency token.
#   2. Apply with one atomic $inc on the balance doc. A strict debit filters on
#      balance >= amount; on a miss it deletes its own entry and raises, so a
#      refused debit leaves no trace and a retry re-evaluates cleanly.
#      allow_negative debits (metered spend) may drive the balance below 0.
#   3. Stamp balance_after and flip applied=True on the entry.
# The only crash window is between 1 and 2, leaving applied=False. reconcile
# re-drives those entries (voiding strict debits that no longer fit), then sums
# applied entries only. The balance is never clamped to 0.
#
# Aggregations run server-side and are iterated with async for, so the same
# code works on real Mongo and the mongomock-motor test harness.

from __future__ import annotations

import inspect
import logging
from datetime import UTC, datetime
from typing import Any

from beanie import PydanticObjectId
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from pocketpaw.config import get_settings  # type: ignore[import-untyped]
from pocketpaw_ee.cloud._core.errors import (
    InsufficientCredits,
    QuotaExceeded,
    ValidationError,
)
from pocketpaw_ee.cloud._core.realtime.emit import emit
from pocketpaw_ee.cloud._core.realtime.events import CreditMovement
from pocketpaw_ee.cloud.credits.domain import (
    GrantResult,
    LedgerEntry,
    ModelSpendRow,
    ReconcileResult,
    credits_to_micro,
    micro_to_credits,
)
from pocketpaw_ee.cloud.models.credit import CreditBalance, CreditLedgerEntry
from pocketpaw_ee.cloud.models.user import User
from pocketpaw_ee.cloud.models.workspace import Workspace

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Mapping helpers
# ---------------------------------------------------------------------------


def _entry_to_domain(doc: CreditLedgerEntry) -> LedgerEntry:
    return LedgerEntry(
        id=str(doc.id),
        workspace_id=doc.workspace,
        kind=doc.kind,
        # The domain object speaks WHOLE credits — it is what the HTTP layer
        # renders. The exact micro figures stay on the document.
        amount_delta=micro_to_credits(doc.amount_delta_micro),
        balance_after=micro_to_credits(doc.balance_after_micro),
        amount_delta_micro=doc.amount_delta_micro,
        balance_after_micro=doc.balance_after_micro,
        member_id=doc.member_id,
        cause=doc.cause,
        ref=dict(doc.ref or {}),
        idempotency_key=doc.idempotency_key,
        created_at=getattr(doc, "createdAt", None),
        applied=doc.applied,
        conditional=doc.conditional,
    )


async def _current_balance(workspace: str) -> int:
    """The workspace's current balance in MICRO-credits, or 0 when no row exists.

    Internal and exact. The public ``balance`` truncates this to whole credits;
    every arithmetic path inside this module uses the micro figure, so rounding
    happens once, at the surface, and never compounds.
    """
    doc = await CreditBalance.find_one(CreditBalance.workspace == workspace)
    return int(doc.balance_micro) if doc is not None else 0


async def _emit_movement(entry: CreditLedgerEntry) -> None:
    """Emit ``credits.movement`` after a successful grant / debit (rule 9)."""
    await emit(
        CreditMovement(
            data={
                "workspace_id": entry.workspace,
                "kind": entry.kind,
                # The event is a published contract, so its credit-denominated
                # keys keep their names and meaning. The exact figures ride
                # alongside for any consumer that needs sub-credit precision —
                # a metered spend is routinely a fraction of a credit, so both
                # whole-credit values can read 0 on a real charge.
                "amount_delta": micro_to_credits(entry.amount_delta_micro),
                "balance_after": micro_to_credits(entry.balance_after_micro),
                "amount_delta_micro": entry.amount_delta_micro,
                "balance_after_micro": entry.balance_after_micro,
                "cause": entry.cause,
                "idempotency_key": entry.idempotency_key,
            }
        )
    )


# ---------------------------------------------------------------------------
# Write API
# ---------------------------------------------------------------------------


async def grant(
    workspace: str,
    amount: int | None = None,
    cause: str = "",
    idempotency_key: str = "",
    *,
    amount_micro: int | None = None,
    member_id: str | None = None,
    ref: dict | None = None,
    kind: str = "grant",
) -> GrantResult:
    """Add credits to the workspace wallet. Idempotent.

    Takes EITHER ``amount`` (whole credits, the original signature) or
    ``amount_micro`` (micro-credits, 1_000_000 == 1 credit) — exactly one, same
    convention as ``debit``. ``amount_micro`` exists for callers that must move
    an exact sub-credit amount (chunk 6: a platform operator adjustment) — a
    grant routed through the whole-credit ``amount`` path truncates toward zero,
    so a 375_000-micro correction would silently become a no-op grant of 0.

    Returns a ``GrantResult`` of ``(balance, created)``. ``created`` is True when
    this call NEWLY applied the grant; a retried call with the same
    ``(workspace, idempotency_key)`` is a no-op that returns the current balance
    with ``created=False`` — the movement already applied. Callers gate their
    "money moved" side effects (e.g. a capture-event emit) on ``created``, never
    on a balance delta, which is unreliable under concurrency.

    ``kind`` defaults to ``"grant"``; pass ``"genesis"`` to seed a fresh
    wallet's first credits (the ledger origin row).

    The returned balance stays WHOLE credits so existing callers are unaffected.
    Use ``balance_micro`` when you need the exact figure.
    """
    # Rule 6 — validate at entry, exactly-one-of, mirroring ``debit``.
    if (amount is None) == (amount_micro is None):
        raise ValidationError(
            "credits.invalid_amount", "Pass exactly one of amount or amount_micro"
        )
    if amount is not None:
        if not isinstance(amount, int) or isinstance(amount, bool) or amount <= 0:
            raise ValidationError(
                "credits.invalid_amount", "Grant amount must be a positive integer"
            )
        amount_micro = credits_to_micro(amount)
    if not isinstance(amount_micro, int) or isinstance(amount_micro, bool) or amount_micro <= 0:
        raise ValidationError(
            "credits.invalid_amount", "Grant amount_micro must be a positive integer"
        )
    if not workspace:
        raise ValidationError("credits.invalid_workspace", "workspace is required")
    if not idempotency_key:
        raise ValidationError("credits.invalid_key", "idempotency_key is required")

    # Step 1 — insert the ledger entry FIRST (the exactly-once guard). It starts
    # ``applied=False`` (the crash-window state) and ``conditional=False`` (a
    # grant is always re-driven unconditionally).
    entry = CreditLedgerEntry(
        workspace=workspace,
        kind=kind,
        amount_delta_micro=amount_micro,
        balance_after_micro=0,  # stamped after the $inc returns the new balance
        applied=False,
        conditional=False,
        member_id=member_id,
        cause=cause,
        ref=dict(ref or {}),
        idempotency_key=idempotency_key,
    )
    try:
        await entry.insert()
    except DuplicateKeyError:
        # This movement was already applied — return the current balance and
        # signal a replay (created=False) so the caller suppresses any
        # money-moved side effect (e.g. a capture-event emit).
        return GrantResult(
            balance=micro_to_credits(await _current_balance(workspace)), created=False
        )

    # Step 2 — apply the effect: unconditional $inc with upsert (creates the
    # balance row at 0 then increments in one atomic call).
    coll = CreditBalance.get_pymongo_collection()
    updated = await coll.find_one_and_update(
        {"workspace": workspace},
        {
            "$inc": {"balance_micro": amount_micro},
            "$setOnInsert": {"createdAt": datetime.now(UTC)},
            "$currentDate": {"updatedAt": True},
        },
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )
    new_balance_micro = int(updated["balance_micro"])

    # Step 3 — the $inc landed: stamp balance_after and mark the entry applied so
    # reconcile counts it (and never re-drives it as a phantom).
    entry.balance_after_micro = new_balance_micro
    entry.applied = True
    await entry.save()

    await _emit_movement(entry)
    return GrantResult(balance=micro_to_credits(new_balance_micro), created=True)


EARLY_ACCESS_CAUSE = "early_access"


async def grant_early_access(user: User) -> None:
    """Grant the one-time early access credits to ``user``. Idempotent.

    Exactly once per USER, never per workspace, and only into a workspace the
    user OWNS (``Workspace.owner``), never one they were invited to. Skipped for
    unverified, inactive or guest users, and while
    ``early_access_grant_credits`` is 0. A user who owns no workspace yet gets
    nothing now and is granted on their first owned-workspace create. Doors:
    social sign-in, workspace create, and the guest social upgrade.

    ``early_access_granted_at`` is the fast path. Behind it the LEDGER is the
    record: any ``early_access:{user_id}`` entry in a workspace the user owns
    (soft-deleted ones included) means the grant already happened, so a stale
    ``User.save()`` that wipes both fields, followed by deleting the granted
    workspace and creating another, still cannot farm a second grant.

    Otherwise claim then grant: set ``early_access_workspace_id`` to the
    OLDEST live owned workspace only if it is unset, read back what is stored,
    and grant ``kind='genesis'`` there. One claimed workspace plus the ledger's
    unique (workspace, key) index make a retry or a concurrent call a no-op.
    """
    if user.early_access_granted_at is not None:
        return
    if not user.is_verified or not user.is_active or user.is_guest:
        return
    amount = get_settings().early_access_grant_credits
    if amount <= 0:
        return

    user_id = str(user.id)
    key = f"early_access:{user_id}"
    users = User.get_pymongo_collection()
    # global-read: the user's own workspaces, looked up by owner id.
    owned = await Workspace.find({"owner": user_id}).sort("_id").to_list()
    prior = (
        await CreditLedgerEntry.find_one(
            {"workspace": {"$in": [str(w.id) for w in owned]}, "idempotency_key": key}
        )
        if owned
        else None
    )
    if prior is not None:
        workspace_id: str = prior.workspace
    else:
        live = [str(w.id) for w in owned if w.deleted_at is None]
        if not live:
            return
        await users.update_one(
            {"_id": user.id, "early_access_workspace_id": None},
            {"$set": {"early_access_workspace_id": live[0]}},
        )
        stored = await users.find_one({"_id": user.id}, {"early_access_workspace_id": 1})
        claimed = (stored or {}).get("early_access_workspace_id")
        if not claimed:
            return
        workspace_id = str(claimed)

    await grant(
        workspace_id,
        amount,
        cause=EARLY_ACCESS_CAUSE,
        idempotency_key=key,
        kind="genesis",
        member_id=user_id,
    )
    granted_at = prior.createdAt if prior is not None else datetime.now(UTC)
    # no-event: the grant above already emitted credits.movement.
    await users.update_one(
        {"_id": user.id},
        {
            "$set": {
                "early_access_workspace_id": workspace_id,
                "early_access_granted_at": granted_at,
            }
        },
    )
    # Keep the caller's copy current so a later save() of it cannot unset these.
    user.early_access_workspace_id = workspace_id
    user.early_access_granted_at = granted_at


async def grant_early_access_best_effort(user: User) -> None:
    """``grant_early_access`` for doors that must not fail on it.

    Sign-in and workspace create call this. A failed grant is logged, not
    raised: every one of those doors retries the (idempotent) grant, so the
    next sign-in or owned-workspace create lands it instead of a 500 that
    leaves a half-created workspace or a refused login behind.
    """
    try:
        await grant_early_access(user)
    except Exception:  # noqa: BLE001 - logged; the next door retries
        logger.exception("early access grant failed for user=%s; will retry", user.id)


async def debit(
    workspace: str,
    amount: int | None = None,
    cause: str = "",
    idempotency_key: str = "",
    *,
    amount_micro: int | None = None,
    member_id: str | None = None,
    ref: dict | None = None,
    kind: str = "spend",
    allow_negative: bool = False,
) -> int:
    """Remove credits from the workspace wallet. Atomic + idempotent.

    Takes EITHER ``amount`` (whole credits, the original signature) or
    ``amount_micro`` (micro-credits, 1_000_000 == 1 credit). Exactly one.

    ``amount_micro`` exists because metered compute cannot be expressed in whole
    credits. A credit is a cent and the proxy prices a single API call, so a
    $0.0015 call is 0.375 of a credit — a real charge with no whole-credit
    representation. Rounding it down served it free; rounding to nearest billed a
    number that was simply not what the customer used. The finer unit is how the
    deduction becomes exactly what was consumed.

    The return value stays WHOLE credits so existing callers are unaffected. Use
    ``balance_micro`` when you need the exact figure.

    Returns the new balance. A retried call with the same
    ``(workspace, idempotency_key)`` is a no-op that returns the current balance.

    Two modes, selected by ``allow_negative``:

    * ``allow_negative=False`` (STRICT, the default): a CONDITIONAL
      ``$inc: -amount`` filtered on ``balance_credits >= amount``. If the wallet
      holds fewer than ``amount`` credits the movement is REJECTED with ZERO side
      effects (the inserted ledger entry is rolled back) and ``InsufficientCredits``
      (402) is raised. The entry is tagged ``conditional=True`` so a crashed
      phantom is re-driven with the same funds guard (and voided if the funds
      aren't there).
    * ``allow_negative=True``: an UNCONDITIONAL ``$inc: -amount`` that may drive
      the balance to or below zero and NEVER raises ``InsufficientCredits``. This
      is metered compute spend (BC-3): a completed run is always billed — the
      spend already happened — so the overage is recorded as a legitimate
      negative balance. The no-overdraft guarantee is enforced at run-start, not
      here. The entry is tagged ``conditional=False``.
    """
    # Rule 6 — validate at entry. Exactly one unit, never both: a caller passing
    # each would otherwise silently get whichever the code happened to prefer, and
    # the two differ by a factor of a million.
    if (amount is None) == (amount_micro is None):
        raise ValidationError(
            "credits.invalid_amount", "Pass exactly one of amount or amount_micro"
        )
    if amount is not None:
        if not isinstance(amount, int) or isinstance(amount, bool) or amount <= 0:
            raise ValidationError(
                "credits.invalid_amount", "Debit amount must be a positive integer"
            )
        amount_micro = credits_to_micro(amount)
    if not isinstance(amount_micro, int) or isinstance(amount_micro, bool) or amount_micro <= 0:
        raise ValidationError(
            "credits.invalid_amount", "Debit amount_micro must be a positive integer"
        )
    if not workspace:
        raise ValidationError("credits.invalid_workspace", "workspace is required")
    if not cause:
        raise ValidationError("credits.invalid_cause", "cause is required")
    if not idempotency_key:
        raise ValidationError("credits.invalid_key", "idempotency_key is required")

    # Step 1 — insert the ledger entry FIRST (the exactly-once guard). The delta
    # is signed negative for a debit. It starts ``applied=False`` (the
    # crash-window state); ``conditional`` records whether this is a STRICT debit
    # (must reject on insufficient funds) so reconcile re-drives a phantom the
    # right way.
    entry = CreditLedgerEntry(
        workspace=workspace,
        kind=kind,
        amount_delta_micro=-amount_micro,
        balance_after_micro=0,  # stamped after the $inc returns the new balance
        applied=False,
        conditional=not allow_negative,
        member_id=member_id,
        cause=cause,
        ref=dict(ref or {}),
        idempotency_key=idempotency_key,
    )
    try:
        await entry.insert()
    except DuplicateKeyError:
        # This movement was already applied — return the current balance.
        return micro_to_credits(await _current_balance(workspace))

    coll = CreditBalance.get_pymongo_collection()
    if allow_negative:
        # Step 2 (metered) — UNCONDITIONAL $inc: the balance may go to or below
        # zero. Upsert so a never-seen wallet still records the spend (it lands
        # at -amount, a legitimate negative). Never raises InsufficientCredits.
        updated = await coll.find_one_and_update(
            {"workspace": workspace},
            {
                "$inc": {"balance_micro": -amount_micro},
                "$setOnInsert": {"createdAt": datetime.now(UTC)},
                "$currentDate": {"updatedAt": True},
            },
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
    else:
        # Step 2 (strict) — CONDITIONAL $inc filtered on
        # ``balance_credits >= amount`` so two racing debits can never both pass
        # the funds check (compare-and-swap, not read-then-write). NO upsert — a
        # wallet that doesn't exist yet has zero credits and cannot satisfy the
        # filter.
        updated = await coll.find_one_and_update(
            {"workspace": workspace, "balance_micro": {"$gte": amount_micro}},
            {"$inc": {"balance_micro": -amount_micro}, "$currentDate": {"updatedAt": True}},
            return_document=ReturnDocument.AFTER,
        )
        if updated is None:
            # Insufficient funds. Roll back the ledger entry we inserted in step
            # 1 so the rejected debit leaves NO trace (invariant b) and a retry
            # with the same key can re-evaluate cleanly (the key is free again).
            await entry.delete()
            available = micro_to_credits(await _current_balance(workspace))
            raise InsufficientCredits(micro_to_credits(amount_micro), available)

    new_balance_micro = int(updated["balance_micro"])

    # Step 3 — the $inc landed: stamp balance_after and mark the entry applied so
    # reconcile counts it (and never re-drives it as a phantom).
    entry.balance_after_micro = new_balance_micro
    entry.applied = True
    await entry.save()

    await _emit_movement(entry)
    return micro_to_credits(new_balance_micro)


async def record_no_movement(
    workspace: str,
    cause: str,
    idempotency_key: str,
    *,
    ref: dict | None = None,
    kind: str = "spend",
) -> bool:
    """Record that a source event was accounted for WITHOUT moving the wallet.

    Writes one ledger entry with ``amount_delta = 0``. Returns True if this call
    wrote it, False if the key was already present. Touches no balance, emits no
    movement event — nothing moved.

    WHY A ZERO ENTRY IS A REAL THING. ``debit`` requires a positive integer, and
    rightly: a zero debit is not a debit. But an ingest that accumulates fractional
    external spend needs somewhere to record "I have seen this row and folded it
    into the remainder", and the only per-row store with an exactly-once guarantee
    is this ledger's unique ``(workspace, idempotency_key)`` index. Without an
    entry, ``is_recorded`` says no and the next overlapping read folds the same row
    in a second time — turning an under-bill into an over-bill, which is worse.

    So the entry is the marker, and ``amount_delta = 0`` is the honest amount. It
    is written ``applied=True`` because there is no ``$inc`` to land and a phantom
    is precisely what it must not look like to ``reconcile``; a zero contributes
    nothing to any ``sum(amount_delta)``, so every existing read is unchanged by it.
    """
    if not workspace:
        raise ValidationError("credits.invalid_workspace", "workspace is required")
    if not idempotency_key:
        raise ValidationError("credits.invalid_key", "idempotency_key is required")

    entry = CreditLedgerEntry(
        workspace=workspace,
        kind=kind,
        amount_delta_micro=0,
        balance_after_micro=await _current_balance(workspace),
        # Nothing to land, so it is applied on arrival. An unapplied zero would
        # read as a crash-window phantom to reconcile and be re-driven forever.
        applied=True,
        conditional=False,
        cause=cause,
        ref=dict(ref or {}),
        idempotency_key=idempotency_key,
    )
    try:
        await entry.insert()
    except DuplicateKeyError:
        return False
    return True


# ---------------------------------------------------------------------------
# Read API (Rule 7 — every read is tenant-filtered on ``workspace``)
# ---------------------------------------------------------------------------


async def balance(workspace: str) -> int:
    """The workspace's spendable balance in WHOLE credits (0 when no wallet).

    Truncated toward zero, so a wallet holding 4.7 credits reads as 4: showing a
    customer a credit they cannot spend is the worse error. ``balance_micro`` is
    the exact figure, and it is what every guard here compares against.
    """
    if not workspace:
        raise ValidationError("credits.invalid_workspace", "workspace is required")
    return micro_to_credits(await _current_balance(workspace))


async def balance_micro(workspace: str) -> int:
    """The workspace's exact balance in micro-credits (1_000_000 == 1 credit).

    For callers that must not lose the fraction — the spend ingest's reporting,
    and any future pre-flight estimate that reasons about sub-credit amounts.
    """
    if not workspace:
        raise ValidationError("credits.invalid_workspace", "workspace is required")
    return await _current_balance(workspace)


async def check_balance(workspace: str) -> None:
    """Raise ``InsufficientCredits`` when the wallet is out of credits.

    The run-start hard-block (BC-4): a workspace whose balance is ``<= 0`` may
    not START a new chat run. A pure, reusable assertion — it carries NO flag
    logic (the caller decides whether enforcement is on), so it stays usable by
    any future gate (e.g. a pre-flight estimate check) without coupling to the
    ``billing_enforced`` setting. A no-op for any positive balance; raises a 402
    ``credits.insufficient`` (mapped by the CloudError handler) at zero or below.
    The reported ``requested`` is 1 — the minimum a new run will consume — and
    ``available`` is the clamped non-negative balance so the message reads
    sensibly even on a metered-overage negative wallet.
    """
    # Compare the EXACT balance, not the displayed one. ``balance`` truncates, so
    # a wallet holding half a credit reads as 0 there — and this gate would lock a
    # customer out of a run they can genuinely pay for. Sub-credit balances were
    # impossible when the wallet stored whole credits; they are routine now.
    bal_micro = await _current_balance(workspace)
    if bal_micro <= 0:
        raise InsufficientCredits(1, max(micro_to_credits(bal_micro), 0))


# The grant cause that EXTENDS the monthly ceiling — a PURCHASED one-time top-up
# (``billing.service.handle_webhook`` records ``payment.succeeded`` as
# ``cause="top_up"``, a positive grant). It is the only cause that raises the cap:
# a top-up is credits the workspace bought ON TOP of its plan, so its monthly cap
# rises by what it purchased this period. The RECURRING monthly allotment is
# recorded under a DIFFERENT cause (``subscription_grant``, in
# ``_handle_subscription_event``) and is deliberately NOT here — the allotment is
# the baseline the plan ``monthly_ceiling`` already accounts for (the ceiling is
# allotment × 1.5 for paid tiers), so counting it would double-credit the cap.
_TOPUP_CAUSE = "top_up"


async def _period_topup_credits(workspace: str) -> int:
    """Sum of PURCHASED top-up credits granted to ``workspace`` this UTC month.

    The amount the effective monthly ceiling is raised by: the POSITIVE
    ``amount_delta`` summed over the APPLIED ``top_up`` grants with
    ``createdAt >= start-of-current-UTC-month``. Only ``top_up`` (a bought
    extension) counts — NOT ``subscription_grant`` (the recurring allotment, which
    is the plan ceiling's own baseline). Runs server-side ``$match`` + ``$group``,
    same as ``month_to_date_spend``. A top-up grant's ``amount_delta`` is positive,
    so the grouped sum is ``>= 0``; a (should-never-happen) net-negative under the
    top-up cause is clamped to 0 so a stray debit can't SHRINK the ceiling.
    """
    month_start = _utc_month_start(datetime.now(UTC))
    query: dict[str, Any] = {
        "workspace": workspace,
        "cause": _TOPUP_CAUSE,
        "applied": True,
        "createdAt": {"$gte": month_start},
    }
    # ``_sum_amount_delta`` sums the stored MICRO field; the ceiling this feeds is
    # expressed in whole credits, so convert before returning.
    net_micro = await _sum_amount_delta(query)
    return micro_to_credits(max(net_micro, 0))


_PAID_CAUSES = (_TOPUP_CAUSE, "subscription_grant")


async def has_paid_credits(workspace: str) -> bool:
    """Whether ``workspace`` ever received paid credits.

    True when the ledger holds at least one APPLIED entry whose cause is a
    purchase (``top_up``) or a paid plan's allotment (``subscription_grant``).
    Any time, not only this month: a workspace that has paid once is not
    platform-funded early access. Read by ``guards.is_platform_funded_free``.
    """
    if not workspace:
        raise ValidationError("credits.invalid_workspace", "workspace is required")
    entry = await CreditLedgerEntry.find_one(
        {"workspace": workspace, "cause": {"$in": list(_PAID_CAUSES)}, "applied": True}
    )
    return entry is not None


async def check_quota(workspace: str) -> None:
    """Raise ``QuotaExceeded`` when the workspace has hit its monthly credit cap.

    The monthly-quota assertion, the credit-spend sibling of ``check_balance``: a
    PURE, flag-free read (it carries NO ``billing_enforced`` logic so the caller
    owns the on/off decision and the check stays reusable by any future gate).

    Resolves the workspace's plan ``monthly_ceiling``
    (``entitlements.resolve_entitlements``). An UNCAPPED plan (Enterprise →
    ceiling is ``None``) is a no-op. Otherwise the EFFECTIVE ceiling is the plan
    ceiling PLUS any credits the workspace PURCHASED via top-ups this period
    (``_period_topup_credits``) — a bought top-up raises the cap; the recurring
    allotment does not (it is already the ceiling's baseline). Raises
    ``QuotaExceeded`` (402 ``credits.quota_exceeded``) when month-to-date spend is
    AT or ABOVE the effective ceiling (boundary is ``>=`` — exactly-at-ceiling
    raises); a no-op below it.

    A workspace with no/unknown plan resolves (via the entitlements resolver) to
    the Free trial ceiling (1000), so this fails CLOSED — an unresolvable plan is
    capped, never uncapped.
    """
    if not workspace:
        raise ValidationError("credits.invalid_workspace", "workspace is required")

    # Lazy import — keeps this module free of the entitlements/billing import chain
    # at load (it pulls the plan catalog + workspace service), mirroring how
    # ``entitlements.service`` itself lazy-imports the workspace service.
    from pocketpaw_ee.cloud.entitlements import service as entitlements_service

    ent = await entitlements_service.resolve_entitlements(workspace)
    ceiling = ent.monthly_ceiling
    # None == uncapped (Enterprise / custom). No cap to enforce — no-op.
    if ceiling is None:
        return

    effective_ceiling = ceiling + await _period_topup_credits(workspace)
    spent = await month_to_date_spend(workspace)
    if spent >= effective_ceiling:
        raise QuotaExceeded(ceiling=effective_ceiling, spent=spent)


async def is_recorded(workspace: str, idempotency_key: str) -> bool:
    """Whether a movement with this ``(workspace, idempotency_key)`` already exists.

    A read-only pre-check for callers that ingest external, already-deduplicated
    events (e.g. MCG-8's LiteLLM spend-log sweep) and want to know whether a given
    source row was ALREADY recorded — so they can report an accurate
    "newly billed" count and skip a redundant insert-then-rollback on the
    duplicate path. This does NOT replace the exactly-once guard: ``grant`` /
    ``debit`` remain idempotent via the unique index regardless, so a race between
    this read and the write still resolves correctly (the write no-ops). It only
    saves the wasted round-trip and lets the caller's bookkeeping stay honest.
    Tenant-filtered (Rule 7).
    """
    if not workspace:
        raise ValidationError("credits.invalid_workspace", "workspace is required")
    if not idempotency_key:
        return False
    existing = await CreditLedgerEntry.find_one(
        CreditLedgerEntry.workspace == workspace,
        CreditLedgerEntry.idempotency_key == idempotency_key,
    )
    return existing is not None


async def find_by_key(workspace: str, idempotency_key: str) -> LedgerEntry | None:
    """The single ledger entry for this ``(workspace, idempotency_key)``, if any.

    Read-only. Added for the platform adjust route (chunk 6): ``grant``/``debit``
    report a balance but not the id of the row they wrote (or, on a replay, the
    id of the row that already existed), and the route must echo
    ``ledger_entry_id`` to the operator without importing ``models.credit``
    directly — only this module may do that.
    """
    if not workspace:
        raise ValidationError("credits.invalid_workspace", "workspace is required")
    if not idempotency_key:
        return None
    doc = await CreditLedgerEntry.find_one(
        CreditLedgerEntry.workspace == workspace,
        CreditLedgerEntry.idempotency_key == idempotency_key,
    )
    return _entry_to_domain(doc) if doc is not None else None


async def wallet_exists(workspace: str) -> bool:
    """Whether a ``CreditBalance`` row exists for this workspace.

    Read-only. Added for the platform wallet-detail route (chunk 6): an
    operator viewing a tenant that has never had a wallet provisioned needs to
    see "no wallet yet" rather than a balance of 0 that reads as identical to
    "an empty wallet".
    """
    if not workspace:
        raise ValidationError("credits.invalid_workspace", "workspace is required")
    doc = await CreditBalance.find_one(CreditBalance.workspace == workspace)
    return doc is not None


async def unapplied_count(workspace: str) -> int:
    """How many ledger entries are stuck at ``applied is False`` right now.

    Read-only signal for the platform wallet-detail route (chunk 6): an
    operator deciding whether a workspace's wallet needs ``reconcile`` should
    not have to run the (write, quiescent-only) repair just to find out.
    """
    if not workspace:
        raise ValidationError("credits.invalid_workspace", "workspace is required")
    return await CreditLedgerEntry.find(
        CreditLedgerEntry.workspace == workspace,
        CreditLedgerEntry.applied == False,  # noqa: E712 — Beanie field equality, not `is`
    ).count()


async def sum_debits_by_cause(
    workspace: str,
    cause: str,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
) -> tuple[int, int]:
    """Sum the credits debited under ``cause`` for ``workspace`` over a window.

    Returns ``(credits, entry_count)`` where ``credits`` is the POSITIVE total
    debited (the absolute value of the summed negative ``amount_delta`` over the
    matching APPLIED entries) and ``entry_count`` is how many entries fed it.

    The window is on ``createdAt``: ``since`` is inclusive (``>=``), ``until`` is
    EXCLUSIVE (``<``) so adjacent windows never double-count a boundary entry; a
    ``None`` bound is open on that side. Only ``applied is True`` entries are
    counted — a committed-but-unapplied phantom never moved the wallet, so it must
    not inflate the sum (the same rule ``reconcile`` enforces).

    WU-F's shadow compare uses this to total the BC-3 ``compute_spend`` debits over
    a tenant's window WITHOUT the llm_provisioning entity reaching into the credit
    ledger doc directly — the credits service owns the read of its own
    ``CreditLedgerEntry`` (EE entity-isolation), so the cross-entity compare goes
    through this tenant-filtered helper. It performs NO writes.
    """
    if not workspace:
        raise ValidationError("credits.invalid_workspace", "workspace is required")

    query: dict[str, Any] = {
        "workspace": workspace,
        "cause": cause,
        "applied": True,
    }
    created_filter: dict[str, Any] = {}
    if since is not None:
        created_filter["$gte"] = since
    if until is not None:
        created_filter["$lt"] = until
    if created_filter:
        query["createdAt"] = created_filter

    entries = await CreditLedgerEntry.find(query).to_list()
    # A debit's ``amount_delta`` is negative; sum and flip the sign to a positive
    # "credits debited" figure. A stray positive delta under this cause (there
    # should be none — compute_spend is debit-only) is clamped out so a bad row
    # can't make the spend total read as a refund.
    total_micro = sum(-int(e.amount_delta_micro) for e in entries if int(e.amount_delta_micro) < 0)
    # Whole credits: the shadow compare puts this beside a LiteLLM-side figure that
    # is also whole credits, and a delta between two units would be meaningless.
    return micro_to_credits(total_micro), len(entries)


# The spend causes that count toward the monthly quota — the SAME two the usage
# graph reads (``spend_by_model``). ``compute_spend`` is the default off-mode
# debit a finished run records against the ledger; ``litellm_spend`` is the
# post-cutover live-proxy debit. They are mutually exclusive per run, so summing
# both is safe (a given run is billed under exactly one). Grants
# (``top_up`` / ``subscription_grant`` / ``genesis`` / ``promo``) are NOT spend
# and never count here.
_SPEND_CAUSES: tuple[str, ...] = ("compute_spend", "litellm_spend")


def _utc_month_start(now: datetime) -> datetime:
    """Start-of-current-UTC-calendar-month (midnight on the 1st), tz-aware UTC."""
    return datetime(now.year, now.month, 1, tzinfo=UTC)


async def _sum_amount_delta(query: dict[str, Any]) -> int:
    """Server-side ``$sum`` of ``amount_delta_micro`` over entries matching ``query``.

    Returns MICRO-credits. Callers that surface a whole-credit figure convert.

    Runs a Mongo ``$match`` + ``$group`` aggregation in the DB (NOT a
    pull-all-then-sum in Python) and returns the summed ``amount_delta`` (signed),
    or 0 when nothing matches. ``get_pymongo_collection().aggregate()`` returns
    DIFFERENT shapes per driver: a COROUTINE under the pymongo-async client the
    app uses in prod (``async for`` raises TypeError — the same live bug that hit
    ``storage.service.workspace_storage_usage``), but a directly-iterable latent
    cursor under the mongomock-motor test harness. We discriminate with
    ``inspect.isawaitable`` (the repo's cross-driver idiom in ``files/router.py``,
    ``connectors/registry.py``) and await only when the cursor is awaitable.
    """
    cursor = CreditLedgerEntry.get_pymongo_collection().aggregate(
        [
            {"$match": query},
            {"$group": {"_id": None, "total": {"$sum": "$amount_delta_micro"}}},
        ]
    )
    if inspect.isawaitable(cursor):
        cursor = await cursor
    async for row in cursor:
        return int(row.get("total") or 0)
    return 0


async def month_to_date_spend(workspace: str) -> int:
    """Total credits spent by ``workspace`` this UTC calendar month.

    Returns the POSITIVE credits debited under a SPEND cause
    (``compute_spend`` / ``litellm_spend``) with ``applied is True`` and
    ``createdAt >= start-of-current-UTC-month``. Grants (``top_up`` /
    ``subscription_grant`` / …) are excluded by the cause filter, and a
    committed-but-unapplied phantom is excluded by ``applied is True`` (it never
    moved the wallet) — the same windowing / applied discipline as
    ``sum_debits_by_cause``.

    The sum runs SERVER-SIDE: a Mongo ``$match`` (workspace + spend causes +
    applied + month window) feeds a ``$group`` ``$sum`` of ``amount_delta``, so a
    busy tenant's full ledger is never pulled into the process. A debit's
    ``amount_delta`` is negative, so the grouped sum is ``<= 0``; we flip the sign
    to a positive "credits spent" figure and CLAMP a (should-never-happen) net
    positive to 0 — a stray positive delta under a spend cause must not read as
    negative spend (the same clamp ``sum_debits_by_cause`` applies). The reusable,
    flag-free read the monthly-quota gate (``check_quota``) compares against.
    """
    if not workspace:
        raise ValidationError("credits.invalid_workspace", "workspace is required")

    month_start = _utc_month_start(datetime.now(UTC))
    query: dict[str, Any] = {
        "workspace": workspace,
        "cause": {"$in": list(_SPEND_CAUSES)},
        "applied": True,
        "createdAt": {"$gte": month_start},
    }
    # ``amount_delta_micro`` is negative for a debit, so the grouped sum is <= 0.
    # Flip to positive spend; clamp a stray net-positive to 0 (spend is debit-only).
    # Converted to whole credits because the quota ceiling is denominated that way —
    # this is the one place the fine unit is deliberately coarsened, and it rounds
    # DOWN, so the gate never fires early on a fraction the customer has not spent.
    net_micro = await _sum_amount_delta(query)
    return micro_to_credits(max(-net_micro, 0))


def _spend_by_model_pipeline(query: dict[str, Any]) -> list[dict[str, Any]]:
    """The ``$match`` + ``$group`` that folds a spend window by (UTC day, model).

    ``query`` is the caller's tenant + cause + applied + ``createdAt`` filter; the
    pipeline adds ``amount_delta < 0`` to it. That predicate is load-bearing:
    spend is debit-only, and a stray non-negative delta under a spend cause must
    be SKIPPED ENTIRELY rather than clamped — out of the credits sum, out of the
    request count, out of the token sum, and unable to conjure a (day, model)
    group of its own. A ``$match`` is the one construct that does all four at
    once. It also drops an entry with no ``createdAt``: a missing or null field
    never satisfies a date range.

    ``day`` uses ``$dateToString`` with NO ``timezone`` key, so it takes Mongo's
    UTC default — the same boundary ``createdAt.date()`` gave on the UTC datetimes
    the driver returns. ``model`` falls back to ``"unknown"`` for a null, missing
    or empty ``ref.model``, because unattributed spend still has to chart for the
    total to reconcile with the wallet. The ``$ifNull`` subexpression is repeated
    instead of bound to a variable: ``$let`` is unimplemented in the
    mongomock-motor harness this read is tested against.

    ``tokens`` sums ``ref.total_tokens`` per group, counting a value only when it
    is a real number above zero — missing, null, negative and non-numeric refs all
    read 0. ``$isNumber`` has to gate ``$toLong``: Mongo's cross-type ordering
    sorts a string ABOVE any number, so a bare ``$gt`` would wave junk through and
    ``$toLong`` would then fail the whole aggregation.
    """
    # ``$cond`` returns "unknown" for null / missing / empty, matching what the
    # Python ``ref.get("model") or "unknown"`` did. A falsy NON-string (0, False)
    # would differ, but the field's only writer stores ``str | None``.
    model_or_unknown = {
        "$cond": [
            {"$eq": [{"$ifNull": ["$ref.model", ""]}, ""]},
            "unknown",
            "$ref.model",
        ]
    }
    tokens_or_zero = {
        "$cond": [
            {"$isNumber": "$ref.total_tokens"},
            {
                "$cond": [
                    {"$gt": ["$ref.total_tokens", 0]},
                    {"$toLong": "$ref.total_tokens"},
                    0,
                ]
            },
            0,
        ]
    }
    return [
        {"$match": {**query, "amount_delta_micro": {"$lt": 0}}},
        {
            "$group": {
                "_id": {
                    "day": {"$dateToString": {"format": "%Y-%m-%d", "date": "$createdAt"}},
                    "model": model_or_unknown,
                },
                # A debit's delta is negative; flip it to positive credits spent.
                "credits": {"$sum": {"$subtract": [0, "$amount_delta_micro"]}},
                "requests": {"$sum": 1},
                "tokens": {"$sum": tokens_or_zero},
            }
        },
    ]


async def spend_by_model(
    workspace: str,
    *,
    since: datetime,
    until: datetime,
    causes: tuple[str, ...] = ("compute_spend", "litellm_spend"),
) -> list[ModelSpendRow]:
    """Break a workspace's spend down by (UTC day, model) over a window.

    Returns one ``ModelSpendRow`` per (day, model) that had spend in the window,
    summed across ``causes``. This is the read the billing usage graph uses so the
    "Usage by model" chart is sourced from the wallet's OWN ledger — the universal
    meter — rather than the LiteLLM proxy. The two spend causes
    (``compute_spend`` in the default off-mode, ``litellm_spend`` after a live
    cutover) are mutually exclusive per run, so reading both is safe: a given run
    is billed under exactly one cause and an off->live cutover leaves both in
    history with no double-count.

    The window is on ``createdAt``: ``since`` is inclusive (``>=``), ``until`` is
    EXCLUSIVE (``<``) — identical to ``sum_debits_by_cause`` so adjacent windows
    never double-count a boundary entry. Only ``applied is True`` entries are
    counted (a committed-but-unapplied phantom never moved the wallet, so it must
    not appear on the chart, the same rule ``reconcile`` enforces).

    Per entry: ``day`` is ``createdAt.date()`` in UTC (``YYYY-MM-DD``); ``model``
    is ``ref.model`` or ``"unknown"`` when the debit carried no model (real
    charged spend with no model still counts toward the day so the chart
    reconciles with the wallet); ``credits_micro`` is the POSITIVE amount debited in
    micro-credits (a stray positive delta is clamped out, like
    ``sum_debits_by_cause``) and ``credits`` is that figure truncated to whole
    credits for display. READ ``credits_micro`` — a group of ordinary light usage
    (a chat run is about 375_000 micro) truncates to 0 whole credits, and an
    aggregate built by summing the truncated field compounds that shortfall over
    every group. ``requests`` is the COUNT of entries in the (day, model) group.
    ``tokens`` is the real total token volume for the group, summed from each
    entry's ``ref.total_tokens`` (the metering path now records it — a legacy entry
    without it contributes 0, so the figure reflects real volume going forward
    without breaking historical reads).

    The fold runs SERVER-SIDE (``_spend_by_model_pipeline``): a Mongo ``$match``
    on the tenant, causes, applied flag and window feeds a ``$group`` on (day,
    model), so a busy tenant's window is never materialised in the process. The
    window is the CALLER's and ``billing/usage.py`` allows up to 366 days of it,
    which is exactly why this must not be a pull-all-then-fold. The
    ``(workspace, createdAt)`` compound index serves the ``$match``.

    EE entity-isolation: billing must not read ``CreditLedgerEntry`` directly, so
    this owns the read (sibling to ``sum_debits_by_cause``). It performs NO writes.
    """
    if not workspace:
        raise ValidationError("credits.invalid_workspace", "workspace is required")

    query: dict[str, Any] = {
        "workspace": workspace,
        "cause": {"$in": list(causes)},
        "applied": True,
        "createdAt": {"$gte": since, "$lt": until},
    }
    # ``aggregate()`` returns a coroutine under the pymongo-async client the app
    # uses in prod and a directly-iterable latent cursor under mongomock-motor;
    # discriminate the same way ``_sum_amount_delta`` does.
    cursor = CreditLedgerEntry.get_pymongo_collection().aggregate(_spend_by_model_pipeline(query))
    if inspect.isawaitable(cursor):
        cursor = await cursor

    rows: list[ModelSpendRow] = []
    async for doc in cursor:
        key = doc.get("_id") or {}
        # The pipeline sums the micro field. Carry it through EXACTLY — truncating
        # here is what made a day of sub-credit runs chart as zeros. ``credits``
        # remains for display; callers that aggregate must fold the micro figure.
        group_micro = int(doc.get("credits") or 0)
        rows.append(
            ModelSpendRow(
                day=str(key.get("day") or ""),
                model=str(key.get("model") or "unknown"),
                credits=micro_to_credits(group_micro),
                credits_micro=group_micro,
                requests=int(doc.get("requests") or 0),
                tokens=int(doc.get("tokens") or 0),
            )
        )
    # ``$group`` makes no ordering promise (nor did the dict this used to build).
    # The caller sorts for display; sort here too so the read itself is stable.
    rows.sort(key=lambda r: (r.day, r.model))
    return rows


async def history(
    workspace: str,
    *,
    limit: int = 50,
    cursor: str | None = None,
    cause: str | None = None,
) -> tuple[list[LedgerEntry], str | None]:
    """Page the workspace's ledger, newest first.

    Returns ``(entries, next_cursor)`` where ``next_cursor`` is the id to pass
    as ``cursor`` for the following page, or ``None`` when this is the last
    page. ``cursor`` is the last id from the previous page — only entries
    OLDER than it are returned (id is a monotonic ObjectId, so ``_id < cursor``
    is a stable "older than" predicate).

    ``cause`` is an optional exact-match filter (e.g. a platform operator
    scoping the wallet history to ``"platform.adjust"`` entries only). Additive:
    the default ``None`` preserves every existing caller's behavior unchanged.
    """
    if not workspace:
        raise ValidationError("credits.invalid_workspace", "workspace is required")
    limit = max(1, min(int(limit), 200))

    query: dict[str, Any] = {"workspace": workspace}
    if cause:
        query["cause"] = cause
    if cursor:
        try:
            query["_id"] = {"$lt": PydanticObjectId(cursor)}
        except Exception:
            raise ValidationError("credits.invalid_cursor", "cursor is not a valid id") from None

    # Fetch one extra to know whether a further page exists.
    docs = await CreditLedgerEntry.find(query).sort("-_id").limit(limit + 1).to_list()
    has_more = len(docs) > limit
    page = docs[:limit]
    next_cursor = str(page[-1].id) if (has_more and page) else None
    return [_entry_to_domain(d) for d in page], next_cursor


async def reconcile(workspace: str) -> ReconcileResult:
    """Re-drive unapplied ledger entries, then repair the CreditBalance doc.

    WARNING — MANUAL RECOVERY TOOL, RUN QUIESCENT ONLY (B4). This is an operator
    repair path for the rare crash-window drift (a committed ledger entry whose
    balance ``$inc`` never landed), NOT a hot-path routine. It re-drives unapplied
    entries and re-sums the ledger in MULTIPLE non-atomic steps, so it MUST run
    with writes to this workspace's wallet PAUSED (quiescent). A concurrent grant
    or debit racing a reconcile can interleave with the re-drive / re-sum and
    corrupt the balance. Do NOT call it on every request, do NOT wire it into the
    grant/debit path, and do NOT "rebuild" it into an always-on reconciler —
    grant/debit are already individually atomic + idempotent; reconcile only exists
    to repair the crash-window phantom after the fact, under operator control.

    The balance is DEFINED as ``sum(amount_delta)`` over the entries whose effect
    actually LANDED (``applied is True``) — NOT over every committed row. A
    committed-but-unapplied entry is a PHANTOM: it survived the step-1 insert but
    crashed before its balance ``$inc``, so summing it blind would invent a
    balance the wallet never held (the review repro: grant 100 + a phantom debit
    -1000 must not yield -950).

    This runs in two phases:

    1. RE-DRIVE every ``applied is False`` entry, oldest first, applying its
       effect for real:
         * a STRICT debit (``conditional is True``) re-drives with the same
           ``balance_credits >= amount`` conditional ``$inc``. If the funds aren't
           there it was never authorized to land — the entry is VOIDED (deleted),
           NOT counted, and NO negative is invented.
         * a grant or ``allow_negative`` debit (``conditional is False``)
           re-drives with an unconditional ``$inc`` (upserting the row).
       Each successfully re-driven entry is stamped ``balance_after`` and marked
       ``applied=True``.
    2. Set ``balance_credits = sum(amount_delta)`` over the now-applied entries
       and write it back when it has drifted. The result is NEVER clamped to
       >= 0 — a legitimately negative balance (from ``allow_negative`` metered
       overage) is preserved as-is.

    Returns a ``ReconcileResult`` of ``(balance, redriven, voided)``. Idempotent:
    a wallet with no phantoms and a balance already in agreement returns
    ``redriven=0, voided=0`` and is left untouched.
    """
    if not workspace:
        raise ValidationError("credits.invalid_workspace", "workspace is required")

    redriven = 0
    voided = 0
    coll = CreditBalance.get_pymongo_collection()

    # Phase 1 — re-drive every unapplied (phantom) entry, oldest first so the
    # conditional debit guards re-evaluate against the same balance order the
    # original calls would have seen.
    unapplied = (
        await CreditLedgerEntry.find(
            CreditLedgerEntry.workspace == workspace,
            CreditLedgerEntry.applied == False,  # noqa: E712 — Beanie field equality, not `is`
        )
        .sort("+_id")
        .to_list()
    )
    for entry in unapplied:
        if entry.conditional:
            # Strict debit: re-apply only if the funds are there. ``amount_delta``
            # is negative, so the required balance is ``-amount_delta``.
            required = -int(entry.amount_delta_micro)
            updated = await coll.find_one_and_update(
                {"workspace": workspace, "balance_micro": {"$gte": required}},
                {
                    "$inc": {"balance_micro": int(entry.amount_delta_micro)},
                    "$currentDate": {"updatedAt": True},
                },
                return_document=ReturnDocument.AFTER,
            )
            if updated is None:
                # Never authorized to land — void it. Do NOT invent a negative.
                logger.warning(
                    "credits.reconcile: workspace=%s voiding unapplied strict debit "
                    "(key=%s, delta=%d) — insufficient funds, never authorized",
                    workspace,
                    entry.idempotency_key,
                    int(entry.amount_delta_micro),
                )
                await entry.delete()
                voided += 1
                continue
        else:
            # Grant or allow_negative debit: unconditional $inc (upsert so a lost
            # balance row is recreated). The balance may legitimately go negative.
            updated = await coll.find_one_and_update(
                {"workspace": workspace},
                {
                    "$inc": {"balance_micro": int(entry.amount_delta_micro)},
                    "$setOnInsert": {"createdAt": datetime.now(UTC)},
                    "$currentDate": {"updatedAt": True},
                },
                upsert=True,
                return_document=ReturnDocument.AFTER,
            )

        # The re-drive landed: stamp and mark applied.
        entry.balance_after_micro = int(updated["balance_micro"])
        entry.applied = True
        await entry.save()
        redriven += 1
        logger.warning(
            "credits.reconcile: workspace=%s re-drove unapplied entry (key=%s, delta=%d) "
            "→ balance_after_micro=%d",
            workspace,
            entry.idempotency_key,
            int(entry.amount_delta_micro),
            entry.balance_after_micro,
        )

    # Phase 2 — the canonical balance is the sum over the APPLIED entries.
    applied_entries = await CreditLedgerEntry.find(
        CreditLedgerEntry.workspace == workspace,
        CreditLedgerEntry.applied == True,  # noqa: E712 — Beanie field equality, not `is`
    ).to_list()
    computed = sum(int(e.amount_delta_micro) for e in applied_entries)

    if computed < 0:
        # Not an error — a metered allow_negative overage legitimately drives the
        # balance below zero. Log it for visibility, but NEVER alter the value.
        logger.warning(
            "credits.reconcile: workspace=%s reconciled to a negative balance (%d) — "
            "metered overage; preserving as-is",
            workspace,
            computed,
        )

    bal_doc = await CreditBalance.find_one(CreditBalance.workspace == workspace)
    if bal_doc is None:
        if computed == 0:
            # No wallet and no applied movements — nothing to repair.
            return ReconcileResult(balance=0, redriven=redriven, voided=voided)
        # Applied entries exist but the balance row was lost — recreate it.
        await coll.update_one(
            {"workspace": workspace},
            {
                "$set": {"balance_micro": computed},
                "$setOnInsert": {"createdAt": datetime.now(UTC)},
                "$currentDate": {"updatedAt": True},
            },
            upsert=True,
        )
        logger.warning(
            "credits.reconcile: workspace=%s had applied-ledger sum=%d but no balance row; "
            "recreated",
            workspace,
            computed,
        )
        return ReconcileResult(balance=micro_to_credits(computed), redriven=redriven, voided=voided)

    if int(bal_doc.balance_micro) != computed:
        logger.warning(
            "credits.reconcile: workspace=%s balance drifted (stored=%d, applied-ledger=%d); "
            "repaired",
            workspace,
            int(bal_doc.balance_micro),
            computed,
        )
        await coll.update_one(
            {"workspace": workspace},
            {"$set": {"balance_micro": computed}, "$currentDate": {"updatedAt": True}},
        )
    # The repair above works in micro end to end — that is what keeps the ledger
    # invariant exact. Only the reported figure is coarsened, for the caller.
    return ReconcileResult(balance=micro_to_credits(computed), redriven=redriven, voided=voided)


# ---------------------------------------------------------------------------
# Startup guard
# ---------------------------------------------------------------------------

# The field names this module replaced on 2026-09-04, and the collections that
# still hold them on a database the micro-credit migration has not converted.
_PRE_MICRO_FIELDS: tuple[tuple[str, str], ...] = (
    ("credit_balances", "balance_credits"),
    ("credit_ledger", "amount_delta"),
    ("credit_ledger", "balance_after"),
)

_MIGRATION_COMMAND = "python -m pocketpaw_ee.cloud.credits.migrate_micro_credits"


async def verify_wallet_migrated() -> None:
    """Fail-fast guard: refuse to boot on a wallet still in whole credits.

    Renaming ``balance_credits`` to ``balance_micro`` made the old shape
    unreadable, but only half of it fails loudly. A ledger row missing
    ``amount_delta_micro`` raises on parse and the history endpoint 500s. A
    BALANCE row missing ``balance_micro`` does not: the field carries a default of
    0, so an unconverted wallet reads as an empty one. Every customer's balance
    shows zero, the run-start gate refuses them, and nothing anywhere logs a
    problem.

    That asymmetry is why the guard is here rather than in the reader. A document
    holding 700 credits and a document holding nothing are indistinguishable once
    the field is absent, so no read can be written that gets both right — the only
    honest move is to not serve at all. This ran in production on 2026-09-04
    because the code shipped and the migration did not; the container came up
    happily and told paying customers they were broke.

    Called from ``init_cloud_db`` after ``init_beanie``, beside the memory-backend
    guard, and raises ``RuntimeError`` naming the collection, the field and the
    script that fixes it.
    """
    db = CreditBalance.get_pymongo_collection().database
    stale: list[str] = []
    for collection, field in _PRE_MICRO_FIELDS:
        # ``limit=1`` — the guard needs to know IF one exists, never how many, and
        # this runs on the boot path of a deployment with a live ledger.
        if await db[collection].count_documents({field: {"$exists": True}}, limit=1):
            stale.append(f"{collection}.{field}")

    if not stale:
        return

    raise RuntimeError(
        "cloud startup: the credit wallet is still in whole credits — "
        f"{', '.join(stale)} present. This build reads micro-credits, and an "
        "unconverted balance row reads as an EMPTY wallet rather than failing, so "
        "serving it would tell paying customers they have no credits. Refusing to "
        f"start. Run `{_MIGRATION_COMMAND} --dry-run` to see what it would "
        f"convert, then `{_MIGRATION_COMMAND}` to convert it."
    )


__all__ = [
    "GrantResult",
    "ModelSpendRow",
    "ReconcileResult",
    "balance",
    "balance_micro",
    "check_balance",
    "check_quota",
    "debit",
    "find_by_key",
    "grant",
    "has_paid_credits",
    "history",
    "is_recorded",
    "month_to_date_spend",
    "reconcile",
    "record_no_movement",
    "spend_by_model",
    "sum_debits_by_cause",
    "unapplied_count",
    "verify_wallet_migrated",
    "wallet_exists",
]
