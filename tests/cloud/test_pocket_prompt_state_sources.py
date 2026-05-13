"""Regression: every prompt that AUTHORS a fresh rippleSpec teaches the
``$source`` mechanism.

Post one-shot edit redesign: the parent interaction prompts delegate
(no authoring) and the edit specialist prompt only applies granular
ops to a payload the parent already fetched (no fresh authoring) — so
``<state-sources>`` is only required on the create specialist's prompt,
which is the one drafting brand-new ``rippleSpec`` from scratch.
"""

from __future__ import annotations

from ee.ripple._pockets import POCKET_SPECIALIST_PROMPT


def test_specialist_prompt_contains_state_sources_block() -> None:
    """The create specialist is the only agent authoring fresh rippleSpec
    from scratch, so it must know about $source markers."""
    prompt = POCKET_SPECIALIST_PROMPT
    assert "<state-sources>" in prompt
    assert "</state-sources>" in prompt
    assert "workspace.pockets" in prompt
    assert "workspace.members" in prompt
    assert '"$source"' in prompt


def test_state_sources_block_appears_before_canonical_shapes_in_specialist() -> None:
    """Agents anchor on concrete props; the state-sources rule must come
    first so the canonical-shapes block can demonstrate $source seeds
    in context. The slim specialist prompt dropped the standalone
    examples block — CANONICAL_SHAPES carries the worked examples now,
    so we anchor the ordering check to it instead."""
    sources_idx = POCKET_SPECIALIST_PROMPT.index("<state-sources>")
    shapes_idx = POCKET_SPECIALIST_PROMPT.index("CANONICAL SHAPES")
    assert sources_idx < shapes_idx
