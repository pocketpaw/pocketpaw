# tests/test_claude_sdk_turn_state.py
# Pins what a Claude SDK warm client is told on EVERY turn, not only at connect.
#
# The SDK applies ``ClaudeAgentOptions`` (the system prompt included) once, at
# ``connect()``. A warm client reused on a later turn is sent the query text and
# nothing else, so anything per-turn that rides the system prompt is frozen at
# whatever the connecting turn (often a message-less prewarm) carried.
#
# Guarantees held here:
#   F1  Per-turn context (``turn_context``: KB hits, scope/participants, uploaded
#       file text, soul recall) reaches the model in the QUERY TEXT on every path
#       — reused warm client, fresh client, stateless, leased — and the system
#       prompt carries none of it, so it is byte-identical across turns.
#   F4  History the live client has not seen is delivered as a delta; history it
#       has seen is not repeated; a history that no longer extends what the
#       client saw (an edit or a delete) evicts it and reconnects with the full
#       conversation. A turn served statelessly is caught up on the next warm turn.
#
# The fake-SDK harness is shared with tests/test_claude_sdk_prewarm.py.

from __future__ import annotations

from unittest.mock import patch

from pocketpaw.agents.backend import LeasedClient
from tests.test_claude_sdk_prewarm import _make_sdk, _patched


async def _run(sdk, message, *, history=None, turn_context=None, session_key="s1", **kwargs):
    run_kwargs = dict(
        system_prompt="identity",
        session_key=session_key,
        history=history,
        system_prompt_digest="d1",
        **kwargs,
    )
    if turn_context is not None:
        run_kwargs["turn_context"] = turn_context

    async def _go():
        return [ev async for ev in sdk.run(message, **run_kwargs)]

    return await _patched(_go)()


async def _prewarm(sdk, *, session_key="s1"):
    async def _go():
        await sdk.prewarm(
            session_key=session_key, system_prompt="identity", system_prompt_digest="d1"
        )

    return await _patched(_go)()


def _stateless_capture(sdk) -> list[str]:
    prompts: list[str] = []

    async def _fake_query(prompt, options):
        prompts.append(prompt)
        yield sdk._ResultMessage()

    sdk._query = _fake_query
    return prompts


# ---------------------------------------------------------------------------
# F1 — per-turn context is not frozen at connect
# ---------------------------------------------------------------------------


async def test_turn_two_context_reaches_a_reused_client():
    counter = [0]
    sdk = _make_sdk(counter)

    await _run(sdk, "What is the codeword?", turn_context="The codeword is PELICAN.")
    await _run(sdk, "And now?", turn_context="The codeword is WALRUS.")

    assert counter[0] == 1, "same key: the warm client must be reused"
    client = sdk._client
    assert "PELICAN" in client.queries[0]
    assert "WALRUS" in client.queries[1], "turn 2's context never reached the model"
    assert "PELICAN" not in client.queries[1]
    assert client.queries[1].rstrip().endswith("And now?"), "the user's text must close the query"
    prompt = client.options.system_prompt
    assert "PELICAN" not in prompt and "WALRUS" not in prompt, (
        "per-turn context must not be baked into the connect-time system prompt"
    )


async def test_system_prompt_is_identical_whatever_the_turn_carries():
    counter = [0]
    sdk = _make_sdk(counter)
    await _run(
        sdk,
        "hi",
        session_key="s1",
        history=[{"role": "user", "content": "old A"}],
        turn_context="kb A",
    )
    first = sdk._client.options.system_prompt
    await _run(
        sdk,
        "hi",
        session_key="s2",
        history=[{"role": "user", "content": "old B"}, {"role": "assistant", "content": "x"}],
        turn_context="kb B",
    )
    assert counter[0] == 2
    assert sdk._client.options.system_prompt == first, (
        "history and per-turn context must stay out of the system prompt"
    )
    assert "old B" in sdk._client.queries[0] and "kb B" in sdk._client.queries[0]


async def test_stateless_turn_carries_context_and_history_in_the_prompt():
    counter = [0]
    sdk = _make_sdk(counter)
    prompts = _stateless_capture(sdk)
    sdk._client_in_use = True  # a sibling run holds the warm client
    await _run(
        sdk,
        "question",
        history=[{"role": "user", "content": "earlier FALCON"}],
        turn_context="kb OSPREY",
    )
    assert prompts, "the turn did not take the stateless path"
    assert "FALCON" in prompts[0] and "OSPREY" in prompts[0]


async def test_image_turn_text_still_carries_the_context():
    import pocketpaw.agents.claude_sdk as mod
    from pocketpaw.agents.backend import ImageAttachment

    counter = [0]
    sdk = _make_sdk(counter)
    seen: list[str] = []
    real = mod.build_streaming_user_message

    def _spy(text, images):
        seen.append(text)
        return real(text, images)

    img = ImageAttachment(data=b"\x89PNG\r\n", media_type="image/png", filename="a.png")
    with patch.object(mod, "build_streaming_user_message", _spy):
        await _run(sdk, "look", turn_context="kb HERON", image_attachments=(img,))
    assert seen and "HERON" in seen[0] and seen[0].rstrip().endswith("look")


# ---------------------------------------------------------------------------
# F4 — the warm client is kept in step with the stored conversation
# ---------------------------------------------------------------------------

_H1 = [
    {"role": "user", "content": "My codeword is PELICAN-42"},
    {"role": "assistant", "content": "Noted."},
]


async def test_prewarmed_client_is_reused_and_given_the_history_in_the_turn():
    counter = [0]
    sdk = _make_sdk(counter)
    await _prewarm(sdk)
    await _run(sdk, "What is my codeword?", history=_H1)
    assert counter[0] == 1, "a history-less prewarm no longer needs rebuilding"
    assert "PELICAN-42" in sdk._client.queries[0]


async def test_only_the_unseen_delta_is_delivered():
    counter = [0]
    sdk = _make_sdk(counter)
    await _run(sdk, "first question", history=_H1)
    # Turn 2's history: what the client already had, the turn it served, and a
    # message another member posted that never reached this client.
    h2 = [
        *_H1,
        {"role": "user", "content": "first question"},
        {"role": "assistant", "content": "ok"},
        {"role": "user", "content": "Bob: the deadline moved to FRIDAY"},
    ]
    await _run(sdk, "when is the deadline?", history=h2)
    assert counter[0] == 1
    q2 = sdk._client.queries[1]
    assert "FRIDAY" in q2, "a message the client never saw was not delivered"
    assert "PELICAN-42" not in q2, "history the client already holds was repeated"
    assert "first question" not in q2.replace("when is the deadline?", "")


async def test_an_edited_history_reconnects_with_the_full_conversation():
    counter = [0]
    sdk = _make_sdk(counter)
    await _run(sdk, "first question", history=_H1)
    edited = [
        {"role": "user", "content": "My codeword is ALBATROSS-7"},
        {"role": "assistant", "content": "Noted."},
        {"role": "user", "content": "first question"},
        {"role": "assistant", "content": "ok"},
    ]
    await _run(sdk, "What is my codeword?", history=edited)
    assert counter[0] == 2, "a diverged conversation must evict the warm client"
    q = sdk._client.queries[0]
    assert "ALBATROSS-7" in q and "first question" in q


async def test_a_stateless_turn_is_caught_up_on_the_next_warm_turn():
    counter = [0]
    sdk = _make_sdk(counter)
    _stateless_capture(sdk)
    await _run(sdk, "m1 question", history=[])
    sdk._client_in_use = True
    await _run(
        sdk,
        "m2 MARLIN question",
        history=[
            {"role": "user", "content": "m1 question"},
            {"role": "assistant", "content": "ok"},
        ],
    )
    sdk._client_in_use = False
    h3 = [
        {"role": "user", "content": "m1 question"},
        {"role": "assistant", "content": "ok"},
        {"role": "user", "content": "m2 MARLIN question"},
        {"role": "assistant", "content": "reply about MARLIN"},
    ]
    await _run(sdk, "m3", history=h3)
    assert counter[0] == 1
    q3 = sdk._client.queries[-1]
    assert "MARLIN" in q3, "the turn served statelessly never reached the warm client"
    assert "m1 question" not in q3


async def test_leased_warm_client_gets_the_delta_and_diverges_to_a_fresh_build():
    counter = [0]
    sdk = _make_sdk(counter)
    built: list = []

    def _on_built(client, key, teardown):
        built.append((client, key))

    await _run(sdk, "turn one", history=_H1, on_client_built=_on_built)
    client, key = built[0]
    assert "PELICAN-42" in client.queries[0]

    lease = LeasedClient(client=client, options_key=key)
    h2 = [
        *_H1,
        {"role": "user", "content": "turn one"},
        {"role": "assistant", "content": "ok"},
        {"role": "user", "content": "Carol: bring the KESTREL deck"},
    ]
    await _run(sdk, "turn two", history=h2, warm_client=lease, on_client_built=_on_built)
    assert len(built) == 1, "a matching lease must be reused"
    assert "KESTREL" in client.queries[1] and "PELICAN-42" not in client.queries[1]

    edited = [{"role": "user", "content": "rewritten"}, *h2[1:]]
    await _run(sdk, "turn three", history=edited, warm_client=lease, on_client_built=_on_built)
    assert len(built) == 2, "a diverged lease must be replaced by a fresh build"
    assert "rewritten" in built[1][0].queries[0]
