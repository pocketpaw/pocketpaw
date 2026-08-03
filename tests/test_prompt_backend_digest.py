# tests/test_prompt_backend_digest.py
# Created: 2026-08-03 (PA-6, feat/prompt-assembler-seam) — pins the cutover of
# the three prompt-caching backends from their own hand-rolled keys onto the
# assembler's `stable_digest`.
#
# WHAT PA-6 MEASURED, because these tests only make sense against the numbers.
# 8 ordinary turns, a soul birthed through PocketPaw's own path, the same
# conversation PA-3b measured on, scored over the 7 turn BOUNDARIES (turn 1 is
# always a cold write and is never counted):
#
#   assembled prompt TEXT              0/7 held   — it moves every turn, by design
#   sha256(text)  (#1842's key)        0/7 held   — so deep_agents/langchain
#                                                   recompiled the graph EVERY turn
#   _behavior_prefix digest            1/7 held   — the claude_sdk warm client
#                                                   rebuilt on 6 of 7 boundaries
#   stable_digest                      7/7 held   — one key across all 8 turns
#
# The `_behavior_prefix` number is the one that matters for reading PA-6's
# success metric: the baseline it improves on was 14%, not "high". The prefix
# strips `# Key Knowledge` but NOT `## Self-Understanding`, which renders above
# it inside `to_system_prompt()` and moved on 6 of 7 boundaries.
#
# THE CONSEQUENCE, stated here rather than left to be discovered: a reused graph
# or warm client keeps the prompt it was built with, so a turn that changed only
# the per-message soul recall now runs against the PREVIOUS turn's recall. That
# is exactly what PA-3 decided when it gave the retrieval layer `cache_key=None`,
# and what `claude_sdk`'s volatile markers have always done. What a reused object
# can never carry is a different agent, surface, override or instruction set.
#
# EACH TEST NAMES THE MUTATION THAT BREAKS IT, and each mutation was run.

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from pocketpaw.agents.deep_agents import DeepAgentsBackend, _prompt_identity
from pocketpaw.agents.langchain_react import LangchainReactBackend
from pocketpaw.agents.pool import _accepts_prompt_digest
from pocketpaw.agents.pydantic_ai import PydanticAIBackend
from pocketpaw.config import Settings

pytestmark = pytest.mark.asyncio

# Two prompts that differ ONLY in the volatile tail — the shape a turn takes when
# nothing but the user's message changed. Same digest, different bytes.
_TURN_1 = "You are Paw.\n\n## THE LAW\nNever fabricate.\n\n## Relevant Past Memories\nasyncpg"
_TURN_2 = "You are Paw.\n\n## THE LAW\nNever fabricate.\n\n## Relevant Past Memories\nrollback"
_DIGEST = "aaaaaaaaaaaaaaaa"
_OTHER_DIGEST = "bbbbbbbbbbbbbbbb"


def _deep_backend() -> DeepAgentsBackend:
    backend = DeepAgentsBackend(Settings())
    backend._custom_tools = []
    return backend


def _react_backend() -> LangchainReactBackend:
    backend = LangchainReactBackend(Settings(deep_agents_model="anthropic:claude-sonnet-4-6"))
    backend._custom_tools = []
    return backend


# ---------------------------------------------------------------------------
# 1 — the digest, where a caller has one
# ---------------------------------------------------------------------------


async def test_a_moved_recall_no_longer_recompiles_the_deep_agents_graph():
    """The cache half of the cutover, on the backend that pays most for it.

    Two turns whose prompts differ only in the per-message recall carry the SAME
    `stable_digest` — the retrieval layer declares itself unkeyed — so the
    compiled graph is reused. Under #1842's `sha256(instructions)` these two are
    different keys and the graph recompiles, which is what the 0/7 measurement
    at the top of this file records.

    THE MUTATION THAT BREAKS THIS: make `_prompt_identity` ignore the digest and
    always hash the text. Run: the two calls produced two graphs and `a1 is a2`
    failed.
    """
    backend = _deep_backend()

    with patch("deepagents.create_deep_agent", side_effect=lambda **kw: MagicMock()):
        a1 = backend._get_or_create_agent(MagicMock(), _TURN_1, system_prompt_digest=_DIGEST)
        a2 = backend._get_or_create_agent(MagicMock(), _TURN_2, system_prompt_digest=_DIGEST)

    assert a1 is a2, "a moved recall must not cost a recompile once the digest keys the graph"


async def test_a_moved_digest_still_recompiles_the_deep_agents_graph():
    """The freshness half, and the one a careless cache fix breaks.

    Identical prompt bytes under two different digests must NOT share a graph:
    the digest is the identity claim, and two identities that happen to render
    alike this turn are still two agents. (`identity` keys on the agent and lets
    soul counters drift beneath it, so equal text under different keys is a real
    state, not a contrived one.)

    THE MUTATION THAT BREAKS THIS: drop `_prompt_identity` from `model_key`
    entirely. Run: one graph served both digests.
    """
    backend = _deep_backend()

    with patch("deepagents.create_deep_agent", side_effect=lambda **kw: MagicMock()):
        a1 = backend._get_or_create_agent(MagicMock(), _TURN_1, system_prompt_digest=_DIGEST)
        a2 = backend._get_or_create_agent(MagicMock(), _TURN_1, system_prompt_digest=_OTHER_DIGEST)

    assert a1 is not a2, "a changed identity must reach a backend that bakes the prompt in"


async def test_the_react_graph_follows_the_digest_in_both_directions():
    """The same pair on `langchain_react`, whose key is a different shape.

    It carries no skills, memory or pocket flag — `create_react_agent` takes
    none — so this is the one key where the prompt identity is half of it.

    THE MUTATION THAT BREAKS THIS: revert this file's `_prompt_identity` import
    to the local `hashlib.sha256(instructions)` it replaced. Run: the reuse
    assertion failed while the rebuild assertion still passed, which is why both
    directions are asserted in one test.
    """
    backend = _react_backend()

    with patch(
        "langgraph.prebuilt.create_react_agent",
        side_effect=lambda **kw: MagicMock(),
    ):
        same = backend._get_or_create_agent(MagicMock(), _TURN_1, [], system_prompt_digest=_DIGEST)
        reused = backend._get_or_create_agent(
            MagicMock(), _TURN_2, [], system_prompt_digest=_DIGEST
        )
        rebuilt = backend._get_or_create_agent(
            MagicMock(), _TURN_2, [], system_prompt_digest=_OTHER_DIGEST
        )

    assert same is reused
    assert rebuilt is not reused


# ---------------------------------------------------------------------------
# 2 — the text hash, where a caller has no digest
# ---------------------------------------------------------------------------


async def test_a_caller_without_a_digest_still_keys_on_the_prompt_text():
    """The channel path, which does not reach the assembler until PA-7.

    `AgentLoop` builds its prompt in `AgentContextBuilder` and calls `run`
    without a digest. If PA-6 had simply DELETED #1842's text hash, those callers
    would key on nothing that can see the prompt — #1842 restored on Telegram,
    Discord, Slack and the CLI, on the same day it was declared fixed for the
    cloud. So the hash survives as the no-digest fallback.

    THE MUTATION THAT BREAKS THIS: return a constant from `_prompt_identity` when
    the digest is empty. Run: the two graphs collapsed into one and the assertion
    failed, which is the leak this asserts against.
    """
    backend = _deep_backend()

    with patch("deepagents.create_deep_agent", side_effect=lambda **kw: MagicMock()):
        a1 = backend._get_or_create_agent(MagicMock(), "SITE: Acme Dental")
        a2 = backend._get_or_create_agent(MagicMock(), "CHAT: fresh session")

    assert a1 is not a2, "a digest-less caller lost its only defence against a stale prompt"


async def test_a_digest_and_a_text_hash_cannot_be_mistaken_for_each_other():
    """The two claims are different, so they may not share a namespace.

    `d:` says "these LAYERS are the same"; `t:` says "these BYTES are the same".
    A caller that gained a digest mid-life (a deploy that ports it) must read as
    a changed prompt and rebuild once, not silently match a key minted under the
    other rule.

    THE MUTATION THAT BREAKS THIS: drop the `d:` / `t:` prefixes. Run: with the
    prefixes gone the two forms still differed here by luck of the hash width, so
    the assertion that actually bites is the one on the PREFIX, not on inequality.
    """
    with_digest = _prompt_identity("some prompt", "cafebabecafebabe")
    without = _prompt_identity("some prompt", "")

    assert with_digest.startswith("d:")
    assert without.startswith("t:")
    assert with_digest != without
    # the digest must be the whole claim — the text must not leak into it, or a
    # moved recall would move the key again and the cutover would buy nothing
    assert _prompt_identity("some prompt", "cafebabecafebabe") == _prompt_identity(
        "a completely different prompt", "cafebabecafebabe"
    )


# ---------------------------------------------------------------------------
# 3 — the pool has to be able to SEE that these backends take a digest
# ---------------------------------------------------------------------------


async def test_the_three_ported_backends_declare_the_digest_on_run():
    """`AgentPool` decides by SIGNATURE, not by a list of class names.

    `_accepts_prompt_digest` inspects `run` and refuses `**kwargs`, so a backend
    that swallowed the digest would look ported while keying on nothing. This is
    the test that fails if a future refactor collapses one of these signatures
    into `**kwargs` — the failure mode is silent otherwise, because everything
    still runs and only the cache stops working.
    """
    for backend_cls in (DeepAgentsBackend, LangchainReactBackend, PydanticAIBackend):
        assert _accepts_prompt_digest(backend_cls), f"{backend_cls.__name__} stopped declaring it"
