# ee/pocketpaw_ee/cloud/metering/service.py: compute-cost metering (BC-3, the
# Meter and Price primitives) and the one daily usage primitive.
#
# Billing. ``resolve_cost(usage, at=)`` turns a run's ``usage`` into USD: a
# reported ``total_cost_usd`` wins when > 0 (keyless / OAuth backends often
# report 0), else ``usage_tracker.price_run`` prices the tokens at the run's OWN
# moment (``at`` is required: the sweeper bills late and prices are dated), else
# 0. ``load_rate_card()`` is the Price (markup and credit value from settings).
# ``bill_run(run_doc)`` converts the cost to micro-credits and debits the
# workspace once, keyed ``run:{run_id}``, with ``allow_negative=True`` (the
# compute already happened; hard-blocking is the run-start gate's job). A run
# with ``credential_source == "byok"`` is never debited, because the workspace's
# own key paid the provider; its cost is still resolved and returned. The
# debit ``ref`` carries cost, source, model and ``total_tokens``.
# ``bill_run_now`` bills a run the moment its executor returns; the sweeper
# stays the backstop and the ledger key makes a double bill a no-op. Both are
# off in the WU-F ``live`` spend mode, where LiteLLM is the only meter.
#
# Exactly once: BC-1's unique ``(workspace, idempotency_key)`` index is the real
# guard; the run's ``billed`` flag only keeps each sweep bounded. That flag is
# written through ``chat.runs.service.mark_billed`` (EE Rule 2: metering never
# writes ``ChatRunDoc``). ``_prompt_tokens`` decides from the payload whether
# ``input_tokens`` already includes the cached portion.
#
# Daily caps. ``try_spend`` / ``refund`` / ``used`` over the single
# ``DailyUsage`` collection, plus the per-meter cap resolvers, for every daily
# budget (turn, upload, comprehension, transcription, illustration, guest
# turn). One rule: ``None`` = uncapped, ``0`` = disabled, ``n > 0`` = cap; each
# resolver maps its legacy env var onto it. See "DAILY CAPS" below.
#
# Early-access ceiling. ``reserve_early_access`` claims a free run's estimate on
# ``DailyMeter.EARLY_ACCESS_CREDITS`` for the one platform subject
# (``PLATFORM_SUBJECT``, shared by every unpaid workspace) and records it on the
# run. ``bill_run`` claims that record once and moves the meter to the run's
# real spend in whole credits rounded up (a refund for an unused estimate, which
# also releases a run refused before the model call, or a charge past the cap
# for an overrun). Never in the ``live`` spend mode, which skips ``bill_run``.
# ``early_access_credits_used_today`` is the reader for the dashboard.
#
# Module-level ``async def`` API. Only ``CloudError`` subclasses propagate; a
# missing or malformed usage dict is treated as no usage.

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from pymongo import ReturnDocument

from pocketpaw_ee.cloud.chat.runs import service as chat_runs_service
from pocketpaw_ee.cloud.credits import service as credits_service
from pocketpaw_ee.cloud.credits.domain import MICRO_PER_CREDIT, micro_to_credits
from pocketpaw_ee.cloud.metering.domain import ComputeCost, DailyMeter, RateCard, SubjectType
from pocketpaw_ee.cloud.metering.dto import BillResult
from pocketpaw_ee.cloud.models.chat_run import ChatRunDoc
from pocketpaw_ee.cloud.models.daily_usage import DailyUsage

logger = logging.getLogger(__name__)

# Business cause stamped on the ledger movement (matches the credits tests'
# convention and the dashboard's spend filter).
_COMPUTE_SPEND_CAUSE = "compute_spend"


def _int(value: Any) -> int:
    """Coerce a usage count to a non-negative int, tolerating None / strings."""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return 0
    return n if n > 0 else 0


def _total_tokens(usage: dict[str, Any] | None) -> int:
    """Total tokens a run consumed, from its ``usage`` dict.

    This is the REAL per-run token volume the backend reports onto
    ``ChatRunDoc.usage`` (via the ``token_usage`` event) in EVERY metering mode —
    NOT the blocked LiteLLM SpendLogs path. Prefers an explicit ``total_tokens``
    the backend supplied; otherwise sums the components (input + output + cached —
    cached_input are real tokens, so they count, matching the runtime
    usage_tracker's own total). Returns 0 for empty / malformed usage.
    """
    usage = usage or {}
    explicit = _int(usage.get("total_tokens"))
    if explicit > 0:
        return explicit
    return (
        _int(usage.get("input_tokens"))
        + _int(usage.get("output_tokens"))
        + _int(usage.get("cached_input_tokens"))
    )


# ---------------------------------------------------------------------------
# The Meter — resolve a run's real USD compute cost from its usage dict.
# ---------------------------------------------------------------------------


def _prompt_tokens(usage: dict[str, Any]) -> tuple[int, int, int]:
    """``(inclusive prompt tokens, cache reads, cache writes)`` from a usage dict.

    The backends do NOT agree on what ``input_tokens`` means and there is no flag
    saying which convention a given run used, so this reads it off the payload's
    own shape instead of guessing:

      * A payload carrying ``cache_read_tokens`` / ``cache_write_tokens`` is
        Anthropic-shaped (``pydantic_ai``, ``claude_sdk``, ``deep_agents`` — the
        three that emit that structured cache telemetry), and there
        ``input_tokens`` is the UNCACHED REMAINDER. The inclusive total is the
        sum of all three. ``cached_input_tokens`` is deliberately ignored on this
        branch: ``claude_sdk`` sets it to read PLUS write, so adding it as well
        would count the cache twice.
      * Anything else is OpenAI-shaped (``codex_cli``, ``openai_agents``,
        ``google_adk``, ``copilot_sdk``), where ``input_tokens`` already includes
        the cached subset and ``cached_input_tokens`` is that subset. ``max`` is
        belt: a payload where the subset exceeds its own total is malformed, and
        the total is the thing to widen.

    Cache WRITES are 0 on the second branch because those providers do not bill a
    write separately.
    """
    raw_input = _int(usage.get("input_tokens"))
    if "cache_read_tokens" in usage or "cache_write_tokens" in usage:
        read = _int(usage.get("cache_read_tokens"))
        write = _int(usage.get("cache_write_tokens"))
        return raw_input + read + write, read, write
    read = _int(usage.get("cached_input_tokens"))
    return max(raw_input, read), read, 0


def resolve_cost(usage: dict[str, Any] | None, *, at: datetime | None) -> ComputeCost:
    """Resolve a run's ``usage`` dict to a USD compute cost (the Meter).

    ``at`` is the run's OWN moment (``ended_at``, else ``createdAt``) and is
    required. Prices are effective-dated and this meter runs on a sweeper that
    drains a backlog, so pricing at ``now()`` bills an old run at today's rate.
    ``None`` is allowed but means "the caller genuinely has no moment", and
    ``price_run`` logs that at WARNING before falling back to now — the fallback
    is explicit and audible, not a silent default.

    Resolution order:
      1. ``usage["total_cost_usd"]`` IF present and strictly ``> 0`` (the backend
         reported a real cost) -> source ``"reported"``.
      2. ELSE ``usage_tracker.price_run(...)`` -> source ``"estimated"`` (the
         keyless / OAuth path where the backend reports no cost). This prices
         cache reads and writes separately and applies any long-context tier.
      3. ELSE 0.0. Source ``"unpriced"`` when the run had a model and real tokens
         and nothing could price it — logged at WARNING, because that is a bill
         we are failing to send and it used to be invisible. Source ``"none"``
         when there was simply nothing to bill, still at DEBUG.
    """
    usage = usage or {}
    model = usage.get("model") or None

    reported = usage.get("total_cost_usd")
    if isinstance(reported, (int, float)) and not isinstance(reported, bool) and reported > 0:
        return ComputeCost(
            cost_usd=float(reported),
            source="reported",
            model=model,
            cost_usd_exact=Decimal(str(reported)),
        )

    # Import lazily so importing the metering entity never drags the runtime
    # usage_tracker into the cloud import graph at module load.
    from pocketpaw.usage_tracker import price_run

    input_tokens, cache_read, cache_write = _prompt_tokens(usage)
    output_tokens = _int(usage.get("output_tokens"))
    billable = bool(input_tokens or output_tokens)

    priced = None
    if model is not None and billable:
        priced = price_run(
            model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read,
            cache_write_tokens=cache_write,
            at=at,
        )

    if priced is not None and priced > 0:
        return ComputeCost(
            cost_usd=float(priced),
            source="estimated",
            model=model,
            cost_usd_exact=priced,
        )

    if model is not None and billable:
        # C4 — this used to log at DEBUG next to the empty-usage case, so a model
        # nothing could price billed $0 in silence. It is the loudest thing this
        # function does now, because it is the only outcome that is a mistake.
        logger.warning(
            "metering.resolve_cost: NO PRICE for model %r — billing 0 for a run "
            "that consumed tokens (in=%d out=%d cache_read=%d cache_write=%d, "
            "at=%s). Add a row to usage_tracker._PRICING if genai-prices lacks "
            "the id.",
            model,
            input_tokens,
            output_tokens,
            cache_read,
            cache_write,
            at.isoformat() if at is not None else "<none>",
        )
        return ComputeCost(cost_usd=0.0, source="unpriced", model=model)

    logger.debug(
        "metering.resolve_cost: no billable cost (model=%r, in=%d, out=%d, cached=%d, "
        "reported=%r) — billing 0",
        model,
        input_tokens,
        output_tokens,
        cache_read,
        reported,
    )
    return ComputeCost(cost_usd=0.0, source="none", model=model)


# ---------------------------------------------------------------------------
# The Price — the rate card from runtime settings.
# ---------------------------------------------------------------------------


def run_moment(run_doc: ChatRunDoc) -> datetime | None:
    """The moment a run actually happened — what its compute must be priced at.

    ``ended_at`` when the run recorded one, else ``createdAt``. Exactly the pair
    ``sweeper._emit_run_completed`` uses for the agent-ledger ``ts``, and for the
    same reason: the sweep drains a backlog FIFO and may run long after an
    outage, so sweep-time is never the right stamp for anything the run owns.
    Price is one of those things — ``claude-sonnet-5`` changed rate on
    2026-09-01, and a run from the 30th billed at sweep time would pay it.

    ``None`` when the doc carries no moment at all; the caller says so out loud
    rather than pretending it is now.
    """
    return getattr(run_doc, "ended_at", None) or getattr(run_doc, "createdAt", None)


def load_rate_card() -> RateCard:
    """Build the rate card (the Price primitive) from runtime settings.

    Reads ``POCKETPAW_BILLING_MARKUP`` (default 2.5) and ``POCKETPAW_CREDIT_USD``
    (default 0.01). Lazy import so the metering entity has no import-time
    dependency on the settings singleton.
    """
    from pocketpaw.config import get_settings

    settings = get_settings()
    return RateCard(markup=float(settings.billing_markup), credit_usd=float(settings.credit_usd))


# ---------------------------------------------------------------------------
# bill_run — debit the wallet for one run's compute, exactly once.
# ---------------------------------------------------------------------------


async def bill_run_now(run_id: str) -> BillResult | None:
    """Bill one run the moment it finishes, so the wallet is not a sweep behind.

    Called by the two run executors right after ``execute_run`` returns. Without
    it nothing charges until the next five-minute tick, which means the balance,
    the allowance meter and the activity list are all stale for a whole interval
    after every run — and a customer who checks immediately sees the state from
    before their own message, with nothing on screen saying so.

    **This does not replace the sweeper, and must not.** It is an optimisation on
    top of it. ``bill_run`` is idempotent on ``run:{run_id}``, so a run billed
    here is a no-op when the sweep later reaches it, and a run this misses (a
    crash between the terminal write and this call, an executor path that does
    not reach here, a transient failure below) is still picked up on the next
    tick. That is why every failure here is swallowed: the cost of losing this
    optimisation is five minutes, and the cost of raising is failing a run that
    already succeeded.

    Returns the ``BillResult`` when it billed, or ``None`` when it deliberately
    did nothing — the run is missing, already billed, or LiteLLM is the sole
    meter.
    """
    # Lazy import — same reason the sweeper does it: avoids a
    # metering <-> llm_provisioning module-load cycle.
    from pocketpaw_ee.cloud.llm_provisioning import service as provisioning_service

    try:
        # WU-F single-meter gate, identical to the sweeper's. In ``live`` LiteLLM
        # is the sole meter and per-run metering must not charge, at completion
        # any more than on a tick.
        if provisioning_service.spend_mode() == "live":
            return None

        doc = await ChatRunDoc.find_one(ChatRunDoc.run_id == run_id)
        if doc is None:
            logger.debug(
                "metering.bill_run_now: run=%s not found — leaving it to the sweep", run_id
            )
            return None
        if doc.billed:
            return None
        if doc.status not in _TERMINAL_STATES_FOR_IMMEDIATE_BILLING:
            # Not finished (or finished in a state the sweeper owns). The sweep
            # picks it up once it settles.
            return None

        return await bill_run(doc)
    except Exception:
        # Never let billing fail a run that already succeeded. The sweeper is the
        # backstop and this run keeps ``billed=False`` until it runs.
        logger.exception("metering.bill_run_now: run=%s failed — the sweep will retry it", run_id)
        return None


async def bill_run(run_doc: ChatRunDoc, *, rate_card: RateCard | None = None) -> BillResult:
    """Bill ``run_doc``'s compute cost to its workspace wallet, exactly once.

    Resolves the USD cost (the Meter), converts to credits via the rate card (the
    Price), and debits the workspace wallet with ``allow_negative=True`` keyed on
    ``run:{run_id}`` so a re-bill is a ledger no-op. When the cost rounds to 0
    credits (empty usage / unknown model / sub-credit cost) NO debit is written —
    but the run is STILL marked ``billed`` so the sweeper never re-visits it.

    The ``billed`` flag is flipped True AFTER the debit lands, via
    ``chat.runs.service.mark_billed`` (the chat.runs entity owns ChatRunDoc — EE
    Rule 2; metering never writes the foreign doc). A crash between the debit and
    the flag write is harmless: the next sweep re-bills, BC-1's ``run:{run_id}``
    key makes that debit a no-op, and the flag is then set.

    A ``byok`` run (``run_doc.credential_source``) is never debited: the
    workspace's own key paid the provider. Its cost is still resolved and
    returned and its usage stays on the run doc, so analytics see it.

    A free run's early-access reservation is reconciled here to the real spend
    (``_reconcile_early_access``), before the flag is set.

    ``rate_card`` may be injected (tests / a future per-workspace card); it
    defaults to the settings-derived card.
    """
    card = rate_card if rate_card is not None else load_rate_card()
    cost = resolve_cost(run_doc.usage, at=run_moment(run_doc))
    # The Decimal when there is one: the price came back exact and rounding it to
    # a float on the way into a rounding function is two roundings for one bill.
    # Micro-credits: a run priced at $0.0015 is 375_000 of them, where rounding to
    # whole credits made it 0 and served the run free. Same rate card, same money,
    # a unit fine enough to express it.
    micro = card.to_micro_credits(
        cost.cost_usd_exact if cost.cost_usd_exact is not None else cost.cost_usd
    )
    byok = run_doc.credential_source == "byok"
    if byok:
        micro = 0
    # Real per-run token volume (see ``_total_tokens``) — stamped on the debit ref
    # so the ledger-sourced usage graph can surface it. Mode-agnostic; never the
    # blocked LiteLLM path.
    tokens = _total_tokens(run_doc.usage)

    workspace = run_doc.workspace
    run_id = run_doc.run_id

    if micro > 0:
        balance_after = await credits_service.debit(
            workspace=workspace,
            amount_micro=micro,
            cause=_COMPUTE_SPEND_CAUSE,
            idempotency_key=f"run:{run_id}",
            ref={
                "run_id": run_id,
                "cost_usd": cost.cost_usd,
                "cost_source": cost.source,
                "model": cost.model,
                "total_tokens": tokens,
            },
            allow_negative=True,
        )
        debited = True
        logger.info(
            "metering.bill_run: run=%s workspace=%s billed %d micro-credits "
            "(cost_usd=%.6f, source=%s, model=%r) -> balance=%d",
            run_id,
            workspace,
            micro,
            cost.cost_usd,
            cost.source,
            cost.model,
            balance_after,
        )
    else:
        # Nothing to charge — still mark billed so the run isn't re-swept forever.
        balance_after = await credits_service.balance(workspace)
        debited = False
        logger.debug(
            "metering.bill_run: run=%s workspace=%s %s "
            "(cost_usd=%.6f, source=%s), marking billed, no debit",
            run_id,
            workspace,
            "ran on its own key" if byok else "is genuinely free",
            cost.cost_usd,
            cost.source,
        )

    await _reconcile_early_access(run_id, micro)

    # Flip the flag last, through the chat.runs entity that OWNS ChatRunDoc (EE
    # Rule 2 — metering never writes a foreign document). Idempotent by
    # construction — a racing flag write that loses is harmless because the ledger
    # key already prevented a double debit.
    await chat_runs_service.mark_billed(run_id)

    return BillResult(
        run_id=run_id,
        workspace_id=workspace,
        cost_usd=cost.cost_usd,
        credits_charged=micro_to_credits(micro),
        micro_charged=micro,
        balance_after=balance_after,
        debited=debited,
        cost_source=cost.source,
        model=cost.model,
    )


# The states a finished run can be in. Mirrors the sweeper's ``_TERMINAL_STATES``
# — kept as its own constant here rather than imported so the metering entity
# does not depend on its own sweeper module.
_TERMINAL_STATES_FOR_IMMEDIATE_BILLING = ("completed", "interrupted", "failed", "cancelled")


# ─── DAILY CAPS ────────────────────────────────────────────────────────────────
#
# One counter per (subject, meter, UTC day), claimed by ``try_spend`` in ONE
# atomic ``$inc`` upsert. Increment FIRST and compare after: a check-then-
# increment lets two concurrent claims both read cap-1 and both spend. An
# over-cap claim is rolled back so a refusal does not hold a slot a later claim
# could have used. It is a cost/abuse ceiling, not billing.
#
# Cap semantics, one rule for every meter: ``None`` = uncapped (no counter is
# touched), ``0`` = disabled (every claim refused), ``n > 0`` = cap.
#
# Failure mode is per meter, so it is a parameter: ``fail_open=True`` for the
# chat-turn and upload meters (the run / upload writes to the same Mongo on the
# next statement, so refusing protects nothing and traps hermetic harnesses),
# ``False`` for the platform-paid extras (comprehension, transcription,
# illustration) and guests, where a degraded database must not become an open
# tab.
#
# ``get_pymongo_collection``, NOT ``get_motor_collection``: the latter is beanie
# 1.x, raises AttributeError on 2.x, and inside a fail-closed meter that reads as
# "the feature is off" rather than as a bug. That exact bug shipped twice.

# TODO(BC-6): no ``Entitlements`` field carries a per-plan daily cap for any of
# these meters yet, so every resolver below reads the deployment env only. When
# BC-6 adds them, resolve the plan value via ``entitlements.resolve_entitlements``
# and keep the env as the override.

_logged_legacy_zero: set[str] = set()


def _env_cap(name: str, default: int, *, zero_means_uncapped: bool) -> int | None:
    """Read a daily cap from ``name``. Unset -> ``default``.

    A non-integer is a misconfiguration, not an instruction: warn and use the
    default rather than reading ``"five hundred"`` as ``0``.

    ``zero_means_uncapped`` is the LEGACY meaning of ``0``/negative for the
    chat-turn and upload meters (an env typo must not take chat or upload off
    the air). It maps to ``None`` with a one-time info log. Without it, ``0`` /
    negative is ``0`` = disabled, as it always was for the paid extras.
    """
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("%s is not an integer (%r) — using the default", name, raw)
        return default
    if value > 0:
        return value
    if zero_means_uncapped:
        if name not in _logged_legacy_zero:
            _logged_legacy_zero.add(name)
            logger.info(
                "%s=%r keeps its legacy meaning: no daily cap (0 means disabled on other meters)",
                name,
                raw,
            )
        return None
    return 0


def workspace_turns_cap() -> int | None:
    """Agent runs per workspace per UTC day (``POCKETPAW_WORKSPACE_TURNS_DAILY``,
    default 500 — well past a heavy human day, well short of a loop)."""
    return _env_cap("POCKETPAW_WORKSPACE_TURNS_DAILY", 500, zero_means_uncapped=True)


def upload_files_cap() -> int | None:
    """Files per workspace per UTC day (``POCKETPAW_WORKSPACE_UPLOAD_FILES_DAILY``,
    default 2000 — about forty full 50-file batches)."""
    return _env_cap("POCKETPAW_WORKSPACE_UPLOAD_FILES_DAILY", 2000, zero_means_uncapped=True)


def upload_bytes_cap() -> int | None:
    """Bytes per workspace per UTC day (``POCKETPAW_WORKSPACE_UPLOAD_BYTES_DAILY``,
    default 20 GB — four times the Free plan's whole storage cap)."""
    return _env_cap(
        "POCKETPAW_WORKSPACE_UPLOAD_BYTES_DAILY", 20_000_000_000, zero_means_uncapped=True
    )


def file_comprehension_cap() -> int | None:
    """Comprehensions per workspace per UTC day (``POCKETPAW_FILE_COMPREHENSION_DAILY``,
    default 500). ``0`` disables comprehension."""
    return _env_cap("POCKETPAW_FILE_COMPREHENSION_DAILY", 500, zero_means_uncapped=False)


def file_transcription_cap() -> int | None:
    """Transcriptions per workspace per UTC day (``POCKETPAW_FILE_TRANSCRIPTION_DAILY``,
    default 100 — the pricier call of the two). ``0`` disables transcription."""
    return _env_cap("POCKETPAW_FILE_TRANSCRIPTION_DAILY", 100, zero_means_uncapped=False)


def illustration_cap() -> int | None:
    """Illustrations per workspace per UTC day
    (``POCKETPAW_OTHER_HAND_DAILY_ILLUSTRATIONS``, default 20). ``0`` disables."""
    return _env_cap("POCKETPAW_OTHER_HAND_DAILY_ILLUSTRATIONS", 20, zero_means_uncapped=False)


def today() -> str:
    """The UTC day a claim made right now is charged to. Public because a caller
    holding a claim across requests (multipart uploads) must persist the day it
    spent on in order to refund it there."""
    return datetime.now(UTC).strftime("%Y-%m-%d")


def _key(subject_type: SubjectType, subject_id: str, meter: DailyMeter, day: str) -> dict:
    return {
        "subject_type": subject_type,
        "subject_id": subject_id,
        "meter": str(meter),
        "day": day,
    }


async def try_spend(
    *,
    subject_type: SubjectType,
    subject_id: str | None,
    meter: DailyMeter,
    amount: int = 1,
    cap: int | None,
    fail_open: bool = False,
    day: str | None = None,
) -> bool:
    """Claim ``amount`` of ``meter`` for ``subject_id`` on ``day`` (default
    today). True = allowed. A caller that records the day to refund on later
    passes the same ``day`` here, so a claim made across UTC midnight is
    refunded on the row it incremented.

    ``cap``: ``None`` uncapped (nothing is counted), ``0`` disabled, ``n`` cap.
    A missing ``subject_id`` is refused: an uncharged claim is exactly what a
    cap exists to prevent. A storage failure answers ``fail_open``.
    """
    if cap is None or amount <= 0:
        return True
    if cap <= 0:
        return False
    if not subject_id:
        logger.warning("daily %s refused — no %s to charge", meter, subject_type)
        return False

    key = _key(subject_type, subject_id, meter, day or today())
    now = datetime.now(UTC)
    try:
        coll = DailyUsage.get_pymongo_collection()
        doc = await coll.find_one_and_update(
            key,
            {
                "$inc": {"used": int(amount)},
                "$setOnInsert": {"createdAt": now},
                "$set": {"updatedAt": now},
            },
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
        # A successful update with no returned document should not happen, but
        # reading it as "0 spent" would be a permanently open gate.
        spent = int((doc or {}).get("used", cap + 1))
    except Exception:
        logger.warning(
            "daily %s counter unavailable for %s=%s; %s",
            meter,
            subject_type,
            subject_id,
            "allowing" if fail_open else "refusing",
            exc_info=True,
        )
        return fail_open

    if spent > cap:
        try:
            await coll.update_one(key, {"$inc": {"used": -int(amount)}})
        except Exception:
            logger.debug("could not roll back an over-cap %s claim", meter, exc_info=True)
        return False
    # no-event: a per-day abuse counter, not domain state; nothing downstream
    # listens for it, and an event per chat turn would be pure noise.
    return True


async def refund(
    *,
    subject_type: SubjectType,
    subject_id: str | None,
    meter: DailyMeter,
    amount: int,
    day: str,
) -> None:
    """Give back ``amount`` claimed on ``day``. Best-effort and silent.

    ``day`` is explicit: a multipart session lives for days, and refunding
    against "today" would decrement a row the claim was never made against.
    Never upserts (no row = nothing to refund — a fail-open claim may hold no
    row) and clamps at zero, so a double refund cannot mint quota.
    """
    if not subject_id or not day or amount <= 0:
        return
    key = _key(subject_type, subject_id, meter, day)
    try:
        coll = DailyUsage.get_pymongo_collection()
        doc = await coll.find_one_and_update(
            key,
            {"$inc": {"used": -int(amount)}, "$set": {"updatedAt": datetime.now(UTC)}},
            return_document=ReturnDocument.AFTER,
        )
        if doc is not None and int(doc.get("used", 0)) < 0:
            await coll.update_one(key, {"$set": {"used": 0}})
    except Exception:
        logger.warning(
            "could not refund a daily %s claim for %s=%s day=%s",
            meter,
            subject_type,
            subject_id,
            day,
            exc_info=True,
        )
    # no-event: see ``try_spend``.


async def used(
    *, subject_type: SubjectType, subject_id: str, meter: DailyMeter, day: str | None = None
) -> int:
    """Read-only: how much of ``meter`` ``subject_id`` has used (today by
    default). 0 when there is no row. Raises on a storage error — each read-only
    caller decides its own failure mode."""
    doc = await DailyUsage.get_pymongo_collection().find_one(
        _key(subject_type, subject_id, meter, day or today())
    )
    return int((doc or {}).get("used", 0))


# ─── EARLY-ACCESS DAILY CEILING ───────────────────────────────────────────────

#: The subject every unpaid workspace's free chat spend is counted against.
PLATFORM_SUBJECT = "__platform__"


def credits_ceil(micro: int) -> int:
    """Micro-credits as whole credits, rounded up, so the meter never undercounts."""
    return -(-max(micro, 0) // MICRO_PER_CREDIT)


async def reserve_early_access(run_id: str, credits: int, *, ceiling_credits: int) -> bool:
    """Reserve ``credits`` of today's early-access ceiling for ``run_id``.

    False when the claim would pass ``ceiling_credits`` (0 refuses every claim)
    or the meter cannot be read: the ceiling fails closed. On success the claim
    is recorded on the run for ``bill_run`` to reconcile; if that write fails
    the claim stays counted (the meter over-counts, never under).
    """
    day = today()
    if not await try_spend(
        subject_type="workspace",
        subject_id=PLATFORM_SUBJECT,
        meter=DailyMeter.EARLY_ACCESS_CREDITS,
        amount=credits,
        cap=ceiling_credits,
        day=day,
    ):
        return False
    try:
        await chat_runs_service.set_early_access_reservation(run_id, credits, day)
    except Exception:
        logger.warning(
            "early access reservation of %d credits not recorded on run %s; it stays counted",
            credits,
            run_id,
            exc_info=True,
        )
    return True


async def _reconcile_early_access(run_id: str, micro: int) -> None:
    """Move the meter from ``run_id``'s reservation to its real spend, once.

    Best-effort: the meter is a ceiling, and a failure here must never fail the
    bill. A run with nothing reserved (paid, BYOK, or already reconciled) is a
    no-op.
    """
    try:
        held = await chat_runs_service.take_early_access_reservation(run_id)
        if held is None:
            return
        reserved, day = held
        delta = credits_ceil(micro) - reserved
        if delta < 0:
            await refund(
                subject_type="workspace",
                subject_id=PLATFORM_SUBJECT,
                meter=DailyMeter.EARLY_ACCESS_CREDITS,
                amount=-delta,
                day=day,
            )
        elif delta > 0:
            # The spend already happened, so it lands past the cap too.
            now = datetime.now(UTC)
            await DailyUsage.get_pymongo_collection().update_one(
                _key("workspace", PLATFORM_SUBJECT, DailyMeter.EARLY_ACCESS_CREDITS, day),
                {
                    "$inc": {"used": delta},
                    "$setOnInsert": {"createdAt": now},
                    "$set": {"updatedAt": now},
                },
                upsert=True,
            )
    except Exception:
        logger.warning("early access reconcile failed for run %s", run_id, exc_info=True)


async def early_access_credits_used_today() -> int:
    """Whole credits on today's early-access meter: reservations in flight plus
    reconciled spend. Raises on a storage error, like ``used``."""
    return await used(
        subject_type="workspace", subject_id=PLATFORM_SUBJECT, meter=DailyMeter.EARLY_ACCESS_CREDITS
    )


__all__ = [
    "PLATFORM_SUBJECT",
    "bill_run",
    "bill_run_now",
    "credits_ceil",
    "early_access_credits_used_today",
    "file_comprehension_cap",
    "file_transcription_cap",
    "illustration_cap",
    "load_rate_card",
    "refund",
    "reserve_early_access",
    "resolve_cost",
    "run_moment",
    "today",
    "try_spend",
    "upload_bytes_cap",
    "upload_files_cap",
    "used",
    "workspace_turns_cap",
]
