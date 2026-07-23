# test_codeagent_edit.py — Edit mode's permission set (CA-4).
#
# Created 2026-07-21 (feat/codeagent-edit). CA-4 widens the agent from three read
# verbs to those plus ``writeFile``, selected by the request's ``mode``. The thing
# worth testing is not that Edit can write — it is that ASK STILL CANNOT, and
# that widening the set did not quietly remove the filter that enforces it.
#
# Kept apart from test_codeagent_tools.py (the loop) and test_codeagent.py (the
# one-shot contract) so a failure here reads as "the permission split broke".
from __future__ import annotations

import pytest
from pocketpaw_ee.cloud.codeagent import service as codeagent_service
from pocketpaw_ee.cloud.codeagent.domain import (
    ASK_TOOL_NAMES,
    EDIT_SYSTEM_PROMPT,
    EDIT_TOOL_NAMES,
    EDIT_TOOLS,
    MUTATING_TOOL_NAMES,
)
from pocketpaw_ee.cloud.codeagent.dto import AgentTurnRequest
from pydantic import ValidationError

WS = "ws-1"
USER = "user-1"


# ── Fakes (mirrors of test_codeagent_tools.py's, kept local so this file stands
#    alone — a shared fixture module would couple three suites that pin three
#    different contracts). ─────────────────────────────────────────────────────


class _Text:
    def __init__(self, text: str) -> None:
        self.type = "text"
        self.text = text


class _ToolUse:
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


def _returning(*blocks) -> _Client:  # noqa: ANN002
    return _Client(_Response(list(blocks), stop_reason="tool_use"))


def _body(mode: str | None = None, question: str = "add a docstring") -> dict:
    body: dict = {"messages": [{"role": "user", "content": question}], "context": []}
    if mode is not None:
        body["mode"] = mode
    return body


# ── The permission split ────────────────────────────────────────────────────


def test_ask_offers_no_mutating_verb() -> None:
    """The property CA-4 must not break while adding Edit.

    Asserted against MUTATING_TOOL_NAMES rather than a literal, so adding a verb
    to CodeFileSession and forgetting to keep Ask read-only fails here.
    """
    assert ASK_TOOL_NAMES.isdisjoint(MUTATING_TOOL_NAMES)


def test_edit_adds_writefile_and_nothing_else() -> None:
    """Edit widens Ask by exactly one verb.

    createEntry / deleteEntry / moveEntry are deliberately withheld: per-hunk
    review only covers one file's content, so there is no gate for "the agent
    deleted a file". This test is what makes that a decision rather than an
    oversight — adding one without a review gate has to fail here first.
    """
    assert EDIT_TOOL_NAMES == ASK_TOOL_NAMES | {"writeFile"}
    assert EDIT_TOOL_NAMES & MUTATING_TOOL_NAMES == {"writeFile"}


def test_write_tool_tells_the_model_nothing_is_applied() -> None:
    """A model that thinks the write landed reports it in the past tense, and the
    user has no reason to doubt it. The description carries the correction."""
    write = next(t for t in EDIT_TOOLS if t["name"] == "writeFile")
    assert "does NOT save" in write["description"]
    assert set(write["input_schema"]["required"]) == {"path", "content"}
    # A fragment silently deletes the rest of the file when applied.
    assert "WHOLE file" in write["description"]


def test_edit_prompt_does_not_claim_the_change_is_made() -> None:
    assert "Nothing you propose is applied automatically" in EDIT_SYSTEM_PROMPT


# ── Mode selection on the wire ──────────────────────────────────────────────


def test_mode_defaults_to_the_read_only_one() -> None:
    """An omitted field, an older client, and a replayed request all land in ask.
    The failure mode of forgetting the field must be 'cannot edit'."""
    assert AgentTurnRequest.model_validate(_body()).mode == "ask"


def test_an_invented_mode_is_rejected_at_the_wire() -> None:
    with pytest.raises(ValidationError):
        AgentTurnRequest.model_validate(_body(mode="admin"))


async def test_edit_mode_offers_the_write_verb() -> None:
    client = _returning(_Text("done"))
    await codeagent_service.run_turn(WS, USER, _body(mode="edit"), client=client)

    offered = {t["name"] for t in client.messages.calls[0]["tools"]}
    assert offered == EDIT_TOOL_NAMES


async def test_edit_mode_uses_the_edit_prompt() -> None:
    """The mode picks the prompt and the tools from one row, so a turn cannot run
    with Edit's tools under Ask's 'you are in READ-ONLY mode' instructions."""
    client = _returning(_Text("done"))
    await codeagent_service.run_turn(WS, USER, _body(mode="edit"), client=client)

    assert client.messages.calls[0]["system"][0]["text"] == EDIT_SYSTEM_PROMPT


# ── The enforcement, not the instruction ────────────────────────────────────


async def test_ask_mode_drops_a_write_the_model_asks_for() -> None:
    """THE test for the CA-4 done-when: an Ask turn is unable to call a mutating
    tool, not merely told not to.

    The client executes whatever it is handed, so a `writeFile` that survived
    this filter would be applied by the browser. It is dropped instead, and with
    no calls left the turn falls through to the answer.
    """
    client = _returning(
        _Text("Here is what I would change."),
        _ToolUse("writeFile", {"path": "a.ts", "content": "malicious"}),
    )

    result = await codeagent_service.run_turn(WS, USER, _body(mode="ask"), client=client)

    assert result.done is True
    assert result.toolCalls == []
    assert result.answer == "Here is what I would change."


@pytest.mark.parametrize("verb", sorted(MUTATING_TOOL_NAMES))
async def test_ask_mode_drops_every_mutating_verb(verb: str) -> None:
    client = _returning(_Text("no."), _ToolUse(verb, {"path": "a.ts"}))

    result = await codeagent_service.run_turn(WS, USER, _body(mode="ask"), client=client)

    assert result.toolCalls == []


@pytest.mark.parametrize("verb", sorted(MUTATING_TOOL_NAMES - {"writeFile"}))
async def test_edit_mode_still_drops_the_verbs_it_does_not_offer(verb: str) -> None:
    """Edit WIDENS the permitted set; it does not turn the filter off. A verb
    Edit never offered — hallucinated, or left over from a stale tool set — is
    dropped exactly as it is in Ask."""
    client = _returning(_Text("hm."), _ToolUse(verb, {"path": "a.ts"}))

    result = await codeagent_service.run_turn(WS, USER, _body(mode="edit"), client=client)

    assert result.toolCalls == []


async def test_edit_mode_forwards_a_write_call() -> None:
    client = _returning(
        _Text("Added the docstring."),
        _ToolUse("writeFile", {"path": "a.ts", "content": "new body"}),
    )

    result = await codeagent_service.run_turn(WS, USER, _body(mode="edit"), client=client)

    assert result.done is False
    assert [c.name for c in result.toolCalls] == ["writeFile"]
    assert result.toolCalls[0].input["content"] == "new body"


async def test_a_write_call_carries_the_models_explanation() -> None:
    """The write call ends the loop at the review gate, so the sentence the model
    wrote alongside it is the only account of what it proposed. Dropping it (as
    an Ask round legitimately does) would send the diff up unlabelled."""
    client = _returning(
        _Text("Renamed the handler and updated its one caller."),
        _ToolUse("writeFile", {"path": "a.ts", "content": "x"}),
    )

    result = await codeagent_service.run_turn(WS, USER, _body(mode="edit"), client=client)

    assert result.answer == "Renamed the handler and updated its one caller."


# ---------------------------------------------------------------------------
# Transport selection (CA-0).
# ---------------------------------------------------------------------------


def test_no_key_falls_back_to_the_cli_instead_of_503ing(monkeypatch) -> None:  # noqa: ANN001
    """THE fix for the 2026-07-22 report: a bare 503 on every Ask and Cmd-K.

    The rule this violated is written down in `instinct/auto_triage.py` — agent
    mode runs with NO key, so the LLM call must shell the `claude` CLI, NOT
    `AsyncAnthropic`. `codeagent` was the one module reaching for the direct
    client anyway, inherited from the deleted `websandbox/edit.py`.
    """
    from pocketpaw_ee.cloud.codeagent import service as codeagent_service
    from pocketpaw_ee.cloud.codeagent import transport

    monkeypatch.setattr(codeagent_service, "_api_key", lambda: "")
    monkeypatch.setattr(transport, "claude_executable", lambda: "/usr/bin/claude")

    assert isinstance(codeagent_service._default_client(), transport.ClaudeCliClient)


def test_a_key_still_wins_over_the_cli(monkeypatch) -> None:  # noqa: ANN001
    """Not a preference for the vendor — a preference for the NATIVE TOOL
    CHANNEL, which the API schema-validates and the CLI protocol cannot."""
    from pocketpaw_ee.cloud.codeagent import service as codeagent_service
    from pocketpaw_ee.cloud.codeagent import transport

    monkeypatch.setattr(codeagent_service, "_api_key", lambda: "sk-ant-test")
    monkeypatch.setattr(transport, "claude_executable", lambda: "/usr/bin/claude")

    assert not isinstance(codeagent_service._default_client(), transport.ClaudeCliClient)


def test_with_neither_the_error_names_both_ways_out(monkeypatch) -> None:  # noqa: ANN001
    """"Not configured" is true and useless. Which fix a reader wants depends on
    where they are running, so the message names both."""
    from pocketpaw_ee.cloud._core.errors import CloudError
    from pocketpaw_ee.cloud.codeagent import service as codeagent_service
    from pocketpaw_ee.cloud.codeagent import transport

    monkeypatch.setattr(codeagent_service, "_api_key", lambda: "")
    monkeypatch.setattr(transport, "claude_executable", lambda: None)

    with pytest.raises(CloudError) as exc:
        codeagent_service._default_client()

    assert exc.value.status_code == 503
    assert "Claude CLI" in exc.value.message
    assert "POCKETPAW_ANTHROPIC_API_KEY" in exc.value.message
