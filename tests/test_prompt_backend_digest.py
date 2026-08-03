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

from pocketpaw.agents.claude_sdk import ClaudeSDKBackend
from pocketpaw.agents.deep_agents import DeepAgentsBackend, _prompt_identity
from pocketpaw.agents.langchain_react import LangchainReactBackend
from pocketpaw.agents.pool import _accepts_prompt_digest, _accepts_prompt_digest_kwarg
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
    assert _accepts_prompt_digest(ClaudeSDKBackend), "claude_sdk stopped declaring it"


# ---------------------------------------------------------------------------
# 4 — the claude_sdk warm client, which had a key before PA-6 and a worse one
# ---------------------------------------------------------------------------

# A soul-shaped identity block with the two sections PA-3b measured drifting, in
# the positions `to_system_prompt()` renders them — MID-block, above the
# `# Key Knowledge` counters that `_behavior_prefix` excises. That position is
# the whole point: the strip never reaches them, so they stayed in the prefix.
_SOUL_BEFORE = (
    "You are Paw.\n\n"
    "## Current State\nMood: satisfied | Energy: 84% | Focus: high\n\n"
    "## Self-Understanding\n\n- synchronous database (high confidence, 35 supporting interactions)"
    "\n\n# Key Knowledge\n- Bond level: 54.1/100\n- Memories: 18"
    "\n\n## THE LAW\nNever fabricate a tool result."
)
_SOUL_AFTER = _SOUL_BEFORE.replace("35 supporting", "36 supporting").replace(
    "Bond level: 54.1/100", "Bond level: 54.9/100"
)


def _opts(system_prompt: str):
    from types import SimpleNamespace

    return SimpleNamespace(
        model="claude-x", allowed_tools=["Agent", "WebSearch"], system_prompt=system_prompt
    )


async def test_the_self_understanding_drift_that_rebuilt_the_client_no_longer_does():
    """PA-6's actual win, and the number behind it.

    `_behavior_prefix` strips `# Key Knowledge` and NOT `## Self-Understanding`,
    which renders above it. So an ordinary turn — the self-image evidence count
    ticking up — moved the warm-client key and respawned the CLI subprocess.
    Measured over 8 turns on a live soul: the prefix held 1 of 7 boundaries. The
    digest held 7 of 7, because `identity`'s key excludes both sections by name.

    The fixture asserts the prefix DOES move first. Without that this test would
    pass against a digest that keys nothing, which is the failure mode a
    "cache hit rate went up" measurement cannot distinguish from a win.

    THE MUTATION THAT BREAKS THIS: fold the prefix back into the digest branch of
    `_client_cache_key`. Run: the two digest-keyed keys diverged and the equality
    failed.
    """
    prefix_before = ClaudeSDKBackend._client_cache_key(_opts(_SOUL_BEFORE), session_key="s1")
    prefix_after = ClaudeSDKBackend._client_cache_key(_opts(_SOUL_AFTER), session_key="s1")
    assert prefix_before != prefix_after, (
        "the fixture no longer drifts through the prefix — this test proves nothing"
    )

    digest_before = ClaudeSDKBackend._client_cache_key(
        _opts(_SOUL_BEFORE), session_key="s1", system_prompt_digest="aaaaaaaaaaaaaaaa"
    )
    digest_after = ClaudeSDKBackend._client_cache_key(
        _opts(_SOUL_AFTER), session_key="s1", system_prompt_digest="aaaaaaaaaaaaaaaa"
    )
    assert digest_before == digest_after, "soul drift still rebuilds the warm subprocess"


async def test_a_changed_identity_still_evicts_the_warm_client():
    """The freshness direction, which the test above would happily destroy.

    A digest that never moves is a perfect cache and a broken agent. Same prompt
    bytes, two digests, two keys — because the digest is the identity claim and
    the warm subprocess bakes its prompt in at `connect()`.

    THE MUTATION THAT BREAKS THIS: drop `system_prompt_digest` from the returned
    key string. Run: both digests produced one key.
    """
    one = ClaudeSDKBackend._client_cache_key(
        _opts(_SOUL_BEFORE), session_key="s1", system_prompt_digest="aaaaaaaaaaaaaaaa"
    )
    two = ClaudeSDKBackend._client_cache_key(
        _opts(_SOUL_BEFORE), session_key="s1", system_prompt_digest="bbbbbbbbbbbbbbbb"
    )
    assert one != two


async def test_a_digest_run_and_a_prefix_run_never_share_a_warm_client():
    """The `d:` / `t:` split, on the key that owns a live subprocess.

    A deploy that ports a caller onto the assembler changes which RULE minted the
    key. Sharing a namespace would let a client warmed under "these bytes match"
    answer a turn asking "these layers match" — two different questions with the
    same 16 hex chars.

    THE MUTATION THAT BREAKS THIS: drop the `d:` prefix so a digest and a prefix
    hash land in the same namespace. Run: the two keys still differed by luck of
    the inputs, so the assertion that bites is the one on the SLOT, which is why
    both are here.
    """
    with_digest = ClaudeSDKBackend._client_cache_key(
        _opts(_SOUL_BEFORE), session_key="s1", system_prompt_digest="aaaaaaaaaaaaaaaa"
    )
    without = ClaudeSDKBackend._client_cache_key(_opts(_SOUL_BEFORE), session_key="s1")

    assert ":d:aaaaaaaaaaaaaaaa:" in with_digest
    assert ":t:" in without
    assert with_digest != without


async def test_a_caller_without_a_digest_keeps_the_behaviour_prefix():
    """The channel path, and why `_behavior_prefix` is still in the tree.

    PA-6 was filed as "delete it". `AgentLoop` builds the Telegram / Discord /
    Slack / CLI prompt in `AgentContextBuilder` and reaches `run` with no digest
    until PA-7 — and `run` itself splices a GROWING `# Recent Conversation` block
    into `options.system_prompt`, so a key over the whole prompt would respawn
    the subprocess every turn. Measured 2026-08-03 over 8 turns of a realistic
    channel prompt: whole-prompt keying held 0 of 7 boundaries, the prefix 7 of 7.

    So the property that has to survive PA-6 is this one: with no digest, a
    change BELOW the volatile markers must not move the key, and a change above
    it must.

    THE MUTATION THAT BREAKS THIS: make the no-digest branch hash the whole
    prompt instead of the prefix. Run: the two per-turn variants keyed apart.
    """
    base = "You are Paw.\n\n## THE LAW\nNever fabricate."
    turn_1 = f"{base}\n\n## Relevant Past Memories\nasyncpg"
    turn_2 = f"{base}\n\n## Relevant Past Memories\nrollback"
    changed = "You are Paw.\n\n## THE LAW\nAlways fabricate."

    assert ClaudeSDKBackend._client_cache_key(
        _opts(turn_1), session_key="s1"
    ) == ClaudeSDKBackend._client_cache_key(_opts(turn_2), session_key="s1")
    assert ClaudeSDKBackend._client_cache_key(
        _opts(turn_1), session_key="s1"
    ) != ClaudeSDKBackend._client_cache_key(_opts(changed), session_key="s1")


async def test_prewarm_takes_the_digest_or_it_evicts_the_client_it_paid_for():
    """The regression this cutover could have shipped silently.

    `prewarm` connects the subprocess turn 1 will reuse, and the two only meet if
    they compute the SAME key. Turn 1 now keys under `d:`. A prewarm that still
    keyed under `t:` would be evicted by the very turn it exists to serve — a net
    loss over not prewarming at all, and invisible except as latency.

    Two halves: the backend has to ACCEPT the digest (the pool asks the signature
    before sending it, so a `prewarm` that dropped the parameter would silently
    stop receiving it), and the key it produces has to match turn 1's despite
    prewarm assembling with no message and no knowledge context.

    THE MUTATION THAT BREAKS THIS: remove `system_prompt_digest` from
    `ClaudeSDKBackend.prewarm`'s signature. Run: `_accepts_prompt_digest_kwarg`
    returned False and the first assertion failed.
    """
    assert _accepts_prompt_digest_kwarg(ClaudeSDKBackend.prewarm), (
        "the pool asks the signature — a prewarm that drops the parameter stops receiving it"
    )

    prewarm_shaped = "You are Paw.\n\n## THE LAW\nNever fabricate."
    turn_one_shaped = f"{prewarm_shaped}\n\n## Relevant Past Memories\nasyncpg"
    assert prewarm_shaped != turn_one_shaped

    assert ClaudeSDKBackend._client_cache_key(
        _opts(prewarm_shaped), session_key="s1", system_prompt_digest="aaaaaaaaaaaaaaaa"
    ) == ClaudeSDKBackend._client_cache_key(
        _opts(turn_one_shaped), session_key="s1", system_prompt_digest="aaaaaaaaaaaaaaaa"
    )


async def test_the_pool_sends_the_digest_to_both_entry_points(monkeypatch):
    """The wiring, held at the pool rather than at the backend.

    Both halves of the parity above are the POOL's to send: `run` and `prewarm`
    each get the digest from the same `AssembledPrompt`, and a turn where one was
    forwarded and the other was not is exactly the eviction described there.
    """
    from pocketpaw.agents.pool import AgentPool

    seen: dict[str, str | None] = {}

    class _Capturing:
        """A real class, not a mock, and it DECLARES the digest on both methods.

        The pool reads the signature and refuses `**kwargs`, so a stub that only
        collected the kwarg would record `None` and read as "the pool forgot" —
        which is what the first draft of this test did.
        """

        async def run(  # noqa: ARG002
            self, message: str, *, system_prompt: str = "", system_prompt_digest: str = "", **kw
        ):
            seen["run"] = system_prompt_digest
            return
            yield  # pragma: no cover — makes this an async generator

        async def prewarm(  # noqa: ARG002
            self, *, session_key: str, system_prompt: str, system_prompt_digest: str = "", **kw
        ):
            seen["prewarm"] = system_prompt_digest

    from types import SimpleNamespace

    instance = SimpleNamespace(
        agent_id="agent-1",
        agent_name="Paw",
        backend=_Capturing(),
        soul_manager=None,
        config={"soul_persona": "WHO I AM", "system_prompt": ""},
        memory_namespace="ns",
        created_from_updated_at=None,
        active_runs=0,
    )
    pool = AgentPool()

    async def _fake_get(agent_id):  # noqa: ARG001
        return instance

    monkeypatch.setattr(pool, "get", _fake_get)

    await pool.prewarm("agent-1", "cloud:session:s1:agent-1", instructions="RIPPLE LAW.")
    async for _ in pool.run(
        "agent-1", "hi", "cloud:session:s1:agent-1", instructions="RIPPLE LAW."
    ):
        pass

    assert seen.get("prewarm"), "the pool prewarmed without a digest — turn 1 will evict it"
    assert seen.get("run"), "the pool ran without a digest"
    assert seen["prewarm"] == seen["run"], (
        "prewarm and turn 1 keyed on different digests — the prewarmed client is evicted"
    )
