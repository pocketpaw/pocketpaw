# ee/pocketpaw_ee/instinct/auto_triage.py — smart-approval auto-triage.
# Created: 2026-06-16 (feat/instinct-smart-triage) — a cheap-model classifier
#   that sits between an Instinct proposal being created and the human being
#   notified. It reads the proposed action + reasoning trace + fabric snapshot +
#   the pocket's standing InstinctRules and returns APPROVE / DENY / ESCALATE.
#   On APPROVE the router auto-approves the action AND the decision (with the
#   triager's reasoning) is written to the hash-chained audit ledger via
#   ``store.auto_approve`` — so an auto-approval is as auditable as a human one.
#   Anything novel / risky / uncertain → ESCALATE to the human (the unchanged
#   path). "Their throughput + our audit."
#
# CRITICAL — agent-mode LLM transport. PocketPaw runs in agent mode with NO
#   ANTHROPIC_API_KEY, so the triager LLM call MUST shell the ``claude`` CLI
#   (``claude -p <prompt> --output-format json``) — the mandate-foreman
#   ``ClaudeCliLlm`` pattern — NOT ``AsyncAnthropic`` (the narrator's direct
#   client fails in agent mode). ``ClaudeCliTriagerLlm`` below mirrors
#   ``ee.cloud.mandates.foreman.ClaudeCliLlm`` exactly. Tests inject a fake
#   ``TriagerLlm`` — a real ``claude -p`` call NEVER runs in code under test.
#
# Safety discipline (encoded here — do not weaken):
#   * Template ESCALATE_APPROVAL / BLOCK rules are NEVER auto-approvable at ANY
#     approval level. ``rule_flagged`` short-circuits to ESCALATE before the LLM
#     is even consulted — a deterministic gate, not a model judgment.
#   * ASK level = today's behaviour: the triager is never invoked; every action
#     goes to the human.
#   * FAIL-SAFE: any triager error / malformed JSON / timeout / low confidence
#     resolves to ESCALATE. The classifier can only ever turn a "human reviews"
#     into an "auto-approve" when it is confident AND the action is unflagged;
#     every failure mode falls back to the human.

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol

from pydantic import BaseModel, field_validator

from pocketpaw.instinct.store import AuditChainError

logger = logging.getLogger(__name__)

# Subprocess timeout for the claude CLI call (seconds). A hung CLI must never
# wedge the propose path — on timeout we fail-safe to ESCALATE.
_CLI_TIMEOUT = 60.0

# Below this confidence, an APPROVE verdict is downgraded to ESCALATE. The
# triager only auto-approves when it is genuinely sure; uncertainty goes to the
# human (fail-safe).
_MIN_CONFIDENCE = 0.75


# ---------------------------------------------------------------------------
# Verdicts, levels, and the classifier output schema
# ---------------------------------------------------------------------------


class TriageVerdict(StrEnum):
    """The classifier's verdict on a single proposed action.

    v1 has NO auto-reject: only ``APPROVE`` auto-approves; every other verdict
    routes the action to the human. ``DENY`` is retained in the enum (and parsed
    if a model still emits it) but is treated exactly like ``ESCALATE`` — the
    orchestration only acts on ``APPROVE``, so a ``DENY`` action still reaches
    the human, never silently killed. The prompt (``build_prompt``) only offers
    APPROVE / ESCALATE so the model's vocabulary matches this behaviour; a future
    slice that adds a real auto-reject path can give ``DENY`` distinct semantics.
    """

    APPROVE = "APPROVE"
    DENY = "DENY"
    ESCALATE = "ESCALATE"


class ApprovalLevel(StrEnum):
    """Per-workspace (per-pocket-overridable) auto-approval posture.

    Modelled on Claude Code permission modes:

    * ``ASK`` — triager OFF. Every action → human. Byte-for-byte today's
      behaviour; the triager is never invoked. THIS IS THE DEFAULT — the
      feature is off until a workspace explicitly opts in (PRD: off-by-default).
    * ``TRIAGE`` — auto-approve routine actions the classifier is confident
      about; escalate everything else. Opt-in.
    * ``TRUSTED`` — a wider auto-approve appetite (the prompt tells the model to
      lean approve on borderline-but-safe actions). Rule-flagged actions are
      STILL never auto-approvable, exactly as at ``TRIAGE``. Opt-in.
    """

    ASK = "ASK"
    TRIAGE = "TRIAGE"
    TRUSTED = "TRUSTED"


# Env override for the default workspace approval level (a real per-workspace
# setting store is a later slice; this keeps v1 configurable + testable).
_LEVEL_ENV = "POCKETPAW_INSTINCT_APPROVAL_LEVEL"
# OFF BY DEFAULT (PRD): with no explicit workspace / pocket / env setting the
# triager is NOT invoked and every action goes to the human — today's behaviour.
# A workspace must opt in to TRIAGE / TRUSTED to turn auto-approval on.
_DEFAULT_LEVEL = ApprovalLevel.ASK


def resolve_approval_level(
    *,
    workspace_level: str | ApprovalLevel | None = None,
    pocket_level: str | ApprovalLevel | None = None,
) -> ApprovalLevel:
    """Resolve the effective approval level for an action.

    Precedence (most specific wins): per-pocket override → per-workspace
    setting → ``POCKETPAW_INSTINCT_APPROVAL_LEVEL`` env → ``ASK`` default
    (off — the triager is not invoked). An unrecognised value anywhere falls
    through to the next source rather than raising — a misconfiguration must
    never crash the propose path, and the safe direction is "less
    auto-approval", so a bad value never silently escalates trust.
    """
    for candidate in (pocket_level, workspace_level, os.environ.get(_LEVEL_ENV)):
        level = _coerce_level(candidate)
        if level is not None:
            return level
    return _DEFAULT_LEVEL


def _coerce_level(value: str | ApprovalLevel | None) -> ApprovalLevel | None:
    if value is None:
        return None
    if isinstance(value, ApprovalLevel):
        return value
    try:
        return ApprovalLevel(str(value).strip().upper())
    except ValueError:
        # An unrecognised level is ignored (we fall through to the next, safer
        # source). Log it so a typo'd setting doesn't silently disable the
        # feature with no trace.
        logger.warning(
            "ignoring unrecognised instinct approval level %r — falling through "
            "to the next configured source",
            value,
        )
        return None


class TriageDecision(BaseModel):
    """The strict-JSON shape the triager LLM must return."""

    verdict: TriageVerdict
    reasoning: str = ""
    confidence: float = 0.0

    @field_validator("confidence")
    @classmethod
    def _clamp_confidence(cls, v: float) -> float:
        # Tolerate a model that emits 0-100 or out-of-range — clamp to [0, 1].
        if v > 1.0:
            v = v / 100.0 if v <= 100.0 else 1.0
        return max(0.0, min(1.0, v))


# ---------------------------------------------------------------------------
# Classifier inputs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TriageContext:
    """Everything the triager sees about one proposed action.

    All fields are plain JSON-able values (dicts / lists / strs) so the OSS
    Action + trace models cross into the prompt without coupling. ``rule_flagged``
    is the deterministic safety signal — when True the action matched a template
    ESCALATE_APPROVAL / BLOCK rule and is NEVER auto-approvable.
    """

    workspace_id: str
    pocket_id: str
    action_id: str
    title: str
    description: str
    recommendation: str
    # The deterministic safety gate: True ⟹ a template ESCALATE_APPROVAL / BLOCK
    # rule matched this action. Never auto-approvable at any level.
    rule_flagged: bool = False
    # The parked write / code-change / external-action blob (method, path,
    # params, ...) when the proposal carries one; {} otherwise.
    parked_blob: dict[str, Any] = field(default_factory=dict)
    # The captured ReasoningTrace (model_dump) — what the agent consumed.
    reasoning_trace: dict[str, Any] = field(default_factory=dict)
    # FabricObjectSnapshots (model_dump list) keyed to the proposal.
    fabric_snapshots: list[dict[str, Any]] = field(default_factory=list)
    # The pocket template's standing InstinctRules (model_dump list).
    instinct_rules: list[dict[str, Any]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Pluggable LLM transport (mirrors ee.cloud.mandates.foreman)
# ---------------------------------------------------------------------------


class TriagerLlm(Protocol):
    """One judgment call: prompt in, raw model text out.

    ``context`` rides along so a deterministic mock can answer without parsing
    prose; the real CLI transport ignores it and sends only the prompt."""

    async def triage(self, *, prompt: str, context: TriageContext) -> str: ...


class ClaudeCliTriagerLlm:
    """Default transport — shells the ``claude`` CLI (agent-mode, no API key).

    ``claude -p <prompt> --output-format json`` prints a JSON envelope whose
    ``result`` field carries the model's text. The prompt is a single argv
    element (the CLI does its own auth); nothing is shell-interpolated. This is
    the exact pattern proven in ``ee.cloud.mandates.foreman.ClaudeCliLlm``."""

    async def triage(self, *, prompt: str, context: TriageContext) -> str:
        proc = await asyncio.create_subprocess_exec(
            "claude",
            "-p",
            prompt,
            "--output-format",
            "json",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=_CLI_TIMEOUT)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            raise RuntimeError(f"claude CLI timed out after {_CLI_TIMEOUT}s") from None
        out = out_b.decode("utf-8", "replace")
        if proc.returncode != 0:
            err = err_b.decode("utf-8", "replace")
            raise RuntimeError(f"claude CLI failed (exit {proc.returncode}): {err.strip()[:300]}")
        try:
            envelope = json.loads(out)
        except json.JSONDecodeError:
            return out
        if isinstance(envelope, dict) and isinstance(envelope.get("result"), str):
            return envelope["result"]
        return out


def resolve_triager_llm() -> TriagerLlm:
    """Pick the transport from ``POCKETPAW_INSTINCT_TRIAGER_LLM`` (``claude``
    default). ``mock`` is for offline demos / dev only — tests inject their own
    fake directly rather than relying on the env."""
    choice = (os.environ.get("POCKETPAW_INSTINCT_TRIAGER_LLM") or "claude").strip().lower()
    if choice == "mock":
        return _DemoMockTriagerLlm()
    return ClaudeCliTriagerLlm()


class _DemoMockTriagerLlm:
    """Deterministic offline transport for demos — always ESCALATE (the safe
    default). Tests use their own injected fakes, not this."""

    async def triage(self, *, prompt: str, context: TriageContext) -> str:
        return json.dumps(
            {
                "verdict": "ESCALATE",
                "reasoning": "Mock triager (offline) defers every action to the human.",
                "confidence": 1.0,
            }
        )


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

# Per-field cap on the prose / JSON blobs interpolated into the prompt. These
# fields are agent- and (transitively) end-user-controlled, so they are a
# prompt-injection surface — truncating each one bounds how much untrusted text
# can try to steer the classifier. The deterministic gates (ASK / rule_flagged /
# confidence floor) and the fail-safe ESCALATE remain the real guarantees; this
# just shrinks the attack surface.
_PROSE_CAP = 500


def _truncate(value: Any, cap: int = _PROSE_CAP) -> str:
    """Render ``value`` as a string and cap its length for prompt interpolation.

    Dicts / lists are JSON-serialized first (so a parked blob / trace stays
    readable); everything else is ``str()``-ed. Over-cap text is clipped with a
    visible ``…[truncated]`` marker so the model knows it isn't the whole thing."""
    if isinstance(value, (dict, list)):
        text = json.dumps(value, indent=2, default=str)
    else:
        text = str(value)
    if len(text) > cap:
        return text[:cap] + " …[truncated]"
    return text


def build_prompt(context: TriageContext, level: ApprovalLevel) -> str:
    """Assemble the single classification prompt.

    The standing InstinctRules ride VERBATIM so the model knows the pocket's
    policy. The approval level tunes the appetite. The model is told that a
    rule-flagged action is never approvable (defence in depth — the
    deterministic gate already short-circuits those before this prompt is
    built, but stating it keeps the model aligned).

    The agent-controlled prose fields (title / description / recommendation /
    parked blob / reasoning trace / fabric snapshots) are each truncated via
    ``_truncate`` before interpolation to bound the prompt-injection surface."""
    appetite = {
        ApprovalLevel.TRIAGE: (
            "Auto-approve ONLY routine, low-risk, clearly-within-policy actions you are "
            "confident about. When in any doubt, ESCALATE."
        ),
        ApprovalLevel.TRUSTED: (
            "You may auto-approve a wider range of safe, in-policy actions, including "
            "borderline-but-clearly-harmless ones. Still ESCALATE anything novel, risky, "
            "irreversible, or outside the standing rules."
        ),
    }.get(level, "When in any doubt, ESCALATE.")

    return f"""You are the TRIAGER — a fast approval classifier that sits between an AI agent's \
proposed action and a human reviewer. You decide whether a proposed action is safe to \
auto-approve, or must be escalated to the human.

You are judged on SAFETY, not throughput. A wrong auto-approval is far worse than an \
unnecessary escalation. When uncertain, ESCALATE.

== APPROVAL APPETITE (level: {level.value}) ==
{appetite}

== ABSOLUTE RULE ==
If the action matches any standing rule whose action is "require_approval" or "block", \
you MUST NOT auto-approve it — return ESCALATE. This is non-negotiable regardless of \
appetite.

== STANDING INSTINCT RULES (the pocket's policy, verbatim) ==
{_truncate(context.instinct_rules) if context.instinct_rules else "(none)"}

== PROPOSED ACTION ==
title: {_truncate(context.title)}
description: {_truncate(context.description)}
recommendation: {_truncate(context.recommendation)}
parked operation: {_truncate(context.parked_blob) if context.parked_blob else "(none)"}

== AGENT REASONING TRACE ==
{_truncate(context.reasoning_trace) if context.reasoning_trace else "(none)"}

== FABRIC SNAPSHOTS (decision-time state) ==
{_truncate(context.fabric_snapshots) if context.fabric_snapshots else "(none)"}

== OUTPUT (STRICT) ==
Reply with STRICT JSON only — no prose, no markdown fences, no commentary:
{{"verdict": "APPROVE" | "ESCALATE", "reasoning": "<one or two sentences>", \
"confidence": <0.0-1.0>}}
Use APPROVE only when you are confident the action is routine and within policy. Use \
ESCALATE for anything novel, risky, uncertain, or rule-flagged — the human reviews it. \
(This v1 has no auto-reject: even a clearly-bad action is ESCALATE'd, never silently \
killed, so the human always sees it.)"""


# ---------------------------------------------------------------------------
# Output parsing
# ---------------------------------------------------------------------------

_FENCE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL)


def parse_decision(raw: str) -> TriageDecision:
    """Parse the model's text into a TriageDecision — tolerating a fenced JSON
    block or stray text around a single top-level JSON object. Raises on
    unparseable output; the caller maps that to a fail-safe ESCALATE."""
    text = raw.strip()
    m = _FENCE.match(text)
    if m:
        text = m.group(1).strip()
    try:
        return TriageDecision.model_validate(json.loads(text))
    except (json.JSONDecodeError, ValueError):
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            return TriageDecision.model_validate(json.loads(text[start : end + 1]))
        raise


# ---------------------------------------------------------------------------
# The triage decision (deterministic gates + the one LLM call + fail-safe)
# ---------------------------------------------------------------------------


def _escalate(reason: str, confidence: float = 1.0) -> TriageDecision:
    return TriageDecision(verdict=TriageVerdict.ESCALATE, reasoning=reason, confidence=confidence)


async def triage_action(
    context: TriageContext,
    *,
    level: ApprovalLevel,
    llm: TriagerLlm | None = None,
) -> TriageDecision:
    """Classify one proposed action.

    Order of evaluation (each gate is a fail-safe toward ESCALATE):

    1. ``ASK`` level → ESCALATE without invoking the model (today's behaviour;
       the router actually short-circuits before calling this, but the gate is
       defensive).
    2. ``rule_flagged`` → ESCALATE. A template ESCALATE_APPROVAL / BLOCK rule
       matched; never auto-approvable at any level. Deterministic — the model
       is never consulted.
    3. The one LLM call. ANY transport error / timeout / unparseable output →
       ESCALATE (the action is never auto-approved on a failure).
    4. An APPROVE verdict below ``_MIN_CONFIDENCE`` → downgraded to ESCALATE.

    Only an APPROVE verdict from the model, at or above the confidence floor,
    on an unflagged action, at a non-ASK level, returns APPROVE.
    """
    if level == ApprovalLevel.ASK:
        return _escalate("Approval level is ASK — every action goes to the human.")

    if context.rule_flagged:
        return _escalate(
            "Action matched a standing ESCALATE_APPROVAL / BLOCK rule — never auto-approvable."
        )

    llm = llm or resolve_triager_llm()
    prompt = build_prompt(context, level)
    try:
        raw = await llm.triage(prompt=prompt, context=context)
        decision = parse_decision(raw)
    except Exception:  # noqa: BLE001 — any failure fails SAFE to ESCALATE
        logger.warning(
            "triager LLM call failed for action=%s (workspace=%s) — fail-safe ESCALATE",
            context.action_id,
            context.workspace_id,
            exc_info=True,
        )
        return _escalate("Triager model failure — fail-safe escalation to the human.")

    if decision.verdict == TriageVerdict.APPROVE and decision.confidence < _MIN_CONFIDENCE:
        return _escalate(
            f"Triager approved at low confidence ({decision.confidence:.2f} < "
            f"{_MIN_CONFIDENCE:.2f}) — escalating to the human.",
            confidence=decision.confidence,
        )
    return decision


# ---------------------------------------------------------------------------
# Orchestration — run the triage and auto-approve on APPROVE (fully audited)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TriageOutcome:
    """What the router does after the triage runs.

    * ``auto_approved`` — True when the action was auto-approved (and audited).
      The router returns ``action`` and does NOT notify the human.
    * ``action`` — the auto-approved Action (status APPROVED) on auto-approve;
      the original (unchanged) Action otherwise, so the router's response is
      byte-for-byte today's when nothing was auto-approved.
    * ``decision`` — the triager verdict, for logging / response metadata.
    """

    auto_approved: bool
    action: Any
    decision: TriageDecision


async def maybe_auto_approve(
    *,
    store: Any,
    action: Any,
    context: TriageContext,
    level: ApprovalLevel,
    llm: TriagerLlm | None = None,
) -> TriageOutcome:
    """Run the triage; on APPROVE auto-approve the action AND audit it.

    On any non-APPROVE verdict (the common, safe case) this returns the
    ORIGINAL action unchanged so the router falls through to the existing
    human-notification path with no behavioural change — the proposal sits in
    the tray exactly as it does today.

    On APPROVE:
      * ``store.auto_approve`` flips the action to APPROVED and writes the
        ``action_auto_approved`` audit row (``actor="system:triager"``) with the
        verdict + reasoning packed into the audit ``context`` — hash-chained
        identically to a human approval.
      * the same Decision-Graph chain events a human approval emits
        (``human.corrected`` + the approve-side ``policy.evaluated``) are
        emitted best-effort for any parked-write proposal, so an auto-approved
        write lands in the Decision Graph as a closed decision just like a human
        one. Chain-emit failures are swallowed (the journal is best-effort; the
        hash-chained ledger above is the source of truth).

    A non-APPROVE verdict NEVER mutates state.
    """
    decision = await triage_action(context, level=level, llm=llm)
    if decision.verdict != TriageVerdict.APPROVE:
        return TriageOutcome(auto_approved=False, action=action, decision=decision)

    try:
        approved = await store.auto_approve(
            str(getattr(action, "id", "")),
            verdict=decision.verdict.value,
            reasoning=decision.reasoning,
            confidence=decision.confidence,
        )
    except AuditChainError:
        # The tamper-evident ledger append failed — the store rolled the status
        # flip back, so the action is still PENDING. This is NOT a routine
        # fail-safe (an LLM timeout): the audit ledger is the governance
        # guarantee, and a chain write that fails is a ledger-integrity event.
        # Log LOUD at ERROR (distinct from the LLM-failure WARNING) and fall
        # through to the human path with the original action — we never claim an
        # auto-approval that wasn't audited.
        logger.error(
            "auto-approve ABORTED for action=%s (workspace=%s): the audit ledger "
            "append failed — action left PENDING for the human. This is a "
            "ledger-integrity event, not an LLM failure.",
            context.action_id,
            context.workspace_id,
            exc_info=True,
        )
        return TriageOutcome(
            auto_approved=False,
            action=action,
            decision=_escalate(
                "Auto-approve aborted: audit ledger append failed — escalating to the human."
            ),
        )
    if approved is None:
        # The action was not in PENDING (a concurrent path already resolved it)
        # — do not claim an auto-approval. Fall through to the human path with
        # the original action.
        logger.info(
            "triager APPROVE for action=%s but store.auto_approve no-op'd "
            "(not pending) — deferring to existing path",
            context.action_id,
        )
        return TriageOutcome(auto_approved=False, action=action, decision=decision)

    _emit_auto_approve_chain(approved=approved, context=context, decision=decision)
    return TriageOutcome(auto_approved=True, action=approved, decision=decision)


def _emit_auto_approve_chain(
    *,
    approved: Any,
    context: TriageContext,
    decision: TriageDecision,
) -> None:
    """Best-effort Decision-Graph emits for an auto-approved parked write.

    Mirrors the human approve path's emits (``human.corrected(accepted)`` +
    ``policy.evaluated(passed=True)``) so an auto-approved write closes its
    Decision-Graph chain the same way a human-approved one does. The actor is
    the triager (``system:triager``) carried through the ``user_id`` slot. Only
    a proposal that carries a chained ``_pocket_write`` blob emits — others have
    no chain to fold into. Failures are swallowed: the hash-chained audit ledger
    (written by ``store.auto_approve``) is the source of truth; these emits are
    the same best-effort projection the human path uses."""
    blob = context.parked_blob
    if not isinstance(blob, dict) or not blob.get("correlation_id"):
        return
    try:
        from pocketpaw_ee.instinct.chain_emitters import (
            _emit_human_corrected,
            _emit_policy_evaluated_approved,
        )

        human_event_id = _emit_human_corrected(
            blob=blob,
            action=approved,
            user_id="system:triager",
            workspace_id=context.workspace_id,
            disposition="accepted",
            note=f"auto-approved by triager: {decision.reasoning}"[:500],
        )
        _emit_policy_evaluated_approved(
            blob=blob,
            action=approved,
            user_id="system:triager",
            workspace_id=context.workspace_id,
            causation_event_id=human_event_id,
        )
    except Exception:  # noqa: BLE001 — chain emit is best-effort
        logger.warning(
            "auto-approve Decision-Graph emit failed for action=%s — "
            "hash-chained ledger is intact; projection will reconcile",
            context.action_id,
            exc_info=True,
        )


__all__ = [
    "ApprovalLevel",
    "ClaudeCliTriagerLlm",
    "TriageContext",
    "TriageDecision",
    "TriageOutcome",
    "TriageVerdict",
    "TriagerLlm",
    "build_prompt",
    "maybe_auto_approve",
    "parse_decision",
    "resolve_approval_level",
    "resolve_triager_llm",
    "triage_action",
]
