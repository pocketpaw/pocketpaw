# tests/cloud/runs/test_run_core_runtime_guards.py
# Pins two guards on the cloud chat run loop (``run_core``):
#   * UX-4: a reply that loops on one sentence or paragraph is stopped, the run
#     ends with a plain message, and only one copy of the loop is kept;
#   * UX-10: provider and validation errors reach the chat as plain sentences
#     (out of credit, token limit, malformed output, generic model error),
#     never as the raw LiteLLM / Pydantic text.
from __future__ import annotations

import fakeredis.aioredis
import pytest
from pocketpaw_ee.cloud.chat.agent_service import ScopeContext, ScopeKind
from pocketpaw_ee.cloud.chat.runs import run_core
from pocketpaw_ee.cloud.chat.runs import service as run_service
from pocketpaw_ee.cloud.chat.runs.domain import RunSpec
from pocketpaw_ee.cloud.chat.runs.redis_stream import RedisStreamTransport

from pocketpaw.agents.repetition_guard import (
    REPETITION_STOPPED_CODE,
    REPETITION_STOPPED_MESSAGE,
)

pytestmark = pytest.mark.asyncio

_LOOP = "I have everything I need. Let me write the complete site file and deliver it to you. "


def _spec() -> RunSpec:
    return RunSpec(
        run_id="r1",
        workspace_id="w1",
        context_type="session",
        scope_id="s1",
        session_key="session:s1",
        group=None,
        user_id="u1",
        agent_id="a1",
        client_message_id="c1",
        user_message_id="m1",
        content="build me a wedding site",
        history=[],
        intent=None,
    )


def _ctx() -> ScopeContext:
    return ScopeContext(
        kind=ScopeKind.SESSION,
        scope_id="s1",
        workspace_id="w1",
        user_id="u1",
        members=["u1"],
        target_agent_id="a1",
    )


async def _noop(*a, **k):
    return None


async def _drive(monkeypatch, events, *, events_fn=None):
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    transport = RedisStreamTransport(redis)
    await run_service.create_run(_spec())
    ctx = _ctx()
    pulled = {"n": 0}

    async def fake_resolve_scope_context(**_):
        return ctx

    async def fake_agent_events(spec, ctx):  # noqa: ARG001
        for name, data in events:
            pulled["n"] += 1
            yield (name, data)

    class _FakePool:
        async def observe(self, *a, **k):
            return None

    monkeypatch.setattr(run_core, "_iter_agent_events", events_fn or fake_agent_events)
    monkeypatch.setattr(run_core, "get_stream_transport", lambda: transport)
    monkeypatch.setattr(run_core, "_mark_running", _noop)
    monkeypatch.setattr(run_core, "_broadcast_agent_typing", _noop)
    monkeypatch.setattr(run_core, "_broadcast_message_new", _noop)
    monkeypatch.setattr(run_core, "get_agent_pool", lambda: _FakePool())
    monkeypatch.setattr(run_core, "resolve_scope_context", fake_resolve_scope_context)

    await run_core.execute_run(_spec())
    frames = [e async for e in transport.read_events("r1", after="0", block_ms=10)]
    return frames, pulled["n"]


# --- UX-4: repetition guard ---------------------------------------------------


async def test_a_looping_reply_is_stopped_with_a_plain_message(monkeypatch, mongo_db):  # noqa: ARG001
    events = [("chunk", {"content": "Lovely, a wedding site.\n\n", "type": "text"})]
    events += [("chunk", {"content": _LOOP, "type": "text"})] * 1555

    frames, pulled = await _drive(monkeypatch, events)

    assert pulled < 20, "the stream kept being consumed after the loop was obvious"
    errors = [f.data for f in frames if f.event == "error"]
    assert errors == [{"code": REPETITION_STOPPED_CODE, "message": REPETITION_STOPPED_MESSAGE}]
    streamed = "".join(f.data.get("content", "") for f in frames if f.event == "chunk")
    assert streamed.count("I have everything I need.") <= 4

    run = await run_service.get_run("r1")
    assert run.status == "failed"
    assert run.partial_text == "Lovely, a wedding site.\n\n" + _LOOP.strip()


async def test_a_reply_with_a_long_list_and_code_completes_normally(monkeypatch, mongo_db):  # noqa: ARG001
    text = (
        "Here is the menu:\n"
        + "- Seasonal special, price on request\n" * 12
        + "```css\n"
        + "  border-radius: var(--radius-large-card);\n" * 12
        + "```\nAll set."
    )
    events = [
        ("chunk", {"content": text[i : i + 9], "type": "text"}) for i in range(0, len(text), 9)
    ]

    frames, _ = await _drive(monkeypatch, events)

    assert [f for f in frames if f.event == "error"] == []
    assert (await run_service.get_run("r1")).status == "completed"


# --- UX-10: provider and validation errors ------------------------------------

_LITELLM_402 = (
    "Pydantic AI error: status_code: 402, model_name: deepseek/deepseek-v4-flash, body: "
    "{'message': 'litellm.BadRequestError: DeepseekException - "
    '{"error":{"message":"Insufficient Balance","type":"unknown_error"}}\'}'
)
_TOKEN_LIMIT = (
    "Pydantic AI error: Model token limit (provider default) exceeded before any response "
    "was generated. Increase the `max_tokens` model setting, or simplify the prompt."
)
_VALIDATION = (
    "1 validation error for SiteSpec\nsections.0.title\n  Field required "
    "[type=missing, input_value={}, input_type=dict]"
)


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        (_LITELLM_402, "agent.provider_out_of_credit"),
        ("insufficient_quota: You exceeded your current quota", "agent.provider_out_of_credit"),
        (_TOKEN_LIMIT, "agent.token_limit"),
        ("This model's maximum context length is 128000 tokens", "agent.token_limit"),
        (f"Pydantic AI error: {_VALIDATION}", "agent.malformed_output"),
        ("Pydantic AI error: Connection reset by peer", "agent.model_error"),
    ],
)
async def test_known_provider_errors_become_plain_sentences(raw, code):
    frame = run_core._backend_error_frame(raw)
    assert frame["code"] == code
    msg = frame["message"]
    for leak in ("Pydantic", "litellm", "status_code", "{", "max_tokens", "validation error"):
        assert leak not in msg
    assert msg.endswith(".")


async def test_a_crash_with_validation_text_reaches_chat_as_a_plain_sentence(
    monkeypatch,
    mongo_db,  # noqa: ARG001
):
    async def raising_events(spec, ctx):  # noqa: ARG001
        yield ("chunk", {"content": "Working on it. ", "type": "text"})
        raise ValueError(_VALIDATION)

    frames, _ = await _drive(monkeypatch, [], events_fn=raising_events)

    err = next(f.data for f in frames if f.event == "error")
    assert err["code"] == "agent.malformed_output"
    assert "validation error" not in err["message"]
    # The raw detail stays on the run doc for operators.
    assert "validation error" in (await run_service.get_run("r1")).error
