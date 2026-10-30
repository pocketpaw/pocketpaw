# events.py — SSE-event payload schemas for the pocket execution router.
# Created: 2026-05-22 (Increment 3) — defines ``PocketExecutionFrame``,
#   the single ``pocket_execution`` observability frame the router emits
#   per ``classify_and_route`` call. The frame is the Thesys-style "what
#   ran / what was skipped" readout: which tier handled the request, a
#   per-stage timeline (each stage's ``ran`` flag, latency, and
#   skipped-reason), total latency, and token spend. Tier 0/1 (the cheap
#   tiers) report ``tokens:{0,0}`` and mark the ``layout_build`` /
#   ``widget_render`` stages ``ran:false`` with reason "data-only change"
#   so a client can show the user exactly what the cheap path avoided.
#
# Pure Pydantic models — no I/O, no Beanie, no LLM. Mirrors the shape of
# ``cloud/chat/agent_schemas.py`` (the sibling SSE frame definitions).
"""SSE-event payload schemas for the pocket execution router."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

# The fixed stage vocabulary of a pocket-edit execution. ``classify`` and
# ``apply`` always run; ``layout_build`` and ``widget_render`` are the
# expensive stages a Tier-0/1 verdict skips (a data-only change needs no
# layout pass). Kept as a Literal so a typo in a stage name fails loudly.
ExecutionStageName = Literal[
    "classify",
    "apply",
    "layout_build",
    "widget_render",
]


class ExecutionStage(BaseModel):
    """One stage in a pocket-edit execution timeline.

    ``ran`` is True when the stage actually did work; when False,
    ``skipped_reason`` explains why (e.g. "data-only change" for the
    layout stages on a Tier-1 state edit). ``ms`` is the wall-clock cost
    of the stage — 0 for a skipped stage. ``detail`` carries an optional
    free-form note (the tier verdict's reasoning, an op count, …).
    """

    stage: ExecutionStageName
    ran: bool
    ms: int = Field(default=0, ge=0, description="Wall-clock cost of the stage, ms.")
    skipped_reason: str | None = Field(
        default=None,
        description="Why the stage did not run — set iff ``ran`` is False.",
    )
    detail: str | None = Field(
        default=None,
        description="Optional free-form note (verdict reasoning, op count, …).",
    )


class TokenSpend(BaseModel):
    """Prompt + completion token spend for one execution.

    Tier 0 and Tier 1 are pure / deterministic — no LLM runs — so they
    report ``{0, 0}``. Only a Tier-2 escalation spends tokens, and the
    specialist's own accounting is not threaded back into this frame in
    Increment 3 (it stays ``{0, 0}`` until the specialist surfaces a
    usage report); the frame still carries the field so the wire shape
    is stable.
    """

    prompt: int = Field(default=0, ge=0)
    completion: int = Field(default=0, ge=0)


class PocketExecutionFrame(BaseModel):
    """The ``pocket_execution`` SSE frame — one per routed request.

    Emitted once by ``router.classify_and_route`` after the chosen tier
    finishes. It is the router's observability surface: a client renders
    it as a "this edit took Tier 1, 14 ms, 0 tokens, skipped layout +
    render" readout.

    ``tier_chosen`` is the tier that actually executed (0 declarative,
    1 deterministic op, 2 specialist). ``stages`` is the ordered
    timeline. ``total_ms`` is the end-to-end router cost. ``tokens`` is
    the LLM spend — ``{0, 0}`` for the cheap tiers.
    """

    type: Literal["pocket_execution"] = "pocket_execution"
    request_id: str = Field(description="Correlation id for this routed request.")
    intent: str = Field(description="The natural-language edit intent that was routed.")
    tier_chosen: Literal[0, 1, 2] = Field(description="Tier that executed the request.")
    stages: list[ExecutionStage] = Field(
        default_factory=list,
        description="Ordered per-stage timeline.",
    )
    total_ms: int = Field(default=0, ge=0, description="End-to-end router cost, ms.")
    tokens: TokenSpend = Field(default_factory=TokenSpend)

    def to_wire(self) -> dict:
        """Serialize to the dict pushed via ``push_pocket_execution``."""
        return self.model_dump(mode="json")


__all__ = [
    "ExecutionStage",
    "ExecutionStageName",
    "PocketExecutionFrame",
    "TokenSpend",
]
