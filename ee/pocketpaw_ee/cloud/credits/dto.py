# ee/pocketpaw_ee/cloud/credits/dto.py — request/response schemas for the
# credit ledger HTTP surface (BC-1, the Ledger primitive).
#
# Distinct Request / Response DTOs per the EE cloud rule 4. The read surface
# (balance + history) is the only HTTP-exposed half in BC-1; grant / debit are
# called in-process by the rest of the billing subsystem (top-ups, subscription
# grants, compute spend), not from a public route.
#
# Created 2026-06-23 (integration/billing-credits, BC-1): new entity.

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from pocketpaw_ee.cloud.credits.domain import LedgerEntry


class BalanceResponse(BaseModel):
    """Current spendable balance for the caller's workspace.

    ``balance_credits`` is integer credits (1 credit == $0.01).
    """

    workspace_id: str
    balance_credits: int


class LedgerEntryResponse(BaseModel):
    """One movement on the wire — mirrors ``domain.LedgerEntry``."""

    id: str
    workspace_id: str
    kind: str
    amount_delta: int
    balance_after: int
    member_id: str | None = None
    cause: str | None = None
    ref: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str = ""
    created_at: datetime | None = None


class HistoryResponse(BaseModel):
    """A page of ledger movements, newest first.

    ``next_cursor`` is the id to pass back as ``cursor`` for the next page,
    or ``None`` when the page is the last one.
    """

    entries: list[LedgerEntryResponse] = Field(default_factory=list)
    next_cursor: str | None = None


def ledger_entry_to_dto(entry: LedgerEntry) -> LedgerEntryResponse:
    """Map a frozen ``domain.LedgerEntry`` to its wire DTO."""
    return LedgerEntryResponse(
        id=entry.id,
        workspace_id=entry.workspace_id,
        kind=entry.kind,
        amount_delta=entry.amount_delta,
        balance_after=entry.balance_after,
        member_id=entry.member_id,
        cause=entry.cause,
        ref=dict(entry.ref),
        idempotency_key=entry.idempotency_key,
        created_at=entry.created_at,
    )
