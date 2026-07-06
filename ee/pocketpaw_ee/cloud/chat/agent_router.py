"""Enterprise agent chat — ``POST /cloud/chat/{scope}/{scope_id}/agent``.

Streams a typed SSE sequence in the response body while persisting the user
message, submitting a ``Run`` to the configured executor, and tailing the
run's Redis Stream so durability sits underneath the wire shape the
frontend already speaks.

Changes: 2026-06-30 (feat/billing-quota-enforcement, chunk 3) — the run-start
credit gate now also enforces the MONTHLY QUOTA. Beside the existing BC-4
``check_balance`` call (inside the same ``if get_settings().billing_enforced:``
block, before ``create_run``/submit), ``post_agent_chat`` now also
``await credits_service.check_quota(workspace_id)`` so a workspace that has spent
up to its monthly ceiling gets a clean 402 ``credits.quota_exceeded`` with NO DB
trace — the same universal cap the executor enforces in
``run_core.execute_run``, mirrored here for the synchronous HTTP path.
``check_balance`` is unchanged and stays FIRST (balance <= 0 is the secondary
guard). Both are no-ops when the flag is OFF.

Changes: 2026-06-25 (fix/worker-trusts-spec-workspace) — ``post_agent_chat``
threads the authenticated ``workspace_id`` into ``resolve_scope_context`` via
``expected_workspace_id``, matching the run worker. The HTTP route already
validates the workspace (the ``current_workspace_id`` dependency rejects an
empty one with 400); passing it lets the resolver fall back to it when a scope
doc's ``workspace`` field is empty and reject a scope whose non-empty doc
workspace disagrees with the caller's (cross-tenant guard). Keeps the
synchronous request path consistent with the worker that actually attaches the
run identity.

Changes: 2026-06-24 (BC-4, integration/billing-credits) — credit hard-block at
run-start. This module is the SINGLE chokepoint that starts a new chat run
(``create_run`` + executor submit), so the credit gate sits here, right after
scope resolution and BEFORE any DB write. When ``settings.billing_enforced`` is
on, a workspace at balance <= 0 gets 402 (``credits.insufficient``) and NO run /
user message is persisted; in-flight runs (already past this point) are never
affected. The flag is OFF by default so OSS / self-host (no ledger) are
unaffected. See ``credits.service.check_balance``.

Changes: 2026-06-18 (PERF-6, feat/sites-minimal-context) — window the history
fed to the agent on the SITES EDIT/REFINE surface only. The sites builder got
progressively slower per edit because ``load_history_for_scope`` loads the FULL
accumulating scope history every turn, so the LLM re-processed ever-more context
per refine. ``_window_history_for_surface`` now caps the loaded history to the
last ``SITES_REFINE_HISTORY_TURNS`` turns (kept for pronoun referents like "make
it bigger") when ``_is_sites_refine_surface`` matches (kind=sites + a refine
``pocket_id`` in the surface meta — the same discriminator the sites preamble /
profile resolver use). Every other surface (general pocket/dm/group chat, the
sites-create gallery, legacy clients with no surface hint) keeps the FULL
history — the windowing is a strict no-op off the sites-refine surface. The
``<pocket-summary>`` block already carries the pocket's current state to the
agent, so injecting the raw component source is intentionally left out of this
change (it would add a build/DB read on the shared chat path); the windowing is
the core latency win.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import AsyncIterator
from typing import Any, Literal

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse

from pocketpaw.config import get_settings
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
        )
        ctx.intent = body.intent
    except InvalidScope:
        raise CloudError(400, "scope.invalid", "Invalid scope") from None

    # BC-4 run-start hard-block + chunk-3 monthly-quota fast-reject. Sit BOTH
    # credit gates at the SINGLE run-start chokepoint — BEFORE any DB write (no
    # user message, no ChatRunDoc) and BEFORE the executor submit — so a blocked
    # run leaves no trace and IN-FLIGHT runs (already past this point) are never
    # killed. Flag-gated: OFF by default (OSS / self-host run no ledger), ON for
    # the cloud via ``POCKETPAW_BILLING_ENFORCED``. We never raise HTTPException
    # here; both gates raise a ``CloudError`` the handler maps to the 402 wire.
    # The credits package is imported locally to keep it off this hot module's
    # import graph.
    #
    #   * ``check_balance`` (BC-4) — the wallet is empty (balance <= 0): raises
    #     ``InsufficientCredits`` (402, credits.insufficient). The SECONDARY
    #     guard; stays first and unchanged.
    #   * ``check_quota`` (chunk 3) — the wallet may still hold credits but the
    #     workspace has spent up to its monthly ceiling (plan cap + period
    #     top-ups): raises ``QuotaExceeded`` (402, credits.quota_exceeded). This
    #     is the universal monthly cap; the same assertion the run-start gate in
    #     ``run_core.execute_run`` enforces, mirrored here so the synchronous
    #     chat HTTP path returns a clean 402 with no DB trace instead of starting
    #     a run that the executor would only reject afterward.
    if get_settings().billing_enforced:
        from pocketpaw_ee.cloud.credits import service as credits_service

        await credits_service.check_balance(workspace_id)
        await credits_service.check_quota(workspace_id)

    transport = get_stream_transport()
    # Resolve the surface-aware context preamble AFTER scope is resolved
    # (so we have ``workspace_id`` / ``user_id`` confirmed) and BEFORE any
    # other prompt assembly. The resolver never raises — failures fall
    # back to a GENERIC context with an empty preamble, which
    # ``build_dynamic_context`` then treats as the legacy three-line
    # shape. Older clients that send neither ``surface`` nor
    # ``surface_meta`` land here as ``{surface: None, meta: {}}`` and
    # produce a GENERIC context with a placeholder preamble that the
    # router still attaches; the chat continues to work either way.
    ctx.surface_context = await resolve_surface_context(
        ctx.workspace_id,
        user_id,
        {"surface": body.surface, "meta": body.surface_meta or {}},
    )

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
