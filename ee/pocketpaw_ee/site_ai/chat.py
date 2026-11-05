# ee/pocketpaw_ee/site_ai/chat.py — POST /sites/{site_id}/ai/chat, end to end.
#
# ``admit`` runs every gate and returns an ``Admitted`` call or raises:
#   1. per-IP bucket (in-process)                       -> 429 rate_limited
#   2. site_gate.authenticate: PAW_GATEWAY_TOKEN = worker; a site key from the
#      site's own hosts = browser, from its own draft = preview -> 401 / 403
#   3. body (<= 400 KB) parsed from an allowlist (validate.py)  -> 400 / 413;
#      tools and tool turns -> 400 ``unsupported`` (the call runs tool-less);
#      images only on the vision alias, as data: URLs
#   4. browser: ``browser_access`` on (-> unavailable browser_disabled); browser and
#      preview: a live ``X-Paw-Ai-Ticket`` of their kind from this IP (-> 401), a
#      preset when the site requires one (-> 403 preset_required)
#   5. preset: its system prompt, model and output cap win
#   6. alias allowed by the plan AND the owner, live site on a plan that sells it,
#      input within the alias context -> 403 / 400
#   7. the alias's agent resolves (``aliases.choice`` + ``side_completion.resolve_target``)
#      and isn't offline -> Unavailable("temporary")
#   8. site AI off or paused                            -> Unavailable("paused")
#   9. SiteRateCounter: Worker per site and per hashed ``user``; browser/preview
#      per (site, IP hash) and per site                  -> 429
#  10. a platform-paid call: ``over_billing_limit`` on the workspace wallet ->
#      Unavailable("limit", wallet_empty); then the worst case is reserved against
#      every cap (budget.py; a BYOK call only against the owner's own caps) ->
#      Unavailable("limit", budget_exhausted)
# ``Unavailable`` answers a stream with one ``unavailable`` event, JSON with 402 / 503.
#
# Then ONE side completion (llm.py -> cloud.chat.side_completion) on the alias's
# agent and model, as the site owner. Every stream ends with ``done``,
# ``unavailable`` or ``error``. A failure maps as: ``billing`` -> unavailable limit /
# 402 wallet_empty; ``unsupported`` (images on a backend that can't see them) ->
# error / 400 unsupported; ``model`` / ``no_agent`` (and a backend that can't run
# tool-less) -> the alias goes offline + unavailable temporary / 503; ``timeout`` /
# ``error`` -> unavailable temporary / 503. The site owner must still be a member of
# the site's workspace (else the alias goes offline). Billing: a call the model never
# started on (``STARTED`` never came), or a backend error with no usage and no
# output, costs nothing; one that started and then failed or was cancelled costs its
# usage, else its input estimate plus what streamed. A stream is cut at the output
# cap (finish ``length``), and no call costs more than its hold. Input tokens are the
# inclusive prompt total (cache reads and writes included). A model with no
# verified price is refused at admission (ERROR, alias offline). ``_price`` prices
# the tokens at the table rate of the model that ran (``aliases.cost_usd``; the
# CLI's ``total_cost_usd`` is only logged, as ``cli_cost_usd``), WARNs when the
# backend reports another model than the one pinned, and clamps an overrun to the
# hold (WARNING with the ratio). ``_persist`` writes the ``ChatRunDoc`` (context
# ``site_ai``, no content, payer as ``credential_source``), settles the hold, checks
# alerts, writes an opted-in transcript, asks for the debit (``bill_run``,
# ``run:{id}``; never BYOK) and logs ``site_ai.call`` (models requested/reported,
# ``cli_cost_usd``, ``hold_ratio``, ``ttft_ms``, ``total_ms``). A finished stream
# sends ``usage`` and ``done`` first, then persists in a shielded task (a disconnect
# after ``done`` cannot skip it); ``record`` runs both in order.

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import aclosing
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from pocketpaw.security.rate_limiter import RateLimiter
from pocketpaw_ee.integrations import site_gate
from pocketpaw_ee.site_ai import alerts, aliases, llm, service, tickets, validate, visitor
from pocketpaw_ee.site_ai import budget as budget_mod
from pocketpaw_ee.site_ai.errors import AiError, error_response, invalid

logger = logging.getLogger(__name__)

WORKER_SITE_PER_MIN = 600
USER_PER_MIN = 60
BROWSER_IP_PER_MIN = 20
MAX_BROWSER_ASSISTANT_TURNS = 20
BROWSER_SITE_PER_MIN = 300
TICKET_IP_PER_MIN = 10

# 240 AI calls a minute per address across every site.
_ip_limiter = RateLimiter(rate=240.0 / 60.0, capacity=240)
_BACKGROUND: set[asyncio.Task[Any]] = set()


class Unavailable(Exception):
    """The call is well-formed but the site can't run it now. ``reason`` is the
    SSE ``unavailable`` reason; ``code`` the JSON error for a non-streaming call."""

    def __init__(self, reason: str, code: str, message: str | None = None, *, stream: bool) -> None:
        self.reason = reason
        self.error = AiError(code, message)
        self.stream = stream
        super().__init__(reason)


@dataclass
class Admitted:
    site: Any
    workspace_id: str
    pocket_id: str
    site_id: str
    caller: str  # worker | browser | preview
    alias: str
    owner_id: str  # the site owner: the identity the side completion runs as
    messages: list[dict[str, Any]]
    max_tokens: int
    input_estimate: int
    stream: bool
    agent_id: str | None = None  # None = the workspace default agent
    model: str | None = None  # None = the agent's own model
    payer: str = "platform"  # platform | byok
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    response_format: dict[str, Any] | None = None
    preset_id: str | None = None
    preset_version: int | None = None
    ip_hash: str = ""
    user_hash: str | None = None
    visitor: str | None = None  # the per-visitor cap's key (hashed)
    hold: budget_mod.Hold | None = None
    sent: bool = False  # the model started on the call (it costs its input now)
    ran_model: str | None = None  # the model that answers (pricing; operators only)
    kind: str = "chat"
    keep_transcript: bool = False
    model_reported: str | None = None  # the model the backend says answered
    admitted_model: str | None = None  # the model the hold was priced at (never moves)
    backend: str | None = None  # the backend that answered (whose cost to trust)
    cli_cost_usd: float | None = None  # the backend's own cost figure, logged only
    started: float = 0.0  # monotonic, when the model call was asked for
    ttft_ms: int | None = None  # first text delta (streams only)
    total_ms: int | None = None  # the model call's end


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _retry_after() -> int:
    return max(1, 60 - int(time.time()) % 60)


def sse(event: str, data: dict[str, Any]) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n".encode()


# ------------------------------------------------------------------ admission


async def authenticate(request: Request, site_id: str) -> tuple[Any, str]:
    ip = visitor.bucket(visitor.client_address(request))
    if not _ip_limiter.check(f"site-ai:{ip}").allowed:
        raise AiError("rate_limited", retry_after=_retry_after())
    try:
        site, gate_caller, preview = await site_gate.authenticate(request, site_id)
    except site_gate.SiteGateError as exc:
        code = "origin_not_allowed" if exc.code == "origin_denied" else "unauthorized"
        raise AiError(code) from None
    if not getattr(site, "pocket_id", None):
        raise AiError("unauthorized")
    if gate_caller == "gateway_token":
        return site, "worker"
    return site, "preview" if preview else "browser"


def admit_browser(
    request: Request, site: Any, caller: str, settings: Any, preset: str | None, stream: bool
) -> str:
    """The browser and preview rules; returns the caller's IP hash. Browser calls
    need ``browser_access`` (-> unavailable ``browser_disabled``), every non-Worker
    call needs a live ticket of its own kind from this address (-> 401), and a
    site that requires presets refuses a call without one (-> 403)."""
    ip_hash = visitor.visitor_key(request)
    if caller == "worker":
        return ip_hash
    if caller == "browser" and not settings.browser_access:
        raise Unavailable("browser_disabled", "browser_disabled", stream=stream)
    tickets.verify(request.headers.get("x-paw-ai-ticket"), caller, str(site.id), ip_hash)
    if settings.require_preset_for_browser and preset is None:
        raise AiError("preset_required")
    return ip_hash


def refuse_unsupported(parsed: validate.ParsedChat) -> None:
    """Site AI runs one tool-less completion: tools and tool turns are a clear 400
    ``unsupported``, never silently dropped. Images go through (vision alias)."""
    if parsed.tools or parsed.tool_choice is not None:
        raise AiError("unsupported", "Tool calling isn't supported by site AI yet.", field="tools")
    for i, msg in enumerate(parsed.messages):
        if msg["role"] == "tool" or msg.get("tool_calls"):
            raise AiError(
                "unsupported",
                "Tool calls and tool results aren't supported by site AI yet.",
                field=f"messages[{i}]",
            )


def _apply_preset(parsed: validate.ParsedChat, preset: Any, caller: str) -> list[dict]:
    """The preset's system prompt first. For a browser (or preview) caller the preset
    is the owner's lock, so the browser's own system messages and response format
    are dropped (presets declare none), and its history may carry at most
    ``MAX_BROWSER_ASSISTANT_TURNS`` assistant turns: a fabricated history can still
    argue with the prompt, which is why the lock is soft and Worker-side calls stay
    the default for anything secret."""
    messages = parsed.messages
    if caller != "worker":
        messages = [m for m in messages if m["role"] != "system"]
        if sum(m["role"] == "assistant" for m in messages) > MAX_BROWSER_ASSISTANT_TURNS:
            raise invalid("messages", "Too many assistant turns for a browser call.")
        parsed.response_format = None
    if preset.system_prompt:
        messages = [{"role": "system", "content": preset.system_prompt}, *messages]
    return messages


async def _owner_id(site: Any) -> str:
    owner = str(getattr(site, "owner", "") or "")
    if owner:
        return owner
    from pocketpaw_ee.cloud.models.pocket import Pocket

    try:
        from beanie import PydanticObjectId

        pocket = await Pocket.get(PydanticObjectId(site.pocket_id))
    except Exception:  # noqa: BLE001 - no owner refuses below
        return ""
    return str(getattr(pocket, "owner", "") or "")


async def owner_is_member(workspace_id: str, owner_id: str) -> bool:
    """The site owner is still a member of the site's workspace: the call runs as
    them, so a former member's identity must not keep paying or resolving."""
    from beanie import PydanticObjectId

    from pocketpaw_ee.cloud.models.user import User

    try:
        user = await User.get(PydanticObjectId(owner_id))
    except Exception:  # noqa: BLE001 - an unreadable owner is not a member
        return False
    return user is not None and any(
        m.workspace == workspace_id for m in getattr(user, "workspaces", None) or ()
    )


async def _target(call: Admitted) -> None:
    """The alias's agent and who pays for it, by the side completion's own
    resolution. An agent that no longer resolves is a misconfigured alias."""
    from pocketpaw_ee.cloud.chat import side_completion

    try:
        target = (
            await side_completion.resolve_target(call.workspace_id, call.owner_id, call.agent_id)
            if call.owner_id
            else None
        )
    except Exception as exc:  # noqa: BLE001 - an unreadable payer refuses, never 500s
        logger.warning(
            "site_ai: target check failed for run %s (%s)", call.run_id, type(exc).__name__
        )
        raise Unavailable("temporary", "provider_unavailable", stream=call.stream) from None
    if target is None:
        aliases.mark_offline(call.workspace_id, call.pocket_id, call.alias, "agent.not_found")
        raise Unavailable("temporary", "provider_unavailable", stream=call.stream)
    if not await owner_is_member(call.workspace_id, call.owner_id):
        aliases.mark_offline(call.workspace_id, call.pocket_id, call.alias, "owner.not_member")
        raise Unavailable("temporary", "provider_unavailable", stream=call.stream)
    call.payer = target.payer
    call.ran_model = call.model or target.model or None


async def admit(request: Request, site_id: str) -> Admitted:
    from pocketpaw_ee.cloud.billing import site_plans

    site, caller = await authenticate(request, site_id)
    workspace_id, pocket_id = site.workspace, site.pocket_id
    parsed = validate.parse_chat(validate.load_json(await validate.read_body(request)))
    refuse_unsupported(parsed)
    settings = await service.load_settings(workspace_id, pocket_id)
    ip_hash = admit_browser(request, site, caller, settings, parsed.preset, parsed.stream)

    alias = parsed.model
    messages = parsed.messages
    cap = aliases.ALIASES[alias].max_output_tokens
    preset_id = preset_version = None
    if parsed.preset is not None:
        preset = await service.find_preset(workspace_id, pocket_id, parsed.preset)
        if preset is None:
            raise invalid("preset", "Unknown preset.")
        alias = preset.model
        cap = min(preset.max_output_tokens, aliases.ALIASES[alias].max_output_tokens)
        messages = _apply_preset(parsed, preset, caller)
        preset_id, preset_version = preset.preset_id, preset.version
    if not messages or all(m["role"] == "system" for m in messages):
        raise invalid("messages", "Send at least one non-system message.")

    plan = service.plan_key(site)
    if caller != "preview" and not site_plans.site_ai_published(plan):
        raise AiError("model_not_allowed", "AI on the live site needs a paid site plan.")
    allowed = set(site_plans.site_ai_models(plan)) & set(settings.allowed_models)
    if alias not in allowed:
        raise AiError("model_not_allowed", field="model")
    spec = aliases.ALIASES[alias]
    if parsed.images and not spec.vision:
        # Checked again here: a preset can move the call onto a text alias.
        raise AiError("model_not_allowed", "Images need the vision model.", field="model")
    estimate = validate.estimate_tokens(messages, None, parsed.response_format)
    if estimate > spec.context_tokens:
        raise invalid("messages", "The conversation is too long for this model.")
    if aliases.is_offline(workspace_id, pocket_id, alias):
        raise Unavailable("temporary", "provider_unavailable", stream=parsed.stream)
    picked = aliases.choice(settings, alias)

    call = Admitted(
        site=site,
        workspace_id=workspace_id,
        pocket_id=pocket_id,
        site_id=str(site.id),
        caller=caller,
        alias=alias,
        owner_id=await _owner_id(site),
        agent_id=picked.agent_id,
        model=picked.model,
        messages=messages,
        max_tokens=min(parsed.max_output_tokens or cap, cap),
        input_estimate=estimate,
        stream=parsed.stream,
        response_format=parsed.response_format,
        preset_id=preset_id,
        preset_version=preset_version,
        ip_hash=ip_hash,
        keep_transcript=settings.keep_transcripts,
        user_hash=_hash(f"{workspace_id}:{parsed.user}") if parsed.user else None,
    )

    budget = await service.load_budget(workspace_id, pocket_id)
    if not settings.enabled or budget.paused:
        raise Unavailable(
            "paused", "budget_exhausted", "AI is paused for this site.", stream=call.stream
        )
    await _target(call)
    await _rate_limit(call)
    call.visitor = call.user_hash if call.caller == "worker" else call.ip_hash
    if call.payer != "byok":
        await _wallet_gate(call)
    await _reserve(call, budget, site_plans.site_ai_allowance_micro(plan))
    return call


async def _wallet_gate(call: Admitted) -> None:
    """The workspace wallet first: kill switch, empty wallet, monthly ceiling."""
    from pocketpaw_ee.cloud.credits.guards import over_billing_limit

    try:
        refusal = await over_billing_limit(call.workspace_id)
    except Exception as exc:  # noqa: BLE001 - an unreadable wallet refuses, never 500s
        logger.warning(
            "site_ai: wallet check failed for run %s (%s)", call.run_id, type(exc).__name__
        )
        raise Unavailable("temporary", "provider_unavailable", stream=call.stream) from None
    if refusal is not None:
        logger.info("site_ai: run %s refused by the wallet (%s)", call.run_id, refusal.code)
        raise Unavailable("limit", "wallet_empty", stream=call.stream)


async def _reserve(call: Admitted, budget: Any, allowance: int) -> None:
    """Hold the call's worst case (input estimate + full output cap) against every
    cap that applies, atomically, before the model is called. A BYOK call runs on
    the workspace's own key: only the owner's own caps hold it (no plan allowance,
    no wallet)."""
    if not aliases.is_priced(call.ran_model):
        logger.error(
            "site_ai: run %s model %s has no verified price; refusing the call",
            call.run_id,
            call.ran_model or "<unresolved>",
        )
        aliases.mark_offline(call.workspace_id, call.pocket_id, call.alias, "model.unpriced")
        raise Unavailable("temporary", "provider_unavailable", stream=call.stream)
    call.admitted_model = call.ran_model
    amount = aliases.reserve_micro(call.alias, call.ran_model, call.input_estimate, call.max_tokens)
    byok = call.payer == "byok"
    caps = budget_mod.caps_for(
        caller=call.caller,
        budget=budget,
        allowance=int(budget.monthly_cap_micro) if byok else allowance,
        visitor=call.visitor,
    )
    try:
        wallet = None if byok else await budget_mod.wallet_cap(call.workspace_id)
        if wallet is not None:
            caps.append(wallet)
        call.hold = await budget_mod.reserve(
            run_id=call.run_id,
            workspace_id=call.workspace_id,
            pocket_id=call.pocket_id,
            site_id=call.site_id,
            caller=call.caller,
            model=call.alias,
            amount=amount,
            caps=caps,
            kind=call.kind,
            preset_id=call.preset_id,
            preset_version=call.preset_version,
        )
    except budget_mod.BudgetRefused as exc:
        logger.info("site_ai: run %s over the %s cap", call.run_id, exc.scope)
        code = "wallet_empty" if exc.scope == "wallet" else "budget_exhausted"
        raise Unavailable("limit", code, stream=call.stream) from None
    except Exception as exc:  # noqa: BLE001 - a failed hold refuses, never 500s
        logger.warning("site_ai: hold failed for run %s (%s)", call.run_id, type(exc).__name__)
        raise Unavailable("temporary", "provider_unavailable", stream=call.stream) from None


async def _rate_limit(call: Admitted) -> None:
    base = f"{call.workspace_id}:{call.pocket_id}"
    if call.caller != "worker":
        if await site_gate.bump("ai_ip", f"{base}:{call.ip_hash}") > BROWSER_IP_PER_MIN:
            raise AiError("rate_limited", retry_after=_retry_after())
        if await site_gate.bump("ai_browser", base) > BROWSER_SITE_PER_MIN:
            raise AiError("rate_limited", retry_after=_retry_after())
        return
    if await site_gate.bump("ai_site", base) > WORKER_SITE_PER_MIN:
        raise AiError("rate_limited", retry_after=_retry_after())
    user_key = f"{base}:{call.user_hash}"
    if call.user_hash and await site_gate.bump("ai_user", user_key) > USER_PER_MIN:
        raise AiError("rate_limited", retry_after=_retry_after())


# ------------------------------------------------------------------ record


def _text_tokens(chars: str) -> int:
    return len(chars.encode("utf-8"))


async def _send(call: Admitted) -> None:
    """Count the call as sent once the model has started on it: from here on it
    costs at least its input, whatever happens to the stream or the process."""
    if call.sent:
        return
    call.sent = True
    try:
        await budget_mod.mark_sent(call.run_id)
    except Exception:  # noqa: BLE001 - the in-memory flag still bills it
        logger.warning("site_ai: could not mark run %s sent", call.run_id, exc_info=True)


def _floor_in(call: Admitted) -> int:
    """Input tokens to bill when the backend's own count is missing: the estimate,
    and only for a call the model actually started on."""
    return call.input_estimate if call.sent else 0


def _tokens(call: Admitted, result: Any, produced: str) -> tuple[int, int]:
    """The backend's token count when it reported one (input as the inclusive
    prompt total, cache reads and writes included), else the estimate."""
    from pocketpaw_ee.cloud.metering.service import _prompt_tokens

    usage = getattr(result, "usage", None) or {}
    tokens_in = max(0, _prompt_tokens(usage)[0])
    tokens_out = max(0, int(usage.get("output_tokens") or 0))
    if tokens_in <= 0 and tokens_out <= 0:
        return _floor_in(call), _text_tokens(produced)
    return tokens_in, tokens_out


@dataclass(frozen=True)
class _Bill:
    """One finished call, priced: what ``_persist`` writes."""

    status: str
    input_tokens: int
    output_tokens: int
    output: str
    cost: Decimal
    micro: int
    hold_ratio: float  # priced cost / hold (0 when there is no hold)


def _price(
    call: Admitted,
    status: str,
    input_tokens: int,
    output_tokens: int,
    output: str = "",
    *,
    usage: dict[str, Any] | None = None,
    model: str | None = None,
) -> _Bill:
    """Price one finished call from its tokens at the verified rate of the model
    that ran. The hold is the most a call may cost the site: an overrun is clamped
    to it and logged at WARNING with its ratio."""
    input_tokens, output_tokens = max(0, int(input_tokens)), max(0, int(output_tokens))
    if model:
        call.ran_model = model
    tokens = (input_tokens, output_tokens)
    usage = usage or {}
    call.model_reported = str(usage.get("model") or "") or None
    reported_cost = usage.get("total_cost_usd")
    if isinstance(reported_cost, (int, float)) and not isinstance(reported_cost, bool):
        call.cli_cost_usd = float(reported_cost)
    if call.model_reported and call.ran_model and call.model_reported != call.ran_model:
        logger.warning(
            "site_ai: run %s model mismatch: requested=%s reported=%s",
            call.run_id,
            call.ran_model,
            call.model_reported,
        )
    cost: Decimal | None
    try:
        cost = aliases.cost_usd(call.alias, call.ran_model, usage, tokens, backend=call.backend)
    except aliases.Unpriced as exc:
        # The backend answered on a model nothing prices. Never a guessed rate:
        # the call is priced at the model it was admitted (and held) on.
        admitted = call.admitted_model or call.ran_model
        logger.error(
            "site_ai: run %s ran on %s, which has no verified price; pricing it as %s",
            call.run_id,
            exc.model,
            admitted,
        )
        unpinned = {k: v for k, v in usage.items() if k != "model"}
        try:
            cost = aliases.cost_usd(call.alias, admitted, unpinned, tokens, backend=call.backend)
        except aliases.Unpriced:
            cost = None
    if cost is None:
        # Not even the admitted model prices (never expected): bill the hold.
        micro = call.hold.reserved if call.hold is not None else 0
        return _Bill(status, input_tokens, output_tokens, output, Decimal(0), micro, 1.0)
    micro = aliases.to_micro(cost)
    ratio = micro / call.hold.reserved if call.hold is not None and call.hold.reserved else 0.0
    if call.hold is not None and micro > call.hold.reserved:
        # The platform absorbs an overrun (a backend with no output cap, a price
        # list above ours) rather than letting concurrent calls push the site's
        # counters past its caps.
        logger.warning(
            "site_ai: run %s cost %d over its %d hold (ratio=%.2f model=%s); billing the hold",
            call.run_id,
            micro,
            call.hold.reserved,
            ratio,
            call.model_reported or call.ran_model,
        )
        cost = cost * Decimal(call.hold.reserved) / Decimal(micro)
        micro = call.hold.reserved
    return _Bill(status, input_tokens, output_tokens, output, cost, micro, ratio)


async def record(
    call: Admitted,
    status: str,
    input_tokens: int,
    output_tokens: int,
    output: str = "",
    *,
    usage: dict[str, Any] | None = None,
    model: str | None = None,
) -> int:
    """Price and persist one finished call; returns its micro-credits. Never raises.
    ``output`` only feeds an opted-in transcript. A BYOK call is priced (the site's
    own caps count it) but never debited."""
    bill = _price(call, status, input_tokens, output_tokens, output, usage=usage, model=model)
    await _persist(call, bill)
    return bill.micro


async def _shielded(coro: Any) -> Any:
    """Run ``coro`` in its own task, awaited through a shield: a caller that goes
    away (the visitor closed the stream) cannot cut a billing write short."""
    task = asyncio.get_running_loop().create_task(coro)
    _BACKGROUND.add(task)
    task.add_done_callback(_BACKGROUND.discard)
    return await asyncio.shield(task)


async def _persist_after(call: Admitted, bill: _Bill) -> None:
    await _shielded(_persist(call, bill))


async def _persist(call: Admitted, bill: _Bill) -> None:
    """Write one priced call: run doc (and the debit it schedules), the hold's
    settle or release, alerts, an opted-in transcript, the ``site_ai.call`` line."""
    status, input_tokens, output_tokens = bill.status, bill.input_tokens, bill.output_tokens
    output, cost, micro = bill.output, bill.cost, bill.micro
    try:
        await _write_run(call, status, input_tokens, output_tokens, cost)
    except Exception:  # noqa: BLE001 - bookkeeping must not break the answer
        logger.warning("site_ai: run doc failed for run %s", call.run_id, exc_info=True)
        try:
            await _debit_direct(call, micro)
        except Exception:  # noqa: BLE001 - logged; the hold row still records the spend
            logger.error("site_ai: run %s is unbilled", call.run_id, exc_info=True)
    try:
        if call.hold is not None:
            if input_tokens or output_tokens:
                await budget_mod.settle(call.hold, micro, input_tokens, output_tokens)
            else:
                await budget_mod.release(call.hold)
    except Exception:  # noqa: BLE001 - the sweeper releases a hold we couldn't
        logger.warning("site_ai: settle failed for run %s", call.run_id, exc_info=True)
    if micro:
        await alerts.check(call.site, call.workspace_id, call.pocket_id)
    if call.keep_transcript and (input_tokens or output_tokens) and call.kind == "chat":
        try:
            if await _still_keeps_transcripts(call):
                await service.write_transcript(
                    workspace_id=call.workspace_id,
                    pocket_id=call.pocket_id,
                    run_id=call.run_id,
                    caller=call.caller,
                    model=call.alias,
                    messages=call.messages,
                    output=output,
                    tool_calls=[],
                )
        except Exception:  # noqa: BLE001 - a transcript never breaks the answer
            logger.warning("site_ai: transcript failed for run %s", call.run_id, exc_info=True)
    logger.info(
        "site_ai.call run=%s site=%s alias=%s caller=%s payer=%s status=%s in=%d out=%d "
        "micro=%d model_requested=%s model_reported=%s cli_cost_usd=%s hold_ratio=%.2f "
        "ttft_ms=%s total_ms=%s",
        call.run_id,
        call.site_id,
        call.alias,
        call.caller,
        call.payer,
        status,
        input_tokens,
        output_tokens,
        micro,
        call.ran_model or "-",
        call.model_reported or "-",
        "-" if call.cli_cost_usd is None else call.cli_cost_usd,
        bill.hold_ratio,
        "-" if call.ttft_ms is None else call.ttft_ms,
        "-" if call.total_ms is None else call.total_ms,
    )


async def _still_keeps_transcripts(call: Admitted) -> bool:
    """Re-read at write time: the owner may have turned transcripts off, or the site
    may have moved to another workspace, while the call ran."""
    from pocketpaw_ee.cloud.models.site import Site

    site = await Site.get(call.site.id)
    if site is None or site.workspace != call.workspace_id:
        return False
    return (await service.load_settings(call.workspace_id, call.pocket_id)).keep_transcripts


async def _debit_direct(call: Admitted, micro: int) -> None:
    """The run doc couldn't be written, so ``bill_run`` will never see this call:
    debit it here under the same ``run:{run_id}`` key (live mode bills at the proxy;
    a BYOK call ran on the workspace's own key and is never debited)."""
    from pocketpaw_ee.cloud.credits import service as credits_service
    from pocketpaw_ee.cloud.llm_provisioning import service as provisioning_service

    if micro <= 0 or call.payer == "byok" or provisioning_service.spend_mode() == "live":
        return
    await credits_service.debit(
        workspace=call.workspace_id,
        amount_micro=micro,
        cause="compute_spend",
        idempotency_key=f"run:{call.run_id}",
        ref={
            "run_id": call.run_id,
            "kind": "site_ai",
            "pocket_id": call.pocket_id,
            "site_id": call.site_id,
            "model": f"site-ai/{call.alias}",
            "cost_source": "site_ai_direct",
        },
        allow_negative=True,
    )


async def _write_run(
    call: Admitted, status: str, input_tokens: int, output_tokens: int, cost: Any
) -> None:
    from pocketpaw_ee.cloud.chat.runs import service as run_service
    from pocketpaw_ee.cloud.chat.runs.domain import RunSpec

    if input_tokens <= 0 and output_tokens <= 0:
        return
    spec = RunSpec(
        run_id=call.run_id,
        workspace_id=call.workspace_id,
        context_type="site_ai",
        scope_id=call.pocket_id,
        session_key=f"site_ai:{call.site_id}",
        group=None,
        user_id=f"site_ai:{call.caller}",
        agent_id="site_ai",
        client_message_id=call.run_id,
        user_message_id="",
        content="",
        history=[],
        intent=None,
        surface="site_ai",
        surface_meta={"site_id": call.site_id, "caller": call.caller, "alias": call.alias},
    )
    await run_service.create_run(spec)
    # ``bill_run`` never debits a ``byok`` run: the workspace's own key paid.
    await run_service.set_credential_source(call.run_id, call.payer)
    # ``model`` is the alias label the owner's spend views show; the model that ran
    # is kept in ``provider_model`` for operators only. ``total_cost_usd`` carries
    # the provider cost, so the meter prices this run without needing the model.
    usage = {
        "model": f"site-ai/{call.alias}",
        "provider_model": call.ran_model,
        "total_cost_usd": float(cost),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "backend": "site_ai",
        "site_id": call.site_id,
        "model_alias": call.alias,
        "caller": call.caller,
    }
    if status == "completed":
        await run_service.mark_completed(
            call.run_id, assistant_message_id=None, partial_text="", usage=usage
        )
    else:
        await run_service.mark_terminal(call.run_id, status=status, usage=usage)
    if call.payer != "byok":
        _schedule_bill(call.run_id)


def _schedule_bill(run_id: str) -> None:
    async def _bill() -> None:
        from pocketpaw_ee.cloud.metering.service import bill_run_now

        await bill_run_now(run_id)

    try:
        task = asyncio.get_running_loop().create_task(_bill())
    except RuntimeError:
        return
    _BACKGROUND.add(task)
    task.add_done_callback(_BACKGROUND.discard)


# ------------------------------------------------------------------ run

_UPSTREAM_MESSAGE = "The AI service stopped mid-answer. Try again."
_BAD_JSON_MESSAGE = "The model's reply wasn't valid JSON. Try again."
# A failed call that means the alias itself is broken, not this request: it goes
# offline and operators get one ERROR line.
_MISCONFIGURED = frozenset({"model", "no_agent"})
_MISCONFIGURED_CODES = frozenset({"side.backend_unsupported", "byok.base_url_rejected"})


def _misconfigured(result: Any) -> bool:
    return result.failure in _MISCONFIGURED or result.code in _MISCONFIGURED_CODES


_IMAGES_MESSAGE = "The model behind this alias can't see images; send text."


def _image_field(call: Admitted) -> str:
    for i, msg in enumerate(call.messages):
        content = msg.get("content")
        if isinstance(content, list) and any(p["type"] == "image_url" for p in content):
            return f"messages[{i}].content"
    return "messages"


def _unsupported(call: Admitted) -> AiError:
    return AiError("unsupported", _IMAGES_MESSAGE, field=_image_field(call))


def _refusal(call: Admitted, result: Any) -> Unavailable:
    """What a failed call tells the site. Logged by kind, never with backend text."""
    logger.warning("site_ai: run %s failed (%s, %s)", call.run_id, result.failure, result.code)
    if result.failure == "billing":
        return Unavailable("limit", "wallet_empty", stream=call.stream)
    if _misconfigured(result):
        aliases.mark_offline(call.workspace_id, call.pocket_id, call.alias, result.code)
    return Unavailable("temporary", "provider_unavailable", stream=call.stream)


async def _bill_failure(call: Admitted, result: Any, produced: str) -> int:
    """A failed call costs nothing unless the model actually worked on it: then its
    reported usage, else its input estimate plus what it produced. A backend error
    with no usage and no output is a refusal and costs nothing; a timeout after
    the run began keeps the input estimate."""
    if result.sent:
        await _send(call)
    refused = result.failure == "error" and not result.usage and not produced
    if not call.sent or refused:
        return await record(call, "failed", 0, 0)
    tokens_in, tokens_out = _tokens(call, result, produced)
    return await record(
        call, "failed", tokens_in, tokens_out, produced, usage=result.usage, model=result.model
    )


def _usage_frame(tokens_in: int, tokens_out: int, micro: int) -> bytes:
    return sse(
        "usage", {"input_tokens": tokens_in, "output_tokens": tokens_out, "credits_micro": micro}
    )


def _elapsed_ms(call: Admitted) -> int:
    return int((time.monotonic() - call.started) * 1000)


async def _events(call: Admitted) -> AsyncIterator[bytes]:
    json_mode = _json_mode(call)
    streamed = ""
    recorded = False
    truncated = False
    result: Any = None
    deferred: _Bill | None = None  # priced, sent, persisted after ``done``
    call.started = time.monotonic()
    try:
        try:
            async with aclosing(llm.stream(call)) as items:
                async for item in items:
                    if isinstance(item, str):
                        if call.ttft_ms is None:
                            call.ttft_ms = _elapsed_ms(call)
                        await _send(call)
                        streamed += item
                        if not json_mode:
                            yield sse("chunk", {"delta": item})
                        if _over_cap(call, streamed):
                            truncated = True
                            break
                    elif llm.is_started(item):
                        await _send(call)  # STARTED: the model call has begun
                    else:
                        result = item
        except Exception as exc:  # noqa: BLE001 - never echoed; the seam never raises
            logger.warning("site_ai: run %s stream crashed (%s)", call.run_id, type(exc).__name__)
            result = None
        call.total_ms = _elapsed_ms(call)
        if result is not None:
            call.backend = getattr(result, "backend", None) or call.backend
        # From here every billing write runs shielded (``_shielded`` / the
        # deferred bill), so ``recorded`` may be set before awaiting them.
        recorded = True
        if truncated:
            tokens_in, tokens_out = _floor_in(call), _text_tokens(streamed)
            deferred = _price(call, "completed", tokens_in, tokens_out, streamed)
            micro = deferred.micro
            if json_mode:
                yield _usage_frame(tokens_in, tokens_out, micro)
                yield sse("error", {"code": "provider_unavailable", "message": _BAD_JSON_MESSAGE})
                return
            yield _usage_frame(tokens_in, tokens_out, micro)
            yield sse("done", {"id": f"aic_{call.run_id}", "finish_reason": "length"})
            return
        if result is not None and result.payer:
            call.payer = result.payer
        if result is None or result.failure is not None:
            result = result or _NO_RESULT
            micro = await _shielded(_bill_failure(call, result, streamed))
            if result.failure == "unsupported":
                err = _unsupported(call)
                yield sse("error", {"code": err.code, "message": err.message, "field": err.field})
            elif streamed and not json_mode:
                tokens_in, tokens_out = _tokens(call, result, streamed)
                yield _usage_frame(tokens_in, tokens_out, micro)
                yield sse("error", {"code": "provider_unavailable", "message": _UPSTREAM_MESSAGE})
            else:
                yield sse("unavailable", {"reason": _refusal(call, result).reason})
            return
        await _shielded(_send(call))
        text = result.text
        tokens_in, tokens_out = _tokens(call, result, text)
        deferred = _price(
            call, "completed", tokens_in, tokens_out, text, usage=result.usage, model=result.model
        )
        micro = deferred.micro
        if json_mode:
            value = llm.json_text(text)
            if value is None:
                yield _usage_frame(tokens_in, tokens_out, micro)
                yield sse("error", {"code": "provider_unavailable", "message": _BAD_JSON_MESSAGE})
                return
            yield sse("chunk", {"delta": value})
        yield _usage_frame(tokens_in, tokens_out, micro)
        yield sse("done", {"id": f"aic_{call.run_id}", "finish_reason": "stop"})
    finally:
        if deferred is not None:
            # Billing after the visitor has ``done``; idempotent, run exactly once.
            bill, deferred = deferred, None
            await _persist_after(call, bill)
        elif not recorded:
            # The caller went away: bill what the model already did, if it started.
            await asyncio.shield(
                record(call, "cancelled", _floor_in(call), _text_tokens(streamed), streamed)
            )


def _over_cap(call: Admitted, streamed: str) -> bool:
    """The backstop for a backend with no output cap: cut the stream once its text
    is past the call's output cap (at a conservative 3 UTF-8 bytes a token)."""
    return (len(streamed.encode("utf-8")) + 2) // 3 > call.max_tokens


class _Unfinished:
    """Stands in for a stream that ended without its result (never expected)."""

    failure = "error"
    code = "site_ai.no_result"
    sent = False
    usage = None
    model = None
    payer = None
    text = ""


_NO_RESULT = _Unfinished()


def _json_mode(call: Admitted) -> bool:
    return bool(call.response_format) and call.response_format.get("type") != "text"


async def _json(call: Admitted) -> Response:
    call.started = time.monotonic()
    try:
        result = await llm.complete(call)
    except Exception as exc:  # noqa: BLE001 - never echoed; the seam never raises
        logger.warning("site_ai: run %s crashed (%s)", call.run_id, type(exc).__name__)
        result = _NO_RESULT
    call.total_ms = _elapsed_ms(call)
    call.backend = getattr(result, "backend", None) or call.backend
    if result.payer:
        call.payer = result.payer
    if result.failure is not None:
        await _bill_failure(call, result, "")
        if result.failure == "unsupported":
            return error_response(_unsupported(call))
        return error_response(_refusal(call, result).error)
    await _send(call)
    text = result.text
    tokens_in, tokens_out = _tokens(call, result, text)
    micro = await record(
        call, "completed", tokens_in, tokens_out, text, usage=result.usage, model=result.model
    )
    content: str | None = text
    if _json_mode(call):
        content = llm.json_text(text)
        if content is None:
            return error_response(AiError("provider_unavailable", _BAD_JSON_MESSAGE))
    return JSONResponse(
        {
            "id": f"aic_{call.run_id}",
            "model": call.alias,
            "message": {"role": "assistant", "content": content},
            "finish_reason": "stop",
            "usage": {
                "input_tokens": tokens_in,
                "output_tokens": tokens_out,
                "credits_micro": micro,
            },
        },
        headers={"Cache-Control": "no-store"},
    )


_SSE_HEADERS = {"Cache-Control": "no-store", "X-Accel-Buffering": "no"}


def _unavailable_stream(reason: str) -> StreamingResponse:
    async def _one() -> AsyncIterator[bytes]:
        yield sse("unavailable", {"reason": reason})

    return StreamingResponse(_one(), media_type="text/event-stream", headers=_SSE_HEADERS)


async def handle(request: Request, site_id: str) -> Response:
    try:
        call = await admit(request, site_id)
    except Unavailable as exc:
        # Only raised after the body parsed, so the caller's ``stream`` is known.
        if exc.stream:
            return _unavailable_stream(exc.reason)
        return error_response(exc.error)
    except AiError as exc:
        return error_response(exc)
    if call.stream:
        return StreamingResponse(
            _events(call), media_type="text/event-stream", headers=_SSE_HEADERS
        )
    return await _json(call)


async def handle_ticket(request: Request, site_id: str) -> Response:
    """POST /sites/{site_id}/ai/ticket: Turnstile in, visitor ticket out."""
    try:
        site, caller = await authenticate(request, site_id)
        if caller == "worker":
            raise invalid("caller", "Tickets are for browser calls; a Worker uses its token.")
        settings = await service.load_settings(site.workspace, site.pocket_id)
        if caller == "browser" and not settings.browser_access:
            raise AiError("browser_disabled")
        body = validate.load_json(await validate.read_body(request, limit=8 * 1024))
        ip = visitor.client_address(request)
        ip_hash = visitor.visitor_key(request)
        key = f"{site.workspace}:{site.pocket_id}:{ip_hash}"
        if await site_gate.bump("ai_ticket", key) > TICKET_IP_PER_MIN:
            raise AiError("rate_limited", retry_after=_retry_after())
        if caller == "browser":
            await tickets.check_turnstile(site, body.get("turnstile_token"), ip)
        ticket, expires_at = tickets.mint(caller, str(site.id), ip_hash)
    except AiError as exc:
        return error_response(exc)
    return JSONResponse(
        {"ticket": ticket, "expires_at": expires_at}, headers={"Cache-Control": "no-store"}
    )
