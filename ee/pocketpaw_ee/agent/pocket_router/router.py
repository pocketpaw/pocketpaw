# router.py — The pocket execution router.
# Created: 2026-05-22 (Increment 3) — ``classify_and_route`` sits in front
#   of ``pocket_specialist__edit``. It runs the pure classifier
#   (classifier.py) and dispatches to the CHEAPEST capable tier:
#
#     Tier 0 (declarative)  — fire a declared source / action via the
#                             existing executors (source_executor.run_sources
#                             / action_executor.run_action). The executors
#                             keep ALL their guards (allowlist, SSRF, rate
#                             limit, fail-closed instinct-reject); the router
#                             only INVOKES them — it bypasses no guard.
#     Tier 1 (deterministic) — apply one granular op through the existing
#                             ``EditAgentModeAdapter`` op-apply path.
#     Tier 2 (specialist)    — escalate to ``run_edit_specialist`` UNCHANGED.
#
# Every call records a per-stage timeline, emits ONE ``pocket_execution``
# SSE frame (the Thesys "what ran / what was skipped" readout) and writes a
# ``pocket_router`` audit entry — WARNING severity on a Tier-0/1 bypass,
# because that is a write/mutation with no agent reasoning behind it and
# deserves a durable trail.
#
# The kill-switch (``settings.pocket_router_enabled``) and the confidence
# floor (``settings.pocket_router_min_confidence``) make the router
# fail-safe: with the switch off, or on any sub-threshold verdict, the
# router escalates and behaves exactly like today.
"""The pocket execution router — classify an edit, route to the cheapest tier."""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any

from pocketpaw_ee.agent.pocket_router.classifier import Classification, classify
from pocketpaw_ee.agent.pocket_router.events import (
    ExecutionStage,
    PocketExecutionFrame,
    TokenSpend,
)

logger = logging.getLogger(__name__)

# Skip-reason string for the layout / render stages on a Tier-0/1 route.
# A declarative refresh or a single-op data edit changes no component
# structure, so the renderer never re-runs layout — the cheap-tier win.
_SKIP_REASON_DATA_ONLY = "data-only change"


class _Timeline:
    """Accumulates ``ExecutionStage`` rows for one routed request.

    A tiny mutable helper — the router opens stages with ``start`` and
    closes them with ``finish`` so the per-stage ``ms`` is real
    wall-clock, then ``skipped`` records a stage that never ran.
    """

    def __init__(self) -> None:
        self._stages: list[ExecutionStage] = []
        self._open: dict[str, float] = {}

    def start(self, stage: str) -> None:
        self._open[stage] = time.monotonic()

    def finish(self, stage: str, detail: str | None = None) -> None:
        began = self._open.pop(stage, None)
        ms = int((time.monotonic() - began) * 1000) if began is not None else 0
        self._stages.append(ExecutionStage(stage=stage, ran=True, ms=ms, detail=detail))  # type: ignore[arg-type]

    def skipped(self, stage: str, reason: str) -> None:
        self._stages.append(
            ExecutionStage(stage=stage, ran=False, ms=0, skipped_reason=reason)  # type: ignore[arg-type]
        )

    def rows(self) -> list[ExecutionStage]:
        return list(self._stages)


def _add_skipped_layout_stages(timeline: _Timeline) -> None:
    """Mark the two expensive stages a cheap-tier route never runs.

    A Tier-0 declarative refresh and a Tier-1 single-op data edit both
    leave the component tree untouched, so ``layout_build`` and
    ``widget_render`` are skipped — this is the readout the user sees in
    the ``pocket_execution`` frame ("skipped: data-only change")."""
    timeline.skipped("layout_build", _SKIP_REASON_DATA_ONLY)
    timeline.skipped("widget_render", _SKIP_REASON_DATA_ONLY)


def _emit_execution_frame(
    *,
    request_id: str,
    intent: str,
    tier: int,
    timeline: _Timeline,
    started: float,
    tokens: TokenSpend,
) -> None:
    """Build and push the single ``pocket_execution`` SSE frame.

    Best-effort — a missing SSE sink (CLI / test) is a no-op, and a push
    failure must never break the edit, so the call is wrapped.
    """
    frame = PocketExecutionFrame(
        request_id=request_id,
        intent=intent,
        tier_chosen=tier,  # type: ignore[arg-type]
        stages=timeline.rows(),
        total_ms=int((time.monotonic() - started) * 1000),
        tokens=tokens,
    )
    try:
        from pocketpaw_ee.cloud.chat.agent_service import push_pocket_execution

        push_pocket_execution(frame.to_wire())
    except Exception:
        logger.debug("push_pocket_execution failed (non-fatal)", exc_info=True)


def _audit_router_decision(
    *,
    actor: str,
    workspace_id: str,
    pocket_id: str,
    tier: int,
    intent: str,
    classification: Classification,
    status: str,
) -> None:
    """Write a ``pocket_router`` audit entry for one routed request.

    A Tier-0/1 verdict is logged at WARNING — it is a write/mutation the
    router performed with NO agent reasoning behind it, so the durable
    trail matters. A Tier-2 escalation is logged at INFO (the specialist
    keeps its own trail). Audit failures never break the edit.
    """
    try:
        from pocketpaw.security.audit import AuditEvent, AuditSeverity, get_audit_logger

        severity = AuditSeverity.WARNING if tier in (0, 1) else AuditSeverity.INFO
        get_audit_logger().log(
            AuditEvent.create(
                severity=severity,
                actor=actor,
                action="pocket.router.route",
                target=pocket_id,
                status=status,
                category="pocket_router",
                workspace_id=workspace_id,
                pocket_id=pocket_id,
                tier=tier,
                intent=intent[:200],
                op=classification.op,
                router_target=classification.target,
                confidence=round(classification.confidence, 3),
                reasoning=classification.reasoning,
            )
        )
    except Exception:  # noqa: BLE001 — audit must never break the route
        logger.warning("pocket-router audit-log write failed", exc_info=True)


async def _resolve_ripple_spec(input: Any) -> dict[str, Any]:
    """Resolve the pocket's rippleSpec for the classifier.

    Uses the caller-supplied ``input.pocket`` view when present (the chat
    agent already fetched it); otherwise reads it via the service's
    ``agent_view``. Returns ``{}`` when neither is available — the
    classifier then escalates (an empty spec matches no cheap-tier rule),
    which is the safe outcome.
    """
    if isinstance(input.pocket, dict):
        spec = input.pocket.get("rippleSpec")
        if isinstance(spec, dict):
            return spec
    try:
        from pocketpaw_ee.cloud.pockets import service as pockets_service

        view, err = await pockets_service.agent_view(input.pocket_id)
        if err is None and isinstance(view, dict):
            spec = view.get("rippleSpec")
            if isinstance(spec, dict):
                return spec
    except Exception:
        logger.debug("router could not resolve ripple_spec — escalating", exc_info=True)
    return {}


async def _run_tier0(
    classification: Classification,
    input: Any,
    *,
    workspace_id: str,
    user_id: str,
    ripple_spec: dict[str, Any],
) -> tuple[bool, str | None]:
    """Execute a Tier-0 declarative verdict — fire the declared source or
    action through the EXISTING executor.

    Returns ``(ok, error)``. The executors are invoked with every guard
    they normally enforce — the router supplies the arguments, it does
    not reach past any gate. A pocket with no backend configured, or a
    user without run access, is a clean failure (``ok=False``), not a
    crash.
    """
    from pocketpaw_ee.cloud.pockets import service as pockets_service

    creds = await pockets_service.get_pocket_backend_for_executor(workspace_id, input.pocket_id)
    if creds is None:
        return False, "pocket has no backend configured — cannot run a declarative tier"
    base_url, auth_type, auth_header, token, allowed_writes = creds

    if classification.op == "run_source":
        # A source run mirrors ``POST /pockets/{id}/sources/run`` — read
        # access only, so no extra gate. The executor keeps its SSRF +
        # rate-limit guards.
        from pocketpaw_ee.cloud.pockets import source_executor

        result = await source_executor.run_sources(
            pocket_id=input.pocket_id,
            user_id=user_id,
            ripple_spec=ripple_spec,
            base_url=base_url,
            auth_type=auth_type,
            auth_header=auth_header,
            token=token,
            only_source=classification.op_args.get("source"),
        )
        errors = result.get("errors") or []
        if errors:
            return False, f"source run reported {len(errors)} error(s)"
        return True, None

    if classification.op == "run_action":
        # A write action — gate run-access exactly like the REST route
        # (``has_action_run_access``: owner or explicit shared_with).
        if not await pockets_service.has_action_run_access(input.pocket_id, user_id):
            return False, "caller lacks run access for this write action"
        action_key = classification.op_args.get("action", "")
        actions = ripple_spec.get("actions")
        raw_action = actions.get(action_key) if isinstance(actions, dict) else None
        if not isinstance(raw_action, dict):
            return False, f"action '{action_key}' is missing or malformed on the pocket"

        from pocketpaw_ee.cloud.pockets import action_executor

        # The executor re-reads method / instinct / allowlist server-side
        # and fails closed on any guard — the router passes data only.
        result = await action_executor.run_action(
            workspace_id=workspace_id,
            pocket_id=input.pocket_id,
            user_id=user_id,
            action=action_key,
            raw_action=raw_action,
            path=raw_action.get("path", ""),
            params=raw_action.get("params") or {},
            base_url=base_url,
            auth_type=auth_type,
            auth_header=auth_header,
            token=token,
            allowed_writes=allowed_writes,
        )
        if not result.get("ok"):
            return False, result.get("error") or "action run failed"
        return True, None

    return False, f"unknown Tier-0 op '{classification.op}'"


async def _run_tier1(
    classification: Classification,
    input: Any,
    *,
    workspace_id: str,
    user_id: str,
    settings: Any,
) -> Any:
    """Execute a Tier-1 deterministic verdict — apply ONE granular op.

    Reuses ``EditAgentModeAdapter`` (its ``_apply_ops`` path): the router
    builds a one-op ``PocketSpecialistEditInput`` and hands it to the
    adapter, so the op runs through the exact same validation, SSE-emit,
    and rejected-op handling the chat-agent edit path uses. No LLM runs.
    Returns the adapter's ``PocketSpecialistEditOutput``.
    """
    from pocketpaw_ee.agent.pocket_specialist.adapters import EditAgentModeAdapter
    from pocketpaw_ee.agent.pocket_specialist.runtime import PocketSpecialistEditInput

    op_input = PocketSpecialistEditInput(
        pocket_id=input.pocket_id,
        intent=input.intent,
        pocket=input.pocket,
        target_node_ids=input.target_node_ids,
        ops=[{"op": classification.op, "args": dict(classification.op_args)}],
    )
    return await EditAgentModeAdapter().edit(
        op_input,
        workspace_id=workspace_id,
        user_id=user_id,
        settings=settings,
    )


def _tier0_output(classification: Classification, *, pocket_id: str, ok: bool, error: str | None):
    """Shape a Tier-0 result as a ``PocketSpecialistEditOutput`` so the
    MCP tool handler gets a uniform return regardless of tier."""
    from pocketpaw_ee.agent.pocket_specialist.runtime import PocketSpecialistEditOutput

    return PocketSpecialistEditOutput(
        ok=ok,
        action="applied" if ok else "failed",
        pocket_id=pocket_id,
        ops=[{"op": classification.op, "args": dict(classification.op_args)}] if ok else [],
        duration_ms=0,
        backend_used="pocket_router:tier0",
        error=error,
        warnings=[],
    )


async def classify_and_route(
    input: Any,
    *,
    workspace_id: str,
    user_id: str,
    settings: Any,
) -> tuple[bool, Any]:
    """Classify an edit ``input`` and route it to the cheapest tier.

    Returns ``(handled, output)``:

    * ``handled is True`` — a cheap tier (0 or 1) ran the request. The
      caller uses ``output`` (a ``PocketSpecialistEditOutput``) directly
      and does NOT fall through to the specialist.
    * ``handled is False`` — the router escalated. ``output`` is ``None``;
      the caller invokes ``run_edit_specialist`` itself (the existing
      flow, unchanged). The router emits its observability frame + audit
      entry for the escalation too, so a Tier-2 route is still traced.

    Fail-safe gates, in order:
      1. ``pocket_router_enabled is False`` — escalate immediately, no
         classification (the kill-switch restores today's behaviour).
      2. The pure classifier returns Tier 2 — escalate.
      3. The verdict's confidence is below ``pocket_router_min_confidence``
         — escalate (a low-confidence cheap tier is not trustworthy).
      4. A cheap tier ran but FAILED — escalate so the specialist can
         still satisfy the intent (the failed cheap attempt changed
         nothing the specialist can't redo).
    """
    started = time.monotonic()
    request_id = f"pr_{uuid.uuid4().hex[:12]}"
    timeline = _Timeline()
    intent = getattr(input, "intent", "") or ""

    # ── gate 1: kill-switch ────────────────────────────────────────────
    if not getattr(settings, "pocket_router_enabled", True):
        timeline.skipped("classify", "router disabled (pocket_router_enabled=false)")
        timeline.skipped("apply", "router disabled — escalating to specialist")
        escalation = Classification(
            tier=2,
            target=None,
            confidence=1.0,
            reasoning="pocket_router_enabled is False — kill-switch escalation",
            op=None,
        )
        _emit_execution_frame(
            request_id=request_id,
            intent=intent,
            tier=2,
            timeline=timeline,
            started=started,
            tokens=TokenSpend(),
        )
        _audit_router_decision(
            actor=user_id,
            workspace_id=workspace_id,
            pocket_id=getattr(input, "pocket_id", ""),
            tier=2,
            intent=intent,
            classification=escalation,
            status="escalated-kill-switch",
        )
        return False, None

    # ── classify (pure) ────────────────────────────────────────────────
    timeline.start("classify")
    ripple_spec = await _resolve_ripple_spec(input)
    classification = classify(intent, ripple_spec)
    timeline.finish("classify", detail=classification.reasoning)

    min_conf = float(getattr(settings, "pocket_router_min_confidence", 0.9))

    # ── gate 2 + 3: Tier-2 verdict, or sub-threshold confidence ────────
    if classification.is_escalation or classification.confidence < min_conf:
        reason = (
            classification.reasoning
            if classification.is_escalation
            else (
                f"confidence {classification.confidence:.2f} below floor "
                f"{min_conf:.2f} — escalating (fail-safe)"
            )
        )
        timeline.skipped("apply", reason)
        _emit_execution_frame(
            request_id=request_id,
            intent=intent,
            tier=2,
            timeline=timeline,
            started=started,
            tokens=TokenSpend(),
        )
        _audit_router_decision(
            actor=user_id,
            workspace_id=workspace_id,
            pocket_id=getattr(input, "pocket_id", ""),
            tier=2,
            intent=intent,
            classification=classification,
            status="escalated",
        )
        logger.info("[pocket-router] %s escalated to Tier 2 — %s", request_id, reason)
        return False, None

    # ── Tier 0 — declarative ───────────────────────────────────────────
    if classification.tier == 0:
        timeline.start("apply")
        ok, error = await _run_tier0(
            classification,
            input,
            workspace_id=workspace_id,
            user_id=user_id,
            ripple_spec=ripple_spec,
        )
        timeline.finish("apply", detail=f"{classification.op} -> {classification.target}")
        _add_skipped_layout_stages(timeline)
        if not ok:
            # A failed cheap tier escalates: the specialist can still try.
            timeline.skipped("classify", f"Tier-0 attempt failed: {error}")
            _emit_execution_frame(
                request_id=request_id,
                intent=intent,
                tier=2,
                timeline=timeline,
                started=started,
                tokens=TokenSpend(),
            )
            _audit_router_decision(
                actor=user_id,
                workspace_id=workspace_id,
                pocket_id=getattr(input, "pocket_id", ""),
                tier=2,
                intent=intent,
                classification=classification,
                status="escalated-tier0-failed",
            )
            logger.info("[pocket-router] %s Tier-0 failed (%s) — escalating", request_id, error)
            return False, None
        _emit_execution_frame(
            request_id=request_id,
            intent=intent,
            tier=0,
            timeline=timeline,
            started=started,
            tokens=TokenSpend(),
        )
        _audit_router_decision(
            actor=user_id,
            workspace_id=workspace_id,
            pocket_id=getattr(input, "pocket_id", ""),
            tier=0,
            intent=intent,
            classification=classification,
            status="applied",
        )
        logger.info("[pocket-router] %s Tier 0 applied (%s)", request_id, classification.op)
        return True, _tier0_output(classification, pocket_id=input.pocket_id, ok=True, error=None)

    # ── Tier 1 — deterministic single granular op ──────────────────────
    timeline.start("apply")
    output = await _run_tier1(
        classification,
        input,
        workspace_id=workspace_id,
        user_id=user_id,
        settings=settings,
    )
    timeline.finish("apply", detail=f"{classification.op} -> {classification.target}")
    _add_skipped_layout_stages(timeline)

    if not getattr(output, "ok", False):
        # The granular op was rejected by the service — escalate so the
        # specialist (which can re-plan) gets a shot.
        err = getattr(output, "error", None) or "Tier-1 op did not apply"
        timeline.skipped("classify", f"Tier-1 op rejected: {err}")
        _emit_execution_frame(
            request_id=request_id,
            intent=intent,
            tier=2,
            timeline=timeline,
            started=started,
            tokens=TokenSpend(),
        )
        _audit_router_decision(
            actor=user_id,
            workspace_id=workspace_id,
            pocket_id=getattr(input, "pocket_id", ""),
            tier=2,
            intent=intent,
            classification=classification,
            status="escalated-tier1-rejected",
        )
        logger.info("[pocket-router] %s Tier-1 op rejected (%s) — escalating", request_id, err)
        return False, None

    _emit_execution_frame(
        request_id=request_id,
        intent=intent,
        tier=1,
        timeline=timeline,
        started=started,
        tokens=TokenSpend(),
    )
    _audit_router_decision(
        actor=user_id,
        workspace_id=workspace_id,
        pocket_id=getattr(input, "pocket_id", ""),
        tier=1,
        intent=intent,
        classification=classification,
        status="applied",
    )
    logger.info("[pocket-router] %s Tier 1 applied (%s)", request_id, classification.op)
    return True, output


__all__ = ["classify_and_route"]
