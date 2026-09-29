# tests/test_claude_sdk_prewarm_history.py
# Pins the prewarm/history contract of the Claude SDK warm client.
#
# History reaches the model in exactly one place: ``_build_options`` bakes a
# ``# Recent Conversation`` block into the system prompt, and the SDK applies the
# system prompt only at ``connect()``. The warm-client cache key deliberately
# leaves history out (it is volatile), so a client connected WITHOUT history
# (a prewarm) and a turn that brings history share a key. If the turn reused
# that client, the model saw none of the earlier conversation.
#
# Guarantees held here:
#   1. A prewarmed client that has never served a turn is REBUILT when the turn
#      supplies more history than it was connected with.
#   2. A client that already served a turn on the key is REUSED even though the
#      next turn carries more history (the CLI holds that conversation natively).
#   3. A prewarm that was given the same history is reused (the warm win stays).
#   4. ``AgentPool.prewarm`` forwards ``history`` to a backend that declares it,
#      and withholds it from one that does not.
#
# The fake-SDK harness is shared with tests/test_claude_sdk_prewarm.py.

from __future__ import annotations

from types import SimpleNamespace

from tests.test_claude_sdk_prewarm import _make_sdk, _patched

_HISTORY = [
    {"role": "user", "content": "My codeword is PELICAN-42"},
    {"role": "assistant", "content": "Noted."},
]


async def _run(sdk, message, *, history=None, session_key="s1"):
    async def _go():
        return [
            ev
            async for ev in sdk.run(
                message,
                system_prompt="identity",
                session_key=session_key,
                history=history,
                system_prompt_digest="d1",
            )
        ]

    return await _patched(_go)()


async def _prewarm(sdk, *, history=None, session_key="s1"):
    async def _go():
        await sdk.prewarm(
            session_key=session_key,
            system_prompt="identity",
            history=history,
            system_prompt_digest="d1",
        )

    return await _patched(_go)()


def _queried_prompt(sdk) -> str:
    client = sdk._client
    assert client is not None and client.queries, "no persistent client received the query"
    return client.options.system_prompt


async def test_history_less_prewarm_is_rebuilt_when_the_turn_brings_history():
    counter = [0]
    sdk = _make_sdk(counter)

    await _prewarm(sdk)
    assert counter[0] == 1

    ev = await _run(sdk, "What is my codeword?", history=_HISTORY)
    assert any(e.type == "done" for e in ev)

    prompt = _queried_prompt(sdk)
    assert "# Recent Conversation" in prompt and "PELICAN-42" in prompt, (
        "the turn was sent to a client connected without its history, so the model "
        "sees none of the earlier conversation"
    )
    assert counter[0] == 2, "the history-less prewarmed client must be replaced once"


async def test_a_client_that_served_a_turn_is_reused_despite_more_history():
    counter = [0]
    sdk = _make_sdk(counter)

    await _run(sdk, "My codeword is PELICAN-42")
    assert counter[0] == 1

    ev = await _run(sdk, "What is my codeword?", history=_HISTORY)
    assert any(e.type == "done" for e in ev)
    assert counter[0] == 1, (
        "the warm client already holds this conversation natively; reconnecting "
        "throws away the warm-reuse win"
    )
    assert len(sdk._client.queries) == 2


async def test_a_prewarm_given_the_history_is_reused():
    counter = [0]
    sdk = _make_sdk(counter)

    await _prewarm(sdk, history=_HISTORY)
    ev = await _run(sdk, "What is my codeword?", history=_HISTORY)
    assert any(e.type == "done" for e in ev)

    assert counter[0] == 1, "a prewarm with the turn's history must be reused"
    assert "PELICAN-42" in _queried_prompt(sdk)


def _instance(backend):
    return SimpleNamespace(
        agent_id="agent-1",
        agent_name="Paw",
        backend=backend,
        soul_manager=None,
        config={"soul_persona": "WHO I AM", "system_prompt": ""},
        memory_namespace="ns",
        created_from_updated_at=None,
        active_runs=0,
    )


async def _pool_prewarm(monkeypatch, backend, **kwargs):
    from pocketpaw.agents.pool import AgentPool

    pool = AgentPool()
    instance = _instance(backend)

    async def _fake_get(agent_id):  # noqa: ARG001
        return instance

    monkeypatch.setattr(pool, "get", _fake_get)
    await pool.prewarm("agent-1", "cloud:session:s1:agent-1", instructions="LAW.", **kwargs)


async def test_pool_prewarm_forwards_history(monkeypatch):
    seen: dict = {}

    class _Backend:
        async def prewarm(self, *, session_key, system_prompt, history=None, **kw):  # noqa: ARG002
            seen["history"] = history

    await _pool_prewarm(monkeypatch, _Backend(), history=_HISTORY)
    assert seen.get("history") == _HISTORY


async def test_pool_prewarm_withholds_history_from_a_narrow_backend(monkeypatch):
    seen: dict = {}

    class _Narrow:
        async def prewarm(self, *, session_key, system_prompt):  # noqa: ARG002
            seen["called"] = True

    await _pool_prewarm(monkeypatch, _Narrow(), history=_HISTORY)
    assert seen.get("called"), "a backend without a history param must still be prewarmed"
