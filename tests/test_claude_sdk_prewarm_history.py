# tests/test_claude_sdk_prewarm_history.py
# Pins the prewarm/history seam around the Claude SDK warm client.
#
# History reaches the model in the QUERY TEXT, not the system prompt (the SDK
# applies the prompt only at ``connect()``), so a warm client is never rebuilt
# just because it was connected without history. The prewarm/turn-context
# contract itself lives in tests/test_claude_sdk_turn_state.py.
#
# Guarantees held here:
#   1. A client that already served a turn on the key is REUSED when the next
#      turn carries more history.
#   2. Neither ``AgentPool.prewarm`` nor the backend's ``prewarm`` takes
#      history: a prewarmed client has seen none, so turn 1 sends it all.
#
# The fake-SDK harness is shared with tests/test_claude_sdk_prewarm.py.

from __future__ import annotations

import inspect

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


def test_prewarm_takes_no_history():
    from pocketpaw.agents.claude_sdk import ClaudeSDKBackend
    from pocketpaw.agents.pool import AgentPool

    assert "history" not in inspect.signature(AgentPool.prewarm).parameters
    assert "history" not in inspect.signature(ClaudeSDKBackend.prewarm).parameters
