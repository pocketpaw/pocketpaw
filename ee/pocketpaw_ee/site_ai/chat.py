# ee/pocketpaw_ee/site_ai/chat.py — POST /sites/{site_id}/ai/chat, end to end.
#
# ``admit`` runs every gate and returns an ``Admitted`` call or raises:
#   1. per-IP bucket (in-process)                       -> 429 rate_limited
#   2. site_gate.authenticate: PAW_GATEWAY_TOKEN = worker; a site key from the
#      site's own hosts = browser, from its own draft = preview -> 401 / 403
#   3. body (<= 400 KB) parsed from an allowlist (validate.py)  -> 400 / 413
#   4. browser: ``browser_access`` on (-> unavailable browser_disabled); browser and
#      preview: a live ``X-Paw-Ai-Ticket`` of their kind from this IP (-> 401), a
#      preset when the site requires one (-> 403 preset_required)
#   5. preset: its system prompt, model, output cap and temperature win
#   6. alias allowed by the plan AND the owner, live site on a plan that sells it,
#      images only on a vision alias, input within the alias context -> 403 / 400
#   7. site AI off or paused                            -> Unavailable("paused")
#   8. SiteRateCounter: Worker per site and per hashed ``user``; browser/preview
#      per (site, IP hash) and per site                  -> 429
#   9. ``over_billing_limit`` on the workspace wallet -> Unavailable("limit",
#      wallet_empty), then the worst case is reserved against every cap
#      (budget.py) -> Unavailable("limit", budget_exhausted)
# ``Unavailable`` answers a streaming request with one
# ``unavailable`` SSE event and a JSON request with its 402.
#
# Then one LiteLLM call (llm.py) with the validated fields only, retried once when
# a transient failure happens before the first token. Tool calls are relayed to
# the caller and never executed. Every stream ends with ``done``, ``unavailable``
# or ``error``. ``record`` prices the tokens (provider usage; an estimate for a cut
# stream), writes the ``ChatRunDoc`` (context ``site_ai``, no content), settles the
# hold to that figure (or releases it when nothing was produced), checks the
# owner's alert thresholds and asks for the wallet debit (``bill_run``, ``run:{id}``).

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
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
    provider_model: str
    messages: list[dict[str, Any]]
    max_tokens: int
    input_estimate: int
    stream: bool
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    tools: list[dict[str, Any]] = field(default_factory=list)
    tool_choice: Any = None
    response_format: dict[str, Any] | None = None
    temperature: float | None = None
    preset_id: str | None = None
    preset_version: int | None = None
    ip_hash: str = ""
    user_hash: str | None = None
    visitor: str | None = None  # the per-visitor cap's key (hashed)
    hold: budget_mod.Hold | None = None
    sent: int = 0  # provider requests that left this process (each costs its input)

    def payload(self) -> dict[str, Any]:
        return llm.build_payload(
            provider_model=self.provider_model,
            messages=self.messages,
            max_tokens=self.max_tokens,
            workspace_id=self.workspace_id,
            tags=[f"site:{self.site_id}", "site_ai", f"alias:{self.alias}"],
            temperature=self.temperature,
            tools=self.tools,
            tool_choice=self.tool_choice,
            response_format=self.response_format,
        )


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


def _apply_preset(parsed: validate.ParsedChat, preset: Any, caller: str) -> list[dict]:
    """The preset's system prompt first. For a browser (or preview) caller the preset
    is the owner's lock, so the browser's own system messages, tools, tool choice
    and response format are dropped (presets declare none), and its history may
    carry at most ``MAX_BROWSER_ASSISTANT_TURNS`` assistant turns: a fabricated
    history can still argue with the prompt, which is why the lock is soft and
    Worker-side calls stay the default for anything secret."""
    messages = parsed.messages
    if caller != "worker":
        messages = [m for m in messages if m["role"] != "system"]
        if sum(m["role"] == "assistant" for m in messages) > MAX_BROWSER_ASSISTANT_TURNS:
            raise invalid("messages", "Too many assistant turns for a browser call.")
        parsed.tools, parsed.tool_choice, parsed.response_format = [], None, None
    if preset.system_prompt:
        messages = [{"role": "system", "content": preset.system_prompt}, *messages]
    return messages


async def admit(request: Request, site_id: str) -> Admitted:
    from pocketpaw_ee.cloud.billing import site_plans

    site, caller = await authenticate(request, site_id)
    workspace_id, pocket_id = site.workspace, site.pocket_id
    parsed = validate.parse_chat(validate.load_json(await validate.read_body(request)))
    settings = await service.load_settings(workspace_id, pocket_id)
    ip_hash = admit_browser(request, site, caller, settings, parsed.preset, parsed.stream)

    alias = parsed.model
    messages = parsed.messages
    cap = aliases.ALIASES[alias].max_output_tokens
    temperature = parsed.temperature
    preset_id = preset_version = None
    if parsed.preset is not None:
        preset = await service.find_preset(workspace_id, pocket_id, parsed.preset)
        if preset is None:
            raise invalid("preset", "Unknown preset.")
        alias, temperature = preset.model, preset.temperature
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
        raise AiError("model_not_allowed", "Images need the vision model.", field="model")
    estimate = validate.estimate_tokens(messages, parsed.tools, parsed.response_format)
    if estimate > spec.context_tokens:
        raise invalid("messages", "The conversation is too long for this model.")
    provider_model = aliases.provider_model(alias)
    if not provider_model:
        raise AiError("provider_unavailable")

    call = Admitted(
        site=site,
        workspace_id=workspace_id,
        pocket_id=pocket_id,
        site_id=str(site.id),
        caller=caller,
        alias=alias,
        provider_model=provider_model,
        messages=messages,
        max_tokens=min(parsed.max_output_tokens or cap, cap),
        input_estimate=estimate,
        stream=parsed.stream,
        tools=parsed.tools,
        tool_choice=parsed.tool_choice,
        response_format=parsed.response_format,
        temperature=temperature,
        preset_id=preset_id,
        preset_version=preset_version,
        ip_hash=ip_hash,
        user_hash=_hash(f"{workspace_id}:{parsed.user}") if parsed.user else None,
    )

    budget = await service.load_budget(workspace_id, pocket_id)
    if not settings.enabled or budget.paused:
        raise Unavailable(
            "paused", "budget_exhausted", "AI is paused for this site.", stream=call.stream
        )
    await _rate_limit(call)
    call.visitor = call.user_hash if call.caller == "worker" else call.ip_hash
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
    cap that applies, atomically, before the provider is called."""
    amount = aliases.reserve_micro(call.alias, call.input_estimate, call.max_tokens)
    caps = budget_mod.caps_for(
        caller=call.caller, budget=budget, allowance=allowance, visitor=call.visitor
    )
    try:
        wallet = await budget_mod.wallet_cap(call.workspace_id)
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
    """Count a provider request as sent BEFORE it is made: from here on the call
    costs at least its input, whatever happens to the stream or the process."""
    call.sent += 1
    try:
        await budget_mod.mark_sent(call.run_id)
    except Exception:  # noqa: BLE001 - the in-memory count still bills it
        logger.warning("site_ai: could not mark run %s sent", call.run_id, exc_info=True)


def _unsent(exc: BaseException) -> bool:
    return isinstance(exc, llm.LlmError) and exc.kind == "connect"


def _floor_in(call: Admitted, extra_attempts: int = 0) -> int:
    """Input tokens to bill when the provider's own count is missing: the estimate
    for every request that left, ``extra_attempts`` beyond the counted one."""
    return call.input_estimate * max(0, call.sent - extra_attempts)


async def record(call: Admitted, status: str, input_tokens: int, output_tokens: int) -> int:
    """Price and persist one finished call; returns its micro-credits. Never raises."""
    input_tokens, output_tokens = max(0, int(input_tokens)), max(0, int(output_tokens))
    micro = aliases.actual_micro(call.alias, input_tokens, output_tokens)
    try:
        await _write_run(call, status, input_tokens, output_tokens)
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
    logger.info(
        "site_ai.call run=%s site=%s alias=%s caller=%s status=%s in=%d out=%d micro=%d",
        call.run_id,
        call.site_id,
        call.alias,
        call.caller,
        status,
        input_tokens,
        output_tokens,
        micro,
    )
    return micro


async def _debit_direct(call: Admitted, micro: int) -> None:
    """The run doc couldn't be written, so ``bill_run`` will never see this call:
    debit it here under the same ``run:{run_id}`` key (live mode bills at the proxy)."""
    from pocketpaw_ee.cloud.credits import service as credits_service
    from pocketpaw_ee.cloud.llm_provisioning import service as provisioning_service

    if micro <= 0 or provisioning_service.spend_mode() == "live":
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


async def _write_run(call: Admitted, status: str, input_tokens: int, output_tokens: int) -> None:
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
    # ``model`` is the alias label the owner's spend views show; the provider id is
    # kept in ``provider_model`` for operators only. ``total_cost_usd`` carries the
    # provider cost, so the meter prices this run without needing the provider id.
    cost = aliases.actual_cost_usd(call.alias, input_tokens, output_tokens)
    usage = {
        "model": f"site-ai/{call.alias}",
        "provider_model": call.provider_model,
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


async def _events(call: Admitted) -> AsyncIterator[bytes]:
    started = False
    finish = "stop"
    usage: tuple[int, int] | None = None
    streamed = ""
    recorded = False
    try:
        try:
            for attempt in (0, 1):
                try:
                    await _send(call)
                    async for ev in llm.stream(call.payload()):
                        if ev.kind == "text":
                            started = True
                            streamed += ev.text
                            yield sse("chunk", {"delta": ev.text})
                        elif ev.kind == "tool":
                            started = True
                            frame: dict[str, Any] = {"index": ev.index}
                            if ev.id:
                                frame["id"] = ev.id
                            if ev.name:
                                frame["name"] = ev.name
                                streamed += ev.name
                            if ev.arguments:
                                frame["arguments_delta"] = ev.arguments
                                streamed += ev.arguments
                            yield sse("tool_call", frame)
                        elif ev.kind == "finish":
                            finish = ev.reason or "stop"
                        elif ev.kind == "usage":
                            usage = (ev.input_tokens, ev.output_tokens)
                    break
                except llm.LlmError as exc:
                    if _unsent(exc):
                        call.sent -= 1
                    if started or not exc.transient or attempt == 1:
                        raise
                    logger.info("site_ai: retrying run %s after %s", call.run_id, exc.kind)
        except Exception as exc:  # noqa: BLE001 - classified below, never echoed
            kind = exc.kind if isinstance(exc, llm.LlmError) else type(exc).__name__
            logger.warning("site_ai: run %s failed (%s)", call.run_id, kind)
            recorded = True
            if not started:
                # A request that left costs its input even with no output.
                await record(call, "failed", _floor_in(call), 0)
                if _rejected(exc):
                    yield sse("error", {"code": "invalid_request", "message": _REJECTED_MESSAGE})
                else:
                    yield sse("unavailable", {"reason": "temporary"})
                return
            tokens_in, tokens_out = _billed(call, usage, streamed)
            micro = await record(call, "failed", tokens_in, tokens_out)
            yield sse(
                "usage",
                {"input_tokens": tokens_in, "output_tokens": tokens_out, "credits_micro": micro},
            )
            yield sse("error", {"code": "provider_unavailable", "message": _UPSTREAM_MESSAGE})
            return
        tokens_in, tokens_out = _billed(call, usage, streamed)
        recorded = True
        micro = await record(call, "completed", tokens_in, tokens_out)
        yield sse(
            "usage",
            {"input_tokens": tokens_in, "output_tokens": tokens_out, "credits_micro": micro},
        )
        yield sse("done", {"id": f"aic_{call.run_id}", "finish_reason": finish})
    finally:
        if not recorded:
            # The caller went away: bill what the provider already did, at least
            # the input of every request that left, output or not.
            tokens_in = _floor_in(call)
            tokens_out = _text_tokens(streamed)
            await asyncio.shield(record(call, "cancelled", tokens_in, tokens_out))


def _billed(call: Admitted, usage: tuple[int, int] | None, streamed: str) -> tuple[int, int]:
    """Tokens to bill for a stream: the provider's count for the last attempt plus
    the input estimate of any earlier attempt that left; the estimate when the
    provider reported nothing (or zeros)."""
    if usage is None or (usage[0] <= 0 and usage[1] <= 0):
        return _floor_in(call), _text_tokens(streamed)
    return usage[0] + _floor_in(call, extra_attempts=1), usage[1]


_UPSTREAM_MESSAGE = "The AI service stopped mid-answer. Try again."
_REJECTED_MESSAGE = "The model rejected this request. Check the messages and tools sent."


def _rejected(exc: BaseException) -> bool:
    """A provider 4xx that isn't a rate limit: the request itself was refused (a
    tool message with no matching call, a schema the model can't take). Retrying
    won't help and it isn't an outage."""
    return (
        isinstance(exc, llm.LlmError)
        and not exc.transient
        and exc.status is not None
        and 400 <= exc.status < 500
    )


async def _json(call: Admitted) -> Response:
    result = None
    for attempt in (0, 1):
        try:
            await _send(call)
            result = await llm.complete(call.payload())
            break
        except Exception as exc:  # noqa: BLE001 - classified, never echoed
            if not isinstance(exc, llm.LlmError):
                exc = llm.LlmError(False, type(exc).__name__)
            if _unsent(exc):
                call.sent -= 1
            logger.warning("site_ai: run %s failed (%s)", call.run_id, exc.kind)
            if not exc.transient or attempt == 1:
                await record(call, "failed", _floor_in(call), 0)
                if _rejected(exc):
                    return error_response(AiError("invalid_request", _REJECTED_MESSAGE))
                return error_response(AiError("provider_unavailable"))
    assert result is not None
    tokens_in, tokens_out = result.input_tokens, result.output_tokens
    if tokens_in <= 0 and tokens_out <= 0:  # the provider reported no usage
        produced = (result.content or "") + "".join(
            c["function"]["name"] + c["function"]["arguments"] for c in result.tool_calls
        )
        tokens_in, tokens_out = _floor_in(call), _text_tokens(produced)
    else:
        tokens_in += _floor_in(call, extra_attempts=1)
    micro = await record(call, "completed", tokens_in, tokens_out)
    message: dict[str, Any] = {"role": "assistant", "content": result.content}
    if result.tool_calls:
        message["tool_calls"] = result.tool_calls
    return JSONResponse(
        {
            "id": f"aic_{call.run_id}",
            "model": call.alias,
            "message": message,
            "finish_reason": result.finish_reason,
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
