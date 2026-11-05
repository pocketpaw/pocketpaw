# ee/pocketpaw_ee/cloud/chat/side_completion.py — one short, tool-less completion
# on a workspace agent's OWN backend and model, outside any chat run.
#
# For features that need a single model answer (the Sites composer's Enhance
# button, a site's AI API) and must run where a chat turn would: the agent's
# backend (Claude SDK, pydantic_ai, ...), the per-send model the user picked, the
# workspace's own key when it has one, and the same run-start gates a chat turn
# passes. ``agent_complete`` returns the whole answer; ``agent_stream`` yields its
# text as it arrives and ends with the same ``SideCompletion``. Both share one
# ``_prepare`` (resolution + gates) and one ``_events`` (the backend run).
#
# ``_prepare`` resolves the agent (a hint must belong to the workspace and be
# visible to the user, else the workspace's default ``pocketpaw`` agent), then
# runs the gates in the chat-turn order: who pays (``guards.resolve_payer`` +
# ``billing_source``), the credit gate (``assert_within_billing``), the free-model
# check (``free_model_refusal``); then the catalog check of a per-send model on a
# platform turn, the BYOK provider/model check, and the no-keyless-guest rule.
# Then ONE ``pool.run`` with tools off, a fresh ``side:<uuid>`` session key and
# the caller's system prompt, under ``baggage(paw.internal=true)`` and a timeout.
#
# Invariants:
#   * ``isolated=True`` (a caller that is not the agent's user, e.g. a site's AI
#     API) runs ``pool.run(isolated=True)``: no soul recall, none of the agent's
#     skills, no backend extras, so the system prompt is exactly ``system``.
#     Enhance and the other in-app callers keep the agent's full context.
#   * ``images`` ride the chat turn's ``image_attachments`` channel to the
#     backend; a backend that doesn't declare it (or the gateway fallback) is a
#     typed ``unsupported`` failure, never a silent drop. No images = the exact
#     kwargs a caller got before.
#   * Never raises: every refusal or error is a typed ``failure`` on the result.
#     ``sent`` says whether a model was called (a gate refusal never is) and
#     ``payer`` who pays (``platform`` / ``byok``) once the gates knew.
#   * Never runs a backend with tools on. A backend whose ``run`` does not declare
#     ``tools_enabled`` falls back to the deployment default model
#     (``pydantic_ai_model``) through the gateway helper, and only on a
#     platform-billed call. Never the first catalog entry.
#   * ``max_requests`` only reaches pydantic_ai (other backends refuse it).
#   * A platform-funded free run is forced onto pydantic_ai with one request and
#     the wallet's token limit, like ``run_core``. It does NOT reserve on the
#     daily early-access ceiling: that hold is reconciled only by ``bill_run``
#     from a ChatRunDoc, which a side call has none of.
#   * Usage is not billed here (no ChatRunDoc for ``bill_run``); it is returned
#     and logged. Callers bill it themselves and keep their own rate limit.
#   * The backend generator is always closed (``aclosing``).

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import aclosing
from dataclasses import dataclass
from typing import Any, Literal

from pocketpaw.agents import pool as agent_pool  # type: ignore[import-untyped]
from pocketpaw.agents.backend import (  # type: ignore[import-untyped]
    ImageAttachment,
    _accepts_image_attachments_kwarg,
    _accepts_tools_enabled_kwarg,
)
from pocketpaw.config import get_settings  # type: ignore[import-untyped]
from pocketpaw.observability import baggage  # type: ignore[import-untyped]
from pocketpaw_ee.cloud._core.errors import CloudError

logger = logging.getLogger(__name__)

# Why a side completion produced no text. ``billing`` covers every credit, quota,
# early-access and keyless-guest refusal; ``unsupported`` images for a backend
# that can't take them; ``model`` an unknown model or one the
# workspace's own key cannot run.
SideFailure = Literal["no_agent", "billing", "model", "unsupported", "timeout", "error"]

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
    # Who pays once the gates knew: ``platform`` or ``byok``.
    payer: Literal["platform", "byok"] | None = None
    # True once a model was actually called (a gate refusal never is).
    sent: bool = False

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


@dataclass(frozen=True)
class SideTarget:
    """What a side completion on an agent hint would run on, resolved ahead of the
    call: the agent, who pays, and the agent's own model ("" = backend default)."""

    agent_id: str
    payer: Literal["platform", "byok"]
    model: str = ""  # the agent's model, else its backend's default ("" = unknown)


async def resolve_target(
    workspace_id: str, user_id: str, agent_id: str | None
) -> SideTarget | None:
    """The agent ``agent_complete`` would run for ``agent_id`` (None = the default
    agent) and who it would bill, by the same resolution; None when no agent
    resolves. For callers that hold a budget before the call."""
    from beanie import PydanticObjectId

    from pocketpaw_ee.cloud.credits import guards
    from pocketpaw_ee.cloud.models.agent import Agent

    target = await _resolve_agent(workspace_id, user_id, agent_id)
    if not target:
        return None
    creds = await guards.resolve_payer(workspace_id)
    source = await guards.billing_source(creds, target)
    model = ""
    try:
        doc = await Agent.get(PydanticObjectId(target))
        if doc is not None:
            model = str(doc.config.model or "") or default_model_for(str(doc.config.backend))
    except Exception:  # noqa: BLE001 - the model is only a pricing hint
        model = ""
    return SideTarget(
        agent_id=target, payer="byok" if source == "byok" else "platform", model=model
    )


def default_model_for(backend: str) -> str:
    """The model ``backend`` runs when an agent names none ("" when unknown):
    ``pydantic_ai_model`` for pydantic_ai, else the backend's own model setting
    as the LLM client resolves it."""
    if backend == PYDANTIC_AI:
        return deployment_default_model()
    try:
        from pocketpaw.llm.client import resolve_llm_client  # type: ignore[import-untyped]

        return str(resolve_llm_client(get_settings(), backend=backend).model or "")
    except Exception:  # noqa: BLE001 - only a pricing hint
        return ""


def deployment_default_model() -> str:
    """``pydantic_ai_model`` as the gateway names it (no ``litellm:`` prefix)."""
    from pocketpaw.agents.pydantic_ai import _KNOWN_PROVIDERS  # type: ignore[import-untyped]

    model = str(get_settings().pydantic_ai_model or "").strip()
    head, sep, tail = model.partition(":")
    return tail if sep and head.lower() in _KNOWN_PROVIDERS else model


@dataclass
class _Plan:
    """What ``_prepare`` decided: the agent, the run kwargs and the time budget.
    ``gateway`` means the deployment default model answers through the gateway
    helper instead of the agent's backend."""

    target: str
    workspace_id: str
    system: str
    prompt: str
    meta: dict[str, Any]
    run_kwargs: dict[str, Any]
    budget: float
    gateway: bool = False
    sent: bool = False


def _result(plan: _Plan, **kw: Any) -> SideCompletion:
    return SideCompletion(sent=plan.sent, **plan.meta, **kw)


def _fail_plan(plan: _Plan, failure: SideFailure, code: str, **kw: Any) -> SideCompletion:
    return _result(plan, failure=failure, code=code, **kw)


async def _prepare(
    *,
    workspace_id: str,
    user_id: str,
    system: str,
    prompt: str,
    agent_id: str | None,
    model: str | None,
    timeout_s: float | Callable[[str], float],
    images: Sequence[ImageAttachment] = (),
    isolated: bool = False,
) -> _Plan | SideCompletion:
    """Resolve the agent and run every gate; a ``SideCompletion`` is a refusal."""
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
    meta["payer"] = "byok" if source == "byok" else "platform"
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
    if isolated:
        run_kwargs["isolated"] = True
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

    gateway = False
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
        gateway = True
        meta.update(model=deployment_default_model(), fallback=True)
    if meta["backend"] == PYDANTIC_AI and not gateway:
        run_kwargs["max_requests"] = 1
    if images:
        # The chat turn's channel for attached pictures. A backend opts in by
        # declaring it (the forced free run is pydantic_ai, which does); the pool
        # would drop them silently for any other, so refuse instead.
        runs_images = bounds is not None or (
            not gateway and _accepts_image_attachments_kwarg(instance.backend.run)
        )
        if not runs_images:
            return _fail("unsupported", "side.images_unsupported", **meta)
        run_kwargs["image_attachments"] = tuple(images)

    budget = timeout_s(meta["backend"]) if callable(timeout_s) else timeout_s
    if budget <= 0:
        return _fail("timeout", "side.timeout", **meta)
    return _Plan(
        target=target,
        workspace_id=workspace_id,
        system=system,
        prompt=prompt,
        meta=meta,
        run_kwargs=run_kwargs,
        budget=budget,
        gateway=gateway,
    )


class _Started:
    """``agent_stream``'s marker: the model call has begun (``sent`` from here)."""

    def __repr__(self) -> str:
        return "STARTED"


STARTED = _Started()


async def _events(plan: _Plan) -> AsyncIterator[tuple[str, Any]]:
    """The run as ``("started", None)``, then ``("text", str)`` / ``("usage",
    dict)`` / ``("error", str)``. Marks the plan ``sent`` before the model is
    called."""
    plan.sent = True
    yield "started", None
    if plan.gateway:
        from pocketpaw_ee.cloud.studio import prompt_soften

        text = await prompt_soften._chat_completion(
            model=plan.meta["model"], system=plan.system, user=plan.prompt
        )
        if text:
            yield "text", text
        return
    pool = agent_pool.get_agent_pool()
    with baggage(**{"paw.internal": "true", "paw.workspace_id": plan.workspace_id}):
        async with aclosing(
            pool.run(plan.target, plan.prompt, f"side:{uuid.uuid4().hex}", **plan.run_kwargs)
        ) as events:
            async for event in events:
                etype = getattr(event, "type", None)
                content = getattr(event, "content", "")
                if etype == "message" and isinstance(content, str):
                    yield "text", content
                elif etype == "token_usage":
                    yield "usage", getattr(event, "metadata", None) or None
                elif etype == "error":
                    yield "error", str(content or "backend error")[:200]
                    return
                elif etype == "done":
                    return


def _finish(
    plan: _Plan, chunks: list[str], usage: dict[str, Any] | None, error: str | None
) -> SideCompletion:
    if error is not None:
        logger.info("side completion: backend error from %s: %s", plan.meta["backend"], error)
        return _fail_plan(plan, "error", "agent.backend_error", usage=usage)
    text = "".join(chunks).strip()
    if not text:
        return _fail_plan(plan, "error", "side.empty", usage=usage)
    return _result(plan, text=text, usage=usage)


def _log(result: SideCompletion, started: float) -> None:
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


async def agent_complete(
    *,
    workspace_id: str,
    user_id: str,
    system: str,
    prompt: str,
    agent_id: str | None = None,
    model: str | None = None,
    timeout_s: float | Callable[[str], float],
    images: Sequence[ImageAttachment] = (),
    isolated: bool = False,
) -> SideCompletion:
    """One tool-less completion on the workspace agent's own backend and model.

    ``timeout_s`` bounds the model call; pass a callable to size it by the
    backend that will serve the call (it gets the backend name). Never raises.
    """
    started = time.monotonic()
    plan: _Plan | None = None
    try:
        prepared = await _prepare(
            workspace_id=workspace_id,
            user_id=user_id,
            system=system,
            prompt=prompt,
            agent_id=agent_id,
            model=model,
            timeout_s=timeout_s,
            images=images,
            isolated=isolated,
        )
        if isinstance(prepared, SideCompletion):
            result = prepared
        else:
            plan = prepared
            result = await _complete_plan(plan)
    except Exception:
        logger.warning("side completion: unexpected failure", exc_info=True)
        result = (
            _fail_plan(plan, "error", "side.unexpected")
            if plan is not None
            else _fail("error", "side.unexpected", agent_id=agent_id, model=model)
        )
    _log(result, started)
    return result


async def _complete_plan(plan: _Plan) -> SideCompletion:
    chunks: list[str] = []
    usage: dict[str, Any] | None = None

    async def _collect() -> str | None:
        nonlocal usage
        async with aclosing(_events(plan)) as events:
            async for kind, value in events:
                if kind == "text":
                    chunks.append(value)
                elif kind == "usage":
                    usage = value
                elif kind == "error":
                    return value
        return None

    try:
        error = await asyncio.wait_for(_collect(), timeout=plan.budget)
    except TimeoutError:
        return _fail_plan(plan, "timeout", "side.timeout", usage=usage)
    return _finish(plan, chunks, usage, error)


async def agent_stream(
    *,
    workspace_id: str,
    user_id: str,
    system: str,
    prompt: str,
    agent_id: str | None = None,
    model: str | None = None,
    timeout_s: float | Callable[[str], float],
    images: Sequence[ImageAttachment] = (),
    isolated: bool = False,
) -> AsyncIterator[str | SideCompletion | _Started]:
    """``agent_complete``, streamed: yields ``STARTED`` once the model call has
    begun, each text delta as a ``str``, and ends with ONE ``SideCompletion``
    (what ``agent_complete`` would return, with the whole text). A gate refusal
    yields only that result. Never raises; ``timeout_s`` bounds the whole run,
    as there."""
    started = time.monotonic()
    plan: _Plan | None = None
    chunks: list[str] = []
    usage: dict[str, Any] | None = None
    try:
        prepared = await _prepare(
            workspace_id=workspace_id,
            user_id=user_id,
            system=system,
            prompt=prompt,
            agent_id=agent_id,
            model=model,
            timeout_s=timeout_s,
            images=images,
            isolated=isolated,
        )
        if isinstance(prepared, SideCompletion):
            result = prepared
        else:
            plan = prepared
            deadline = time.monotonic() + plan.budget
            error: str | None = None
            async with aclosing(_events(plan)) as events:
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError
                    try:
                        kind, value = await asyncio.wait_for(anext(events), timeout=remaining)
                    except StopAsyncIteration:
                        break
                    if kind == "started":
                        yield STARTED
                    elif kind == "text":
                        chunks.append(value)
                        yield value
                    elif kind == "usage":
                        usage = value
                    else:
                        error = value
                        break
            result = _finish(plan, chunks, usage, error)
    except TimeoutError:
        assert plan is not None  # only the run itself has a deadline
        result = _fail_plan(plan, "timeout", "side.timeout", usage=usage)
    except Exception:
        logger.warning("side completion: unexpected stream failure", exc_info=True)
        result = (
            _fail_plan(plan, "error", "side.unexpected", usage=usage)
            if plan is not None
            else _fail("error", "side.unexpected", agent_id=agent_id, model=model)
        )
    _log(result, started)
    yield result


__all__ = [
    "SideCompletion",
    "SideFailure",
    "STARTED",
    "SideTarget",
    "agent_complete",
    "agent_stream",
    "default_model_for",
    "deployment_default_model",
    "resolve_target",
]
