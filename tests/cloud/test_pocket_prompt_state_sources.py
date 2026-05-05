"""Regression: every creation AND interaction prompt teaches the $source mechanism."""

from __future__ import annotations

import pytest

from ee.ripple._pockets import (
    POCKET_CREATION_PROMPT_CLI,
    POCKET_CREATION_PROMPT_MCP,
    POCKET_INTERACTION_PROMPT_CLI,
    POCKET_INTERACTION_PROMPT_MCP,
)

_ALL_PROMPTS = [
    POCKET_CREATION_PROMPT_MCP,
    POCKET_CREATION_PROMPT_CLI,
    POCKET_INTERACTION_PROMPT_MCP,
    POCKET_INTERACTION_PROMPT_CLI,
]
_ALL_IDS = ["create-mcp", "create-cli", "interact-mcp", "interact-cli"]


@pytest.mark.parametrize("prompt", _ALL_PROMPTS, ids=_ALL_IDS)
def test_all_prompts_contain_state_sources_block(prompt: str) -> None:
    """Both creation and interaction agents must know about $source —
    edits to existing pockets need the same vocabulary as new builds."""
    assert "<state-sources>" in prompt
    assert "</state-sources>" in prompt
    assert "workspace.pockets" in prompt
    assert "workspace.members" in prompt
    assert '"$source"' in prompt


@pytest.mark.parametrize(
    "prompt",
    [POCKET_CREATION_PROMPT_MCP, POCKET_CREATION_PROMPT_CLI],
    ids=["mcp", "cli"],
)
def test_state_sources_block_appears_before_examples(prompt: str) -> None:
    """Agents anchor on examples; the rule must come first so the example
    can demonstrate it. Creation prompts only — interaction prompts have
    no examples block."""
    sources_idx = prompt.index("<state-sources>")
    examples_idx = prompt.index("<creation-examples>")
    assert sources_idx < examples_idx
