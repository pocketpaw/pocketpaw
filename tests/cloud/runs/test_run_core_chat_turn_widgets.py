# tests/cloud/runs/test_run_core_chat_turn_widgets.py
# Pins how the Paw OS chat agent learns which purpose-built Ripple widget fits a
# turn: chat_ripple_prompt carries the purpose-built-first rule and the figure
# and 3D model ids, chat_turn_widgets builds the per-turn prop lines from Paw
# Bar's pick_widgets/full_line, and run_core hands them to AgentPool.run in
# knowledge_context, never in the keyed instructions layer (that would rebuild
# the warm client every turn). Mutation plan: tests/mutations/chat_turn_widgets.json.

from __future__ import annotations

from typing import Any

import pytest
from pocketpaw_ee.cloud.chat.agent_service import (
    PURPOSE_BUILT_FIRST_RULE,
    ScopeContext,
    ScopeKind,
    chat_ripple_prompt,
    chat_turn_widgets,
)
from pocketpaw_ee.cloud.chat.runs import run_core
from pocketpaw_ee.paw_bar.card_spec import RIPPLE_PROFILE
from pocketpaw_ee.paw_bar.concierge_runtime import ripple_rules

BIKE = "How do bike gears work? Show me the drivetrain and let me change the gear."


def test_the_chat_prompt_puts_purpose_built_widgets_first():
    p = chat_ripple_prompt()
    assert PURPOSE_BUILT_FIRST_RULE in p
    assert "Compose from flex, grid, slider, each and inputs only when no purpose-built" in p
    # Its node budget applies only to a card built from primitives.
    assert "Only when no purpose-built widget fits and you build from primitives" in p
    assert "Aim for about 25 to 90 nodes when building from primitives" not in p


def test_paw_bar_keeps_its_node_budget_line():
    assert any(
        "Aim for about 25 to 90 nodes when building from primitives" in r
        for r in ripple_rules(RIPPLE_PROFILE)
    )


def test_the_chat_prompt_lists_figure_and_model_ids():
    p = chat_ripple_prompt()
    assert "bike-gears (" in p and "heart (" in p  # figure ids with titles
    assert "- model-viewer {asset" in p and "t-rex(jaws," in p  # assets with anchors


def test_turn_widgets_carry_the_named_figure_in_full():
    block = chat_turn_widgets(chat_ripple_prompt(), BIKE)
    assert block.startswith("## Widgets for this message")
    assert "- figure {figure" in block
    # The named figure with its sliders and anchors, not just its title.
    assert "bike-gears(" in block and "anchors " in block


def test_turn_widgets_need_the_chat_prompt_and_a_message():
    assert chat_turn_widgets("some other surface's instructions", BIKE) == ""
    assert chat_turn_widgets(chat_ripple_prompt(), "") == ""


class _CapturingPool:
    def __init__(self) -> None:
        self.kwargs: dict[str, Any] = {}

    async def get(self, _agent_id):
        return type("Inst", (), {"config": {"backend": "claude_agent_sdk"}})()

    def run(self, _agent_id, prompt, _session_key, **kwargs):
        self.kwargs = kwargs

        async def _empty():
            return
            yield  # pragma: no cover

        return _empty()


async def _drive(monkeypatch, instructions: str) -> _CapturingPool:
    pool = _CapturingPool()
    monkeypatch.setattr(run_core, "get_agent_pool", lambda: pool)

    async def _fake_knowledge(_ctx, **kwargs):
        return "KB"

    monkeypatch.setattr(run_core, "build_knowledge_context", _fake_knowledge)
    monkeypatch.setattr(run_core, "build_behavior_instructions", lambda *a, **k: instructions)
    monkeypatch.setattr(run_core, "attach_sse_event_sink", lambda *a, **k: None)
    monkeypatch.setattr(run_core, "attach_agent_identity", lambda **k: None)
    monkeypatch.setattr(run_core, "detach_sse_event_sink", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(run_core, "detach_agent_identity", lambda *a, **k: None, raising=False)

    async def _is_cancelled():
        return False

    ctx = ScopeContext(
        kind=ScopeKind.SESSION,
        scope_id="s1",
        workspace_id="w1",
        user_id="u1",
        members=["u1"],
        target_agent_id="a1",
    )
    async for _ in run_core._drive_agent_loop(
        ctx,
        user_content=BIKE,
        attachments_in=None,
        mentions_in=None,
        history=[],
        is_cancelled=_is_cancelled,
        emit_stream_start=False,
    ):
        pass
    return pool


@pytest.mark.asyncio
async def test_a_chat_turn_gets_its_widget_picks_below_the_cache_cut(monkeypatch):
    instructions = chat_ripple_prompt()
    pool = await _drive(monkeypatch, instructions)
    knowledge = pool.kwargs.get("knowledge_context") or ""
    assert knowledge.startswith("KB\n\n## Widgets for this message")
    assert "bike-gears(" in knowledge
    # The keyed layer is untouched, so the warm client's cache key holds.
    assert pool.kwargs.get("instructions") == instructions


@pytest.mark.asyncio
async def test_a_turn_without_the_chat_card_prompt_gets_no_picks(monkeypatch):
    pool = await _drive(monkeypatch, "INSTR")
    assert pool.kwargs.get("knowledge_context") == "KB"
