# ee/pocketpaw_ee/site_ai/chat.py — POST /sites/{site_id}/ai/chat, end to end.
#
# ``admit`` runs every gate and returns an ``Admitted`` call or raises:
#   1. per-IP bucket (in-process)                       -> 429 rate_limited
#   2. site_gate.authenticate: PAW_GATEWAY_TOKEN = worker; a site key from the
#      site's own hosts = browser, from its own draft = preview -> 401 / 403
#   3. browser and preview callers are refused here     -> 403 browser_disabled
#   4. body (<= 400 KB) parsed from an allowlist (validate.py)  -> 400 / 413
#   5. preset: its system prompt, model, output cap and temperature win
#   6. alias allowed by the plan AND the owner, live site on a plan that sells it,
#      images only on a vision alias, input within the alias context -> 403 / 400
#   7. site AI off or paused                            -> Unavailable("paused")
#   8. SiteRateCounter per site, and per hashed ``user`` when sent -> 429
# ``Unavailable`` (paused, later budget) answers a streaming request with one
# ``unavailable`` SSE event and a JSON request with its 402.
#
# Then one LiteLLM call (llm.py) with the validated fields only, retried once when
# a transient failure happens before the first token. Tool calls are relayed to
# the caller and never executed. Every stream ends with ``done``, ``unavailable``
# or ``error``. ``record`` prices the tokens (provider usage; an estimate for a cut
# stream), writes the ``ChatRunDoc`` (context ``site_ai``, no content) and the
# ``SiteAiCallDoc``, and asks for the wallet debit (``bill_run`` keyed ``run:{id}``).

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from pocketpaw.security.rate_limiter import RateLimiter
from pocketpaw_ee.integrations import site_gate
from pocketpaw_ee.site_ai import aliases, llm, service, validate
from pocketpaw_ee.site_ai.errors import AiError, error_response, invalid

logger = logging.getLogger(__name__)

WORKER_SITE_PER_MIN = 600
USER_PER_MIN = 60

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
    from pocketpaw_ee.cloud._core.rate_limit import client_ip

    ip = client_ip(request)
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


async def _admit_caller(request: Request, site: Any, caller: str) -> None:
    """Browser and preview callers: not on this path yet."""
    if caller != "worker":
        raise AiError("browser_disabled")


def _apply_preset(parsed: validate.ParsedChat, preset: Any, caller: str) -> list[dict]:
    messages = parsed.messages
    if caller != "worker":
        # A browser caller never overrides the preset's system prompt.
        messages = [m for m in messages if m["role"] != "system"]
    if preset.system_prompt:
        messages = [{"role": "system", "content": preset.system_prompt}, *messages]
    return messages


async def admit(request: Request, site_id: str) -> Admitted:
    from pocketpaw_ee.cloud._core.rate_limit import client_ip
    from pocketpaw_ee.cloud.billing import site_plans

    site, caller = await authenticate(request, site_id)
    await _admit_caller(request, site, caller)
    workspace_id, pocket_id = site.workspace, site.pocket_id
    parsed = validate.parse_chat(validate.load_json(await validate.read_body(request)))
    settings = await service.load_settings(workspace_id, pocket_id)

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
        ip_hash=site_gate.rate_key(client_ip(request)),
        user_hash=_hash(f"{workspace_id}:{parsed.user}") if parsed.user else None,
    )

    budget = await service.load_budget(workspace_id, pocket_id)
    if not settings.enabled or budget.paused:
        raise Unavailable(
            "paused", "budget_exhausted", "AI is paused for this site.", stream=call.stream
        )
    await _rate_limit(call)
    return call


async def _rate_limit(call: Admitted) -> None:
    base = f"{call.workspace_id}:{call.pocket_id}"
    if await site_gate.bump("ai_site", base) > WORKER_SITE_PER_MIN:
        raise AiError("rate_limited", retry_after=_retry_after())
    user_key = f"{base}:{call.user_hash}"
    if call.user_hash and await site_gate.bump("ai_user", user_key) > USER_PER_MIN:
        raise AiError("rate_limited", retry_after=_retry_after())


# ------------------------------------------------------------------ record


def _text_tokens(chars: str) -> int:
    return len(chars.encode("utf-8"))


async def record(call: Admitted, status: str, input_tokens: int, output_tokens: int) -> int:
    """Price and persist one finished call; returns its micro-credits. Never raises."""
    input_tokens, output_tokens = max(0, int(input_tokens)), max(0, int(output_tokens))
    micro = aliases.actual_micro(call.alias, input_tokens, output_tokens)
    try:
        await _write_run(call, status, input_tokens, output_tokens)
        await _settle(call, micro, input_tokens, output_tokens)
    except Exception:  # noqa: BLE001 - bookkeeping must not break the answer
        logger.warning("site_ai: bookkeeping failed for run %s", call.run_id, exc_info=True)
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
    usage = {
        "model": call.provider_model,
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


async def _settle(call: Admitted, micro: int, input_tokens: int, output_tokens: int) -> None:
    from pocketpaw_ee.cloud.models.site_ai import SiteAiCallDoc, SiteAiCounterDoc

    now = datetime.now(UTC)
    await SiteAiCallDoc(
        run_id=call.run_id,
        workspace=call.workspace_id,
        pocket_id=call.pocket_id,
        site_id=call.site_id,
        caller=call.caller,  # type: ignore[arg-type]
        model=call.alias,
        preset_id=call.preset_id,
        preset_version=call.preset_version,
        actual_micro=micro,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        state="settled",
        day=service.day_period(now),
        settled_at=now,
    ).insert()
    coll = SiteAiCounterDoc.get_pymongo_collection()
    for scope, period in (("day", service.day_period(now)), ("month", service.month_period(now))):
        await coll.update_one(
            {"key": service.counter_key(call.workspace_id, call.pocket_id, scope, period)},
            {
                "$inc": {"committed_micro": micro, "spent_micro": micro, "calls": 1},
                "$setOnInsert": {
                    "workspace": call.workspace_id,
                    "pocket_id": call.pocket_id,
                    "held_micro": 0,
                    "created_at": now,
                },
            },
            upsert=True,
        )


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
                    if started or not exc.transient or attempt == 1:
                        raise
                    logger.info("site_ai: retrying run %s after %s", call.run_id, exc.kind)
        except Exception as exc:  # noqa: BLE001 - classified below, never echoed
            kind = exc.kind if isinstance(exc, llm.LlmError) else type(exc).__name__
            logger.warning("site_ai: run %s failed (%s)", call.run_id, kind)
            recorded = True
            if not started:
                await record(call, "failed", 0, 0)
                if _rejected(exc):
                    yield sse("error", {"code": "invalid_request", "message": _REJECTED_MESSAGE})
                else:
                    yield sse("unavailable", {"reason": "temporary"})
                return
            tokens_in, tokens_out = usage or (call.input_estimate, _text_tokens(streamed))
            micro = await record(call, "failed", tokens_in, tokens_out)
            yield sse(
                "usage",
                {"input_tokens": tokens_in, "output_tokens": tokens_out, "credits_micro": micro},
            )
            yield sse("error", {"code": "provider_unavailable", "message": _UPSTREAM_MESSAGE})
            return
        tokens_in, tokens_out = usage or (call.input_estimate, _text_tokens(streamed))
        recorded = True
        micro = await record(call, "completed", tokens_in, tokens_out)
        yield sse(
            "usage",
            {"input_tokens": tokens_in, "output_tokens": tokens_out, "credits_micro": micro},
        )
        yield sse("done", {"id": f"aic_{call.run_id}", "finish_reason": finish})
    finally:
        if not recorded:
            # The caller went away mid-stream: bill what the provider already did.
            tokens_in = call.input_estimate if started else 0
            tokens_out = _text_tokens(streamed)
            await asyncio.shield(record(call, "cancelled", tokens_in, tokens_out))


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
            result = await llm.complete(call.payload())
            break
        except llm.LlmError as exc:
            logger.warning("site_ai: run %s failed (%s)", call.run_id, exc.kind)
            if not exc.transient or attempt == 1:
                await record(call, "failed", 0, 0)
                if _rejected(exc):
                    return error_response(AiError("invalid_request", _REJECTED_MESSAGE))
                return error_response(AiError("provider_unavailable"))
    assert result is not None
    micro = await record(call, "completed", result.input_tokens, result.output_tokens)
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
                "input_tokens": result.input_tokens,
                "output_tokens": result.output_tokens,
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
