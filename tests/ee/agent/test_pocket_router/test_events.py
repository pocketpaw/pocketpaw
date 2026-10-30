# test_events.py — Tests for the pocket-router SSE event schema
#   (Increment 3).
# Created: 2026-05-22 — pins ``PocketExecutionFrame`` / ``ExecutionStage``:
#   the wire shape, the ``tier_chosen`` bound, and the skipped-stage
#   contract that the ``pocket_execution`` frame carries.
"""Schema tests for the pocket-router ``pocket_execution`` SSE frame."""

from __future__ import annotations

import pytest
from pocketpaw_ee.agent.pocket_router.events import (
    ExecutionStage,
    PocketExecutionFrame,
    TokenSpend,
)
from pydantic import ValidationError


def test_execution_stage_ran():
    s = ExecutionStage(stage="classify", ran=True, ms=4, detail="Tier 1")
    assert s.ran is True
    assert s.skipped_reason is None


def test_execution_stage_skipped():
    s = ExecutionStage(stage="layout_build", ran=False, skipped_reason="data-only change")
    assert s.ran is False
    assert s.ms == 0
    assert s.skipped_reason == "data-only change"


def test_token_spend_defaults_to_zero():
    t = TokenSpend()
    assert t.prompt == 0
    assert t.completion == 0


def test_execution_frame_to_wire_round_trips():
    frame = PocketExecutionFrame(
        request_id="pr_abc123",
        intent="mark task 1 done",
        tier_chosen=1,
        stages=[
            ExecutionStage(stage="classify", ran=True, ms=2),
            ExecutionStage(stage="apply", ran=True, ms=11),
            ExecutionStage(stage="layout_build", ran=False, skipped_reason="data-only change"),
            ExecutionStage(stage="widget_render", ran=False, skipped_reason="data-only change"),
        ],
        total_ms=13,
    )
    wire = frame.to_wire()
    assert wire["type"] == "pocket_execution"
    assert wire["request_id"] == "pr_abc123"
    assert wire["tier_chosen"] == 1
    assert wire["tokens"] == {"prompt": 0, "completion": 0}
    assert len(wire["stages"]) == 4
    skipped = [s for s in wire["stages"] if not s["ran"]]
    assert {s["stage"] for s in skipped} == {"layout_build", "widget_render"}


@pytest.mark.parametrize("bad_tier", [3, -1, 5])
def test_tier_chosen_rejects_out_of_range(bad_tier):
    with pytest.raises(ValidationError):
        PocketExecutionFrame(request_id="r", intent="i", tier_chosen=bad_tier)


@pytest.mark.parametrize("tier", [0, 1, 2])
def test_tier_chosen_accepts_valid_tiers(tier):
    frame = PocketExecutionFrame(request_id="r", intent="i", tier_chosen=tier)
    assert frame.tier_chosen == tier
