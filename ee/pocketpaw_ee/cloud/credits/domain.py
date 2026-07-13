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
    this movement landed.
    """

    id: str
    workspace_id: str
    kind: str
    amount_delta: int
    balance_after: int
    member_id: str | None
    cause: str | None
    ref: dict = field(default_factory=dict)
    idempotency_key: str = ""
    created_at: datetime | None = None
