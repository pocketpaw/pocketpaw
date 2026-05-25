# ee/pocketpaw_ee/cloud/decisions/explain/narrator.py
# Created: 2026-05-25 (RFC 07 Slice 3a) — the narrator that produces
#   grounded prose from a Decision + its trace. RFC 07 § narrator
#   constraints:
#     - every claim cites a decision id
#     - no speculation outside the trace
#     - verifier hook scans the narrative for decision-id citations;
#       sentences without a citation are stripped or flagged
#
#   Two backends:
#     - "llm"        — Sonnet (Anthropic) call with system prompt cached;
#                      the trace is compressed to ~1.5k tokens in the
#                      variable user message. Output streams when the SDK
#                      supports it (we collect and return the joined text
#                      since the route is request/response, not SSE).
#     - "templated"  — deterministic walk over the trace nodes. Produces
#                      one paragraph per Decision with citations. Used
#                      when the LLM call fails, the SDK is missing, no
#                      API key is configured, or the caller opts in via
#                      `backend_pref="templated"` on the pocket overlay.
#
#   The verifier is the same code path for both backends — stripping
#   ungrounded sentences from the templated narrator is a no-op (every
#   sentence carries a citation), but the symmetry catches future drift
#   if the template ever ships an ungrounded sentence by accident.
"""Narrator for the natural-language explain pipeline."""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from pocketpaw_ee.cloud.decisions.domain import Decision
from pocketpaw_ee.cloud.decisions.service import TraceResult

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Domain output
# ---------------------------------------------------------------------------


class Explanation(BaseModel):
    """The narrator's grounded output, with provenance for the verifier.

    ``decisions_walked`` is what the cache layer uses for invalidation —
    a new event folded into any of those decisions invalidates the cache
    entry.

    ``ungrounded_sentences`` carries every sentence the verifier flagged.
    Default behavior strips them from `narrative`; the list is kept so
    the caller (and tests) can see what the model tried to assert.
    """

    model_config = ConfigDict(frozen=False)

    narrative: str
    decisions_walked: list[UUID] = Field(default_factory=list)
    depth_reached: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    ungrounded_sentences: list[str] = Field(default_factory=list)
    backend_used: Literal["llm", "templated"] = "templated"


# ---------------------------------------------------------------------------
# Narrator system prompt (cache-able)
# ---------------------------------------------------------------------------

_NARRATOR_SYSTEM = """You explain decisions by walking a supplied decision graph.

Constraints — every output must follow these:
1. Every factual claim cites a decision id in square brackets, e.g. "[a1b2c3d4]". Use the short form (first 8 hex characters of the UUID).
2. You do not assert anything outside the supplied trace. If the trace does not contain a fact, you do not state it.
3. You produce ONE paragraph by default — 2-4 sentences, each citing the decision id it summarizes. Follow-up questions can expand.
4. You do not editorialize. You do not speculate about motives. You report what the decisions and their inputs/approvers/outcomes show.

If the trace is empty or has no decisions, reply with exactly: "No matching decision was found in the supplied trace."

Output the paragraph only — no preamble, no markdown headers, no closing remarks."""


_MAX_TRACE_TOKENS = 1500  # RFC 07 § compress trace to ~1.5k tokens


# ---------------------------------------------------------------------------
# Compression — trim the trace to a token budget for the LLM
# ---------------------------------------------------------------------------


def _compress_trace(root: Decision, trace: TraceResult) -> dict[str, Any]:
    """Reduce the trace to the fields the narrator needs.

    Drops large payload blobs. Keeps ids, timestamps, actor, intent,
    action, approvers, outcome status, instinct_policy, and the short
    label / kind for non-decision nodes. The result is JSON-serialisable
    and small enough to fit in the LLM budget without truncation.

    Decision count cap = 12. With each decision averaging ~120 tokens
    in this shape, 12 stays well under the 1.5k budget. The cap is a
    safety net — `trace()` already enforces depth + fanout caps upstream.
    """
    decisions: list[dict[str, Any]] = []
    others: list[dict[str, Any]] = []

    decision_count = 0
    for node_id, node in trace.nodes.items():
        if node.kind == "decision" and node.decision is not None:
            if decision_count >= 12:
                continue
            decisions.append(_compress_decision(node.decision))
            decision_count += 1
        else:
            others.append(
                {
                    "id": node.id,
                    "kind": node.kind,
                    "label": node.label,
                }
            )

    edges = [
        {
            "src": str(e.src_id),
            "target": e.target_id,
            "relation": e.relation,
            "weight": e.weight,
        }
        for e in trace.edges
    ]

    return {
        "root": _compress_decision(root),
        "decisions": decisions,
        "other_nodes": others,
        "edges": edges,
        "depth_reached": trace.depth_reached,
        "truncated": trace.truncated,
    }


def _compress_decision(decision: Decision) -> dict[str, Any]:
    """One-decision shape for the narrator. Short id is what the
    narrator's system prompt cites in brackets."""
    return {
        "id": str(decision.id),
        "short_id": str(decision.id)[:8],
        "ts": decision.ts.isoformat(),
        "decided_by": {
            "kind": decision.decided_by.kind,
            "id": decision.decided_by.id,
        },
        "intent": decision.intent,
        "action": decision.action,
        "scope_kind": decision.scope_kind,
        "pocket_id": decision.pocket_id,
        "inputs": [
            {
                "kind": i.kind,
                "id": i.id,
                "label": i.label,
            }
            for i in decision.inputs
        ],
        "approvers": [
            {
                "actor_id": a.actor.id,
                "actor_kind": a.actor.kind,
                "approved_at": a.approved_at.isoformat(),
            }
            for a in decision.approvers
        ],
        "instinct_policy": decision.instinct_policy,
        "instinct_policy_passed": decision.instinct_policy_passed,
        "outcome": (
            {
                "status": decision.outcome.status,
                "landed_at": (
                    decision.outcome.landed_at.isoformat()
                    if decision.outcome.landed_at
                    else None
                ),
                "metered": decision.outcome.metered,
            }
            if decision.outcome
            else None
        ),
    }


# ---------------------------------------------------------------------------
# Verifier — every sentence must cite a decision id from the trace
# ---------------------------------------------------------------------------

# Match `[abc12345]` (the 8-hex short-id form the narrator emits).
_CITATION_RE = re.compile(r"\[([0-9a-fA-F]{8})\]")
# Match end-of-sentence — period / exclamation / question.
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[\.!?])\s+(?=[A-Z\"“])")


def _verify_grounding(
    narrative: str,
    valid_short_ids: set[str],
) -> tuple[str, list[str]]:
    """Strip sentences that don't cite at least one decision id from the
    trace. Returns the cleaned narrative + the list of stripped sentences
    for the caller to surface.

    A sentence is grounded when it carries one or more `[xxxxxxxx]`
    citations AND every cited short id appears in `valid_short_ids`. A
    sentence that cites an id outside the trace is ungrounded (the
    model hallucinated a reference).

    Multi-sentence narratives are split on `.`, `!`, or `?` followed by
    whitespace and a capital. A single-sentence narrative without a
    citation is stripped entirely (returning ""); the caller decides
    whether to surface a placeholder.
    """
    text = narrative.strip()
    if not text:
        return "", []

    if not valid_short_ids:
        # No trace to ground against — drop everything.
        return "", [text]

    sentences = _SENTENCE_SPLIT_RE.split(text)
    kept: list[str] = []
    stripped: list[str] = []
    for raw in sentences:
        sent = raw.strip()
        if not sent:
            continue
        citations = _CITATION_RE.findall(sent)
        if not citations:
            stripped.append(sent)
            continue
        # Every citation must reference a real decision in the trace.
        if not all(c.lower() in valid_short_ids for c in citations):
            stripped.append(sent)
            continue
        kept.append(sent)

    cleaned = " ".join(kept).strip()
    return cleaned, stripped


# ---------------------------------------------------------------------------
# Public entry point — picks the backend and runs the pipeline
# ---------------------------------------------------------------------------


async def narrate_decision(
    root: Decision,
    trace: TraceResult,
    *,
    backend: Literal["llm", "templated"] = "llm",
    api_key: str | None = None,
    model: str = "claude-sonnet-4-7",
) -> Explanation:
    """Produce a grounded explanation of `root` + its trace.

    Args:
        root: The Decision the narrator centers the story on.
        trace: The depth-bounded BFS result from `DecisionGraph.trace`.
        backend: "llm" (default) or "templated". On any LLM failure the
            method falls back to the templated path so the response
            shape is identical to the caller.
        api_key: Override the Anthropic API key (test seam).
        model: Sonnet-class model id.

    Returns:
        An Explanation. Never raises — LLM errors fall through to the
        templated path; trace-empty inputs return the canned "no
        matching decision" sentence.
    """
    walked = _collect_walked(root, trace)
    valid_short_ids = {str(d_id)[:8].lower() for d_id in walked}

    if backend == "templated":
        narrative = _render_templated(root, trace)
        # Templated narratives are pre-grounded; verifier still runs for
        # symmetry so any future template drift surfaces in the test.
        cleaned, stripped = _verify_grounding(narrative, valid_short_ids)
        return Explanation(
            narrative=cleaned or narrative,
            decisions_walked=walked,
            depth_reached=trace.depth_reached,
            tokens_in=_approx_tokens(_compress_trace(root, trace)),
            tokens_out=len(narrative) // 4,
            ungrounded_sentences=stripped,
            backend_used="templated",
        )

    # LLM path — fall back on any error.
    if api_key is None:
        try:
            from pocketpaw.config import get_settings

            api_key = get_settings().anthropic_api_key
        except Exception:  # noqa: BLE001
            api_key = None

    if not api_key:
        return await narrate_decision(root, trace, backend="templated")

    try:
        from anthropic import AsyncAnthropic
    except ImportError:
        logger.info("anthropic SDK missing; narrator falling back to templated path")
        return await narrate_decision(root, trace, backend="templated")

    compressed = _compress_trace(root, trace)
    user_message = (
        "Decision graph trace (JSON):\n"
        + _json_dumps(compressed)
        + "\n\nProduce the grounded paragraph now."
    )

    try:
        client = AsyncAnthropic(api_key=api_key, timeout=20.0, max_retries=1)
        response = await client.messages.create(
            model=model,
            max_tokens=600,
            system=[
                {
                    "type": "text",
                    "text": _NARRATOR_SYSTEM,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": user_message}],
        )
    except Exception:  # noqa: BLE001
        logger.warning("narrator LLM call failed; falling back to templated", exc_info=True)
        return await narrate_decision(root, trace, backend="templated")

    try:
        raw_text = response.content[0].text
    except (AttributeError, IndexError):
        return await narrate_decision(root, trace, backend="templated")

    cleaned, stripped = _verify_grounding(raw_text, valid_short_ids)

    # Approximate token counts from the response shape; the SDK also
    # surfaces usage but we keep this provider-agnostic.
    tokens_in = getattr(response.usage, "input_tokens", _approx_tokens(compressed))
    tokens_out = getattr(response.usage, "output_tokens", len(raw_text) // 4)

    return Explanation(
        narrative=cleaned,
        decisions_walked=walked,
        depth_reached=trace.depth_reached,
        tokens_in=int(tokens_in),
        tokens_out=int(tokens_out),
        ungrounded_sentences=stripped,
        backend_used="llm",
    )


# ---------------------------------------------------------------------------
# Templated narrator — deterministic walk that mirrors the PoC
# ---------------------------------------------------------------------------


def _render_templated(root: Decision, trace: TraceResult) -> str:
    """Deterministic paragraph generator. Mirrors the structure of the
    PoC at /tmp/team-rfc07/explain/narrator.py `render_narrative` but
    enforces the [short-id] citation style the verifier checks for."""

    sentences: list[str] = []

    primary_input = next(
        (i for i in root.inputs if i.kind == "fabric_object"),
        root.inputs[0] if root.inputs else None,
    )
    target_label = (
        primary_input.label or primary_input.id
        if primary_input
        else "an unspecified target"
    )

    # 1. Headline — what / who / when
    root_short = str(root.id)[:8]
    if root.approvers:
        approver_names = ", ".join(_humanize_actor(a.actor.id) for a in root.approvers)
        first_at = root.approvers[0].approved_at
        sentences.append(
            f"{target_label} was approved by {approver_names} "
            f"on {_format_dt(first_at)} [{root_short}]."
        )
    else:
        sentences.append(
            f"The {root.action} action on {target_label} was decided "
            f"on {_format_dt(root.ts)} [{root_short}]."
        )

    # 2. Agent intent
    proposer = _humanize_actor(root.decided_by.id)
    sentences.append(
        f"The {proposer} agent proposed the action with intent: "
        f"{root.intent.strip()} [{root_short}]."
    )

    # 3. Precedents — cite each
    precedent_nodes: list[Decision] = []
    for pref in root.precedents:
        node = trace.nodes.get(str(pref.decision_id))
        if node and node.decision:
            precedent_nodes.append(node.decision)

    if precedent_nodes:
        cited = "; ".join(_describe_precedent(p) for p in precedent_nodes[:3])
        suffix = "s" if len(precedent_nodes) != 1 else ""
        sentences.append(
            f"This drew on {len(precedent_nodes)} precedent decision{suffix}: {cited}."
        )

    # 4. Policy
    if root.instinct_policy:
        passed = "passed" if root.instinct_policy_passed else "blocked"
        sentences.append(
            f"The Instinct policy `{root.instinct_policy}` {passed} at decision time "
            f"[{root_short}]."
        )

    # 5. Outcome
    if root.outcome and root.outcome.status == "landed":
        landed_phrase = (
            f" at {_format_dt(root.outcome.landed_at)}"
            if root.outcome.landed_at
            else ""
        )
        metered_phrase = " and was metered" if root.outcome.metered else ""
        sentences.append(
            f"The outcome landed{landed_phrase}{metered_phrase} [{root_short}]."
        )
    elif root.outcome and root.outcome.status == "rejected":
        sentences.append(
            f"The decision did not produce a landed outcome — it was rejected [{root_short}]."
        )

    return " ".join(sentences)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _collect_walked(root: Decision, trace: TraceResult) -> list[UUID]:
    """Every Decision id reachable in the trace — used for cache
    invalidation. Root is always first so the cache reverse index has a
    stable primary key."""
    walked: list[UUID] = [root.id]
    seen = {root.id}
    for _, node in trace.nodes.items():
        if node.kind == "decision" and node.decision is not None:
            if node.decision.id not in seen:
                walked.append(node.decision.id)
                seen.add(node.decision.id)
    return walked


def _humanize_actor(actor_id: str) -> str:
    """Turn an actor id into a render-friendly label.

    Handles the three prefix conventions in use: `did:soul:<name>`,
    `user:<name>`, and `system:<name>`. A bare id without a colon
    prefix (the cloud convention where `Actor.kind` carries the
    namespace) is title-cased so "prakash" → "Prakash". A name with
    underscores ("renewal_specialist") becomes "renewal specialist".
    """
    if actor_id.startswith("did:soul:"):
        return actor_id.split(":", 2)[2].replace("_", " ")
    if actor_id.startswith("user:"):
        return actor_id.split(":", 1)[1].replace("_", " ").title()
    if actor_id.startswith("system:"):
        return actor_id
    if ":" not in actor_id:
        # Bare id — Actor.kind carries the namespace; here we only have
        # the id. Title-case so user ids look like names.
        return actor_id.replace("_", " ").title()
    return actor_id


def _describe_precedent(pre: Decision) -> str:
    obj = next((i for i in pre.inputs if i.kind == "fabric_object"), None)
    descriptor = obj.label or obj.id if obj else pre.intent[:40]
    when = pre.ts.strftime("%Y-%m-%d")
    short = str(pre.id)[:8]
    return f"{descriptor}, decided {when} [{short}]"


def _format_dt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d at %H:%M UTC")


def _approx_tokens(payload: dict[str, Any]) -> int:
    """Rough token approximation: 4 characters per token."""
    return len(_json_dumps(payload)) // 4


def _json_dumps(payload: dict[str, Any]) -> str:
    import json

    return json.dumps(payload, separators=(",", ":"), default=str)


__all__ = ["Explanation", "narrate_decision"]
