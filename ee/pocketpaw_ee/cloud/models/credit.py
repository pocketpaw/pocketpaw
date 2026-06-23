# ee/pocketpaw_ee/cloud/models/credit.py — the credit-wallet documents.
#
# Two Beanie documents back the workspace-scoped credit ledger (BC-1, the
# Ledger primitive — the foundation the rest of the billing subsystem grants
# and debits against):
#
#   * ``CreditBalance``     — one row per workspace, the current spendable
#                             balance. Mutated only via the atomic single-doc
#                             ``$inc`` CAS in ``credits.service`` (NO Mongo
#                             transactions — single-node Mongo has no replica
#                             set, so ``session.start_transaction`` is
#                             unavailable). The unique ``workspace`` index keeps
#                             it one row per tenant and lets the grant upsert
#                             key on it.
#   * ``CreditLedgerEntry`` — the append-only audit log. Every movement (genesis
#                             / grant / spend / transfer) is one immutable row.
#                             The UNIQUE compound index on
#                             ``(workspace, idempotency_key)`` is the exactly-once
#                             guard: a retried movement collides on insert
#                             (DuplicateKeyError, Mongo code 11000) so the
#                             service can no-op it.
#
# Credits are integers — 1 credit == $0.01 — so there is no float drift.
#
# Created 2026-06-23 (integration/billing-credits, BC-1): new entity. Both docs
# are registered in ``cloud.models.__init__`` (``get_all_documents()`` +
# ``__all__``) so ``init_beanie`` wires the ``credit_balances`` /
# ``credit_ledger`` collections. Only ``cloud.credits.service`` imports these
# doc classes (entity-isolation boundary, mirroring the pockets entity).

from __future__ import annotations

from beanie import Indexed
from pymongo import IndexModel

from pocketpaw_ee.cloud.models.base import TimestampedDocument


class CreditBalance(TimestampedDocument):
    """The current spendable credit balance for one workspace.

    Exactly one row per workspace (the ``workspace`` index is UNIQUE). The
    balance is mutated only by the atomic conditional ``$inc`` in
    ``credits.service`` — a debit uses a ``{balance_credits: {$gte: amount}}``
    filter so two racing debits can never both pass the funds check
    (compare-and-swap, not read-then-write).
    """

    # UNIQUE — one balance row per workspace. The grant path upserts on this
    # key; the debit path CAS-updates the matching row.
    workspace: Indexed(str, unique=True)  # type: ignore[valid-type]
    # Integer credits; 1 credit == $0.01. Never negative — the debit CAS only
    # decrements a row that already holds >= the debit amount.
    balance_credits: int = 0

    class Settings:
        name = "credit_balances"


class CreditLedgerEntry(TimestampedDocument):
    """One immutable movement on a workspace's credit wallet (append-only).

    Every grant / debit / genesis / transfer writes exactly one entry. The
    entry is inserted FIRST, stamped with ``idempotency_key``; the UNIQUE
    compound index on ``(workspace, idempotency_key)`` makes a duplicate
    movement collide on insert (``DuplicateKeyError`` / Mongo code 11000) so
    the service returns the current balance without re-applying.

    ``amount_delta`` is signed (positive for grant/genesis, negative for
    spend). ``balance_after`` is the wallet balance once this entry's effect
    landed — stamped after the atomic ``$inc`` returns the new balance.
    ``reconcile`` recomputes ``balance == sum(amount_delta)`` over these rows.
    """

    # Tenant scope. Indexed (non-unique) — many entries per workspace.
    workspace: Indexed(str)  # type: ignore[valid-type]
    # One of: ``genesis`` | ``grant`` | ``spend`` | ``transfer``.
    kind: str
    # Signed delta this entry applied (e.g. +500 on a grant, -120 on a spend).
    amount_delta: int
    # Wallet balance once this entry's effect landed. Stamped after the $inc.
    balance_after: int = 0
    # The workspace member responsible, when known (None for system grants).
    member_id: str | None = None
    # Business reason, e.g. ``top_up`` | ``subscription_grant`` | ``promo`` |
    # ``referral`` | ``compute_spend`` | ``genesis``.
    cause: str | None = None
    # Free-form provenance, e.g. ``{"run_id": ...}`` / ``{"event_id": ...}`` /
    # ``{"cost_basis": ...}``. Default-empty so a movement always has a dict.
    ref: dict = {}  # noqa: RUF012 — Beanie field default, not a shared mutable
    # The caller-supplied exactly-once key. Unique per workspace.
    idempotency_key: str

    class Settings:
        name = "credit_ledger"
        indexes = [
            # Exactly-once guard. A retried movement (same workspace + key)
            # collides here on insert → DuplicateKeyError → the service no-ops.
            IndexModel(
                [("workspace", 1), ("idempotency_key", 1)],
                unique=True,
                name="uq_workspace_idempotency_key",
            ),
        ]
