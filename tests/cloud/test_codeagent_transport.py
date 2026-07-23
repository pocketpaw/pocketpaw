# test_codeagent_transport.py — the keyless model transport (CA-0).
#
# Created 2026-07-22, after `/codeagent/turn` returned 503 on a machine whose
# chat agent works perfectly well without a key.
#
# The decoding half carries the risk. A native `tool_use` block is parsed by the
# vendor's SDK; here it is JSON in a model's prose, and every way that can go
# wrong is a way the agent silently stops being able to read files:
#
#   * a fenced reply that parses as nothing -> looks like a refusal
#   * a `tool_calls` entry with no name    -> a client asked to run ""
#   * an id collision across two rounds    -> the API rejects the next turn
#
# So the parser is tested against the shapes a model actually emits, not just the
# shape the prompt asks for.
from __future__ import annotations

import json

from pocketpaw_ee.cloud.codeagent import transport

# ── Decoding ────────────────────────────────────────────────────────────────


def test_a_plain_answer_becomes_a_text_block() -> None:
    response = transport.decode_reply('{"answer": "It renders the sidebar."}')

    assert response.stop_reason == "end_turn"
    assert [b.type for b in response.content] == ["text"]
    assert response.content[0].text == "It renders the sidebar."


def test_a_tool_call_becomes_a_tool_use_block() -> None:
    raw = '{"tool_calls": [{"name": "readFile", "input": {"path": "src/App.tsx"}}]}'

    response = transport.decode_reply(raw)

    assert response.stop_reason == "tool_use"
    block = response.content[0]
    assert (block.type, block.name, block.input) == (
        "tool_use",
        "readFile",
        {"path": "src/App.tsx"},
    )


def test_a_fenced_reply_still_parses() -> None:
    """Models fence JSON no matter how firmly the prompt says not to, and an
    unparsed fence would surface as the model refusing to use its tools."""
    raw = '```json\n{"answer": "Yes."}\n```'

    assert transport.decode_reply(raw).content[0].text == "Yes."


def test_json_wrapped_in_prose_still_parses() -> None:
    raw = 'Sure! Here you go:\n{"answer": "Two callers."}\nHope that helps.'

    assert transport.decode_reply(raw).content[0].text == "Two callers."


def test_unparseable_prose_degrades_to_an_answer_not_an_error() -> None:
    """A reply we cannot parse is still a reply. Surfacing the model's own words
    beats reporting 'the model call failed' for a call that succeeded — the only
    thing lost is a tool call this round."""
    response = transport.decode_reply("I think it handles routing.")

    assert response.stop_reason == "end_turn"
    assert response.content[0].text == "I think it handles routing."


def test_a_nameless_tool_call_is_dropped() -> None:
    """The client executes what it is handed. A call with no name would reach the
    executor as `""` and fail there, far from here."""
    raw = '{"tool_calls": [{"input": {"path": "a"}}, {"name": "search", "input": {"query": "x"}}]}'

    response = transport.decode_reply(raw)

    assert [b.name for b in response.content] == ["search"]


def test_an_empty_tool_call_list_is_not_a_tool_turn() -> None:
    """`{"tool_calls": []}` means the model chose the wrong shape, not that it
    wants zero tools run. Treated as an answer so the loop can terminate."""
    response = transport.decode_reply('{"tool_calls": []}')

    assert response.stop_reason == "end_turn"


def test_tool_ids_are_unique_across_calls() -> None:
    """The id pairs a `tool_use` with its `tool_result` on the NEXT turn. Two
    calls sharing one id make that turn invalid, and the failure surfaces as a
    model error rather than as anything pointing here."""
    raw = json.dumps(
        {"tool_calls": [{"name": "readFile", "input": {}}, {"name": "search", "input": {}}]}
    )

    ids = [b.id for b in transport.decode_reply(raw).content]

    assert len(set(ids)) == 2
    assert all(i.startswith("toolu_") for i in ids)


def test_well_formed_json_of_the_wrong_shape_still_says_something() -> None:
    response = transport.decode_reply('{"thoughts": "hmm"}')

    assert response.stop_reason == "end_turn"
    assert "thoughts" in response.content[0].text


# ── Prompt rendering ────────────────────────────────────────────────────────


def test_the_prompt_carries_the_tools_and_the_protocol() -> None:
    prompt = transport.render_prompt(
        "You answer questions about code.",
        [{"role": "user", "content": "what does App do?"}],
        [{"name": "readFile", "description": "Read a file", "input_schema": {}}],
    )

    assert "You answer questions about code." in prompt
    assert "readFile" in prompt
    assert "tool_calls" in prompt  # the protocol
    assert "what does App do?" in prompt


def test_prior_tool_traffic_is_rendered_not_dropped() -> None:
    """The replayed conversation holds what was already asked and answered. A
    model that cannot see its own earlier call will make it again, forever."""
    messages = [
        {"role": "user", "content": "what imports this?"},
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "t1", "name": "search", "input": {"query": "Sidebar"}}
            ],
        },
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "src/App.tsx:3"}],
        },
    ]

    prompt = transport.render_prompt("sys", messages, None)

    assert "search" in prompt
    assert "Sidebar" in prompt
    assert "src/App.tsx:3" in prompt


def test_the_final_round_says_there_are_no_tools() -> None:
    """The loop's last call passes none. Saying so beats leaving the model to
    infer it from an absent section and calling something anyway."""
    prompt = transport.render_prompt("sys", [{"role": "user", "content": "hi"}], None)

    assert "None. Answer from what you already have." in prompt


def test_an_oversized_prompt_is_trimmed_from_the_front() -> None:
    """The tail holds the current question and the freshest tool results. Trim
    the head — losing the question would be losing the point."""
    messages = [{"role": "user", "content": "x" * 60_000}, {"role": "user", "content": "LAST"}]

    prompt = transport.render_prompt("sys", messages, None)

    assert len(prompt) <= transport.MAX_PROMPT_CHARS
    assert "LAST" in prompt


# ── The client shape ────────────────────────────────────────────────────────


async def test_the_cli_client_is_a_drop_in_for_asyncanthropic(monkeypatch) -> None:  # noqa: ANN001
    """`service._run_model` calls `client.messages.create(**kwargs)` and its
    consumers duck-type the blocks. If this shape drifts, the CLI path breaks
    while the keyed path keeps passing — and the CLI path is the one nobody has
    a key to test."""
    client = transport.ClaudeCliClient("/usr/bin/claude")
    captured: dict = {}

    async def fake_run(self, prompt: str) -> str:  # noqa: ANN001
        captured["prompt"] = prompt
        return '{"answer": "done"}'

    monkeypatch.setattr(transport.ClaudeCliClient, "_run", fake_run)

    response = await client.messages.create(
        model="claude-opus-4-8",
        max_tokens=4096,
        thinking={"type": "adaptive"},
        output_config={"effort": "high"},
        system=[{"type": "text", "text": "be helpful"}],
        messages=[{"role": "user", "content": "hi"}],
    )

    # Messages-API-only parameters are accepted and dropped, so the call site
    # stays identical for both transports.
    assert response.content[0].text == "done"
    assert "be helpful" in captured["prompt"]


async def test_a_cli_failure_is_raised_not_swallowed(monkeypatch) -> None:  # noqa: ANN001
    """`_run_model` wraps this into a clean 502. A silently-empty answer would
    be reported to the user as the model having nothing to say."""
    import pytest

    client = transport.ClaudeCliClient("/usr/bin/claude")

    async def boom(self, prompt: str) -> str:  # noqa: ANN001
        raise RuntimeError("the claude CLI failed (exit 1)")

    monkeypatch.setattr(transport.ClaudeCliClient, "_run", boom)

    with pytest.raises(RuntimeError, match="exit 1"):
        await client.messages.create(system="", messages=[])
