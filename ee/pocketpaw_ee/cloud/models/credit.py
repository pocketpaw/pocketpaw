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
#
# Changed 2026-06-24 (BC-1 reconcile fix): ``CreditLedgerEntry`` gains two
# fields — ``applied`` (the balance ``$inc`` for this entry has landed) and
# ``conditional`` (this is a STRICT debit that must reject on insufficient
# funds). They let ``reconcile`` distinguish an entry whose effect landed from a
# phantom (committed-but-unapplied) entry, and re-drive each phantom the right
# way instead of blindly counting it. Also corrected the ``CreditBalance``
# invariant: the balance MAY go slightly negative from metered compute overage
# (BC-3) — the no-overdraft guarantee is enforced at run-start, not by clamping
# the balance to >= 0 here.
# Changed 2026-06-30 (feat/billing-quota-enforcement, chunk 2): added a second
# ``CreditLedgerEntry`` index ``ix_workspace_created_at`` on
# ``(workspace, createdAt)``. The monthly-quota read (``month_to_date_spend``)
# and the sibling windowed reads (``sum_debits_by_cause`` / ``spend_by_model``)
# all ``$match`` on ``workspace`` + a ``createdAt`` range; this compound index
# keeps that scan off the full per-tenant ledger.

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
    # Integer credits; 1 credit == $0.01. Usually >= 0 — a STRICT debit CAS only
    # decrements a row that already holds >= the debit amount. It MAY go slightly
    # negative from a metered ``allow_negative`` compute-spend debit (BC-3): a
    # completed run is always billed, the spend already happened, so the overage
    # is recorded as a legitimate negative balance rather than dropped. The
    # no-overdraft guarantee is enforced at run-start (a later task) — NOT by
    # clamping this field to >= 0. Always integer; never a float.
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
    ``reconcile`` recomputes ``balance == sum(amount_delta)`` over the rows whose
    effect actually landed (``applied is True``).

    ``applied`` closes the crash window. An entry is inserted FIRST (the
    idempotency guard), then the balance ``$inc`` runs; ``applied`` flips to True
    ONLY after that ``$inc`` lands. A committed entry left at ``applied is False``
    is a phantom — its effect never reached the balance — so ``reconcile`` must
    re-drive it, not count it.

    ``conditional`` records HOW to re-drive a phantom. True only for a STRICT
    debit (one that must reject on insufficient funds): reconcile re-drives it
    with the ``$gte`` conditional ``$inc`` and VOIDS it if the funds aren't there
    (it was never authorized to land). False for grants and for ``allow_negative``
    metered debits: reconcile re-drives those with an unconditional ``$inc``.
    """

    # Tenant scope. Indexed (non-unique) — many entries per workspace.
    workspace: Indexed(str)  # type: ignore[valid-type]
    # One of: ``genesis`` | ``grant`` | ``spend`` | ``transfer``.
    kind: str
    # Signed delta this entry applied (e.g. +500 on a grant, -120 on a spend).
    amount_delta: int
    # Wallet balance once this entry's effect landed. Stamped after the $inc.
    balance_after: int = 0
    # The balance $inc for this entry has LANDED. False is the crash-window
    # state: the entry committed but its effect never reached the balance.
    # ``reconcile`` re-drives every ``applied is False`` entry, then sums only the
    # applied ones. Set True in the grant / debit success paths after the $inc.
    applied: bool = False
    # True only for a STRICT debit (must reject on insufficient funds). False for
    # grants and ``allow_negative`` debits. Tells ``reconcile`` whether to re-drive
    # a phantom with the conditional $gte $inc (and void it on insufficiency) or
    # an unconditional $inc.
    conditional: bool = False
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
            # Windowed-read index. ``month_to_date_spend`` /
            # ``sum_debits_by_cause`` / ``spend_by_model`` all filter the ledger by
            # ``workspace`` + a ``createdAt`` range (and group/sum server-side). This
            # compound index serves that ``$match`` (workspace equality + createdAt
            # range scan) so the monthly-quota aggregation never collection-scans a
            # busy tenant's full ledger.
            IndexModel(
                [("workspace", 1), ("createdAt", 1)],
                name="ix_workspace_created_at",
            ),
        ]
