# ee/pocketpaw_ee/cloud/credits/service.py — the credit ledger business logic
# (BC-1, the Ledger primitive). Sole owner of writes to the ``CreditBalance``
# and ``CreditLedgerEntry`` Beanie documents (entity isolation — only THIS
# module imports ``models.credit``).
#
# Module-level ``async def`` API (NOT a class, per EE cloud rule). Public API:
#   * ``grant``     — add credits, idempotent. Returns the new balance.
#   * ``debit``     — remove credits, idempotent + atomic. Raises
#                     ``InsufficientCredits`` (402) on over-debit, with ZERO
#                     side effects. Returns the new balance.
#   * ``balance``   — read the current spendable balance (tenant-scoped).
#   * ``check_balance`` — raise ``InsufficientCredits`` when the wallet is
#                     out of credits (balance <= 0). The pure, flag-free
#                     assertion behind BC-4's run-start hard-block.
#   * ``history``   — page the append-only ledger, newest first.
#   * ``reconcile`` — recompute ``balance == sum(amount_delta)`` and repair the
#                     CreditBalance doc if it drifted (covers a crash between
#                     the ledger insert and the balance ``$inc``).
#
# ATOMICITY (verdict from grounding: NO Mongo transactions — single-node Mongo
# has no replica set, so ``session.start_transaction`` is unavailable and used
# nowhere). Movements are made atomic with single-document operators only:
#
#   1. Exactly-once guard — INSERT the ledger entry FIRST, stamped with the
#      caller's ``idempotency_key``. The UNIQUE compound index on
#      ``(workspace, idempotency_key)`` makes a retried movement collide on
#      insert (``DuplicateKeyError`` / Mongo code 11000) → the movement was
#      already applied → return the current balance (no-op, no re-apply,
#      no second emit). This holds invariant (a): a duplicate key never
#      double-applies.
#
#   2. Apply the effect with a conditional atomic ``$inc`` on the single
#      CreditBalance doc (the ``find_one_and_update`` CAS idiom — copied from
#      ``auth/service.py``'s claim_home_pocket_id and ``leads/service.py``'s
#      _bump):
#        * grant — unconditional ``$inc: +amount`` with ``upsert=True`` (creates
#          the balance row at 0 then increments in one call).
#        * debit — CONDITIONAL ``$inc: -amount`` filtered on
#          ``balance_credits >= amount``. If it returns ``None`` the wallet had
#          insufficient funds → we DELETE the ledger entry we inserted in step 1
#          (rollback) and raise ``InsufficientCredits``. This holds invariant
#          (b): a rejected over-debit leaves NO ledger entry and NO balance
#          change, AND a later retry with the same key re-evaluates cleanly
#          (the key is free again because the entry was deleted).
#
#   WHY insert-first then rollback-on-reject (not "insert only after a
#   successful $inc"): the ledger entry IS the idempotency token. If we applied
#   the $inc first and inserted the entry second, two concurrent calls with the
#   same key could both pass the $inc before either inserted (double-spend), and
#   the unique-key guard would only catch it AFTER the money already moved. By
#   inserting first, the unique index serializes same-key callers before any
#   balance moves; the conditional $inc serializes DIFFERENT-key debits against
#   the funds check. The deletion on insufficient funds is the only compensating
#   write, and it runs before the balance ever changed, so invariant (b) holds.
#
#   3. The new ``balance_after`` (returned by the atomic ``$inc``) is written
#      back onto the ledger entry, and ``applied`` is flipped to True, so the
#      audit row is self-describing AND ``reconcile`` can tell a landed entry
#      from a phantom.
#
#   The only crash window is between the step-1 insert and the step-2 $inc: a
#   committed ledger entry whose effect never landed on the balance — i.e. one
#   left at ``applied is False``. ``reconcile`` closes it by RE-DRIVING every
#   unapplied entry (not by blindly summing the whole ledger, which would count
#   the phantom as if it had applied).
#
# Rule 9 — ``emit(CreditMovement(...))`` fires after each SUCCESSFUL grant /
# debit (never on a no-op replay). Rule 10 — raise ``InsufficientCredits``
# (CloudError), never HTTPException.
#
# Created 2026-06-23 (integration/billing-credits, BC-1): new entity.
# Changed 2026-06-24 (BC-1 reconcile fix): an adversarial review found
# ``reconcile`` summed ``amount_delta`` over ALL committed entries with no notion
# of whether each entry's $inc actually LANDED, so a phantom (committed but
# unapplied) debit drove the balance to a fabricated value. Three changes close
# it: (1) every entry now carries an ``applied`` flag flipped True only after its
# $inc lands; (2) ``debit`` gains ``allow_negative`` — an unconditional debit for
# metered compute spend (BC-3) that may drive the balance below zero (a completed
# run is always billed); a strict debit is tagged ``conditional=True``; (3)
# ``reconcile`` now RE-DRIVES every ``applied is False`` entry (re-applying grants
# and allow_negative debits unconditionally, re-applying strict debits with the
# $gte guard and VOIDING them if the funds aren't there) and then sums only the
# applied entries. The balance is NEVER clamped to >= 0 — a legitimate negative
# from metered overage is preserved.
# Changed 2026-06-24 (BC-4 run-start hard-block): added ``check_balance`` — a
# pure, flag-free read that raises ``InsufficientCredits`` (402) when the wallet
# is at or below zero. The chat run-start chokepoint (chat/agent_router.py) calls
# it BEFORE create_run/submit, but ONLY when ``settings.billing_enforced`` is on,
# so OSS/self-host (flag default False) is unaffected and IN-FLIGHT runs are never
# touched. Kept flag-free here so the check stays reusable.

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from beanie import PydanticObjectId
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from pocketpaw_ee.cloud._core.errors import InsufficientCredits, ValidationError
from pocketpaw_ee.cloud._core.realtime.emit import emit
from pocketpaw_ee.cloud._core.realtime.events import CreditMovement
from pocketpaw_ee.cloud.credits.domain import LedgerEntry
from pocketpaw_ee.cloud.models.credit import CreditBalance, CreditLedgerEntry

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Mapping helpers
# ---------------------------------------------------------------------------


def _entry_to_domain(doc: CreditLedgerEntry) -> LedgerEntry:
    return LedgerEntry(
        id=str(doc.id),
        workspace_id=doc.workspace,
        kind=doc.kind,
        amount_delta=doc.amount_delta,
        balance_after=doc.balance_after,
        member_id=doc.member_id,
        cause=doc.cause,
        ref=dict(doc.ref or {}),
        idempotency_key=doc.idempotency_key,
        created_at=getattr(doc, "createdAt", None),
    )


async def _current_balance(workspace: str) -> int:
    """Read the workspace's current balance, or 0 when no row exists yet."""
    doc = await CreditBalance.find_one(CreditBalance.workspace == workspace)
    return int(doc.balance_credits) if doc is not None else 0


async def _emit_movement(entry: CreditLedgerEntry) -> None:
    """Emit ``credits.movement`` after a successful grant / debit (rule 9)."""
    await emit(
        CreditMovement(
            data={
                "workspace_id": entry.workspace,
                "kind": entry.kind,
                "amount_delta": entry.amount_delta,
                "balance_after": entry.balance_after,
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
    amount: int,
    cause: str,
    idempotency_key: str,
    *,
    member_id: str | None = None,
    ref: dict | None = None,
    kind: str = "grant",
) -> int:
    """Add ``amount`` credits to the workspace wallet. Idempotent.

    Returns the new balance. A retried call with the same
    ``(workspace, idempotency_key)`` is a no-op that returns the current
    balance — the movement already applied.

    ``kind`` defaults to ``"grant"``; pass ``"genesis"`` to seed a fresh
    wallet's first credits (the ledger origin row).
    """
    # Rule 6 — validate at entry. Money-handling: an amount must be a positive
    # integer (1 credit == $0.01) and the idempotency key must be present.
    if not isinstance(amount, int) or isinstance(amount, bool) or amount <= 0:
        raise ValidationError("credits.invalid_amount", "Grant amount must be a positive integer")
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
        amount_delta=amount,
        balance_after=0,  # stamped after the $inc returns the new balance
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
        # This movement was already applied — return the current balance.
        return await _current_balance(workspace)

    # Step 2 — apply the effect: unconditional $inc with upsert (creates the
    # balance row at 0 then increments in one atomic call).
    coll = CreditBalance.get_pymongo_collection()
    updated = await coll.find_one_and_update(
        {"workspace": workspace},
        {
            "$inc": {"balance_credits": amount},
            "$setOnInsert": {"createdAt": datetime.now(UTC)},
            "$currentDate": {"updatedAt": True},
        },
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )
    new_balance = int(updated["balance_credits"])

    # Step 3 — the $inc landed: stamp balance_after and mark the entry applied so
    # reconcile counts it (and never re-drives it as a phantom).
    entry.balance_after = new_balance
    entry.applied = True
    await entry.save()

    await _emit_movement(entry)
    return new_balance


async def debit(
    workspace: str,
    amount: int,
    cause: str,
    idempotency_key: str,
    *,
    member_id: str | None = None,
    ref: dict | None = None,
    kind: str = "spend",
    allow_negative: bool = False,
) -> int:
    """Remove ``amount`` credits from the workspace wallet. Atomic + idempotent.

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
    # Rule 6 — validate at entry.
    if not isinstance(amount, int) or isinstance(amount, bool) or amount <= 0:
        raise ValidationError("credits.invalid_amount", "Debit amount must be a positive integer")
    if not workspace:
        raise ValidationError("credits.invalid_workspace", "workspace is required")
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
        amount_delta=-amount,
        balance_after=0,  # stamped after the $inc returns the new balance
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
        return await _current_balance(workspace)

    coll = CreditBalance.get_pymongo_collection()
    if allow_negative:
        # Step 2 (metered) — UNCONDITIONAL $inc: the balance may go to or below
        # zero. Upsert so a never-seen wallet still records the spend (it lands
        # at -amount, a legitimate negative). Never raises InsufficientCredits.
        updated = await coll.find_one_and_update(
            {"workspace": workspace},
            {
                "$inc": {"balance_credits": -amount},
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
            {"workspace": workspace, "balance_credits": {"$gte": amount}},
            {"$inc": {"balance_credits": -amount}, "$currentDate": {"updatedAt": True}},
            return_document=ReturnDocument.AFTER,
        )
        if updated is None:
            # Insufficient funds. Roll back the ledger entry we inserted in step
            # 1 so the rejected debit leaves NO trace (invariant b) and a retry
            # with the same key can re-evaluate cleanly (the key is free again).
            await entry.delete()
            available = await _current_balance(workspace)
            raise InsufficientCredits(amount, available)

    new_balance = int(updated["balance_credits"])

    # Step 3 — the $inc landed: stamp balance_after and mark the entry applied so
    # reconcile counts it (and never re-drives it as a phantom).
    entry.balance_after = new_balance
    entry.applied = True
    await entry.save()

    await _emit_movement(entry)
    return new_balance


# ---------------------------------------------------------------------------
# Read API (Rule 7 — every read is tenant-filtered on ``workspace``)
# ---------------------------------------------------------------------------


async def balance(workspace: str) -> int:
    """Return the workspace's current spendable balance (0 when no wallet)."""
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
    bal = await balance(workspace)
    if bal <= 0:
        raise InsufficientCredits(1, max(bal, 0))


async def history(
    workspace: str,
    *,
    limit: int = 50,
    cursor: str | None = None,
) -> tuple[list[LedgerEntry], str | None]:
    """Page the workspace's ledger, newest first.

    Returns ``(entries, next_cursor)`` where ``next_cursor`` is the id to pass
    as ``cursor`` for the following page, or ``None`` when this is the last
    page. ``cursor`` is the last id from the previous page — only entries
    OLDER than it are returned (id is a monotonic ObjectId, so ``_id < cursor``
    is a stable "older than" predicate).
    """
    if not workspace:
        raise ValidationError("credits.invalid_workspace", "workspace is required")
    limit = max(1, min(int(limit), 200))

    query: dict[str, Any] = {"workspace": workspace}
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


async def reconcile(workspace: str) -> int:
    """Re-drive unapplied ledger entries, then repair the CreditBalance doc.

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

    Returns the reconciled balance. Idempotent: a wallet with no phantoms and a
    balance already in agreement is left untouched.
    """
    if not workspace:
        raise ValidationError("credits.invalid_workspace", "workspace is required")

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
            required = -int(entry.amount_delta)
            updated = await coll.find_one_and_update(
                {"workspace": workspace, "balance_credits": {"$gte": required}},
                {
                    "$inc": {"balance_credits": int(entry.amount_delta)},
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
                    int(entry.amount_delta),
                )
                await entry.delete()
                continue
        else:
            # Grant or allow_negative debit: unconditional $inc (upsert so a lost
            # balance row is recreated). The balance may legitimately go negative.
            updated = await coll.find_one_and_update(
                {"workspace": workspace},
                {
                    "$inc": {"balance_credits": int(entry.amount_delta)},
                    "$setOnInsert": {"createdAt": datetime.now(UTC)},
                    "$currentDate": {"updatedAt": True},
                },
                upsert=True,
                return_document=ReturnDocument.AFTER,
            )

        # The re-drive landed: stamp and mark applied.
        entry.balance_after = int(updated["balance_credits"])
        entry.applied = True
        await entry.save()
        logger.warning(
            "credits.reconcile: workspace=%s re-drove unapplied entry (key=%s, delta=%d) "
            "→ balance_after=%d",
            workspace,
            entry.idempotency_key,
            int(entry.amount_delta),
            entry.balance_after,
        )

    # Phase 2 — the canonical balance is the sum over the APPLIED entries.
    applied_entries = await CreditLedgerEntry.find(
        CreditLedgerEntry.workspace == workspace,
        CreditLedgerEntry.applied == True,  # noqa: E712 — Beanie field equality, not `is`
    ).to_list()
    computed = sum(int(e.amount_delta) for e in applied_entries)

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
            return 0
        # Applied entries exist but the balance row was lost — recreate it.
        await coll.update_one(
            {"workspace": workspace},
            {
                "$set": {"balance_credits": computed},
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
        return computed

    if int(bal_doc.balance_credits) != computed:
        logger.warning(
            "credits.reconcile: workspace=%s balance drifted (stored=%d, applied-ledger=%d); "
            "repaired",
            workspace,
            int(bal_doc.balance_credits),
            computed,
        )
        await coll.update_one(
            {"workspace": workspace},
            {"$set": {"balance_credits": computed}, "$currentDate": {"updatedAt": True}},
        )
    return computed


__all__ = [
    "balance",
    "check_balance",
    "debit",
    "grant",
    "history",
    "reconcile",
]
