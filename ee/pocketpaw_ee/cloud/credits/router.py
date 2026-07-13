# ee/pocketpaw_ee/cloud/credits/router.py — the credit ledger read surface
# (BC-1, the Ledger primitive).
#
# Two read routes, both scoped to the caller's CURRENT workspace (resolved via
# the standard ``current_workspace_id`` / ``current_user_id`` deps):
#   * GET /credits/balance  — the current spendable balance.
#   * GET /credits/history  — a page of ledger movements, newest first.
#
# THIN adapters per the "primitive = service + thin adapters" shape — all logic
# lives in ``credits.service``. Grant / debit are NOT exposed here: they are
# called in-process by the rest of the billing subsystem (top-ups, subscription
# grants, compute spend), not from a public route. Mounted in ``mount_cloud()``.
#
# Created 2026-06-23 (integration/billing-credits, BC-1): new entity.

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from pocketpaw_ee.cloud.credits import service as credits_service
from pocketpaw_ee.cloud.credits.dto import (
    BalanceResponse,
    HistoryResponse,
    ledger_entry_to_dto,
)
from pocketpaw_ee.cloud.license import require_license
from pocketpaw_ee.cloud.shared.deps import current_workspace_id

router = APIRouter(prefix="/credits", tags=["Credits"], dependencies=[Depends(require_license)])


@router.get("/balance", response_model=BalanceResponse)
async def get_credit_balance(
    workspace_id: str = Depends(current_workspace_id),
) -> BalanceResponse:
    """Return the current spendable credit balance for the caller's workspace.

    ``balance_credits`` is integer credits (1 credit == $0.01). A workspace
    with no wallet yet reads back ``0``.
    """
    bal = await credits_service.balance(workspace_id)
    return BalanceResponse(workspace_id=workspace_id, balance_credits=bal)


@router.get("/history", response_model=HistoryResponse)
async def get_credit_history(
    workspace_id: str = Depends(current_workspace_id),
    limit: int = Query(default=50, ge=1, le=200),
    cursor: str | None = Query(default=None),
) -> HistoryResponse:
    """Page the workspace's append-only credit ledger, newest first.

    Pass the returned ``next_cursor`` back as ``cursor`` to fetch the next
    (older) page; a ``null`` ``next_cursor`` marks the last page.
    """
    entries, next_cursor = await credits_service.history(workspace_id, limit=limit, cursor=cursor)
    return HistoryResponse(
        entries=[ledger_entry_to_dto(e) for e in entries],
        next_cursor=next_cursor,
    )
