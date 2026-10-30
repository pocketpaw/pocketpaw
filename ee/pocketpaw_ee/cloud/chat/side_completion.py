# ee/pocketpaw_ee/cloud/chat/side_completion.py — one short, tool-less completion
# on a workspace agent's OWN backend and model, outside any chat run.
#
# For features that need a single model answer (the Sites composer's Enhance
# button) and must run where a chat turn would: the agent's backend (Claude SDK,
# pydantic_ai, ...), the per-send model the user picked, the workspace's own key
# when it has one, and the same run-start gates a chat turn passes.
#
# ``agent_complete`` resolves the agent (a hint must belong to the workspace and
# be visible to the user, else the workspace's default ``pocketpaw`` agent), then
# runs the gates in the chat-turn order: who pays (``guards.resolve_payer`` +
# ``billing_source``), the credit gate (``assert_within_billing``), the free-model
# check (``free_model_refusal``); then the catalog check of a per-send model on a
# platform turn, the BYOK provider/model check, and the no-keyless-guest rule.
# Then ONE ``pool.run`` with tools off, a fresh ``side:<uuid>`` session key and
# the caller's system prompt, under ``baggage(paw.internal=true)`` and a timeout.
#
# Invariants:
#   * Never raises: every refusal or error is a typed ``failure`` on the result.
#   * Never runs a backend with tools on. A backend whose ``run`` does not declare
#     ``tools_enabled`` would silently keep its tools, so the call falls back to
#     the deployment default model (``pydantic_ai_model``) through the gateway
#     helper, and only on a platform-billed call. Never the first catalog entry.
#   * ``max_requests`` only reaches pydantic_ai (other backends refuse it).
#   * A platform-funded free run is forced onto pydantic_ai with one request and
#     the wallet's token limit, like ``run_core``. It does NOT reserve on the
#     daily early-access ceiling: that hold is reconciled only by ``bill_run``
#     from a ChatRunDoc, which a side call has none of.
#   * Usage is not billed to the wallet (no ChatRunDoc for ``bill_run``); it is
#     returned and logged. Callers keep their own rate limit.
#   * The backend generator is always closed (``aclosing``).

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Callable
from contextlib import aclosing
from dataclasses import dataclass
from typing import Any, Literal

from pocketpaw.agents import pool as agent_pool  # type: ignore[import-untyped]
from pocketpaw.agents.backend import (  # type: ignore[import-untyped]
    _accepts_tools_enabled_kwarg,
)
from pocketpaw.config import get_settings  # type: ignore[import-untyped]
from pocketpaw.observability import baggage  # type: ignore[import-untyped]
from pocketpaw_ee.cloud._core.errors import CloudError

logger = logging.getLogger(__name__)

# Why a side completion produced no text. ``billing`` covers every credit, quota,
# early-access and keyless-guest refusal; ``model`` an unknown model or one the
# workspace's own key cannot run.
SideFailure = Literal["no_agent", "billing", "model", "timeout", "error"]

DEFAULT_BACKEND = "claude_agent_sdk"
PYDANTIC_AI = "pydantic_ai"


@dataclass(frozen=True)
class SideCompletion:
    text: str = ""
    model: str | None = None
    backend: str | None = None
    agent_id: str | None = None
    failure: SideFailure | None = None
    # The refusal's machine code (``credits.insufficient`` ...), for logs.
    code: str | None = None
    # True when the agent's backend could not run tool-less and the deployment
    # default model answered through the gateway instead.
    fallback: bool = False
    usage: dict[str, Any] | None = None

    @property
    def ok(self) -> bool:
        return self.failure is None and bool(self.text.strip())


def _fail(failure: SideFailure, code: str, **kw: Any) -> SideCompletion:
    return SideCompletion(failure=failure, code=code, **kw)


async def _agent_visible(agent_id: str, workspace_id: str, user_id: str) -> bool:
    """True when ``agent_id`` is an enabled agent in ``workspace_id`` that
    ``user_id`` may use (a private agent only by its owner)."""
    try:
        from beanie import PydanticObjectId

        from pocketpaw_ee.cloud.models.agent import Agent

        doc = await Agent.get(PydanticObjectId(agent_id))
    except Exception:
        return False
    if doc is None or doc.disabled or str(doc.workspace) != workspace_id:
        return False
    return doc.visibility != "private" or str(doc.owner) == user_id


async def _resolve_agent(workspace_id: str, user_id: str, agent_id: str | None) -> str | None:
    if agent_id:
        return agent_id if await _agent_visible(agent_id, workspace_id, user_id) else None
    from pocketpaw_ee.cloud.chat import agent_service

    return await agent_service._get_default_workspace_agent_id(workspace_id)


def deployment_default_model() -> str:
    """``pydantic_ai_model`` as the gateway names it (no ``litellm:`` prefix)."""
    from pocketpaw.agents.pydantic_ai import _KNOWN_PROVIDERS  # type: ignore[import-untyped]

    model = str(get_settings().pydantic_ai_model or "").strip()
    head, sep, tail = model.partition(":")
    return tail if sep and head.lower() in _KNOWN_PROVIDERS else model


async def _gateway_fallback(
    *, system: str, prompt: str, timeout_s: float, agent_id: str, backend: str
) -> SideCompletion:
    from pocketpaw_ee.cloud.studio import prompt_soften

    model = deployment_default_model()
    meta = {"agent_id": agent_id, "backend": backend, "model": model, "fallback": True}
    try:
        text = await asyncio.wait_for(
            prompt_soften._chat_completion(model=model, system=system, user=prompt),
            timeout=timeout_s,
        )
    except TimeoutError:
        return _fail("timeout", "side.timeout", **meta)
    if not text:
        return _fail("error", "side.empty", **meta)
    return SideCompletion(text=text, **meta)


async def agent_complete(
    *,
    workspace_id: str,
    user_id: str,
    system: str,
    prompt: str,
    agent_id: str | None = None,
    model: str | None = None,
    timeout_s: float | Callable[[str], float],
) -> SideCompletion:
    """One tool-less completion on the workspace agent's own backend and model.

    ``timeout_s`` bounds the model call; pass a callable to size it by the
    backend that will serve the call (it gets the backend name). Never raises.
    """
    started = time.monotonic()
    try:
        result = await _agent_complete(
            workspace_id=workspace_id,
            user_id=user_id,
            system=system,
            prompt=prompt,
            agent_id=agent_id,
            model=model,
            timeout_s=timeout_s,
        )
    except Exception:
        logger.warning("side completion: unexpected failure", exc_info=True)
        result = _fail("error", "side.unexpected", agent_id=agent_id, model=model)
    logger.info(
        "side completion: agent=%s backend=%s model=%s fallback=%s failure=%s code=%s "
        "latency=%.2fs usage=%s",
        result.agent_id,
        result.backend,
        result.model,
        result.fallback,
        result.failure,
        result.code,
        time.monotonic() - started,
        result.usage,
    )
    return result


async def _agent_complete(
    *,
    workspace_id: str,
    user_id: str,
    system: str,
    prompt: str,
    agent_id: str | None,
    model: str | None,
    timeout_s: float | Callable[[str], float],
) -> SideCompletion:
    from pocketpaw.agents.errors import AgentDisabled  # type: ignore[import-untyped]
    from pocketpaw_ee.cloud.byok import service as byok_service
    from pocketpaw_ee.cloud.chat.runs import run_core
    from pocketpaw_ee.cloud.credits import guards

    target = await _resolve_agent(workspace_id, user_id, agent_id)
    if not target:
        return _fail("no_agent", "agent.not_found", model=model)
    meta: dict[str, Any] = {"agent_id": target, "model": model}

    # Who pays, then the credit gate and the free-model check: the order
    # ``agent_router._run_start_gates`` runs them in. ``one_active_run`` stays
    # off: a one-request side call does not compete with the chat run it helps.
    try:
        creds = await guards.resolve_payer(workspace_id)
    except byok_service.GatewayEgressRejected:
        return _fail("error", "byok.base_url_rejected", **meta)
    source = await guards.billing_source(creds, target)
    try:
        await guards.assert_within_billing(workspace_id, source)
    except CloudError as exc:
        return _fail("billing", exc.code, **meta)
    refusal = await guards.free_model_refusal(workspace_id, source, target, model)
    if refusal is not None:
        return _fail("billing", refusal.code, **meta)

    if model and source != "byok" and await run_core._model_is_unknown_to_gateway(model):
        return _fail("model", "model.not_available", **meta)

    pool = agent_pool.get_agent_pool()
    try:
        instance = await pool.get(target)
    except AgentDisabled:
        return _fail("no_agent", "agent.unavailable", **meta)
    config = instance.config if isinstance(getattr(instance, "config", None), dict) else {}
    backend = str(config.get("backend") or DEFAULT_BACKEND)
    agent_model = str(config.get("model") or "")
    meta.update(backend=backend, model=model or agent_model or None)

    run_kwargs: dict[str, Any] = {"system_message_override": system, "tools_enabled": False}
    if model:
        run_kwargs["model_override"] = model
    if creds.source == "byok" and creds.api_key:
        turn_model = model or agent_model
        if not byok_service.provider_allows_model(creds.provider, turn_model):
            return _fail("model", "byok.model_provider_mismatch", **meta)
        run_kwargs["byok_api_key"] = creds.api_key
        override = byok_service.build_settings_override(creds)
        if override:
            run_kwargs["byok_settings_override"] = override
    else:
        from pocketpaw_ee.cloud.auth import guest_budget

        try:
            guest = await guest_budget.load_guest(user_id)
        except Exception:
            guest = True  # fail closed, like run_core
        if guest is not None:
            return _fail("billing", "guest_key_required", **meta)

    bounds = await guards.free_run_bounds(workspace_id, source)
    if bounds is not None:
        # run_core's free run: pydantic_ai whatever the agent says, bounded.
        run_kwargs.pop("byok_api_key", None)
        run_kwargs.pop("byok_settings_override", None)
        run_kwargs["force_pydantic_ai"] = True
        forced_model = run_core._forced_pydantic_ai_model(instance, backend, model)
        limit = guards.free_run_token_limit(bounds.balance_micro, forced_model)
        if limit is not None:
            run_kwargs["total_tokens_limit"] = limit
        meta.update(backend=PYDANTIC_AI, model=forced_model)
    elif not _accepts_tools_enabled_kwarg(instance.backend.run):
        # This backend would keep its tools. Only a platform-billed call may
        # fall back to our gateway; a call gated as the workspace's own key
        # must not quietly spend ours.
        if source == "byok":
            return _fail("error", "side.backend_unsupported", **meta)
        budget = timeout_s(backend) if callable(timeout_s) else timeout_s
        return await _gateway_fallback(
            system=system, prompt=prompt, timeout_s=budget, agent_id=target, backend=backend
        )
    if meta["backend"] == PYDANTIC_AI:
        run_kwargs["max_requests"] = 1

    budget = timeout_s(meta["backend"]) if callable(timeout_s) else timeout_s
    if budget <= 0:
        return _fail("timeout", "side.timeout", **meta)
    chunks: list[str] = []
    usage: dict[str, Any] | None = None

    async def _collect() -> str | None:
        nonlocal usage
        async with aclosing(
            pool.run(target, prompt, f"side:{uuid.uuid4().hex}", **run_kwargs)
        ) as events:
            async for event in events:
                etype = getattr(event, "type", None)
                content = getattr(event, "content", "")
                if etype == "message" and isinstance(content, str):
                    chunks.append(content)
                elif etype == "token_usage":
                    usage = getattr(event, "metadata", None) or None
                elif etype == "error":
                    return str(content or "backend error")[:200]
                elif etype == "done":
                    break
        return None

    try:
        with baggage(**{"paw.internal": "true", "paw.workspace_id": workspace_id}):
            error = await asyncio.wait_for(_collect(), timeout=budget)
    except TimeoutError:
        return _fail("timeout", "side.timeout", usage=usage, **meta)
    if error is not None:
        logger.info("side completion: backend error from %s: %s", meta["backend"], error)
        return _fail("error", "agent.backend_error", usage=usage, **meta)
    text = "".join(chunks).strip()
    if not text:
        return _fail("error", "side.empty", usage=usage, **meta)
    return SideCompletion(text=text, usage=usage, **meta)


__all__ = [
    "SideCompletion",
    "SideFailure",
    "agent_complete",
    "deployment_default_model",
]
