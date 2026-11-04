# ee/pocketpaw_ee/site_ai/budget.py — reserve, settle, release: the site AI hard caps.
#
# Every call is held against its worst-case cost BEFORE the provider is called:
#   reserve  insert the ``SiteAiCallDoc`` (state held, expires_at), then for each
#            cap in order one conditional ``$inc`` on that cap's counter:
#            ``{key, committed_micro <= cap - amount}`` -> committed += amount,
#            held += amount. The test and the increment are one atomic Mongo
#            operation, so N concurrent calls can never commit past a cap. If a
#            cap refuses, every counter already taken is rolled back and the call
#            is released; the caller answers ``budget_exhausted``.
#   settle   held -> settled (one conditional transition, so it happens once), then
#            per counter: committed += actual - reserved, held -= reserved,
#            spent += actual. If the sweeper released the hold first, the actual
#            spend is added on its own so nothing is lost.
#   release  held -> released, counters give the whole hold back (an error before
#            any output costs nothing).
#   sweep    ``sweep_expired_holds`` ends holds whose call died (crash, kill) after
#            ``HOLD_TTL``: a call that was SENT (``mark_sent``) settles to its full
#            hold and is debited (the provider charged us); an unsent one releases.
#            Registered in ``extensions._sweeps``.
# Settle is unconditional (real spend can pass a cap); a counter past its cap then
# refuses every new hold until the period rolls over.
# The wallet hold: ``wallet_cap`` holds every call against the workspace balance
# (plus ``WALLET_OVERDRAFT_MICRO``) on one workspace-wide in-flight counter, so
# concurrent calls across sites can't push the wallet far below zero. That counter
# is held only: settle and release both give the hold back, the debit is the spend.
#
# Counters (``SiteAiCounterDoc``) are keyed "{ws}:{pocket}:{scope}:{period}"; caps
# per caller come from ``caps_for``. Wallet debits are separate (``bill_run``,
# keyed ``run:{run_id}``); these counters are what the site's caps are tested on.

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from pocketpaw_ee.cloud.models.site_ai import SiteAiCallDoc, SiteAiCounterDoc
from pocketpaw_ee.site_ai import service

logger = logging.getLogger(__name__)

# Long enough for the longest stream (8k tokens at a slow rate); a call that
# outlives it still settles (see ``settle``), it just stops holding headroom.
HOLD_TTL = timedelta(minutes=15)
WALLET_OVERDRAFT_MICRO = 10 * 1_000_000
_WALLET_SUFFIX = ":wallet:inflight"


class BudgetRefused(Exception):
    def __init__(self, scope: str) -> None:
        self.scope = scope
        super().__init__(scope)


@dataclass(frozen=True)
class Cap:
    scope: str  # day | month | preview | visitor:<hash> | wallet
    period: str
    limit: int
    key: str | None = None  # overrides the per-site key (the wallet counter)


@dataclass
class Hold:
    run_id: str
    workspace_id: str
    pocket_id: str
    reserved: int
    counters: list[str]


def caps_for(
    *,
    caller: str,
    budget: Any,
    allowance: int,
    visitor: str | None = None,
    now: datetime | None = None,
) -> list[Cap]:
    """The caps one call is held against, by caller:
    preview -> the preview daily cap only (same wallet, its own small cap);
    worker  -> site day + month (month = min(owner cap, plan allowance)), plus the
               per-visitor daily cap for a hashed ``user`` when one is sent;
    browser -> site day + month + the per-visitor daily cap (always)."""
    day, month = service.day_period(now), service.month_period(now)
    if caller == "preview":
        return [Cap("preview", day, int(budget.preview_daily_cap_micro))]
    caps = [
        Cap("day", day, int(budget.daily_cap_micro)),
        Cap("month", month, min(int(budget.monthly_cap_micro), int(allowance))),
    ]
    if visitor:
        caps.append(Cap(f"visitor:{visitor}", day, int(budget.per_visitor_daily_micro)))
    return caps


def _coll():
    return SiteAiCounterDoc.get_pymongo_collection()


def wallet_key(workspace_id: str) -> str:
    return f"{workspace_id}:*{_WALLET_SUFFIX}"


async def wallet_cap(workspace_id: str) -> Cap | None:
    """The workspace in-flight cap, when credits are enforced: holds may not exceed
    the balance plus a small overdraft."""
    from pocketpaw.config import get_settings

    settings = get_settings()
    if not (settings.billing_enforced or settings.credits_enforced):
        return None
    from pocketpaw_ee.cloud.credits import service as credits_service

    balance = await credits_service.balance_micro(workspace_id)
    return Cap("wallet", "inflight", balance + WALLET_OVERDRAFT_MICRO, wallet_key(workspace_id))


async def mark_sent(run_id: str) -> None:
    await SiteAiCallDoc.get_pymongo_collection().update_one(
        {"run_id": run_id, "sent_at": None}, {"$set": {"sent_at": datetime.now(UTC)}}
    )


async def _ensure_counter(key: str, workspace_id: str, pocket_id: str) -> None:
    try:
        await _coll().update_one(
            {"key": key},
            {
                "$setOnInsert": {
                    "workspace": workspace_id,
                    "pocket_id": pocket_id,
                    "committed_micro": 0,
                    "held_micro": 0,
                    "spent_micro": 0,
                    "calls": 0,
                    "created_at": datetime.now(UTC),
                }
            },
            upsert=True,
        )
    except DuplicateKeyError:  # a concurrent upsert won; the row exists
        pass


async def reserve(
    *,
    run_id: str,
    workspace_id: str,
    pocket_id: str,
    site_id: str,
    caller: str,
    model: str,
    amount: int,
    caps: list[Cap],
    kind: str = "chat",
    preset_id: str | None = None,
    preset_version: int | None = None,
) -> Hold:
    now = datetime.now(UTC)
    amount = max(1, int(amount))
    await SiteAiCallDoc(
        run_id=run_id,
        workspace=workspace_id,
        pocket_id=pocket_id,
        site_id=site_id,
        caller=caller,  # type: ignore[arg-type]
        model=model,
        kind=kind,  # type: ignore[arg-type]
        preset_id=preset_id,
        preset_version=preset_version,
        reserved_micro=amount,
        state="held",
        day=service.day_period(now),
        created_at=now,
        expires_at=now + HOLD_TTL,
    ).insert()
    taken: list[str] = []
    calls = SiteAiCallDoc.get_pymongo_collection()
    for cap in caps:
        key = cap.key or service.counter_key(workspace_id, pocket_id, cap.scope, cap.period)
        await _ensure_counter(key, workspace_id, pocket_id)
        doc = await _coll().find_one_and_update(
            {"key": key, "committed_micro": {"$lte": cap.limit - amount}},
            {"$inc": {"committed_micro": amount, "held_micro": amount}},
            return_document=ReturnDocument.AFTER,
        )
        if doc is None:
            for got in taken:
                await _coll().update_one(
                    {"key": got}, {"$inc": {"committed_micro": -amount, "held_micro": -amount}}
                )
            await calls.update_one(
                {"run_id": run_id},
                {"$set": {"state": "released", "counters": [], "settled_at": now}},
            )
            raise BudgetRefused(cap.scope.split(":", 1)[0])
        taken.append(key)
        await calls.update_one({"run_id": run_id}, {"$push": {"counters": key}})
    return Hold(run_id, workspace_id, pocket_id, amount, taken)


async def settle(hold: Hold, actual: int, input_tokens: int, output_tokens: int) -> None:
    actual = max(0, int(actual))
    now = datetime.now(UTC)
    moved = await SiteAiCallDoc.get_pymongo_collection().find_one_and_update(
        {"run_id": hold.run_id, "state": "held"},
        {
            "$set": {
                "state": "settled",
                "actual_micro": actual,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "settled_at": now,
            }
        },
    )
    if moved is not None:
        for key in moved.get("counters") or []:
            if key.endswith(_WALLET_SUFFIX):
                await _coll().update_one(
                    {"key": key},
                    {"$inc": {"committed_micro": -hold.reserved, "held_micro": -hold.reserved}},
                )
                continue
            await _coll().update_one(
                {"key": key},
                {
                    "$inc": {
                        "committed_micro": actual - hold.reserved,
                        "held_micro": -hold.reserved,
                        "spent_micro": actual,
                        "calls": 1,
                    }
                },
            )
        return
    # The sweeper released this hold already (the call outlived HOLD_TTL): record
    # the spend on its own so the counters and usage still see it.
    released = await SiteAiCallDoc.get_pymongo_collection().find_one_and_update(
        {"run_id": hold.run_id, "state": "released", "actual_micro": 0},
        {
            "$set": {
                "state": "settled",
                "actual_micro": actual,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "settled_at": now,
            }
        },
    )
    if released is not None:
        for key in hold.counters:
            if key.endswith(_WALLET_SUFFIX):
                continue
            await _coll().update_one(
                {"key": key},
                {"$inc": {"committed_micro": actual, "spent_micro": actual, "calls": 1}},
            )


async def _release_doc(doc: dict[str, Any]) -> bool:
    moved = await SiteAiCallDoc.get_pymongo_collection().find_one_and_update(
        {"run_id": doc["run_id"], "state": "held"},
        {"$set": {"state": "released", "settled_at": datetime.now(UTC)}},
    )
    if moved is None:
        return False
    amount = int(moved.get("reserved_micro") or 0)
    for key in moved.get("counters") or []:
        await _coll().update_one(
            {"key": key}, {"$inc": {"committed_micro": -amount, "held_micro": -amount}}
        )
    return True


async def release(hold: Hold) -> None:
    await _release_doc({"run_id": hold.run_id})


async def _debit_dead_call(row: SiteAiCallDoc) -> None:
    """Bill a sent call whose process died before it could: its full hold, keyed
    ``run:{run_id}`` like every site AI debit. Skipped in LiteLLM ``live`` mode,
    where the proxy's own spend ingest bills the call."""
    from pocketpaw_ee.cloud.credits import service as credits_service
    from pocketpaw_ee.cloud.llm_provisioning import service as provisioning_service

    if provisioning_service.spend_mode() == "live" or row.reserved_micro <= 0:
        return
    await credits_service.debit(
        workspace=row.workspace,
        amount_micro=row.reserved_micro,
        cause="compute_spend",
        idempotency_key=f"run:{row.run_id}",
        ref={
            "run_id": row.run_id,
            "kind": "site_ai",
            "pocket_id": row.pocket_id,
            "site_id": row.site_id,
            "model": f"site-ai/{row.model}",
            "cost_source": "site_ai_hold",
        },
        allow_negative=True,
    )


async def sweep_expired_holds(now: datetime | None = None, limit: int = 500) -> int:
    """End holds past their expiry (the call's process died). A sent call settles to
    its full hold and is debited; an unsent one is released. Returns how many."""
    now = now or datetime.now(UTC)
    rows = (
        await SiteAiCallDoc.find({"state": "held", "expires_at": {"$lt": now}})
        .limit(limit)
        .to_list()
    )
    ended = 0
    for row in rows:
        if row.sent_at is None:
            ended += int(await _release_doc({"run_id": row.run_id}))
            continue
        hold = Hold(row.run_id, row.workspace, row.pocket_id, row.reserved_micro, row.counters)
        await settle(hold, row.reserved_micro, 0, 0)
        try:
            await _debit_dead_call(row)
        except Exception:  # noqa: BLE001 - the next pass retries the same key
            logger.warning("site_ai: dead-call debit failed for %s", row.run_id, exc_info=True)
        ended += 1
    if ended:
        logger.info("site_ai: ended %d expired hold(s)", ended)
    return ended


__all__ = [
    "HOLD_TTL",
    "BudgetRefused",
    "Cap",
    "Hold",
    "caps_for",
    "mark_sent",
    "wallet_cap",
    "wallet_key",
    "release",
    "reserve",
    "settle",
    "sweep_expired_holds",
]
