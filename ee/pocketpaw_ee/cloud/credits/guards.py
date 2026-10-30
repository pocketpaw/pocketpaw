# ee/pocketpaw_ee/cloud/credits/guards.py: the shared run-start credit gate every
# agent-run seam calls, so an over-budget workspace is refused the same way
# everywhere and no model call happens on a refusal.
#
# * ``over_billing_limit(workspace, source=...)`` is the core: None unless
#   ``billing_enforced`` or ``credits_enforced`` is on, and always None for
#   ``source="byok"`` or an empty workspace. Otherwise, in order: the launch
#   kill switch (503 ``early_access.paused``, unpaid early access only), wallet
#   <= 0 (402 ``credits.insufficient``), monthly ceiling (402
#   ``credits.quota_exceeded``), then with ``one_active_run`` an older active
#   chat run on an unpaid workspace (429 ``credits.run_in_progress``; the oldest
#   of two racing runs proceeds). Returns the first CloudError, never raises
#   those; a real infra failure propagates.
# * ``resolve_payer`` and ``billing_source`` decide who pays before the gate.
#   ``byok`` only when the BYOK resolver says so AND the agent's backend spends
#   the key (``pool.spends_byok_key``); a resolver failure or any lookup failure
#   answers platform (gated and charged, never free). ``GatewayEgressRejected``
#   propagates as a refusal of the turn.
# * ``assert_within_billing`` raises the refusal for HTTP routes (call it before
#   any local ``except Exception``); ``reject_if_over_billing`` is the executor
#   variant and ends the run through ``refuse_run`` (terminal ``error`` frame
#   ``{"code", "message"}``, ``mark_terminal(failed)``, stream TTL, each
#   best-effort).
# * ``is_platform_funded_free`` is the one definition of an unpaid early-access
#   workspace: plan ``free``, ``payments_open`` off, no applied ``top_up`` or
#   ``subscription_grant``. ``early_access_denial`` refuses the spend surfaces
#   the ledger never meters for such a workspace (403
#   ``early_access.not_available``), under the same flags as the gate.
# * Free runs (platform-funded, unpaid): ``free_run_bounds`` gives ``run_core``
#   the turn cap and starting wallet; ``free_run_token_limit`` sizes the token
#   limit from it. ``free_model_denial`` (400 ``model.not_allowed``) admits only
#   allowlisted, priced models, checked over ``free_run_models`` by ``run_core``
#   and, as ``free_model_refusal``, by the chat HTTP seam. ``reserve_free_run``
#   then holds ``free_run_reserve_credits`` (or the smaller wallet) on the daily
#   early-access ceiling (503 ``early_access.paused`` past it or when
#   unreadable; ``bill_run`` reconciles to the real spend, overrun included).
#
# ``credits_enforced`` gates only this chat credit check; ``billing_enforced``
# also turns on the seat, pocket, connector and other paid gates elsewhere.
# Credits and run-service imports are local to keep this module off the hot
# import graph.

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Literal

from pocketpaw.config import get_settings  # type: ignore[import-untyped]

if TYPE_CHECKING:
    from pocketpaw_ee.cloud._core.errors import CloudError
    from pocketpaw_ee.cloud.byok.service import TurnCredentials
    from pocketpaw_ee.cloud.metering.domain import RateCard

logger = logging.getLogger(__name__)


def _stream_ttl() -> int:
    """Stream-doc TTL in seconds — mirrors ``run_core._stream_ttl`` so a rejected
    run's error frame expires on the same schedule as a normal run's."""
    return int(os.environ.get("POCKETPAW_CLOUD_RUN_STREAM_TTL", "3600"))


CredentialSource = Literal["platform", "byok"]


async def resolve_payer(workspace_id: str) -> TurnCredentials:
    """Who pays for this turn, decided before the credit gate.

    The BYOK resolver's answer, or ``platform`` credentials when it fails for
    any reason, so a broken resolver gates and charges the turn rather than
    letting it run free. ``GatewayEgressRejected`` propagates: the stored
    gateway address must not be dialed, and degrading would run the tenant's
    turn on our key, so the caller refuses the turn instead.

    The returned value can carry a plaintext key. Never log it.
    """
    from pocketpaw_ee.cloud.byok import service as byok_service

    try:
        return await byok_service.resolve_turn_credentials(workspace_id)
    except byok_service.GatewayEgressRejected:
        raise
    except Exception:
        logger.exception(
            "byok: resolve_turn_credentials failed for workspace=%s; gating and "
            "charging this turn as platform",
            workspace_id,
        )
        return byok_service.TurnCredentials(source="platform")


async def billing_source(
    creds: TurnCredentials, agent_id: str | None, *, exclusive_tools: bool = False
) -> CredentialSource:
    """Who the credit gate and ``bill_run`` treat as paying for this turn.

    ``byok`` only when ``creds`` is a BYOK answer AND the backend that will
    serve ``agent_id`` spends the key. Otherwise ``platform``: a stored key on
    a backend that ignores it would let the turn skip the wallet while it runs
    on our credentials. A missing or unreadable agent answers ``platform``.
    """
    if creds.source != "byok":
        return "platform"
    if exclusive_tools:
        return "byok"
    try:
        from beanie import PydanticObjectId

        from pocketpaw.agents.pool import spends_byok_key  # type: ignore[import-untyped]
        from pocketpaw_ee.cloud.models.agent import Agent

        doc = await Agent.get(PydanticObjectId(agent_id)) if agent_id else None
        if doc is None:
            return "platform"
        backend = doc.config.model_dump().get("backend")
        return "byok" if spends_byok_key(backend) else "platform"
    except Exception:
        logger.warning(
            "byok: could not read agent %s's backend; gating this turn as platform",
            agent_id,
            exc_info=True,
        )
        return "platform"


async def over_billing_limit(
    workspace_id: str,
    source: CredentialSource = "platform",
    *,
    one_active_run: bool = False,
    run_id: str | None = None,
) -> CloudError | None:
    """Return the billing rejection for ``workspace_id``, or None to proceed.

    The pure, transport-agnostic core of the run-start billing gate. A no-op
    (returns None) unless ``billing_enforced`` or ``credits_enforced`` is on;
    OSS / self-host deployments run no credit ledger. When enforced, an unpaid
    early-access workspace is refused first with ``EarlyAccessPaused`` (503) while
    the ``free_usage_paused`` launch control is on. Then it runs the two
    credit assertions in the SAME order as the chat chokepoint: ``check_balance``
    (empty wallet) first, then ``check_quota`` (monthly ceiling). The FIRST that
    trips is returned (an ``InsufficientCredits`` or ``QuotaExceeded``, both 402
    CloudErrors); if both pass it returns None.

    An uncapped plan (Enterprise, ``monthly_ceiling`` is None) never trips the
    quota leg (``check_quota`` no-ops there). ``source="byok"`` returns None:
    the workspace's own key pays the provider, so neither its balance nor its
    quota applies. With ``one_active_run`` an unpaid platform-funded workspace
    that already has an older active chat run than ``run_id`` (any, when
    ``run_id`` is None) gets a 429 ``credits.run_in_progress``, after the
    wallet checks. An empty ``workspace_id`` returns
    None: there is no wallet to attribute, and the run-transport callers already
    validate a non-empty workspace upstream. Only the two credit CloudErrors are
    caught here — any other exception (a real infra failure) propagates, exactly
    as the pre-existing run_core gate let it.
    """
    settings = get_settings()
    if not (settings.billing_enforced or settings.credits_enforced):
        return None
    if not workspace_id or source == "byok":
        return None

    from pocketpaw_ee.cloud._core.errors import (
        EarlyAccessPaused,
        InsufficientCredits,
        QuotaExceeded,
    )
    from pocketpaw_ee.cloud.credits import service as credits_service
    from pocketpaw_ee.cloud.launch_controls.service import get_launch_controls

    # Kill switch first: cached, so the common (unpaused) case costs no DB read,
    # and the plan/ledger lookup only runs while the switch is on.
    controls = await get_launch_controls()
    if controls.free_usage_paused and await is_platform_funded_free(workspace_id):
        return EarlyAccessPaused()

    try:
        # Order mirrors chat/agent_router: the empty-wallet block is the primary
        # money guarantee; the monthly-ceiling block sits beside it.
        await credits_service.check_balance(workspace_id)
        await credits_service.check_quota(workspace_id)
    except (InsufficientCredits, QuotaExceeded) as exc:
        return exc
    # The run read first: it is one indexed query and nearly always empty, so
    # the plan and ledger reads only run for a workspace that has another run.
    if (
        one_active_run
        and await _older_active_run(workspace_id, run_id)
        and await is_platform_funded_free(workspace_id)
    ):
        from pocketpaw_ee.cloud._core.errors import RateLimited

        return RateLimited(
            "credits.run_in_progress",
            "Your last message is still being answered. Wait for it to finish, "
            "then send this one again.",
        )
    return None


#: How far back an active run still counts. A run whose worker died stops
#: beating and the stale-run sweeper moves it out of ``running`` within minutes;
#: this only bounds the query, and no bounded free run lasts this long.
_ACTIVE_RUN_WINDOW = timedelta(hours=1)
#: Rows read. Newest first, so an executor's own run sits near the top.
_ACTIVE_RUN_SCAN = 20


async def _older_active_run(workspace_id: str, run_id: str | None) -> bool:
    """Whether ``workspace_id`` has an active chat run that blocks this one.

    With no ``run_id`` (the HTTP seam, before the run doc exists) any active run
    blocks. With one (the executor, whose own run is already active) only a run
    created before it blocks, ties broken by run id, so of two racing runs
    exactly the older proceeds. If the own run is not among the rows read,
    every other active run blocks (fail closed).
    """
    from pocketpaw_ee.cloud.chat.runs import service as run_service

    rows = await run_service.find_active_runs_for_workspace(
        workspace_id=workspace_id,
        since=datetime.now(UTC) - _ACTIVE_RUN_WINDOW,
        limit=_ACTIVE_RUN_SCAN,
    )
    mine = next((r for r in rows if r.run_id == run_id), None) if run_id else None
    if mine is None:
        return bool(rows)
    # Strictly older, so the own row never counts against itself.
    return any((r.created_at, r.run_id) < (mine.created_at, mine.run_id) for r in rows)


async def assert_within_billing(
    workspace_id: str, source: CredentialSource = "platform", *, one_active_run: bool = False
) -> None:
    """Raise the billing rejection (402, or 503 while paused) when refused; else no-op.

    For HTTP routes: the raised ``InsufficientCredits`` / ``QuotaExceeded`` is a
    CloudError that ``_core.http`` maps to a 402 wire response. Call it BEFORE any
    local ``try/except Exception`` in the handler so the 402 isn't swallowed into
    a generic 500. Flag-gated and Enterprise-safe via ``over_billing_limit``.
    """
    exc = await over_billing_limit(workspace_id, source, one_active_run=one_active_run)
    if exc is not None:
        raise exc


async def reject_if_over_billing(
    workspace_id: str,
    *,
    run_id: str,
    transport: Any,
    log_label: str | None = None,
    source: CredentialSource = "platform",
) -> bool:
    """Reject an over-budget run on the run/stream transport. Returns True if it did.

    The run-transport variant (run_core.execute_run): when ``over_billing_limit``
    trips, emit a terminal ``error`` stream frame, ``mark_terminal(failed)`` the
    run doc, and set the stream TTL — the SAME clean shape the ART-3 jail-quota
    reject uses — then return True so the caller early-returns BEFORE any model /
    agent work (the no-overspend money guarantee). Returns False to proceed.

    Every side effect is best-effort: a transient Redis / Mongo failure while
    writing the rejection must not turn a clean 402-equivalent block into an
    unhandled crash that takes the worker down.
    """
    exc = await over_billing_limit(workspace_id, source, one_active_run=True, run_id=run_id)
    if exc is None:
        return False
    await refuse_run(run_id, exc, transport=transport, log_label=log_label)
    return True


async def refuse_run(
    run_id: str, exc: CloudError, *, transport: Any, log_label: str | None = None
) -> None:
    """End a run before any agent work with ``exc`` as its terminal ``error``
    frame ``{"code", "message"}``, ``mark_terminal(failed)`` and the stream TTL.

    Each write is best-effort: a transient Redis or Mongo failure must not turn
    a clean refusal into a crash that takes the worker down.
    """
    label = log_label or run_id
    message = str(exc)
    logger.warning("run %s refused at run start: %s (%s)", label, message, exc.code)
    try:
        await transport.append_event(run_id, "error", {"code": exc.code, "message": message})
    except Exception:
        logger.debug("billing error frame append failed for %s", run_id, exc_info=True)
    try:
        from pocketpaw_ee.cloud.chat.runs import service as run_service

        await run_service.mark_terminal(run_id, status="failed", error=message)
    except Exception:
        logger.exception("mark_terminal(failed) failed for over-billing run %s", run_id)
    try:
        await transport.set_ttl(run_id, _stream_ttl())
    except Exception:
        logger.debug("billing stream ttl set failed for %s", run_id, exc_info=True)


async def is_platform_funded_free(workspace_id: str) -> bool:
    """True when ``workspace_id`` is an unpaid early-access workspace.

    All three must hold: the workspace resolves to the ``free`` plan, payments
    are closed (``payments_open`` is off), and its ledger holds no applied
    ``top_up`` or ``subscription_grant``. An unknown or missing workspace
    resolves to ``free`` (the entitlements resolver fails closed), so it counts
    as unpaid. The ledger leg cannot trip while payments are closed; it keeps
    the answer right once they open.
    """
    if get_settings().payments_open:
        return False

    from pocketpaw_ee.cloud.credits import service as credits_service
    from pocketpaw_ee.cloud.entitlements import service as entitlements_service

    ent = await entitlements_service.resolve_entitlements(workspace_id)
    if ent.plan != "free":
        return False
    return not await credits_service.has_paid_credits(workspace_id)


async def early_access_denial(workspace_id: str) -> CloudError | None:
    """``EarlyAccessNotAvailable`` when ``workspace_id`` is an unpaid early-access
    workspace and the credit gate is on, else None.

    For spend that never reaches the ledger, so the wallet cannot bound it: the
    surface is refused outright rather than metered. Off while neither
    ``billing_enforced`` nor ``credits_enforced`` is set (self-host, and prod
    before the launch flip). An empty ``workspace_id`` is None, like
    ``over_billing_limit``.
    """
    settings = get_settings()
    if not (settings.billing_enforced or settings.credits_enforced) or not workspace_id:
        return None
    if not await is_platform_funded_free(workspace_id):
        return None

    from pocketpaw_ee.cloud._core.errors import EarlyAccessNotAvailable

    return EarlyAccessNotAvailable()


async def assert_early_access_allowed(workspace_id: str) -> None:
    """Raise ``early_access_denial``'s refusal (403) when there is one."""
    exc = await early_access_denial(workspace_id)
    if exc is not None:
        raise exc


@dataclass(frozen=True)
class FreeRunBounds:
    """What a platform-funded free run may use: model requests and wallet."""

    max_turns: int
    balance_micro: int


async def free_run_bounds(workspace_id: str, source: CredentialSource) -> FreeRunBounds | None:
    """The bounds for this run, or None when it is not a platform-funded free run.

    None unless ``credits_enforced`` or ``billing_enforced`` is on, the run is
    paid by the platform, and the workspace is unpaid early access. Read at run
    start, after the gate, so ``balance_micro`` is the wallet the run starts on.
    """
    settings = get_settings()
    enforced = settings.billing_enforced or settings.credits_enforced
    if not enforced or not workspace_id or source != "platform":
        return None
    if not await is_platform_funded_free(workspace_id):
        return None

    from pocketpaw_ee.cloud.credits import service as credits_service

    return FreeRunBounds(
        max_turns=settings.free_run_max_turns,
        balance_micro=await credits_service.balance_micro(workspace_id),
    )


def free_run_token_limit(
    balance_micro: int, model: str, *, rate_card: RateCard | None = None
) -> int | None:
    """Tokens ``balance_micro`` pays for on ``model``, priced as output tokens.

    The output price is the higher one, so the run stops at or before the
    wallet whatever its input/output mix. ``model`` may carry a provider prefix
    (``litellm:claude-haiku-4-5``). None when nothing prices the model: the turn
    cap still binds, and unpriced models are refused upstream. 0 for an empty
    wallet.
    """
    if balance_micro <= 0:
        return 0

    from pocketpaw_ee.cloud.metering.service import load_rate_card

    per_million = _output_usd_per_million(model)
    if per_million is None:
        return None
    card = rate_card if rate_card is not None else load_rate_card()
    micro_per_million = card.to_micro_credits(per_million)
    if micro_per_million <= 0:
        return None
    return balance_micro * 1_000_000 // micro_per_million


def _output_usd_per_million(model: str) -> Decimal | None:
    """USD for a million output tokens on ``model``, through a provider prefix
    (``litellm:claude-haiku-4-5`` prices as ``claude-haiku-4-5``). None when
    nothing on the metering ladder prices it, or prices it at 0."""
    from pocketpaw.usage_tracker import price_run

    now = datetime.now(UTC)
    for name in (model, model.partition(":")[2]):
        per_million = (
            price_run(name, input_tokens=0, output_tokens=1_000_000, at=now) if name else None
        )
        if per_million:
            return per_million
    return None


def free_model_denial(model: str) -> CloudError | None:
    """``ModelNotAllowed`` unless ``model`` may run on early access credits.

    It must be on ``free_model_allowlist`` (exact, ignoring case and one
    leading ``provider:`` prefix naming a provider pydantic_ai knows) and
    priced by the metering ladder, because an unpriced model bills 0 and so
    would run free. Says nothing about who pays:
    callers apply it to platform-funded turns on unpaid workspaces only.
    """

    from pocketpaw.agents.pydantic_ai import _KNOWN_PROVIDERS

    def bare(name: str) -> str:
        head, sep, tail = name.strip().partition(":")
        return (tail if sep and head.lower() in _KNOWN_PROVIDERS else name).strip().lower()

    allowed = {bare(m) for m in get_settings().free_model_allowlist}
    if bare(model) in allowed - {""} and _output_usd_per_million(bare(model)):
        return None

    from pocketpaw_ee.cloud._core.errors import ModelNotAllowed

    return ModelNotAllowed(model)


async def free_model_refusal(
    workspace_id: str, source: CredentialSource, agent_id: str | None, model_override: str | None
) -> CloudError | None:
    """The first ``free_model_denial`` over ``free_run_models(agent_id,
    model_override)`` for a platform-funded turn on an unpaid early-access
    workspace while the credit gate is on, else None. The chat HTTP seam's
    check, so an agent stored on Opus is refused with a 400 before a run doc,
    a stream or a turn counter exists; ``run_core`` checks again and enforces.
    """
    settings = get_settings()
    if source != "platform" or not (settings.billing_enforced or settings.credits_enforced):
        return None
    if not workspace_id:
        return None
    for model in await free_run_models(agent_id, model_override):
        exc = free_model_denial(model)
        if exc is not None:
            return exc if await is_platform_funded_free(workspace_id) else None
    return None


async def reserve_free_run(run_id: str, bounds: FreeRunBounds) -> CloudError | None:
    """Reserve a free run's hold on today's early-access ceiling, or refuse.

    Call once per run, after ``free_run_bounds`` returned ``bounds`` and the
    model check passed; it writes, so the check-only HTTP seam must not call
    it. ``EarlyAccessPaused`` when the reservation does not fit under
    ``daily_free_ceiling_credits``, or when the launch controls or the meter
    cannot be read. None once reserved.
    """
    from pocketpaw_ee.cloud._core.errors import EarlyAccessPaused
    from pocketpaw_ee.cloud.launch_controls.service import get_launch_controls
    from pocketpaw_ee.cloud.metering import service as metering

    controls = await get_launch_controls()
    if controls.read_failed:
        return EarlyAccessPaused()
    # ponytail: a fixed per-run hold, not the wallet the token limit allows, so
    # concurrency alone cannot fill the ceiling. bill_run adds any overrun, so
    # in-flight runs may pass the ceiling: a soft burn-rate limit. Clamp the
    # free-run token limit to the same figure if a hard ceiling is ever needed.
    reserve = get_settings().free_run_reserve_credits
    estimate = max(1, min(metering.credits_ceil(bounds.balance_micro), reserve))
    if not await metering.reserve_early_access(
        run_id, estimate, ceiling_credits=controls.daily_free_ceiling_credits
    ):
        return EarlyAccessPaused()
    return None


async def free_run_models(agent_id: str | None, model_override: str | None) -> list[str]:
    """Every model a bounded free run on ``agent_id`` must be allowed to use.

    The one asked for (per-send model, else the agent's own, else the
    deployment's ``pydantic_ai_model``), plus, for an agent not on pydantic_ai,
    that deployment default: the forced pydantic_ai backend really runs on it
    and drops the agent's model. A missing or unreadable agent counts as one
    with no model on another backend, so both checks still apply.
    """
    default = get_settings().pydantic_ai_model
    backend, agent_model = "", ""
    try:
        from beanie import PydanticObjectId

        from pocketpaw_ee.cloud.models.agent import Agent

        doc = await Agent.get(PydanticObjectId(agent_id)) if agent_id else None
        if doc is not None:
            backend, agent_model = doc.config.backend, doc.config.model
    except Exception:
        logger.warning("could not read agent %s for the model check", agent_id, exc_info=True)
    asked = model_override or agent_model or default
    return [asked] if backend == "pydantic_ai" else [asked, default]


__all__ = [
    "FreeRunBounds",
    "assert_early_access_allowed",
    "assert_within_billing",
    "billing_source",
    "early_access_denial",
    "free_model_denial",
    "free_model_refusal",
    "free_run_bounds",
    "free_run_models",
    "free_run_token_limit",
    "is_platform_funded_free",
    "over_billing_limit",
    "refuse_run",
    "reject_if_over_billing",
    "reserve_free_run",
    "resolve_payer",
]
