# tests/test_channel_prompt_digest.py
# Created: 2026-08-03 (PA-7b, feat/prompt-assembler-channel) — pins the delivery
# of the channel path's `stable_digest` from the assembler to the backend, and
# the two things that delivery is deliberately blind to.
#
# WHAT THIS IS AND IS NOT. PA-7a moved Telegram / Discord / Slack / CLI onto the
# layered assembler, which computed a digest and dropped it on the floor. PA-7b
# threads it. That is a CORRECTNESS change, not a hit-rate one, and the
# difference matters for reading these tests: `claude_sdk._behavior_prefix`
# already held 7/7 turn boundaries on a realistic channel prompt (measured at
# PA-6), so nothing here should be read as "the cache got warmer". What the
# prefix cannot do is see a REAL behaviour change that happens to sit below the
# volatile marker it cuts at — it infers which bytes are stable by pattern-
# matching text two modules away from the layers that know. The digest is what
# those layers said about themselves. No cache-rate number is claimed here
# because none was measured.
#
# ONE MECHANISM WAS measured, in `test_a_changed_recall_moves_the_prefix_and_not_
# the_digest`, and it is worth reading before concluding this buys nothing: the
# prefix's volatile markers are the CLOUD path's block headers, and the channel
# path's memory / kb headers are not among them. So the per-message recall stays
# inside the channel prefix and moves it, while the layers that render it declare
# `cache_key=None` and the digest holds. That is a two-turn probe of a mechanism,
# not a rate over live traffic, and it is deliberately not converted into one.
#
# THE SIGNATURE GATE IS THE LOAD-BEARING PART. The digest is set on every channel
# turn, so it cannot ride the withhold-when-empty contract the other per-run
# kwargs use; it is gated on whether a backend's `run` DECLARES the parameter
# (`agents.backend._accepts_prompt_digest`, which refuses `**kwargs`). Three
# dispatch points now ask that question — `AgentRouter.run`'s primary branch, its
# generic fallback loop, and `BackendFailoverRunner`'s chain — and the two
# fallback paths are the ones a careless thread drops, because they only run when
# something else has already failed.
#
# EACH TEST NAMES THE MUTATION THAT BREAKS IT, and every mutation below was
# applied to the production file, run, observed to fail, and reverted.

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pocketpaw.agents.backend import BackendInfo, Capability
from pocketpaw.agents.protocol import AgentEvent
from pocketpaw.agents.router import AgentRouter
from pocketpaw.bus import Channel, InboundMessage
from pocketpaw.config import Settings
from pocketpaw.prompt import AssembledPrompt

pytestmark = pytest.mark.asyncio

_DIGEST = "c0ffee0000000000"


def _info(name: str) -> BackendInfo:
    """The registry path calls ``cls.info()``; a fake without one logs an error
    and leaves ``_active_backend_name`` unset, which is a different backend than
    the one under test."""
    return BackendInfo(name=name, display_name=name, capabilities=Capability.STREAMING)


# ---------------------------------------------------------------------------
# Fake backends. Each one exists to be a DIFFERENT answer to "does this backend
# declare the digest", so they are written out rather than parameterised — the
# signatures ARE the fixtures.
# ---------------------------------------------------------------------------


class _Declares:
    """A ported backend: names `system_prompt_digest` in `run`."""

    seen: dict[str, Any] = {}

    @staticmethod
    def info() -> BackendInfo:
        return _info("declares")

    def __init__(self, settings=None):  # noqa: ARG002 - registry calls with settings
        pass

    async def run(  # noqa: ARG002 - the fake ignores everything but the digest
        self,
        message: str,
        *,
        system_prompt: str | None = None,
        history: list[dict] | None = None,
        session_key: str | None = None,
        system_prompt_digest: str = "",
    ):
        type(self).seen["digest"] = system_prompt_digest
        yield AgentEvent(type="message", content="declares")
        yield AgentEvent(type="done", content="")


class _NarrowSignature:
    """An out-of-tree backend written before the digest existed.

    Not a contrived shape: `codex_cli` and `opencode` — the two harnesses the
    shipped failover chain falls back to — have exactly this signature. Passing
    the kwarg to it raises TypeError.
    """

    ran = False

    @staticmethod
    def info() -> BackendInfo:
        return _info("narrow")

    def __init__(self, settings=None):  # noqa: ARG002
        pass

    async def run(  # noqa: ARG002
        self,
        message: str,
        *,
        system_prompt: str | None = None,
        history: list[dict] | None = None,
        session_key: str | None = None,
    ):
        type(self).ran = True
        yield AgentEvent(type="message", content="narrow")
        yield AgentEvent(type="done", content="")


class _SwallowsKwargs:
    """The dangerous shape: accepts anything, declares nothing.

    Sending it the digest would raise no error and key on nothing — it would
    look ported forever. The guard refuses `**kwargs` for this reason.
    """

    seen: dict[str, Any] = {}

    @staticmethod
    def info() -> BackendInfo:
        return _info("swallows")

    def __init__(self, settings=None):  # noqa: ARG002
        pass

    async def run(self, message: str, **kwargs: Any):  # noqa: ARG002
        type(self).seen = dict(kwargs)
        yield AgentEvent(type="message", content="swallows")
        yield AgentEvent(type="done", content="")


class _RaisesLaneDown:
    """A harness whose lane is down before it streams anything."""

    @staticmethod
    def info() -> BackendInfo:
        return _info("lane_down")

    def __init__(self, settings=None):  # noqa: ARG002
        pass

    async def run(self, message: str, **kwargs: Any):  # noqa: ARG002
        raise RuntimeError("anthropic.APIStatusError: 529 {'type': 'overloaded_error'}")
        yield  # pragma: no cover — makes this an async generator


def _register(monkeypatch, name: str, cls_name: str) -> None:
    from pocketpaw.agents import registry

    monkeypatch.setitem(
        registry._BACKEND_REGISTRY, name, ("tests.test_channel_prompt_digest", cls_name)
    )


def _reset() -> None:
    _Declares.seen = {}
    _SwallowsKwargs.seen = {}
    _NarrowSignature.ran = False


# ---------------------------------------------------------------------------
# 1 — the builder stopped discarding what it computed
# ---------------------------------------------------------------------------


async def test_the_channel_builder_hands_back_the_digest_it_used_to_drop():
    """`assemble_system_prompt` returns both halves; `build_system_prompt` is `.text`.

    The second assertion is the byte guarantee in its smallest form: the two
    methods share ONE body, so the text a caller reads cannot drift from the text
    the digest was computed over. (The bytes themselves are pinned against six
    committed golden files in `test_channel_prompt_goldens.py`, which still call
    `build_system_prompt` and were not edited for PA-7b — that is the point of
    keeping its return type.)

    THE MUTATION THAT BREAKS THIS: return `AssembledPrompt(text=..., stable_digest="")`
    from `AgentContextBuilder.assemble_system_prompt`. Run: the digest assertion
    failed while the text assertion still passed, which is exactly the state
    PA-7a left the code in and why an "it still works" test would not have
    noticed it.
    """
    from pocketpaw.bootstrap.context_builder import AgentContextBuilder

    memory = MagicMock()
    memory.get_context_for_agent = AsyncMock(return_value="")
    memory.get_semantic_context = AsyncMock(return_value="")
    builder = AgentContextBuilder(memory_manager=memory)

    assembled = await builder.assemble_system_prompt(include_memory=False, channel=Channel.TELEGRAM)
    text = await builder.build_system_prompt(include_memory=False, channel=Channel.TELEGRAM)

    assert assembled.stable_digest, "the assembler computed a digest and it was dropped again"
    assert assembled.text == text, "the delegate and the assembler disagree about the bytes"


# ---------------------------------------------------------------------------
# 2 — AgentRouter, which is where the channel path meets a backend
# ---------------------------------------------------------------------------


async def test_a_backend_that_declares_the_digest_receives_it(monkeypatch):
    """The primary path: the whole point of the task.

    THE MUTATION THAT BREAKS THIS: in `AgentRouter.run`, call
    `self._backend.run(message, **base_kwargs)` instead of wrapping in
    `forward_prompt_digest`. Run: the backend recorded "" and the assertion
    failed.
    """
    _reset()
    _register(monkeypatch, "declares", "_Declares")
    router = AgentRouter(Settings(agent_backend="declares"))

    async for _ in router.run("hi", system_prompt="P", system_prompt_digest=_DIGEST):
        pass

    assert _Declares.seen["digest"] == _DIGEST


async def test_a_backend_that_never_declared_the_parameter_still_runs(monkeypatch):
    """The out-of-tree contract: a narrower signature keeps working untouched.

    This is the assertion that decides the whole shape of the change. Sending the
    digest on the basis of anything other than the signature — a class list, a
    "everyone has it by now" assumption — turns a working third-party backend
    into a TypeError on its first turn.

    THE MUTATION THAT BREAKS THIS: set `base_kwargs["system_prompt_digest"] =
    system_prompt_digest` unconditionally in `AgentRouter.run`. Run: TypeError,
    `_NarrowSignature.run() got an unexpected keyword argument`.
    """
    _reset()
    _register(monkeypatch, "narrow", "_NarrowSignature")
    router = AgentRouter(Settings(agent_backend="narrow"))

    events = [e async for e in router.run("hi", system_prompt="P", system_prompt_digest=_DIGEST)]

    assert _NarrowSignature.ran
    assert [e.content for e in events if e.type == "message"] == ["narrow"]
    assert not any(e.type == "error" for e in events)


async def test_a_backend_that_only_swallows_kwargs_is_not_counted_as_ported(monkeypatch):
    """`**kwargs` is not a declaration, at the router as at the pool.

    A backend that accepts the digest into `**kwargs` and never uses it would
    take delivery of every turn's digest and key on nothing — permanently, and
    invisibly, because nothing raises. The guard refuses it, so such a backend
    keeps its old text-hash behaviour instead of a silent no-op.

    THE MUTATION THAT BREAKS THIS: make `_accepts_prompt_digest_kwarg` return
    True when the signature has a VAR_KEYWORD parameter. Run: the swallowing
    backend recorded the digest and the assertion failed.
    """
    _reset()
    _register(monkeypatch, "swallows", "_SwallowsKwargs")
    router = AgentRouter(Settings(agent_backend="swallows"))

    async for _ in router.run("hi", system_prompt="P", system_prompt_digest=_DIGEST):
        pass

    assert "system_prompt_digest" not in _SwallowsKwargs.seen
    assert _SwallowsKwargs.seen["system_prompt"] == "P", "the other kwargs must still arrive"


async def test_the_generic_fallback_backend_gets_the_digest_too(monkeypatch):
    """`AgentRouter.run` has TWO dispatch points and the second is easy to miss.

    The fallback loop runs when the primary already failed, so a digest dropped
    here downgrades the warm-client key to a text hash exactly when the user is
    already having a bad turn — and it never shows up in a normal test run,
    because the fallback is not normally reached.

    THE MUTATION THAT BREAKS THIS: restore the fallback loop's explicit
    `system_prompt=` / `history=` / `session_key=` call. Run: the fallback
    backend recorded "".
    """
    _reset()
    _register(monkeypatch, "lane_down", "_RaisesLaneDown")
    _register(monkeypatch, "declares", "_Declares")
    router = AgentRouter(
        Settings(agent_backend="lane_down", fallback_backends=["declares"]),
    )

    events = [e async for e in router.run("hi", system_prompt="P", system_prompt_digest=_DIGEST)]

    assert [e.content for e in events if e.type == "message"] == ["declares"]
    assert _Declares.seen["digest"] == _DIGEST


# ---------------------------------------------------------------------------
# 3 — the failover chain, the other path that only runs when things are wrong
# ---------------------------------------------------------------------------


async def test_the_failover_chain_carries_the_digest_to_the_harness_that_takes_it(monkeypatch):
    """L2 harness failover: the switched-to harness must key like the first would.

    THE MUTATION THAT BREAKS THIS: drop `system_prompt_digest=system_prompt_digest`
    from `run_with_failover`'s `runner.run(...)` call. Run: the recovering harness
    recorded "".
    """
    _reset()
    _register(monkeypatch, "lane_down", "_RaisesLaneDown")
    _register(monkeypatch, "declares", "_Declares")
    monkeypatch.setattr(
        "pocketpaw.agents.failover.BackendFailoverRunner._audit_switch",
        lambda self, **kw: None,
    )
    router = AgentRouter(
        Settings(
            agent_backend="lane_down",
            backend_failover_chain=["lane_down", "declares"],
            backend_failover_enabled=True,
        )
    )

    events = [
        e
        async for e in router.run_with_failover(
            "hi", system_prompt="P", system_prompt_digest=_DIGEST
        )
    ]

    assert [e.content for e in events if e.type == "message"] == ["declares"]
    assert _Declares.seen["digest"] == _DIGEST


async def test_a_narrow_harness_in_the_chain_does_not_crash_the_switch(monkeypatch):
    """The runner forwards `run_kwargs` verbatim, and that is why it needs a filter.

    The shipped chain is claude_agent_sdk -> codex_cli -> opencode, and only the
    first declares the digest. Forwarding verbatim turns a survivable lane-down
    into a TypeError on the harness that was supposed to rescue the turn.

    THE MUTATION THAT BREAKS THIS: delete the `attempt_kwargs` filter in
    `BackendFailoverRunner.run` and pass `**run_kwargs`. Run: TypeError out of
    `_NarrowSignature.run`, the recovery event never arrived, and the runner
    surfaced the crash as the turn's error.
    """
    _reset()
    _register(monkeypatch, "lane_down", "_RaisesLaneDown")
    _register(monkeypatch, "narrow", "_NarrowSignature")
    monkeypatch.setattr(
        "pocketpaw.agents.failover.BackendFailoverRunner._audit_switch",
        lambda self, **kw: None,
    )
    router = AgentRouter(
        Settings(
            agent_backend="lane_down",
            backend_failover_chain=["lane_down", "narrow"],
            backend_failover_enabled=True,
        )
    )

    events = [
        e
        async for e in router.run_with_failover(
            "hi", system_prompt="P", system_prompt_digest=_DIGEST
        )
    ]

    assert _NarrowSignature.ran, "the second harness never got to run"
    assert [e.content for e in events if e.type == "message"] == ["narrow"]


async def test_the_disabled_failover_flag_still_delivers_the_digest(monkeypatch):
    """The default configuration, which is the one almost every install runs.

    `backend_failover_enabled` is False by default, so `run_with_failover`
    delegates to `run`. A digest threaded only into the enabled branch would
    reach nobody in production.

    THE MUTATION THAT BREAKS THIS: drop `system_prompt_digest=system_prompt_digest`
    from the delegating `self.run(...)` call in `run_with_failover`. Run: the
    backend recorded "".
    """
    _reset()
    _register(monkeypatch, "declares", "_Declares")
    router = AgentRouter(Settings(agent_backend="declares", backend_failover_enabled=False))

    async for _ in router.run_with_failover("hi", system_prompt="P", system_prompt_digest=_DIGEST):
        pass

    assert _Declares.seen["digest"] == _DIGEST


# ---------------------------------------------------------------------------
# 4 — what the digest does NOT cover, which is the design and not a gap
# ---------------------------------------------------------------------------


async def test_a_changed_recall_moves_the_prefix_and_not_the_digest():
    """What the prefix was never able to cut on THIS path, measured rather than assumed.

    `_behavior_prefix` cuts the volatile tail at `_VOLATILE_PROMPT_MARKERS`:
    `## Your Knowledge Base`, `## Relevant Past Memories`, `# Recent Conversation`.
    Those are the CLOUD path's block headers. The channel path emits
    `# Memory Context (already loaded…)` and `# Knowledge Base (relevant
    articles…)`, and neither matches — so the per-message recall stays INSIDE the
    prefix and moves it on any turn where the recall changes, which on a semantic
    memory backend is most turns. The two channel layers that produce those blocks
    declare `cache_key=None`, so the digest does not move.

    READ THIS NARROWLY. It is a two-turn probe of a MECHANISM, run here, not a
    hit-rate over live traffic: no turn count, no percentage, nothing comparable
    to PA-6's 8-turn measurement on a live soul. It says the prefix and the digest
    disagree about the recall on this path and which one is right. It does not say
    how often that costs a rebuild for a real user.

    THE MUTATION THAT BREAKS THIS: add `"\\n\\n# Memory Context"` to
    `ClaudeSDKBackend._VOLATILE_PROMPT_MARKERS`. Run: the prefix started cutting
    at the memory block, the two prefixes became equal, and the first assertion
    failed.
    """
    from pocketpaw.agents.claude_sdk import ClaudeSDKBackend
    from pocketpaw.bootstrap.context_builder import AgentContextBuilder

    async def _assemble(recall: str):
        memory = MagicMock()
        memory.get_semantic_context = AsyncMock(return_value=recall)
        memory.get_context_for_agent = AsyncMock(return_value=recall)
        return await AgentContextBuilder(memory_manager=memory).assemble_system_prompt(
            include_memory=True, user_query="hi", channel=Channel.TELEGRAM, sender_id="s1"
        )

    turn_1 = await _assemble("user asked about asyncpg pools")
    turn_2 = await _assemble("user asked about rollback semantics")

    prefix_1 = ClaudeSDKBackend._behavior_prefix(turn_1.text)
    prefix_2 = ClaudeSDKBackend._behavior_prefix(turn_2.text)

    assert "# Memory Context" in prefix_1, (
        "the per-message recall left the prefix — re-read this test before trusting it"
    )
    assert prefix_1 != prefix_2, "the prefix stopped moving on a changed recall"
    assert turn_1.stable_digest == turn_2.stable_digest, (
        "a changed recall moved the digest — the memory/kb layers stopped declaring "
        "themselves volatile"
    )


@patch("pocketpaw.agents.loop.get_message_bus")
@patch("pocketpaw.agents.loop.get_memory_manager")
@patch("pocketpaw.agents.loop.AgentContextBuilder")
@patch("pocketpaw.agents.loop.AgentRouter")
async def test_identity_reinforcement_moves_the_text_and_never_the_digest(
    mock_router_cls,
    mock_builder_cls,
    mock_get_memory,
    mock_get_bus,
):
    """Two turns differing ONLY by identity reinforcement must key identically.

    `AgentLoop` appends the whole identity block again every fifth message. It is
    a per-turn mutation of content the prompt ALREADY contains, so a digest that
    moved for it would tear down and respawn the warm CLI subprocess every fifth
    message in every long conversation, for no change in what the agent is. This
    is the same decision `claude_sdk`'s volatile markers and the `retrieval`
    layer's `cache_key=None` already encode, arriving from a third direction.

    The test asserts the TEXT moved first. Without that it would pass against a
    digest computed from nothing at all, which is the failure a "the digests
    matched" assertion cannot tell apart from a win.

    (The backend's own `# Recent Conversation` splice is the same class of
    mutation and is outside the digest for the same reason. It is not re-tested
    here: it happens inside `ClaudeSDKBackend.run`, downstream of everything this
    file can observe, and `_client_cache_key`'s `d:` branch never reads
    `options.system_prompt` at all.)

    THE MUTATION THAT BREAKS THIS: in `AgentLoop._process_message_inner`, pass
    `system_prompt_digest=hashlib.sha256(system_prompt.encode()).hexdigest()[:16]`
    — i.e. re-derive the digest from the reinforced text instead of forwarding
    `assembled_prompt.stable_digest`. Run: the two turns produced different
    digests and the equality assertion failed.
    """
    from pocketpaw.agents.loop import AgentLoop
    from pocketpaw.bootstrap.protocol import BootstrapContext

    bus = MagicMock()
    bus.consume_inbound = AsyncMock()
    bus.publish_outbound = AsyncMock()
    bus.publish_system = AsyncMock()
    mock_get_bus.return_value = bus

    memory = MagicMock()
    memory.add_to_session = AsyncMock()
    memory.get_compacted_history = AsyncMock(return_value=[])
    memory.resolve_session_key = AsyncMock(side_effect=lambda k: k)
    mock_get_memory.return_value = memory

    turns: list[tuple[str, str]] = []

    async def capturing_run(
        message, *, system_prompt=None, history=None, session_key=None, system_prompt_digest=""
    ):
        turns.append((system_prompt, system_prompt_digest))
        yield AgentEvent(type="done", content="")

    router = MagicMock()
    router.run = capturing_run
    router.stop = AsyncMock()
    mock_router_cls.return_value = router

    bootstrap = MagicMock()
    bootstrap.get_context = AsyncMock(
        return_value=BootstrapContext(
            name="TestAgent",
            identity="You are a test agent",
            soul="Test soul",
            style="Test style",
            user_profile="Test profile",
        )
    )
    builder = mock_builder_cls.return_value
    builder.bootstrap = bootstrap
    builder.assemble_system_prompt = AsyncMock(
        return_value=AssembledPrompt(
            text="<identity>You are PocketPaw</identity>", stable_digest=_DIGEST
        )
    )

    with patch("pocketpaw.agents.loop.get_settings") as get_settings:
        settings = MagicMock()
        settings.agent_backend = "claude_agent_sdk"
        settings.max_concurrent_conversations = 5
        get_settings.return_value = settings
        with patch("pocketpaw.agents.loop.Settings") as settings_cls:
            settings_cls.load.return_value = settings
            loop = AgentLoop()

            for count in (4, 5):  # 5 is the reinforcement boundary; 4 is not
                memory._store.get_session = AsyncMock(
                    return_value=[MagicMock(role="user", content=f"m{i}") for i in range(count)]
                )
                await loop._process_message(
                    InboundMessage(
                        channel=Channel.CLI,
                        sender_id="user1",
                        chat_id="chat1",
                        content="keep going",
                    )
                )

    assert len(turns) == 2, "both turns must have reached the router"
    plain_text, plain_digest = turns[0]
    reinforced_text, reinforced_digest = turns[1]

    assert reinforced_text != plain_text, (
        "the fixture no longer reinforces on the 5th message — this test proves nothing"
    )
    assert reinforced_text.count("<identity>") > plain_text.count("<identity>")
    assert plain_digest == _DIGEST == reinforced_digest, (
        "identity reinforcement moved the digest — every 5th message now respawns the CLI"
    )
