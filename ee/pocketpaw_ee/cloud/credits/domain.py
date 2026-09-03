# ee/pocketpaw_ee/cloud/credits/domain.py — frozen value objects for the
# credit ledger entity (BC-1, the Ledger primitive).
#
# These are the plain, framework-free shapes the service hands back across the
# entity boundary — never the Beanie documents themselves. Only
# ``credits.service`` imports ``models.credit``; routers / DTOs / other entities
# consume these domain objects (the same entity-isolation boundary the pockets
# entity uses).
#
# Created 2026-06-23 (integration/billing-credits, BC-1): new entity.
# Updated 2026-06-24 (B1 review fix): added ``GrantResult`` — ``grant`` now
# reports whether the ledger entry was NEWLY created (``created=True``) vs a
# duplicate-key replay (``created=False``), alongside the new balance. Billing's
# webhook capture-event emit was previously gated on a balance-delta heuristic
# (``new_balance == before + amount``) that mis-fires under concurrency: a racing
# grant could make a genuine first grant look like a replay (suppressing the
# capture emit) or a replay look genuine (a spurious emit). ``created`` is the
# authoritative signal — set True only when the insert landed, False on
# ``DuplicateKeyError``.
# Changed 2026-06-29 (fix/billing-usage-ledger-source): added ``ModelSpendRow`` —
# one (day, model) spend aggregate the credits service hands back from
# ``spend_by_model`` so the billing usage graph can be sourced from the wallet's
# own ledger (the universal meter, mode-agnostic across compute_spend /
# litellm_spend) instead of the LiteLLM proxy. Framework-free like the other
# domain shapes — billing consumes this, never the Beanie doc.
# Changed 2026-07-11 (feat/llm-cost-attribution): ``ModelSpendRow`` gained a
# ``tokens`` field — the real per-(day, model) token volume summed from the debit
# ``ref.total_tokens`` the metering path now records. Defaults to 0 so a legacy
# entry (written before the ref carried tokens) contributes nothing rather than
# breaking the read; the credits + requests figures are unaffected.

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class GrantResult:
    """The outcome of a ``credits.grant`` call.

    ``balance`` is the wallet balance after the call. ``created`` is True only
    when this call NEWLY inserted the ledger entry (a real grant applied); it is
    False when the call collided on the unique ``(workspace, idempotency_key)``
    index — i.e. a duplicate-key REPLAY of an already-applied grant (a no-op that
    returns the current balance). Callers gate their "money moved" side effects
    (e.g. billing's capture-event emit) on ``created``, never on a balance delta,
    which is unreliable under concurrency.
    """

    balance: int
    created: bool


@dataclass(frozen=True)
class ModelSpendRow:
    """One (day, model) spend aggregate over a workspace's credit ledger.

    The unit the credits service returns from ``spend_by_model`` so the billing
    usage graph reads the wallet's own decomposition rather than the LiteLLM
    proxy. ``day`` is ``YYYY-MM-DD`` (UTC, from the entry's ``createdAt`` date);
    ``model`` is the charged model id (``ref.model``, or ``"unknown"`` when the
    debit carried no model); ``credits`` is the POSITIVE credits debited for that
    (day, model) over the window (integer CREDITS, 1 credit == $0.01 — NOT USD);
    ``requests`` is the number of ledger entries in that group (a proxy for request
    count); ``tokens`` is the real total token volume for the group, summed from
    each debit's ``ref.total_tokens`` (0 for legacy entries written before the ref
    carried tokens — the credits + requests figures stay accurate regardless).
    """

    day: str
    model: str
    credits: int
    requests: int
    tokens: int = 0


@dataclass(frozen=True)
class LedgerEntry:
    """One immutable movement on a workspace's credit wallet.

    Mirrors ``models.credit.CreditLedgerEntry`` with framework-free types.
    ``amount_delta`` is signed; ``balance_after`` is the wallet balance once
    this movement landed. Both are WHOLE credits, for display — the exact figures
    are the ``_micro`` pair beside them, and the two can differ by up to a credit
    on a sub-credit movement (a $0.0015 proxy call is 375_000 micro, which is 0
    whole credits). Anything reasoning about money must read the micro fields;
    ``amount_delta`` exists so the HTTP layer and the audit UI keep rendering the
    unit a customer understands.
    """

    id: str
    workspace_id: str
    kind: str
    amount_delta: int
    balance_after: int
    member_id: str | None
    cause: str | None
    amount_delta_micro: int = 0
    balance_after_micro: int = 0
    ref: dict = field(default_factory=dict)
    idempotency_key: str = ""
    created_at: datetime | None = None


# ---------------------------------------------------------------------------
# The wallet's storage unit (2026-09-04, feat/exact-credit-deduction).
# ---------------------------------------------------------------------------
#
# Balances and ledger amounts are stored in MICRO-CREDITS: millionths of a
# credit, so 1 credit == 1_000_000 micro == $0.01.
#
# WHY. The wallet used to store whole credits, and a credit is a cent, while the
# proxy prices a single API call — routinely a tenth of a cent or less. Every
# debit therefore had to round to a unit far coarser than the thing it was
# charging for. Rounding down served cheap calls free; rounding to the nearest
# credit was unbiased in aggregate but wrong on every individual charge, and
# unboundedly wrong for a workload of uniformly cheap calls where nothing rounds
# up to offset anything. A thousand $0.0015 requests cost $1.50 and billed zero.
#
# A micro-credit is $0.00000001 of pre-markup compute. The smallest spend row
# observed on the production proxy ($0.00014545) is 36,362 of them, so per-row
# rounding error is about eight orders of magnitude below the amount charged.
# Summed over real proxy rows the drift is 2e-9 USD. That is exact for money.
#
# INTEGER, still. This is a finer integer unit, NOT a float: floats cannot hold a
# ledger invariant, and ``balance == sum(amount_delta)`` is checked by
# ``reconcile``. Every atomic ``$inc`` and every CAS comparison keeps working
# unchanged because they are all integer operations either way.
#
# WHAT THE CUSTOMER SEES IS UNCHANGED. The HTTP surface still speaks whole
# credits — ``dto`` converts at the boundary — so no balance, plan, top-up or
# price changes meaning. This is a storage precision change, not a repricing.
MICRO_PER_CREDIT = 1_000_000


def credits_to_micro(credits: int | float) -> int:
    """Whole credits -> micro-credits. For callers that speak the public unit."""
    return round(credits * MICRO_PER_CREDIT)


def micro_to_credits(micro: int) -> int:
    """Micro-credits -> whole credits for DISPLAY, rounded toward zero.

    Truncating rather than rounding is deliberate on the balance: showing a
    customer a credit they cannot spend is worse than showing them one fewer than
    they hold. The exact figure is always in the stored value.
    """
    return int(micro / MICRO_PER_CREDIT)
