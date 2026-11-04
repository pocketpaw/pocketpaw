# ee/pocketpaw_ee/site_ai/embed.py — POST /sites/{site_id}/ai/embed.
#
# Embeddings for site-side retrieval: ``{input: string | string[], model?: "embed"}``
# -> ``{vectors: number[][], usage: {input_tokens, credits_micro}}``. Same callers
# and gates as ``/ai/chat`` (chat.py): the Worker token, or a browser / own-draft
# caller with browser access and a live ticket. ``model`` defaults to ``embed`` and
# may only be ``embed``; the plan must sell it (``_SITE_PLAN_AI_MODELS``).
# Limits: 64 inputs, 8,000 chars each, 1 MB body. The worst case (UTF-8 bytes, an
# upper bound on tokens) is reserved against the same caps and settled to the
# provider's count; a request that left but failed bills its input estimate.
# JSON only: a paused site, an empty wallet or a spent budget is a 402.

from __future__ import annotations

import logging
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse, Response

from pocketpaw_ee.site_ai import aliases, chat, llm, service, validate
from pocketpaw_ee.site_ai.errors import AiError, error_response, invalid

logger = logging.getLogger(__name__)

MAX_INPUTS = 64
MAX_INPUT_CHARS = 8_000
MAX_BODY_BYTES = 1024 * 1024


def _inputs(body: dict[str, Any]) -> list[str]:
    raw = body.get("input")
    items = [raw] if isinstance(raw, str) else raw
    if not isinstance(items, list) or not items:
        raise invalid("input", "input must be a string or a list of strings.")
    if len(items) > MAX_INPUTS:
        raise invalid("input", f"At most {MAX_INPUTS} inputs per request.")
    for i, item in enumerate(items):
        if not isinstance(item, str) or not item:
            raise invalid(f"input[{i}]", "Each input must be a non-empty string.")
        if len(item) > MAX_INPUT_CHARS:
            raise invalid(f"input[{i}]", f"Each input can be at most {MAX_INPUT_CHARS} chars.")
    return items


async def _admit(request: Request, site_id: str) -> tuple[chat.Admitted, list[str]]:
    from pocketpaw_ee.cloud.billing import site_plans

    site, caller = await chat.authenticate(request, site_id)
    body = validate.load_json(await validate.read_body(request, limit=MAX_BODY_BYTES))
    if body.get("model", "embed") != "embed":
        raise invalid("model", "model must be embed.")
    inputs = _inputs(body)
    settings = await service.load_settings(site.workspace, site.pocket_id)
    ip_hash = chat.admit_browser(request, site, caller, settings, None, stream=False)
    plan = service.plan_key(site)
    if caller != "preview" and not site_plans.site_ai_published(plan):
        raise AiError("model_not_allowed", "AI on the live site needs a paid site plan.")
    if "embed" not in site_plans.site_ai_models(plan):
        raise AiError("model_not_allowed", field="model")
    provider_model = aliases.provider_model("embed")
    if not provider_model:
        raise AiError("provider_unavailable")
    estimate = sum(len(t.encode("utf-8")) for t in inputs)  # bytes >= tokens
    call = chat.Admitted(
        site=site,
        workspace_id=site.workspace,
        pocket_id=site.pocket_id,
        site_id=str(site.id),
        caller=caller,
        alias="embed",
        provider_model=provider_model,
        messages=[],
        max_tokens=0,
        input_estimate=estimate,
        stream=False,
        ip_hash=ip_hash,
        kind="embed",
    )
    budget = await service.load_budget(site.workspace, site.pocket_id)
    if not settings.enabled or budget.paused:
        raise chat.Unavailable(
            "paused", "budget_exhausted", "AI is paused for this site.", stream=False
        )
    await chat._rate_limit(call)
    call.visitor = call.ip_hash if caller != "worker" else None
    await chat._wallet_gate(call)
    await chat._reserve(call, budget, site_plans.site_ai_allowance_micro(plan))
    return call, inputs


async def handle(request: Request, site_id: str) -> Response:
    try:
        call, inputs = await _admit(request, site_id)
    except chat.Unavailable as exc:
        return error_response(exc.error)
    except AiError as exc:
        return error_response(exc)
    try:
        await chat._send(call)
        vectors, tokens = await llm.embed(
            provider_model=call.provider_model,
            inputs=inputs,
            workspace_id=call.workspace_id,
            tags=[f"site:{call.site_id}", "site_ai", "alias:embed"],
        )
    except Exception as exc:  # noqa: BLE001 - classified, never echoed
        kind = exc.kind if isinstance(exc, llm.LlmError) else type(exc).__name__
        logger.warning("site_ai: embed run %s failed (%s)", call.run_id, kind)
        if chat._unsent(exc):
            call.sent -= 1
        # A request that left costs its input, answered or not.
        await chat.record(call, "failed", chat._floor_in(call), 0)
        return error_response(AiError("provider_unavailable"))
    tokens = tokens or call.input_estimate
    micro = await chat.record(call, "completed", tokens, 0)
    return JSONResponse(
        {"vectors": vectors, "usage": {"input_tokens": tokens, "credits_micro": micro}},
        headers={"Cache-Control": "no-store"},
    )
