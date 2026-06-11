# router.py — The pocket execution router.
# Updated: 2026-06-10 (W2c — surface Tier-0 agent-parked writes for approval)
#   — the Tier-0 ``run_action`` path now ROUTES the executor's
#   ``instinct_pending`` park into the Instinct approval queue via
#   ``instinct_bridge.propose_pocket_write`` (the same surface the REST
#   ``/actions/run`` route uses), so a deny-by-default agent write lands as a
#   PENDING Instinct Action a human sees in the Tray — closing W2a's gap
#   where the park was reported honestly but never reached a human. The new
#   ``_route_tier0_park`` helper fetches the pocket wire dict (owner /
#   approver resolution) and threads the creds' ``approval_route``; the
#   threaded creds field (formerly discarded as ``_approval_route``) now
#   carries the pocket's configured approver. ``_run_tier0`` returns a
#   three-state ``_Tier0Result`` (fired / parked / failed): a parked write is
#   HANDLED (no escalation, no re-fire) and surfaced as an
#   ``instinct_pending`` output, not a false fired-success.
# Updated: 2026-06-10 (W2a — deny-by-default Instinct governance) — the
#   Tier-0 ``run_action`` path recognizes the executor's
#   ``instinct_pending`` sentinel. Under W2a ``ActionBinding.requires_instinct``
#   defaults True, so an agent-authored write that omits the field PARKS at
#   the executor gate even though the classifier (which reads the raw spec
#   dict, not the parsed binding) waved it through as a Tier-0 auto-fire.
#   (W2a reported the park honestly; W2c above wires it to the approval
#   queue.)
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
from dataclasses import dataclass
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


@dataclass(frozen=True)
class _Tier0Result:
    """The three-state outcome of a Tier-0 declarative run (W2c).

    ``ok`` — the source ran / the write fired.
    ``parked`` — a deny-by-default write was parked into the Instinct
    approval queue; ``proposed_action_id`` carries the pending Action id.
    ``error`` — a clean failure message (caller escalates to specialist).

    Exactly one of ``ok`` / ``parked`` is True on a non-error outcome; a
    failure sets neither and carries ``error``.
    """

    ok: bool = False
    parked: bool = False
    proposed_action_id: str | None = None
    error: str | None = None


async def _route_tier0_park(
    pocket_id: str,
    park: dict[str, Any],
    *,
    workspace_id: str,
    user_id: str,
    base_url: str,
    auth_type: str,
    allowed_writes: Any,
    approval_route: Any,
) -> _Tier0Result:
    """Route a deny-by-default parked Tier-0 write into the Instinct queue.

    Mirrors the REST ``/actions/run`` route's binding-level park handling:
    fetch the pocket wire dict (for owner / approver resolution + name) and
    hand the executor's ``_park`` blob to
    ``instinct_bridge.propose_pocket_write`` so it lands as a PENDING
    Instinct Action a human sees in the Tray. The credential token is NOT
    forwarded — ``propose_pocket_write`` re-loads it at execution time.

    A failure to fetch the pocket or build the proposal is a clean
    ``error`` outcome (the caller escalates), never a crash and never a
    silent fire — the deny-by-default no-bypass guarantee holds either way.
    """
    from pocketpaw_ee.cloud.pockets import instinct_bridge
    from pocketpaw_ee.cloud.pockets import service as pockets_service

    try:
        pocket = await pockets_service.get(pocket_id, user_id)
    except Exception:
        logger.warning(
            "[pocket-router] could not fetch pocket %s to route a parked Tier-0 write",
            pocket_id,
            exc_info=True,
        )
        return _Tier0Result(
            error=(
                "this write requires Instinct approval but the pocket "
                "could not be loaded to queue it"
            )
        )

    try:
        proposed_id = await instinct_bridge.propose_pocket_write(
            pocket=pocket,
            backend_config={
                "base_url": base_url,
                "auth_type": auth_type,
                "allowed_writes": allowed_writes,
                "approval_route": approval_route,
            },
            parked_write=park,
            requested_by=user_id,
        )
    except Exception:
        logger.warning(
            "[pocket-router] failed to propose a parked Tier-0 write on pocket %s",
            pocket_id,
            exc_info=True,
        )
        return _Tier0Result(error="this write requires Instinct approval but could not be queued")

    logger.info(
        "[pocket-router] Tier-0 write on pocket %s parked for approval → Instinct action %s",
        pocket_id,
        proposed_id,
    )
    return _Tier0Result(parked=True, proposed_action_id=proposed_id)


async def _run_tier0(
    classification: Classification,
    input: Any,
    *,
    workspace_id: str,
    user_id: str,
    ripple_spec: dict[str, Any],
) -> _Tier0Result:
    """Execute a Tier-0 declarative verdict — fire the declared source or
    action through the EXISTING executor.

    Returns a ``_Tier0Result`` with THREE outcomes (W2c):

    * ``ok=True`` — the source ran / the write fired. The caller returns a
      Tier-0 ``applied`` output.
    * ``parked=True`` — a deny-by-default write was PARKED at the executor's
      gate and routed into the Instinct approval queue
      (``proposed_action_id`` carries the pending Action id). The caller
      treats this as HANDLED (it does NOT escalate to the specialist — the
      write was governed correctly), but the output is marked pending, not
      applied. No re-plan, no re-fire.
    * ``ok=False`` (and not parked) — a clean failure (no backend, no run
      access, executor error). The caller escalates to the specialist so it
      can still satisfy the intent.

    The executors are invoked with every guard they normally enforce — the
    router supplies the arguments, it does not reach past any gate. A pocket
    with no backend configured, or a user without run access, is a clean
    failure (``ok=False``), not a crash.
    """
    from pocketpaw_ee.cloud.pockets import service as pockets_service

    creds = await pockets_service.get_pocket_backend_for_executor(workspace_id, input.pocket_id)
    if creds is None:
        return _Tier0Result(
            error="pocket has no backend configured — cannot run a declarative tier"
        )
    # RFC 05 M2b.1 / W2c — the executor-creds tuple is a 6-tuple. The
    # trailing `approval_route` is now THREADED into the binding-level park
    # routing below: under W2a deny-by-default a binding that omits
    # `requires_instinct` still parks at the executor gate, and that parked
    # write must reach the approval surface with the pocket's configured
    # approver route — exactly as the REST `/actions/run` path passes it to
    # `propose_pocket_write`.
    base_url, auth_type, auth_header, token, allowed_writes, approval_route = creds

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
            workspace_id=workspace_id,
        )
        errors = result.get("errors") or []
        if errors:
            return _Tier0Result(error=f"source run reported {len(errors)} error(s)")
        return _Tier0Result(ok=True)

    if classification.op == "run_action":
        # A write action — gate run-access exactly like the REST route
        # (``has_action_run_access``: owner or explicit shared_with).
        if not await pockets_service.has_action_run_access(input.pocket_id, user_id):
            return _Tier0Result(error="caller lacks run access for this write action")
        action_key = classification.op_args.get("action", "")
        actions = ripple_spec.get("actions")
        raw_action = actions.get(action_key) if isinstance(actions, dict) else None
        if not isinstance(raw_action, dict):
            return _Tier0Result(
                error=f"action '{action_key}' is missing or malformed on the pocket"
            )

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
        # W2c — DENY-BY-DEFAULT, fully wired. `ActionBinding.requires_instinct`
        # now defaults True (W2a), so a binding the agent authored WITHOUT
        # setting the field PARKS at the executor's gate even though the
        # classifier (which reads the raw dict's `requires_instinct` key,
        # sees it unset, and so does not escalate to the specialist tier)
        # routed it here as a Tier-0 auto-fire. The executor returns
        # `{ok:True, code:"instinct_pending"}` carrying the resolved write
        # under `_park` — the write was NOT fired.
        #
        # W2a reported that honestly but stopped short: the parked write
        # never reached the approval queue, so a human never saw it in the
        # Tray. W2c closes that gap by routing the park into
        # `instinct_bridge.propose_pocket_write` — the SAME surface the REST
        # `/actions/run` route uses — so the write lands as a PENDING
        # Instinct Action a human can approve or reject.
        #
        # The Tier-0 declarative run threads NO template (a `rippleSpec`
        # action binding need not be a template action), so the executor's
        # only park shape here is the gate-7 binding-level park: it carries
        # `_park` and never an `approval_id` (that is the template-CEL
        # escalation shape, which only the REST route can produce). We route
        # the `_park` blob with the pocket's configured approver route and
        # report the action as parked-for-approval, not fired.
        if result.get("code") == "instinct_pending":
            return await _route_tier0_park(
                input.pocket_id,
                result.get("_park") or {},
                workspace_id=workspace_id,
                user_id=user_id,
                base_url=base_url,
                auth_type=auth_type,
                allowed_writes=allowed_writes,
                approval_route=approval_route,
            )
        if not result.get("ok"):
            return _Tier0Result(error=result.get("error") or "action run failed")
        return _Tier0Result(ok=True)

    return _Tier0Result(error=f"unknown Tier-0 op '{classification.op}'")


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


def _tier0_pending_output(
    classification: Classification, *, pocket_id: str, proposed_action_id: str | None
):
    """Shape a PARKED Tier-0 write (W2c) as a ``PocketSpecialistEditOutput``.

    A deny-by-default write that the executor parked and the router routed
    into the Instinct approval queue is HANDLED (not escalated) but did NOT
    apply: ``ok=False``, ``action="instinct_pending"``, no ops, and a clear
    message naming the pending Action id so the agent tells the human the
    write is waiting for approval rather than claiming it landed. The
    ``backend_used`` carries the proposed id so a caller that surfaces the
    raw output can deep-link the Tray entry.
    """
    from pocketpaw_ee.agent.pocket_specialist.runtime import PocketSpecialistEditOutput

    return PocketSpecialistEditOutput(
        ok=False,
        action="instinct_pending",
        pocket_id=pocket_id,
        ops=[],
        duration_ms=0,
        backend_used=f"pocket_router:tier0:instinct_pending:{proposed_action_id or ''}",
        error="this write requires Instinct approval — proposed for review, not auto-fired",
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

    * ``handled is True`` — a cheap tier (0 or 1) ran the request, OR a
      Tier-0 write was PARKED into the Instinct approval queue (W2c
      deny-by-default). The caller uses ``output`` (a
      ``PocketSpecialistEditOutput``) directly and does NOT fall through to
      the specialist. A parked write returns ``output.ok=False`` with
      ``action="instinct_pending"`` so the agent reports it as needing
      approval, not as applied — but it is HANDLED (no re-plan, no re-fire).
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
        tier0 = await _run_tier0(
            classification,
            input,
            workspace_id=workspace_id,
            user_id=user_id,
            ripple_spec=ripple_spec,
        )
        timeline.finish("apply", detail=f"{classification.op} -> {classification.target}")
        _add_skipped_layout_stages(timeline)

        # ── W2c — DENY-BY-DEFAULT park ─────────────────────────────────
        # The write was PARKED at the executor's gate and routed into the
        # Instinct approval queue. This is HANDLED — the router must NOT
        # escalate (escalating would hand the parked write to the specialist
        # to re-plan / re-fire, defeating the gate) and must NOT report a
        # fired success. Surface a pending output and audit it as parked.
        if tier0.parked:
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
                status="parked-instinct-pending",
            )
            logger.info(
                "[pocket-router] %s Tier-0 write parked for approval (%s, action=%s)",
                request_id,
                classification.op,
                tier0.proposed_action_id,
            )
            return True, _tier0_pending_output(
                classification,
                pocket_id=input.pocket_id,
                proposed_action_id=tier0.proposed_action_id,
            )

        if not tier0.ok:
            # A failed cheap tier escalates: the specialist can still try.
            timeline.skipped("classify", f"Tier-0 attempt failed: {tier0.error}")
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
            logger.info(
                "[pocket-router] %s Tier-0 failed (%s) — escalating", request_id, tier0.error
            )
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
