# tests/test_channel_prompt_layers.py
# Created: 2026-08-03 (PA-7a, feat/prompt-assembler-channel) — the properties
# the goldens cannot see.
#
# tests/test_channel_prompt_goldens.py pins the BYTES, which is the acceptance
# criterion, but bytes only prove today's inputs produce today's output. Four
# things about the cutover would survive a byte-identical prompt and break
# later, and each gets a test here:
#
#   1. THE EMISSION ORDER IS A DERIVED FACT, not a list someone typed. The old
#      assembler sorted blocks by priority; the new one emits in list order and
#      never sorts. So the list must BE the old sort. Asserting the fifteen
#      names would pass just as happily if someone later changed a priority and
#      "fixed" the list to match — the rule would be gone and the test still
#      green. So the sort is re-derived from the append order and the layers'
#      own priorities.
#   2. THE THREE I/O FETCHES STILL RUN CONCURRENTLY. Moving bootstrap, memory
#      and kb into self-fetching layers would serialize them (``assemble``
#      renders in a for-loop) and add their SUM to every channel turn where the
#      gather adds their MAX — with a subprocess in the middle. A byte-identical
#      prompt is exactly what that regression produces.
#   3. ONE LAYER RAISING DOES NOT FAIL THE TURN. Five blocks had their own
#      ``try/except`` and now rely on the assembler's render guard. "The guard
#      exists" is not the claim; "the guard covers these five" is, so each of
#      the five is made to raise for real.
#   4. THE TWO .md FILES ARE STILL FOUND. They ship beside ``bootstrap/`` and
#      are now read from a module two directories away.
#
# Every test names the mutation that must break it. Each was applied and re-run
# on 2026-08-03; the outcomes are in the commit body.

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from pocketpaw.bootstrap.context_builder import AgentContextBuilder
from pocketpaw.bootstrap.protocol import BootstrapContext
from pocketpaw.prompt import PromptContext, assemble, prompt_layer_registry
from pocketpaw.prompt.channel import (
    CHANNEL_LAYER_TYPES,
    CHANNEL_PROMPT_LAYERS,
    ChannelInputs,
)
from pocketpaw.prompt.channel import __dict__ as _channel_pkg

pytestmark = pytest.mark.asyncio

_SOURCE_ORDER = _channel_pkg["_CHANNEL_BLOCK_SOURCE_ORDER"]


# ---------------------------------------------------------------------------
# 1. The order
# ---------------------------------------------------------------------------


async def test_the_emission_order_is_the_old_priority_sort():
    """THE RULE: ``CHANNEL_PROMPT_LAYERS`` == append order, stably sorted by priority.

    ``_assemble_with_budget`` did ``sorted(blocks, key=lambda b: b[1])`` — a
    stable sort on priority alone, so ties kept the order the builder appended
    them in — and emitted that. ``assemble`` emits the caller's list order and
    never reorders. The two agree only while the list IS that sort, and this
    re-derives it rather than transcribing the answer: change a layer's
    ``priority`` without moving it in the list and this fails, which is the
    whole point.

    MUTATION: swap any two adjacent names in ``CHANNEL_PROMPT_LAYERS``, or flip
    ``ChannelHealthLayer.priority`` from LOW to MEDIUM. Both fail here.
    """
    priority_of = {name: prompt_layer_registry.get(name).priority for name in _SOURCE_ORDER}
    derived = sorted(_SOURCE_ORDER, key=lambda name: priority_of[name])
    assert list(CHANNEL_PROMPT_LAYERS) == derived


async def test_the_sort_is_stable_within_a_priority():
    """Ties keep their APPEND order, which is what ``sorted`` guarantees.

    Stated separately because it is the half of the rule a reimplementation
    would lose: a sort that reversed ties, or one keyed on ``(priority, name)``,
    would still be "sorted by priority" and would reorder five HIGH blocks.

    MUTATION: key the derivation in the test above on ``(priority, name)``.
    Alphabetical order puts ``current_pocket`` before ``kb_context`` and this
    fails.
    """
    for priority in {prompt_layer_registry.get(n).priority for n in _SOURCE_ORDER}:
        in_source = [n for n in _SOURCE_ORDER if prompt_layer_registry.get(n).priority == priority]
        in_emission = [
            n for n in CHANNEL_PROMPT_LAYERS if prompt_layer_registry.get(n).priority == priority
        ]
        assert in_source == in_emission, f"{priority.name} blocks reordered within their tie"


async def test_channel_hints_is_appended_fifth_and_emitted_second_to_last():
    """The one case where append order and emission order visibly disagree.

    Worth pinning by name: it is the block most likely to be "corrected" back to
    its source position by someone reading ``build_system_prompt``'s history,
    and doing so reorders every channel prompt while every presence assertion in
    the suite stays green.

    MUTATION: move ``channel.channel_hints`` to index 4 of
    ``CHANNEL_PROMPT_LAYERS``. Fails here and moves four golden files.
    """
    assert _SOURCE_ORDER.index("channel.channel_hints") == 4
    assert CHANNEL_PROMPT_LAYERS.index("channel.channel_hints") == len(CHANNEL_PROMPT_LAYERS) - 2


async def test_every_channel_layer_is_registered_and_ordered_exactly_once():
    """No layer defined-but-unregistered, none listed twice, none missing.

    MUTATION: drop ``ChannelGwsLayer`` from ``CHANNEL_LAYER_TYPES``. The
    registry lookup in ``build_system_prompt`` would raise ``KeyError`` on every
    channel turn; this fails first.
    """
    defined = {layer.name for layer in CHANNEL_LAYER_TYPES}
    assert defined == set(CHANNEL_PROMPT_LAYERS) == set(_SOURCE_ORDER)
    assert len(CHANNEL_PROMPT_LAYERS) == len(set(CHANNEL_PROMPT_LAYERS)) == 15
    for name in CHANNEL_PROMPT_LAYERS:
        assert prompt_layer_registry.get(name).name == name


async def test_the_channel_layers_did_not_displace_a_cloud_layer():
    """The registry is one flat dict, and ``identity`` was already taken.

    An unprefixed ``identity`` registration would silently REPLACE
    ``AgentIdentityLayer`` — the cloud path would keep assembling, keep passing
    its byte tests against layers it resolves by the same names, and serve every
    cloud agent a persona block that renders only from ``channel_inputs``.

    MUTATION: rename ``ChannelIdentityLayer.name`` to ``"identity"``. This fails,
    and so does most of tests/test_prompt_*.py.
    """
    from pocketpaw.prompt import AgentIdentityLayer, AtlasPrimerLayer, UserInfoLayer

    assert type(prompt_layer_registry.get("identity")) is AgentIdentityLayer
    assert type(prompt_layer_registry.get("atlas")) is AtlasPrimerLayer
    assert type(prompt_layer_registry.get("user")) is UserInfoLayer
    assert all(name.startswith("channel.") for name in CHANNEL_PROMPT_LAYERS)


# ---------------------------------------------------------------------------
# 2. The latency property
# ---------------------------------------------------------------------------


class _GatheredBootstrap:
    """A bootstrap provider that will not finish until the other two have started."""

    def __init__(self, barrier: asyncio.Barrier) -> None:
        self._barrier = barrier

    async def get_context(self) -> BootstrapContext:
        await self._barrier.wait()
        return BootstrapContext(name="B", identity="i", soul="s", style="st")


async def test_the_three_io_fetches_still_run_concurrently(monkeypatch):
    """bootstrap, memory and kb must be IN FLIGHT AT THE SAME TIME.

    An ``asyncio.Barrier(3)`` is the assertion: each of the three stubs waits
    for the other two to arrive before it returns. All three in one
    ``asyncio.gather`` pass it immediately. Any arrangement that awaits them one
    after another — three self-fetching layers rendered by ``assemble``'s
    sequential for-loop is the obvious one — deadlocks on the first, and the
    ``wait_for`` below turns that into a failure rather than a hung suite.

    This is a TIMING-FREE test on purpose. A wall-clock assertion ("under 150ms")
    is exactly the test that goes flaky on a loaded CI box and gets deleted; a
    rendezvous either happens or it does not.

    MUTATION: replace the ``asyncio.gather`` in ``build_system_prompt`` with
    three sequential awaits. Times out and fails. (Applied 2026-08-03.)
    """
    barrier = asyncio.Barrier(3)

    async def _memory(*args, **kwargs) -> str:
        await barrier.wait()
        return "mem"

    async def _kb(*args, **kwargs) -> str:
        await barrier.wait()
        return "kb"

    monkeypatch.setattr(AgentContextBuilder, "_get_kb_context", staticmethod(_kb))
    memory = MagicMock()
    memory.get_semantic_context = AsyncMock(side_effect=_memory)
    memory.get_context_for_agent = AsyncMock(side_effect=_memory)
    builder = AgentContextBuilder(
        bootstrap_provider=_GatheredBootstrap(barrier), memory_manager=memory
    )

    prompt = await asyncio.wait_for(
        builder.build_system_prompt(include_memory=True, user_query="q"), timeout=5.0
    )
    # And the results really did reach the prompt — a rendezvous that returned
    # nothing would pass the barrier and prove nothing.
    assert "mem" in prompt
    assert "kb" in prompt


async def test_the_memory_fetch_is_the_semantic_one_when_there_is_a_query(monkeypatch):
    """Which coroutine goes into the gather is still chosen by ``user_query``.

    The gather stayed in the builder, so this choice stayed with it. Pinned
    because the layers now render whatever they are handed and would look
    identical either way.

    MUTATION: always call ``get_context_for_agent``. Fails.
    """

    async def _kb(*args, **kwargs) -> str:
        return ""

    monkeypatch.setattr(AgentContextBuilder, "_get_kb_context", staticmethod(_kb))
    memory = MagicMock()
    memory.get_semantic_context = AsyncMock(return_value="semantic")
    memory.get_context_for_agent = AsyncMock(return_value="plain")
    builder = AgentContextBuilder(bootstrap_provider=_StubBootstrap(), memory_manager=memory)

    with_query = await builder.build_system_prompt(include_memory=True, user_query="q")
    assert "semantic" in with_query
    memory.get_context_for_agent.assert_not_called()

    without_query = await builder.build_system_prompt(include_memory=True, user_query=None)
    assert "plain" in without_query


# ---------------------------------------------------------------------------
# 3. The render guard
# ---------------------------------------------------------------------------


class _StubBootstrap:
    async def get_context(self) -> BootstrapContext:
        return BootstrapContext(name="B", identity="the persona", soul="s", style="st")


@pytest.mark.parametrize(
    ("layer_name", "target"),
    [
        ("channel.skills_list", "pocketpaw.skills.get_skill_loader"),
        ("channel.atlas_primer", "pocketpaw.atlas.store.get_atlas_store"),
        ("channel.agents_md", "pocketpaw.agents_md.AgentsMdLoader"),
        ("channel.gws_instructions", "pocketpaw.mcp.config.load_mcp_config"),
        ("channel.health_state", "pocketpaw.health.get_health_engine"),
    ],
)
async def test_a_raising_environment_layer_does_not_fail_the_turn(monkeypatch, layer_name, target):
    """Each of the five ex-``try/except`` blocks, made to raise for real.

    ``build_system_prompt`` used to wrap these five itself. It does not any
    more; ``assemble``'s render guard is the whole of the protection, and this
    drives a genuine exception through each resource to check that the guard
    actually reaches it rather than assuming a ``try`` somewhere covers it. The
    turn must still produce a prompt, and the layers after it must be
    unaffected.

    ``agents_md_dir`` is set for ALL five parametrisations, not just the
    AGENTS.md one, and that is not tidiness. The first draft left it unset; the
    narrowed-guard mutation below is what caught it, because the agents_md layer
    returns early on a missing directory, so that case never reached
    ``AgentsMdLoader`` and passed the mutation while the other four failed. A
    parametrisation that cannot reach the resource it names tests nothing.

    MUTATION: narrow ``assemble``'s ``except Exception`` to
    ``except ValueError``. All five parametrisations fail.
    """

    def _boom(*args, **kwargs):
        raise RuntimeError(f"{target} is broken")

    monkeypatch.setattr(target, _boom)

    async def _kb(*args, **kwargs) -> str:
        return ""

    monkeypatch.setattr(AgentContextBuilder, "_get_kb_context", staticmethod(_kb))
    memory = MagicMock()
    memory.get_context_for_agent = AsyncMock(return_value="")
    builder = AgentContextBuilder(bootstrap_provider=_StubBootstrap(), memory_manager=memory)

    prompt = await builder.build_system_prompt(
        include_memory=False, session_key="s-1", agents_md_dir="/does/not/matter"
    )
    assert "the persona" in prompt  # the turn survived
    assert "# Session Management" in prompt  # and so did the layers after it


async def test_the_guard_records_the_failure_rather_than_swallowing_it():
    """A failed layer lands in ``dropped`` and keys as a failure.

    This is what the five old handlers did NOT do — four of them were a
    ``logger.debug`` or a bare ``pass``, which threw away the exception type,
    the message and the traceback. Invisible in the channel prompt's bytes (the
    channel path returns only ``text``), which is why it is asserted here rather
    than left to the goldens.

    MUTATION: delete the ``dropped.append(...)`` from ``assemble``'s except
    clause. Fails.
    """

    class _Boom:
        name = "channel.health_state"
        priority = prompt_layer_registry.get("channel.health_state").priority
        max_chars = None

        async def render(self, ctx):
            raise RuntimeError("engine down")

    ctx = PromptContext(
        instance=None,
        agent_id="",
        message="",
        instructions="",
        knowledge_context="",
        system_message_override=None,
        channel_inputs=ChannelInputs(identity="persona"),
    )
    assembled = await assemble([prompt_layer_registry.get("channel.identity"), _Boom()], ctx)
    assert assembled.text == "persona"
    assert [(d.name, d.reason) for d in assembled.dropped] == [
        ("channel.health_state", "render raised RuntimeError")
    ]


async def test_a_bootstrap_provider_that_claims_a_non_string_key_cannot_kill_the_turn():
    """A malformed ``identity_cache_key`` must not reach the digest.

    ``BootstrapContext`` declares the field ``str | None``, but the bootstrap
    provider is a Protocol and the identity layer reads whatever the returned
    object actually carries. A ``MagicMock`` — which is what several existing
    suites pass — has a truthy attribute for every name, so a bare ``or``
    forwards the mock as a cache key. ``assembler._digest`` then calls
    ``.encode`` on it and raises, and THAT IS OUTSIDE THE RENDER GUARD: it does
    not degrade one layer, it fails the whole turn. On the channel path that is
    all cost and no benefit, because ``build_system_prompt`` throws the digest
    away.

    This is a bug PA-7a introduced and the existing suite caught (six failures
    across tests/test_memory_isolation.py and tests/test_mem0_store.py) — the
    old channel path never read the field at all.

    MUTATION: change the layer back to ``claim or _short_digest(...)``. Fails
    with ``TypeError: object supporting the buffer API required``.
    """
    provider = MagicMock()
    context = MagicMock()
    context.to_system_prompt.return_value = "base prompt"
    provider.get_context = AsyncMock(return_value=context)
    memory = MagicMock()
    memory.get_context_for_agent = AsyncMock(return_value="")
    builder = AgentContextBuilder(bootstrap_provider=provider, memory_manager=memory)

    prompt = await builder.build_system_prompt(include_memory=False)
    assert "base prompt" in prompt

    # And the layer still produces a usable key rather than dropping to None,
    # which would quietly take the persona out of any future digest.
    from pocketpaw.prompt.channel.request import ChannelIdentityLayer

    out = await ChannelIdentityLayer().render(
        PromptContext(
            instance=None,
            agent_id="",
            message="",
            instructions="",
            knowledge_context="",
            system_message_override=None,
            channel_inputs=ChannelInputs(identity="p", identity_cache_key=context.whatever),
        )
    )
    assert isinstance(out.cache_key, str) and out.cache_key


# ---------------------------------------------------------------------------
# 4. The files that did not move
# ---------------------------------------------------------------------------


async def test_the_channel_files_resolve_to_the_bootstrap_package():
    """``discord.md`` and ``gws.md`` still ship beside ``context_builder.py``.

    The loaders moved into ``prompt/channel/`` and used to resolve their path
    from ``__file__``. They now resolve it from the installed ``pocketpaw``
    package, because importing ``pocketpaw.bootstrap`` to ask would close an
    import cycle (bootstrap imports the prompt registry, which imports the
    channel package). A wrong directory is silent — both loaders return ``""``
    on a missing file — so it is asserted rather than trusted.

    MUTATION: change ``_bootstrap_dir`` to ``.parent / "bootstrapp"``. Fails
    here; the goldens fail too, which is the belt to this test's braces.
    """
    import pocketpaw.bootstrap.context_builder as ctx_mod
    from pocketpaw.prompt.channel.request import _bootstrap_dir

    assert _bootstrap_dir() == Path(ctx_mod.__file__).resolve().parent
    assert (_bootstrap_dir() / "discord.md").exists()
    assert (_bootstrap_dir() / "gws.md").exists()


# ---------------------------------------------------------------------------
# 5. The intended divergence
# ---------------------------------------------------------------------------


async def test_a_critical_block_over_budget_is_emitted_whole_not_cut_to_fit():
    """THE ONE BEHAVIOUR PA-7a CHANGED ON PURPOSE.

    BEFORE: ``_assemble_with_budget`` cut a CRITICAL block to ``remaining`` —
    ``content = content[:remaining]`` — and logged a warning. The identity block
    arrived at whatever length the budget had left, mid-sentence.

    AFTER: it is emitted WHOLE and the budget overruns, loudly.

    WHY: a cut sized from ``remaining`` is sized from what the block's SIBLINGS
    rendered. Two turns with the same persona and different memory blocks would
    carry different amounts of that persona under one cache key, so one key
    would name two different prompts — which is #1842 arriving through the
    budget instead of through the backend (``prompt.layer.Priority``'s
    docstring). A layer that must be bounded takes a constant ``max_chars``,
    which is a pure function of its own bytes and composes with any key.
    ``channel.identity`` deliberately does not, because its key under-reports
    its text when a soul provider makes a claim.

    THE COST, stated plainly: a channel turn whose identity block alone exceeds
    ``budget_chars`` now returns a prompt LONGER than the budget. Everything
    else is still dropped, so the overrun is bounded by the identity block. The
    32,000-char default is far above any measured identity block; PA-9 is the
    task that gets to size it with numbers.

    This test replaced ``test_a_critical_block_over_budget_is_truncated_today``
    in tests/test_channel_prompt_goldens.py, which pinned the old behaviour so
    this change could not land silently.

    MUTATION: restore the old behaviour — truncate CRITICAL to ``remaining`` in
    ``_fit_to_budget``. Fails.
    """
    ctx = PromptContext(
        instance=None,
        agent_id="",
        message="",
        instructions="",
        knowledge_context="",
        system_message_override=None,
        channel_inputs=ChannelInputs(identity="P" * 500, session_key="s-1"),
    )
    assembled = await assemble(
        [
            prompt_layer_registry.get("channel.identity"),
            prompt_layer_registry.get("channel.session_key"),
        ],
        ctx,
        budget_chars=100,
    )
    assert assembled.text == "P" * 500
    assert len(assembled.text) > 100
    # The overrun does not become a licence: everything droppable still went.
    assert "# Session Management" not in assembled.text
    assert [d.name for d in assembled.dropped] == ["channel.session_key"]
