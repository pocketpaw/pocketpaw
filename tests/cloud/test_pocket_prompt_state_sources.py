"""Regression: every prompt that builds rippleSpec teaches the $source mechanism.

Post-Task-11: the calling-agent creation prompts only carry the STEP 0
delegation block, so the heavy ``<state-sources>`` / ``<creation-examples>``
content moved onto the specialist's own prompt
(``POCKET_SPECIALIST_PROMPT``).

Post-#1163: the prompt-split moved the heavy mutation block off the main
chat agent's ``POCKET_INTERACTION_PROMPT_*`` (now a slim delegation rule)
and onto the edit specialist's ``POCKET_EDIT_SPECIALIST_PROMPT_*``. The
edit specialist is the agent that actually edits rippleSpec directly, so
that is the prompt that must carry ``<state-sources>``.
"""

from __future__ import annotations

import pytest

from pocketpaw.ripple._pockets import (
    POCKET_EDIT_SPECIALIST_PROMPT_CLI,
    POCKET_EDIT_SPECIALIST_PROMPT_MCP,
    POCKET_SPECIALIST_PROMPT,
)

_PROMPTS_WITH_SOURCES = [
    POCKET_SPECIALIST_PROMPT,
    POCKET_EDIT_SPECIALIST_PROMPT_MCP,
    POCKET_EDIT_SPECIALIST_PROMPT_CLI,
]
_IDS = ["specialist", "edit-mcp", "edit-cli"]


@pytest.mark.parametrize("prompt", _PROMPTS_WITH_SOURCES, ids=_IDS)
def test_prompts_that_build_specs_contain_state_sources_block(prompt: str) -> None:
    """Specialist (creates) and interaction (edits) agents must know about
    $source — they're the ones authoring rippleSpec."""
    assert "<state-sources>" in prompt
    assert "</state-sources>" in prompt
    assert "workspace.pockets" in prompt
    assert "workspace.members" in prompt
    assert '"$source"' in prompt


def test_state_sources_block_appears_before_examples_in_specialist() -> None:
    """Agents anchor on examples; the rule must come first so the example
    can demonstrate it. Specialist prompt only — interaction prompts have
    no examples block, calling-agent creation prompts have neither."""
    sources_idx = POCKET_SPECIALIST_PROMPT.index("<state-sources>")
    examples_idx = POCKET_SPECIALIST_PROMPT.index("<creation-examples>")
    assert sources_idx < examples_idx
