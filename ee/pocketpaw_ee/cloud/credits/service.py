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
#      back onto the ledger entry so the audit row is self-describing.
#
#   The only crash window is between the step-1 insert and the step-2 $inc: a
#   committed ledger entry whose effect never landed on the balance.
#   ``reconcile`` closes it — balance is defined as ``sum(amount_delta)`` over
#   the committed ledger, so re-summing repairs any drift.
#
# Rule 9 — ``emit(CreditMovement(...))`` fires after each SUCCESSFUL grant /
# debit (never on a no-op replay). Rule 10 — raise ``InsufficientCredits``
# (CloudError), never HTTPException.
#
# Created 2026-06-23 (integration/billing-credits, BC-1): new entity.

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

    # Step 1 — insert the ledger entry FIRST (the exactly-once guard).
    entry = CreditLedgerEntry(
        workspace=workspace,
        kind=kind,
        amount_delta=amount,
        balance_after=0,  # stamped after the $inc returns the new balance
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

    # Step 3 — write balance_after back onto the audit row.
    entry.balance_after = new_balance
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
) -> int:
    """Remove ``amount`` credits from the workspace wallet. Atomic + idempotent.

    Returns the new balance. Raises ``InsufficientCredits`` (402) when the
    wallet holds fewer than ``amount`` credits — with ZERO side effects (no
    ledger entry, no balance change). A retried call with the same
    ``(workspace, idempotency_key)`` is a no-op that returns the current
    balance.
    """
    # Rule 6 — validate at entry.
    if not isinstance(amount, int) or isinstance(amount, bool) or amount <= 0:
        raise ValidationError("credits.invalid_amount", "Debit amount must be a positive integer")
    if not workspace:
        raise ValidationError("credits.invalid_workspace", "workspace is required")
    if not idempotency_key:
        raise ValidationError("credits.invalid_key", "idempotency_key is required")

    # Step 1 — insert the ledger entry FIRST (the exactly-once guard). The delta
    # is signed negative for a debit.
    entry = CreditLedgerEntry(
        workspace=workspace,
        kind=kind,
        amount_delta=-amount,
        balance_after=0,  # stamped after the $inc returns the new balance
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

    # Step 2 — apply the effect: CONDITIONAL $inc filtered on
    # ``balance_credits >= amount`` so two racing debits can never both pass the
    # funds check (compare-and-swap, not read-then-write). NO upsert — a wallet
    # that doesn't exist yet has zero credits and cannot satisfy the filter.
    coll = CreditBalance.get_pymongo_collection()
    updated = await coll.find_one_and_update(
        {"workspace": workspace, "balance_credits": {"$gte": amount}},
        {"$inc": {"balance_credits": -amount}, "$currentDate": {"updatedAt": True}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        # Insufficient funds. Roll back the ledger entry we inserted in step 1 so
        # the rejected debit leaves NO trace (invariant b) and a retry with the
        # same key can re-evaluate cleanly (the key is free again).
        await entry.delete()
        available = await _current_balance(workspace)
        raise InsufficientCredits(amount, available)

    new_balance = int(updated["balance_credits"])

    # Step 3 — write balance_after back onto the audit row.
    entry.balance_after = new_balance
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
    """Recompute the balance from the ledger and repair the CreditBalance doc.

    Balance is DEFINED as ``sum(amount_delta)`` over the workspace's committed
    ledger. This re-sums every entry and writes the result back onto the
    CreditBalance doc when it has drifted (covers a crash between a ledger
    insert and its balance ``$inc``). Returns the reconciled balance.

    Idempotent: a wallet already in agreement is left untouched (and the call
    still returns the correct balance).
    """
    if not workspace:
        raise ValidationError("credits.invalid_workspace", "workspace is required")

    entries = await CreditLedgerEntry.find(CreditLedgerEntry.workspace == workspace).to_list()
    computed = sum(int(e.amount_delta) for e in entries)

    coll = CreditBalance.get_pymongo_collection()
    bal_doc = await CreditBalance.find_one(CreditBalance.workspace == workspace)
    if bal_doc is None:
        if computed == 0:
            # No wallet and no movements — nothing to repair.
            return 0
        # A ledger exists but the balance row was lost — recreate it.
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
            "credits.reconcile: workspace=%s had ledger sum=%d but no balance row; recreated",
            workspace,
            computed,
        )
        return computed

    if int(bal_doc.balance_credits) != computed:
        logger.warning(
            "credits.reconcile: workspace=%s balance drifted (stored=%d, ledger=%d); repaired",
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
    "debit",
    "grant",
    "history",
    "reconcile",
]
