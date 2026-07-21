# test_codeagent_tools.py — The Code Mode retrieval loop (CA-2).
#
# Created 2026-07-21 (feat/codeagent-tools). Covers the half of the agent that
# CA-1 did not have: the model asking for files, and the client being the one
# that reads them.
#
# The split from test_codeagent.py is deliberate — that file pins the one-shot
# Ask contract, this one pins the loop. A failure here says "retrieval broke",
# not "Ask broke".
from __future__ import annotations

import pytest
from pocketpaw_ee.cloud._core.errors import CloudError
from pocketpaw_ee.cloud.codeagent import service as codeagent_service
from pocketpaw_ee.cloud.codeagent.domain import (
    ASK_TOOL_NAMES,
    ASK_TOOLS,
    MAX_TOOL_ITERATIONS,
    MAX_TOOL_RESULT_CHARS,
)

WS = "ws-1"
USER = "user-1"

MUTATING_VERBS = {"writeFile", "createEntry", "deleteEntry", "moveEntry"}


# ── Fakes ───────────────────────────────────────────────────────────────────


class _Text:
    def __init__(self, text: str) -> None:
        self.type = "text"
        self.text = text


class _ToolUse:
    """What the model returns when it wants a file read."""

    def __init__(self, name: str, input_: dict, id_: str = "tu-1") -> None:
        self.type = "tool_use"
        self.name = name
        self.input = input_
        self.id = id_


class _Response:
    def __init__(self, blocks: list, stop_reason: str = "end_turn") -> None:
        self.content = blocks
        self.stop_reason = stop_reason


class _Messages:
    def __init__(self, response: _Response) -> None:
        self._response = response
        self.calls: list[dict] = []

    async def create(self, **kwargs):  # noqa: ANN003, ANN201
        self.calls.append(kwargs)
        return self._response


class _Client:
    def __init__(self, response: _Response) -> None:
        self.messages = _Messages(response)


def _answering(text: str = "Because the limit is never applied.") -> _Client:
    return _Client(_Response([_Text(text)]))


def _asking(*blocks: _ToolUse) -> _Client:
    return _Client(_Response(list(blocks), stop_reason="tool_use"))


def _body(results: list[dict] | None = None, context: list[dict] | None = None) -> dict:
    return {
        "messages": [{"role": "user", "content": "where is the conflict checked?"}],
        "context": context or [],
        "toolResults": results or [],
    }


def _result(id_: str, path: str = "a.ts", output: str = "x", **extra) -> dict:  # noqa: ANN003
    return {"id": id_, "name": "readFile", "input": {"path": path}, "output": output, **extra}


# ── The tool surface ────────────────────────────────────────────────────────


def test_tool_names_mirror_the_client_read_verbs():
    """The names ARE CodeFileSession's verbs. That is the mechanism, not a
    convention: the client dispatches by name, and it implements these against a
    Daytona socket AND against the in-tab WebContainer fs. A rename here breaks
    the executor on one runtime and not the other."""
    assert {t["name"] for t in ASK_TOOLS} == {"listDir", "readFile", "search"}
    assert set(ASK_TOOL_NAMES) == {"listDir", "readFile", "search"}


def test_no_mutating_verb_is_exposed_in_ask_mode():
    """The mutating four are CA-4's Edit permission set."""
    assert ASK_TOOL_NAMES.isdisjoint(MUTATING_VERBS)


def test_every_tool_declares_a_schema_the_model_can_fill():
    for tool in ASK_TOOLS:
        schema = tool["input_schema"]
        assert schema["type"] == "object"
        assert schema["required"], f"{tool['name']} has no required input"
        for name in schema["required"]:
            assert name in schema["properties"]
        assert tool["description"].strip()


# ── Asking for files ────────────────────────────────────────────────────────


async def test_a_tool_call_comes_back_for_the_client_to_execute():
    """The model asks; the CLIENT reads. The server never fetches a file itself,
    which is what keeps this working in a tab with no sandbox row."""
    client = _asking(_ToolUse("readFile", {"path": "src/a.ts"}))

    result = await codeagent_service.run_turn(WS, USER, _body(), client=client)

    assert result.done is False
    assert result.answer == ""
    assert [(c.name, c.input) for c in result.toolCalls] == [("readFile", {"path": "src/a.ts"})]


async def test_several_calls_in_one_round_all_come_back():
    """Each round is a browser round trip, so the model asking for three things
    at once must not be collapsed to one."""
    client = _asking(
        _ToolUse("readFile", {"path": "a.ts"}, "t1"),
        _ToolUse("readFile", {"path": "b.ts"}, "t2"),
        _ToolUse("search", {"query": "booking"}, "t3"),
    )

    result = await codeagent_service.run_turn(WS, USER, _body(), client=client)

    assert [c.id for c in result.toolCalls] == ["t1", "t2", "t3"]


async def test_a_mutating_call_never_reaches_the_client():
    """THE load-bearing guard for Ask mode.

    The client executes whatever it is handed. If a writeFile call could ride out
    of here — a stale tool set, a hallucinated name — then "Ask cannot edit"
    would be a polite request to the browser rather than a fact.
    """
    client = _asking(
        _ToolUse("writeFile", {"path": "a.ts", "content": "wiped"}, "bad"),
        _ToolUse("readFile", {"path": "a.ts"}, "ok"),
    )

    result = await codeagent_service.run_turn(WS, USER, _body(), client=client)

    assert [c.name for c in result.toolCalls] == ["readFile"]


async def test_an_unknown_tool_name_is_dropped_too():
    client = _asking(
        _ToolUse("rmMinusRf", {"path": "/"}, "bad"), _ToolUse("search", {"query": "x"}, "ok")
    )

    result = await codeagent_service.run_turn(WS, USER, _body(), client=client)

    assert [c.name for c in result.toolCalls] == ["search"]


async def test_a_round_whose_calls_are_all_dropped_fails_rather_than_answering_blank():
    """With every call filtered there is no answer text either, and a blank
    bubble reads as the agent having nothing to say."""
    client = _asking(_ToolUse("writeFile", {"path": "a.ts"}, "bad"))

    with pytest.raises(CloudError) as exc:
        await codeagent_service.run_turn(WS, USER, _body(), client=client)

    assert exc.value.code == "codeagent.failed"


# ── Replaying what the client already read ──────────────────────────────────


async def test_results_replay_as_matched_use_and_result_pairs():
    """The server kept no record of what it asked for, so the client hands back
    both halves and they are rebuilt here. An unanswered tool_use is rejected by
    the API outright, so the pairing is load-bearing, not cosmetic."""
    client = _answering("Found it.")
    body = _body(
        [
            {
                "id": "t1",
                "name": "search",
                "input": {"query": "booking"},
                "output": "src/book.ts:12: conflict check",
            }
        ]
    )

    result = await codeagent_service.run_turn(WS, USER, body, client=client)

    assert result.done is True
    sent = client.messages.calls[0]["messages"]
    use, res = sent[-2], sent[-1]
    assert use["role"] == "assistant"
    assert use["content"][0]["type"] == "tool_use"
    assert use["content"][0]["id"] == "t1"
    assert use["content"][0]["name"] == "search"
    assert use["content"][0]["input"] == {"query": "booking"}
    assert res["role"] == "user"
    assert res["content"][0]["type"] == "tool_result"
    assert res["content"][0]["tool_use_id"] == "t1"
    assert "src/book.ts:12" in res["content"][0]["content"]


async def test_replay_preserves_the_order_the_reads_happened_in():
    client = _answering()
    body = _body([_result("t1", "a.ts"), _result("t2", "b.ts"), _result("t3", "c.ts")])

    await codeagent_service.run_turn(WS, USER, body, client=client)

    ids = [
        m["content"][0]["id"]
        for m in client.messages.calls[0]["messages"]
        if m["role"] == "assistant" and isinstance(m["content"], list)
    ]
    assert ids == ["t1", "t2", "t3"]


async def test_a_failed_tool_is_flagged_rather_than_read_as_an_empty_result():
    """'I looked and found nothing' and 'the lookup broke' are different facts."""
    client = _answering()
    body = _body([_result("t1", "gone.ts", "no such file", isError=True)])

    await codeagent_service.run_turn(WS, USER, body, client=client)

    assert client.messages.calls[0]["messages"][-1]["content"][0]["is_error"] is True


async def test_an_empty_result_is_not_sent_as_an_empty_string():
    """An empty tool_result block is rejected by the API, and 'nothing found' is
    itself a result the model should be able to act on."""
    client = _answering()
    body = _body([{"id": "t1", "name": "search", "input": {"query": "zzz"}, "output": ""}])

    await codeagent_service.run_turn(WS, USER, body, client=client)

    assert client.messages.calls[0]["messages"][-1]["content"][0]["content"] == "(empty)"


async def test_an_enormous_result_is_capped_server_side():
    """The client caps what it sends, but a server that trusts a client-supplied
    length has no ceiling at all — one readFile of a bundled asset would blow the
    context in a single round."""
    client = _answering()
    body = _body([_result("t1", "big.js", "x" * (MAX_TOOL_RESULT_CHARS * 2))])

    await codeagent_service.run_turn(WS, USER, body, client=client)

    sent = client.messages.calls[0]["messages"][-1]["content"][0]["content"]
    assert len(sent) < MAX_TOOL_RESULT_CHARS + 100
    assert sent.endswith("(truncated)")


async def test_the_selected_excerpt_survives_into_the_retrieval_rounds():
    """The thing the user pointed at must not fall out of the conversation once
    the model starts fetching other files — it is what the question is about."""
    client = _answering()
    body = _body([_result("t1")], context=[{"path": "sel.ts", "content": "SELECTED"}])

    result = await codeagent_service.run_turn(WS, USER, body, client=client)

    assert "SELECTED" in client.messages.calls[0]["messages"][0]["content"]
    assert result.citedPaths == ["sel.ts"]


# ── The budget ──────────────────────────────────────────────────────────────


async def test_exhausting_the_budget_withdraws_the_tools_and_forces_an_answer():
    """Telling the model to stop looking leaves it free to call again. Taking the
    tools away leaves it no option but to answer with what it has — a better
    outcome than failing a question it could mostly answer."""
    client = _answering("Here is what I found before running out.")
    body = _body([_result(f"t{i}", f"{i}.ts") for i in range(MAX_TOOL_ITERATIONS)])

    result = await codeagent_service.run_turn(WS, USER, body, client=client)

    assert result.done is True
    assert "tools" not in client.messages.calls[0]


async def test_the_exhausted_turn_says_why_it_is_stopping():
    client = _answering()
    body = _body([_result(f"t{i}", f"{i}.ts") for i in range(MAX_TOOL_ITERATIONS)])

    await codeagent_service.run_turn(WS, USER, body, client=client)

    last = client.messages.calls[0]["messages"][-1]
    assert last["role"] == "user"
    assert "budget" in last["content"]


async def test_below_the_budget_the_tools_are_still_offered():
    client = _answering()
    body = _body([_result(f"t{i}", f"{i}.ts") for i in range(MAX_TOOL_ITERATIONS - 1)])

    await codeagent_service.run_turn(WS, USER, body, client=client)

    assert "tools" in client.messages.calls[0]


async def test_an_exhausted_turn_cannot_be_talked_into_more_tool_calls():
    """Belt and braces: even if the model emits tool_use on the final round, the
    loop is over and the client must not be sent back out."""
    client = _asking(_ToolUse("readFile", {"path": "one-more.ts"}))
    # ...but give it text too, so the turn has something to answer with.
    client.messages._response.content.append(_Text("Answering with what I have."))
    body = _body([_result(f"t{i}", f"{i}.ts") for i in range(MAX_TOOL_ITERATIONS)])

    result = await codeagent_service.run_turn(WS, USER, body, client=client)

    assert result.done is True
    assert result.toolCalls == []
    assert result.answer == "Answering with what I have."
