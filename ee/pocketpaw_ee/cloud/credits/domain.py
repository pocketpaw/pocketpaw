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

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


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
