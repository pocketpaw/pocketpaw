"""Regression: every creation prompt teaches the $source mechanism."""

from __future__ import annotations

import pytest

from ee.ripple._pockets import (
    POCKET_CREATION_PROMPT_CLI,
    POCKET_CREATION_PROMPT_MCP,
)


@pytest.mark.parametrize(
    "prompt",
    [POCKET_CREATION_PROMPT_MCP, POCKET_CREATION_PROMPT_CLI],
    ids=["mcp", "cli"],
)
def test_creation_prompts_contain_state_sources_block(prompt: str) -> None:
    assert "<state-sources>" in prompt
    assert "</state-sources>" in prompt
    # The two v1 sources must be named so the agent learns the allowlist.
    assert "workspace.pockets" in prompt
    assert "workspace.members" in prompt
    # The literal marker syntax must appear so the agent emits the right shape.
    assert '"$source"' in prompt


@pytest.mark.parametrize(
    "prompt",
    [POCKET_CREATION_PROMPT_MCP, POCKET_CREATION_PROMPT_CLI],
    ids=["mcp", "cli"],
)
def test_state_sources_block_appears_before_examples(prompt: str) -> None:
    """Agents anchor on examples; the rule must come first so the example
    can demonstrate it."""
    sources_idx = prompt.index("<state-sources>")
    examples_idx = prompt.index("<creation-examples>")
    assert sources_idx < examples_idx
