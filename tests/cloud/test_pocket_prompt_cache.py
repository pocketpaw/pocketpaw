"""Regression guard: the interaction prompt must keep its per-session
``__POCKET_ID__`` substitution in a trailing block, not embedded in the
bulk of the prompt.

Why this matters: DeepSeek V3+ and Anthropic prompt caching both work
on the longest common prefix. Every byte of dynamic content in the
middle of the prompt invalidates caching for everything after it.

A regression here is invisible at unit-test level (the prompt still
renders fine) but kills cache-hit rate in production, multiplying
input-token cost and latency 10x on warm turns.
"""

from __future__ import annotations

import pytest

from ee.ripple import (
    POCKET_EDIT_SPECIALIST_PROMPT_CLI,
    POCKET_EDIT_SPECIALIST_PROMPT_MCP,
    POCKET_ID_TOKEN,
)

# The edit-specialist prompt is intentionally slim now (one-shot edit
# contract: the parent agent fetches the pocket and sends the full
# payload; the specialist has no read tool). Prompt caching still
# matters but the entire prompt is small enough that cache hits aren't
# load-bearing the way they were under the previous ~40k-char heavy
# variant. This floor only guards against POCKET_ID_TOKEN drifting up
# into the body of the prompt — keep it modest. Chars, not tokens.
_MIN_CACHEABLE_CHARS = 1_500


@pytest.mark.parametrize(
    ("name", "prompt"),
    [
        ("mcp", POCKET_EDIT_SPECIALIST_PROMPT_MCP),
        ("cli", POCKET_EDIT_SPECIALIST_PROMPT_CLI),
    ],
    ids=["mcp", "cli"],
)
def test_pocket_id_token_appears_only_once_at_end(name: str, prompt: str) -> None:
    """The pocket id substitution must appear exactly once, in the trailing
    block. Multiple occurrences fragment the cacheable prefix; an early
    occurrence collapses cacheable prefix to a few hundred tokens."""
    occurrences = [i for i in range(len(prompt)) if prompt.startswith(POCKET_ID_TOKEN, i)]
    assert len(occurrences) == 1, (
        f"{name} prompt has {len(occurrences)} occurrences of POCKET_ID_TOKEN; "
        "must be exactly 1 (in the trailing <current-pocket> block)"
    )
    first = occurrences[0]
    cacheable_chars = first
    assert cacheable_chars >= _MIN_CACHEABLE_CHARS, (
        f"{name} prompt: cacheable prefix only {cacheable_chars} chars "
        f"(< {_MIN_CACHEABLE_CHARS}). Something moved POCKET_ID_TOKEN earlier "
        "in the prompt — DeepSeek/Anthropic cache hit rate will crater."
    )
    # And it must be in the trailing region — within the last 250 chars,
    # which fits the `<current-pocket>` template comfortably. A percentage
    # threshold worked for the old ~60k-char prompt but breaks down on
    # a 2k-char prompt where the trailing template itself is ~170 chars.
    trailing_threshold = max(len(prompt) - 250, 0)
    assert first >= trailing_threshold, (
        f"{name} prompt: POCKET_ID_TOKEN sits at offset {first} of "
        f"{len(prompt)} ({first / len(prompt) * 100:.1f}%). It must live in "
        f"the trailing <current-pocket> block (offset >= {trailing_threshold})."
    )


@pytest.mark.parametrize(
    ("name", "prompt"),
    [
        ("mcp", POCKET_EDIT_SPECIALIST_PROMPT_MCP),
        ("cli", POCKET_EDIT_SPECIALIST_PROMPT_CLI),
    ],
    ids=["mcp", "cli"],
)
def test_current_pocket_block_is_at_end(name: str, prompt: str) -> None:
    """The literal trailing block must close out the prompt — nothing of
    substance after it (the assembly trailing newline is fine)."""
    assert "<current-pocket>" in prompt, f"{name}: trailing block missing"
    assert "</current-pocket>" in prompt, f"{name}: trailing block close missing"
    after_close = prompt.split("</current-pocket>", 1)[1]
    # Allow only whitespace after the close tag.
    assert after_close.strip() == "", (
        f"{name}: content after </current-pocket> would break cache stability "
        f"of any prompt rendered before {{pocket-id}} substitution. "
        f"Found: {after_close!r}"
    )


def test_substitution_yields_a_well_formed_prompt() -> None:
    """Substituting the token in produces a coherent prompt the agent can
    actually read. Sanity-only — no rendering, no tool registration."""
    sample_id = "507f1f77bcf86cd799439011"
    rendered = POCKET_EDIT_SPECIALIST_PROMPT_MCP.replace(POCKET_ID_TOKEN, sample_id)
    assert POCKET_ID_TOKEN not in rendered
    assert sample_id in rendered
    # The workflow block must reference the trailing block so the agent
    # knows where to find its id.
    assert "<current-pocket>" in rendered
