"""Enterprise agent chat: ``POST /cloud/chat/{scope}/{scope_id}/agent``.

Streams a typed SSE sequence in the response body while persisting the user
message, submitting a ``Run`` to the configured executor, and tailing the run's
Redis Stream, so durability sits underneath the wire shape the frontend speaks.

This is the single chokepoint that starts a chat run. ``scope_workspace_id``
comes from ``resolve_scope_context``, which is handed the authenticated
``workspace_id`` as ``expected_workspace_id`` (a scope doc whose workspace
disagrees is rejected). Then ``_run_start_gates`` runs the read-only run-start
checks BEFORE any DB write, so a refused turn leaves no run and no message:

* who pays (``credits.guards.resolve_payer`` and ``billing_source``), then
  the credit gate (``assert_within_billing``: balance, then monthly quota, 402
  ``credits.insufficient`` / ``credits.quota_exceeded``, and 429
  ``credits.run_in_progress`` for an unpaid early-access workspace that already
  has a chat run active). A no-op unless
  ``billing_enforced`` or ``credits_enforced``, and skipped for a turn on the
  workspace's own key when the serving backend spends it. The surface is
  resolved before these gates, because a deny-by-default surface changes
  which backend serves the turn.
* the guest turn gate (check-only; the executor owns the atomic spend).
* the workspace daily turn ceiling (check-only, fails open).

The executor (``run_core.execute_run``) repeats these on every path into it
(HTTP, WebSocket, queued, resumed) and is the enforcement; this route answers a
clean pre-stream error. In-flight runs are never affected.

On the sites edit/refine surface only, ``_window_history_for_surface`` caps the
history fed to the agent to the last ``SITES_REFINE_HISTORY_TURNS`` turns; every
other surface gets the full history.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncIterator
from typing import Any, Literal

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse

from pocketpaw_ee.cloud._core.errors import CloudError
from pocketpaw_ee.cloud.chat.agent_schemas import CloudAgentChatRequest
from pocketpaw_ee.cloud.chat.agent_service import (
    InvalidScope,
    ScopeContext,
    load_history_for_scope,
    resolve_scope_context,
    session_key_for,
)
from pocketpaw_ee.cloud.chat.runs import service as run_service
from pocketpaw_ee.cloud.chat.runs.domain import RunSpec
from pocketpaw_ee.cloud.chat.runs.executor import get_executor
from pocketpaw_ee.cloud.chat.runs.transport import get_stream_transport
from pocketpaw_ee.cloud.license import require_license
from pocketpaw_ee.cloud.shared.deps import current_user_id, current_workspace_id
from pocketpaw_ee.cloud.surface import (
    SurfaceContext,
    SurfaceKind,
    resolve_profile,
    resolve_surface_context,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Cloud Agent Chat"], dependencies=[Depends(require_license)])


Scope = Literal["dm", "group", "pocket", "session"]

# PERF-6: how many trailing TURNS of history to keep on the sites EDIT/REFINE
# surface. A "turn" is one user request plus its assistant reply (2 stored
# ``Message`` rows), so the window keeps the last
# ``2 * SITES_REFINE_HISTORY_TURNS`` rows — enough to resolve a pronoun referent
# ("make IT bigger", "now the SAME for the footer") without re-feeding the whole
# accumulating edit log to the LLM every refine. Kept as a module constant so
# the window size is tunable in one place.
SITES_REFINE_HISTORY_TURNS = 2
_SITES_REFINE_HISTORY_ROWS = 2 * SITES_REFINE_HISTORY_TURNS


async def _assert_under_daily_turns(workspace_id: str) -> None:
    """Raise ``DailyTurnLimitError`` when the workspace is at today's turn cap.

    READ-ONLY and fails OPEN: the executor's ``metering.try_spend`` owns the one
    atomic spend and is the gate that has to be right; this only saves a capped
    account a run doc, a Redis stream and a TTL. A database blip costs latency,
    not correctness. Incrementing here would make a turn cost two.
    """
    from pocketpaw_ee.cloud.metering import service as metering
    from pocketpaw_ee.cloud.metering.domain import DailyMeter

    cap = metering.workspace_turns_cap()
    if cap is None or not workspace_id:
        return
    try:
        spent = await metering.used(
            subject_type="workspace", subject_id=workspace_id, meter=DailyMeter.WORKSPACE_TURNS
        )
    except Exception:
        logger.debug("turn budget pre-check unavailable for %s", workspace_id, exc_info=True)
        return
    if spent >= cap:
        from pocketpaw_ee.cloud._core.errors import DailyTurnLimitError

        raise DailyTurnLimitError(cap)


async def _run_start_gates(
    workspace_id: str,
    user_id: str,
    scope_workspace_id: str,
    *,
    agent_id: str | None = None,
    exclusive_tools: bool = False,
    model_override: str | None = None,
) -> None:
    """The read-only run-start checks, run concurrently, first failure wins.

    Precedence is the declared order, the order these ran in when they were
    sequential awaits: credit balance, monthly quota, guest turn gate, daily
    turn ceiling. Every check runs to completion (they only read), then the
    earliest one that raised is re-raised, so a turn failing several gets the
    same error as before. None of them writes, so running a later check after
    an earlier one failed has no side effect.

    * Balance + quota (BC-4 / chunk 3): ``credits.guards.assert_within_billing``,
      a no-op unless ``billing_enforced`` or ``credits_enforced``; balance
      before quota inside it. When neither flag is on the leg returns before
      resolving anything. Otherwise who pays is resolved first
      (``credits.guards.resolve_payer`` then ``billing_source``): a turn on the
      workspace's own key skips the wallet only when ``agent_id``'s backend
      (or a deny-by-default ``exclusive_tools`` run) spends that key, a
      resolver failure is gated as platform, and a gateway address the egress
      guard rejects raises its 422. An unpaid early-access workspace with a
      chat run already active gets 429 ``credits.run_in_progress``, and a
      model it may not use on early access credits (per-send, else the
      agent's own, plus the deployment default) gets 400 ``model.not_allowed``.
      The executor checks the same list again and is the enforcement.
    * Guest turn gate (feat/byok-guest-backend): the frozen
      ``guest_limit_reached`` / ``guest_key_required`` 402 the signup prompt
      keys on. Does NOT increment; ``run_core._reject_if_guest_over_limit``
      owns the single atomic spend. One indexed read for non-guests.
    * Workspace daily turn ceiling (feat/abuse-budgets): without it a capped
      account still creates a run doc and opens a stream on every send. Does
      NOT increment and fails OPEN; the executor's gate is the one that has to
      be right.
    """
    from pocketpaw_ee.cloud.auth.guest_gates import assert_guest_turn_allowed
    from pocketpaw_ee.cloud.credits import guards

    async def _billing() -> None:
        # Advisory here: the executor resolves again on its own ctx and that
        # answer is the one that gates and bills the run. Billing off means
        # nothing to gate, so skip the key read, decrypt and egress check.
        settings = guards.get_settings()
        if not (settings.billing_enforced or settings.credits_enforced):
            return
        payer = await guards.resolve_payer(workspace_id)
        source = await guards.billing_source(payer, agent_id, exclusive_tools=exclusive_tools)
        await guards.assert_within_billing(workspace_id, source, one_active_run=True)
        refusal = await guards.free_model_refusal(workspace_id, source, agent_id, model_override)
        if refusal is not None:
            raise refusal

    checks = [
        # Flag-gated no-op unless billing_enforced or credits_enforced;
        # balance then quota inside, skipped for a BYOK turn.
        _billing(),
        assert_guest_turn_allowed(user_id, scope_workspace_id),
        _assert_under_daily_turns(scope_workspace_id),
    ]

    for outcome in await asyncio.gather(*checks, return_exceptions=True):
        if isinstance(outcome, BaseException):
            raise outcome


def _surface_is_exclusive(surface_context: SurfaceContext | None) -> bool:
    """Whether the surface's base profile is deny-by-default (``exclusive_tools``).

    The base profile only: a pocket-entity override that turns exclusivity on is
    folded in by the executor, not here, so for that rare turn the route can
    gate a stored key as platform where the executor would not. A lookup error
    answers True, the lenient side, since the executor gates again.
    """
    if surface_context is None:
        return False
    try:
        return bool(resolve_profile(surface_context.kind, surface_context.meta).exclusive_tools)
    except Exception:
        logger.debug("surface profile lookup failed; treating as exclusive", exc_info=True)
        return True


async def _assert_own_key_if_kiosk_requires_it(ctx: Any) -> None:
    """Raise ``ByokKeyRequired`` when the kiosk needs a key this account lacks.

    A CHECK, never a spend, and a convenience rather than a control: the
    surface it reads was resolved from a client-supplied hint, so omitting the
    hint skips it. ``run_core._requires_own_key`` is the gate that decides,
    from the server-side context, and it runs on every path into the executor.

    Guests are left alone here: ``assert_guest_turn_allowed`` above already
    refused a keyless guest with their own code, and answering them twice with
    two different codes would make the prompt flicker between "create an
    account" and "add a key".

    Shares ``kiosk_byok.requires_own_key`` with the executor as of 2026-09-13,
    when the rule grew a plan term. Two copies were harmless while the rule was
    flag-plus-surface; the moment it could differ, the seam that answers FIRST
    would have refused a paying member the other was about to admit.
    """
    from pocketpaw_ee.cloud.chat import kiosk_byok

    if not await kiosk_byok.requires_own_key(ctx):
        return

    from pocketpaw_ee.cloud.auth import guest_budget

    if await guest_budget.load_guest(ctx.user_id) is not None:
        return

    from pocketpaw_ee.cloud.byok import service as byok_service

    status = await byok_service.get_status(ctx.workspace_id)
    if not getattr(status, "configured", False):
        from pocketpaw_ee.cloud._core.errors import ByokKeyRequired

        raise ByokKeyRequired()


def _is_sites_refine_surface(surface_context: SurfaceContext | None) -> bool:
    """True only for the /sites EDIT/REFINE chat surface.

    The refine surface is identified the SAME way the sites preamble
    (``surface/handlers/sites.py``) and the profile resolver
    (``surface/service.resolve_profile``) identify it: ``kind == SITES`` AND the
    surface meta carries a ``pocket_id`` (the source pocket being edited). The
    /sites gallery CREATE surface is ``kind == SITES`` with NO ``pocket_id``, and
    a general pocket chat is ``kind == POCKET`` — both return ``False`` so the
    windowing never touches general chat. A ``None`` surface context (legacy
    client that sent no surface hint) is never refine.
    """
    if surface_context is None:
        return False
    return surface_context.kind == SurfaceKind.SITES and bool(
        getattr(surface_context.meta, "pocket_id", None)
    )


def _window_history_for_surface(
    history: list[dict[str, str]], surface_context: SurfaceContext | None
) -> list[dict[str, str]]:
    """Cap ``history`` to the last ``SITES_REFINE_HISTORY_TURNS`` turns on the
    sites-refine surface; return it unchanged everywhere else.

    This is the PERF-6 fix: on the sites builder each refine fed the FULL
    growing scope history to the agent, so the LLM re-processed ever-more context
    per edit. For the refine surface we keep only the trailing turns (enough for
    pronoun referents); for EVERY other surface — general pocket/dm/group chat,
    the sites-create gallery, legacy no-surface clients — the history passes
    through untouched, so general chat behavior is byte-identical.
    """
    if not _is_sites_refine_surface(surface_context):
        return history
    if len(history) <= _SITES_REFINE_HISTORY_ROWS:
        return history
    return history[-_SITES_REFINE_HISTORY_ROWS:]


def _sse(event: str, data: dict[str, Any], *, entry_id: str | None = None) -> bytes:
    # ``id:`` powers EventSource Last-Event-Id resume; synthetic frames omit it.
    head = f"id: {entry_id}\n" if entry_id else ""
    return f"{head}event: {event}\ndata: {json.dumps(data)}\n\n".encode()


@router.post("/cloud/chat/{scope}/{scope_id}/agent")
async def post_agent_chat(
    scope: Scope,
    scope_id: str,
    body: CloudAgentChatRequest,
    user_id: str = Depends(current_user_id),
    workspace_id: str = Depends(current_workspace_id),
) -> StreamingResponse:
    try:
        # Thread the authenticated workspace (fix/worker-trusts-spec-workspace).
        # The HTTP route already validated it via ``current_workspace_id``; the
        # resolver uses it the same way the worker does — fall back when the
        # scope doc's ``workspace`` is empty, reject a scope whose non-empty doc
        # workspace disagrees with the caller's (cross-tenant guard). Keeps the
        # synchronous path consistent with the run worker.
        ctx = await resolve_scope_context(
            scope=scope,
            scope_id=scope_id,
            user_id=user_id,
            agent_id_hint=body.agent_id,
            expected_workspace_id=workspace_id,
            # CX-3: on /code the resolver routes an unhinted turn to the code
            # agent (exclusive file-tool policy). No-op on every other surface.
            surface=body.surface,
        )
        ctx.intent = body.intent
    except InvalidScope:
        raise CloudError(400, "scope.invalid", "Invalid scope") from None

    # The run-start checks, at the SINGLE chokepoint — BEFORE any DB write (no
    # user message, no ChatRunDoc) and BEFORE the executor submit — so a blocked
    # run leaves no trace and IN-FLIGHT runs are never killed. Each raises a
    # ``CloudError`` the handler maps to the wire; never HTTPException.
    #
    #   * Billing: ``credits.guards.assert_within_billing``, the same gate
    #     ``run_core.execute_run`` uses. Flag-gated (``POCKETPAW_BILLING_ENFORCED``,
    #     off for OSS / self-host). Inside it, ``check_balance`` (empty wallet,
    #     402 credits.insufficient) runs before ``check_quota`` (monthly
    #     ceiling, 402 credits.quota_exceeded), so the synchronous HTTP path
    #     answers 402 instead of starting a run the executor would reject.
    #   * The guest turn gate and the daily turn ceiling (read-only; the
    #     executor owns the spend).
    #
    # They are independent, so ``_run_start_gates`` runs them concurrently and
    # raises the FIRST failure in that declared order. A turn failing several
    # checks gets the same error it always did.
    #
    # The surface is resolved first, because a deny-by-default surface (the
    # concierge) is served by pydantic_ai and so spends a stored key whatever
    # the bound agent's backend is; the billing leg needs that answer.
    #
    # Surface: resolved AFTER scope (so ``workspace_id`` / ``user_id`` are
    # confirmed) and BEFORE any prompt assembly. The resolver never raises:
    # failures fall back to a GENERIC context with an empty preamble, which
    # ``build_dynamic_context`` treats as the legacy three-line shape. Older
    # clients that send neither ``surface`` nor ``surface_meta`` land here as
    # ``{surface: None, meta: {}}`` and get a GENERIC context.
    ctx.surface_context = await resolve_surface_context(
        ctx.workspace_id,
        user_id,
        {"surface": body.surface, "meta": body.surface_meta or {}},
    )
    await _run_start_gates(
        workspace_id,
        user_id,
        ctx.workspace_id,
        agent_id=ctx.target_agent_id,
        exclusive_tools=_surface_is_exclusive(ctx.surface_context),
        model_override=body.model,
    )

    transport = get_stream_transport()

    # Kiosk BYOK fast-reject (2026-09-12). UX ONLY — see the docstring.
    #
    # The browser gets a clean pre-stream 402 with the code the key prompt
    # keys on, instead of a run doc whose first frame is an error. But the
    # surface here comes from ``body.surface``, which the CLIENT sends, so a
    # caller that simply omits it walks straight past this. The enforcement
    # lives in the executor (``run_core._requires_own_key``), which reads the
    # server-resolved context. Never move the gate here and delete that one.
    await _assert_own_key_if_kiosk_requires_it(ctx)

    # Supersede any prior in-flight run for this scope. ``request_cancel``
    # writes the cancel flag in Redis so a worker in another process notices.
    prior = await run_service.find_active_run_for_scope(
        workspace_id=workspace_id, context_type=scope, scope_id=scope_id
    )
    if prior is not None:
        await transport.request_cancel(prior.run_id)

    # Load history BEFORE persisting the new user message so it excludes this turn.
    history = await load_history_for_scope(ctx)
    # PERF-6: on the sites EDIT/REFINE surface, window the loaded history to the
    # last few turns so the LLM stops re-processing the whole accumulating edit
    # log on every refine (the progressive-slowdown cause). No-op for general
    # chat — see ``_window_history_for_surface`` / ``_is_sites_refine_surface``.
    history = _window_history_for_surface(history, ctx.surface_context)
    user_message_id = await _persist_user_message(ctx, body)

    # Resolve the sidebar Session up-front so ``message.persisted`` carries
    # ``session_id`` — a mid-stream refresh can still find the thread.
    try:
        ctx.session_id = await _ensure_scope_session(ctx)
    except Exception:
        logger.exception("ensure session failed for scope %s", ctx.kind.value)
        ctx.session_id = None

    client_message_id = body.client_message_id or uuid.uuid4().hex
    spec = RunSpec(
        run_id=uuid.uuid4().hex,
        workspace_id=workspace_id,
        context_type=scope,
        scope_id=scope_id,
        session_key=session_key_for(ctx),
        group=scope_id if scope in ("dm", "group") else None,
        user_id=user_id,
        agent_id=ctx.target_agent_id,
        client_message_id=client_message_id,
        user_message_id=user_message_id,
        content=body.content,
        history=history,
        intent=body.intent,
        attachments=body.attachments or [],
        mentions=[],
        reply_to=body.reply_to,
        # Carry the surface hint to the executor so it can re-resolve
        # ``ctx.surface_context`` — the resolution at :77 lives on THIS
        # request's ctx and is dropped when the run is submitted.
        surface=body.surface,
        surface_meta=body.surface_meta or {},
        # CS-13 — carry the per-send model override to the executor. Validated on
        # the request body; ``None`` for every older client leaves model selection
        # to the backend.
        model_override=body.model,
        # Per-send tool switch. ``None`` for every older client, which is the
        # legacy path; only an explicit ``False`` changes anything.
        tools_enabled=body.tools,
        # Studio Flow build context — ride the spec to the executor so the agent
        # knows the ACTIVE FLOW ID and ``build_studio_flow`` persists into the
        # flow project the user is on (``None`` on every non-studio surface).
        flow_context=body.flow_context,
    )
    # create_run is idempotent on (workspace, client_message_id) — when a doc
    # already exists, re-use its run_id so the executor + SSE stream both
    # tail the same Redis Stream as the prior request for this client_message_id.
    run = await run_service.create_run(spec)
    if run.run_id != spec.run_id:
        spec = spec.model_copy(update={"run_id": run.run_id})
    run_id = run.run_id
    await get_executor().submit(spec)

    async def gen() -> AsyncIterator[bytes]:
        persisted_payload: dict[str, Any] = {
            "user_message_id": user_message_id,
            "client_message_id": client_message_id,
            "run_id": run_id,
        }
        if ctx.session_id:
            persisted_payload["session_id"] = ctx.session_id
        yield _sse("message.persisted", persisted_payload)

        cursor = "0"
        while True:
            saw_terminal = False
            # Short block timeout so cancellation is checked promptly
            # when the executor hasn't yet written a terminal event
            # (e.g. blocked on pool.get / build_knowledge_context before
            # its is_cancelled loop). If the cancel flag is set while
            # read_events is waiting, we detect it between blocks.
            async for ev in transport.read_events(run_id, after=cursor, block_ms=2000):
                cursor = ev.entry_id
                yield _sse(ev.event, ev.data, entry_id=ev.entry_id)
                if ev.is_terminal:
                    saw_terminal = True
            if saw_terminal:
                return
            # Check if the executor set the cancel flag (via /agent/stop).
            # If so, yield a terminal event so the client sees the stream
            # end, even if the executor hasn't written one yet (e.g. it
            # is blocked before its cancellation loop).
            if await transport.is_cancelled(run_id):
                yield _sse("interrupted", {"reason": "user_cancelled"})
                return
            yield b": ping\n\n"

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/cloud/chat/{scope}/{scope_id}/agent/stop")
async def post_agent_chat_stop(
    scope: Scope,
    scope_id: str,
    user_id: str = Depends(current_user_id),  # noqa: ARG001
    workspace_id: str = Depends(current_workspace_id),
) -> dict[str, Any]:
    """Cancel the active run for this scope. Idempotent — returns ``ok`` even
    when no run is in flight so the frontend's fire-and-forget stop button
    doesn't surface a 404 toast."""
    prior = await run_service.find_active_run_for_scope(
        workspace_id=workspace_id, context_type=scope, scope_id=scope_id
    )
    if prior is not None:
        await get_stream_transport().request_cancel(prior.run_id)
    return {"status": "ok"}


async def _ensure_scope_session(ctx: ScopeContext) -> str | None:
    """Find-or-create the sidebar ``Session`` for this scope+agent pair."""
    from pocketpaw_ee.cloud.sessions import service as sessions_service

    return await sessions_service.ensure_for_agent_scope(
        kind=ctx.kind.value,
        scope_id=ctx.scope_id,
        workspace_id=ctx.workspace_id,
        user_id=ctx.user_id,
        target_agent_id=ctx.target_agent_id,
    )


async def _persist_user_message(ctx: ScopeContext, body: CloudAgentChatRequest) -> str:
    # Bypasses ``send_message`` to skip the legacy ``agent_bridge`` auto-response —
    # the run executor is the sole driver of the reply here.
    from pocketpaw_ee.cloud.chat import message_service

    return await message_service.persist_user_message_for_scope(
        kind=ctx.kind.value,
        scope_id=ctx.scope_id,
        user_id=ctx.user_id,
        workspace_id=ctx.workspace_id,
        session_key=session_key_for(ctx),
        content=body.content,
        attachments=body.attachments,
        mentions=body.mentions,
        reply_to=body.reply_to,
    )
