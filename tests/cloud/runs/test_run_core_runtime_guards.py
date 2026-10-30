# tests/cloud/runs/test_run_core_runtime_guards.py
# Pins two guards on the cloud chat run loop (``run_core``):
#   * UX-4: a reply that loops on one sentence or paragraph is stopped and only
#     one copy is kept. A text-only run fails with the notice; a run whose tools
#     did work completes with it. Tool calls break a run of text.
#   * UX-10: provider failures a user can act on (out of credit, token limit,
#     malformed output) reach the chat as plain sentences; operator diagnostics
#     (auth, unknown model, bad base URL, connection refused, run-limit
#     settings, unsupported parameters) and lookalike numbers pass through raw.
#     The run doc keeps the raw text. On the crash path only provider/backend
#     exceptions are classified; our own validation errors stay internal.
from __future__ import annotations

from types import SimpleNamespace

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


def _streamed(frames) -> str:
    return "".join(f.data.get("content", "") for f in frames if f.event == "chunk")


# --- UX-4: repetition guard ---------------------------------------------------


async def test_a_looping_reply_is_stopped_with_a_plain_message(monkeypatch, mongo_db):  # noqa: ARG001
    events = [("chunk", {"content": "Lovely, a wedding site.\n\n", "type": "text"})]
    events += [("chunk", {"content": _LOOP, "type": "text"})] * 1555

    frames, pulled = await _drive(monkeypatch, events)

    assert pulled < 30, "the stream kept being consumed after the loop was obvious"
    errors = [f.data for f in frames if f.event == "error"]
    assert errors == [{"code": REPETITION_STOPPED_CODE, "message": REPETITION_STOPPED_MESSAGE}]
    assert _streamed(frames).count("I have everything I need.") <= 8

    run = await run_service.get_run("r1")
    assert run.status == "failed"
    assert run.partial_text == "Lovely, a wedding site.\n\n" + _LOOP.strip()


async def test_a_loop_after_tool_work_completes_with_the_notice(monkeypatch, mongo_db):  # noqa: ARG001
    """The tools already built the site; a looping closing message must not turn
    that into a failed run."""
    events = [
        ("tool_start", {"tool": "write_file", "input": {}}),
        ("tool_result", {"tool": "write_file", "output": "ok"}),
        ("chunk", {"content": "The site is built.\n\n", "type": "text"}),
    ]
    events += [("chunk", {"content": _LOOP, "type": "text"})] * 200

    frames, _ = await _drive(monkeypatch, events)

    assert [f for f in frames if f.event == "error"] == []
    assert (await run_service.get_run("r1")).status == "completed"
    end = next(f.data for f in frames if f.event == "stream_end")
    assert end["cancelled"] is False
    assert _streamed(frames).endswith(REPETITION_STOPPED_MESSAGE)


async def test_a_status_line_between_tool_calls_does_not_stop_the_run(monkeypatch, mongo_db):  # noqa: ARG001
    line = {"content": "Let me check the preview and fix what I find.\n", "type": "text"}
    events = []
    for _ in range(30):
        events.append(("chunk", line))
        events.append(("tool_start", {"tool": "preview", "input": {}}))
        events.append(("tool_result", {"tool": "preview", "output": "ok"}))
    events.append(("chunk", {"content": "All done.", "type": "text"}))

    frames, _ = await _drive(monkeypatch, events)

    assert [f for f in frames if f.event == "error"] == []
    assert (await run_service.get_run("r1")).status == "completed"


async def test_a_reply_with_a_long_list_and_code_completes_normally(monkeypatch, mongo_db):  # noqa: ARG001
    text = (
        "Here is the menu:\n"
        + "- Seasonal special, price on request\n" * 40
        + "```css\n"
        + "  border-radius: var(--radius-large-card);\n" * 40
        + "```\nAll set."
    )
    step = 9
    events = [
        ("chunk", {"content": text[i : i + step], "type": "text"})
        for i in range(0, len(text), step)
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
_LITELLM_401_REWRITE = (
    "The LiteLLM proxy rejected model 'x' with a 401 from its UPSTREAM provider. "
    "Original error: status_code: 401, body: Insufficient Balance on upstream key"
)


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        (_LITELLM_402, "agent.provider_out_of_credit"),
        ("Error code: 402 - Payment Required", "agent.provider_out_of_credit"),
        ("insufficient_quota: You exceeded your current quota", "agent.provider_out_of_credit"),
        (_TOKEN_LIMIT, "agent.token_limit"),
        ("This model's maximum context length is 128000 tokens", "agent.token_limit"),
        (
            "maximum context length is 128000 tokens, you requested 140200 tokens",
            "agent.token_limit",
        ),
        (f"Pydantic AI error: {_VALIDATION}", "agent.malformed_output"),
    ],
)
async def test_known_provider_errors_become_plain_sentences(raw, code):
    frame = run_core._backend_error_frame(raw)
    assert frame["code"] == code
    msg = frame["message"]
    for leak in ("Pydantic", "litellm", "status_code", "{", "max_tokens", "validation error"):
        assert leak not in msg
    assert msg.endswith(".")


@pytest.mark.parametrize(
    "raw",
    [
        # A number that merely contains 402 is not a payment error.
        "Pydantic AI error: request a1b2-4020-c3d4 failed on port 14025",
        "you requested 140200 tokens",
        "context of 14290 tokens exceeds the limit",
        # Operator diagnostics: the raw text names the fix.
        "Pydantic AI error: status_code: 401, body: Invalid API key provided",
        "Pydantic AI error: status_code: 404, model 'gpt-9' not found",
        "Pydantic AI error: The next request would exceed the request_limit of 50",
        "Pydantic AI error: Exceeded the total_tokens_limit of 200000 (total_tokens=200100)",
        "Pydantic AI error: Unsupported parameter: 'max_tokens' is not supported with this model",
        "codex sdk missing",
    ],
)
async def test_diagnostics_and_lookalikes_pass_through_raw(raw):
    assert run_core._backend_error_frame(raw) == {"code": "agent.backend_error", "message": raw}


@pytest.mark.parametrize(
    "raw",
    [
        _LITELLM_401_REWRITE,
        "Pydantic AI error: Connection refused: http://localhost:11434 (is Ollama running?)",
        "Pydantic AI error: Invalid base_url 'htp://proxy'",
        "httpx.ConnectError: [Errno -2] Name or service not known",
    ],
)
async def test_an_unreachable_or_misconfigured_provider_gets_the_admin_sentence(raw):
    assert run_core._backend_error_frame(raw) == {
        "code": run_core.PROVIDER_SETUP_CODE,
        "message": run_core.PROVIDER_SETUP_MESSAGE,
    }


async def test_the_litellm_rewrite_never_shows_the_proxy_or_setting_names():
    rewrite = (
        "The LiteLLM proxy rejected model 'x' with a 401 from its UPSTREAM provider. "
        "curl -H 'Authorization: Bearer $POCKETPAW_LITELLM_API_KEY' "
        "https://llm.internal.example:4000/health and set POCKETPAW_PYDANTIC_AI_MODEL"
    )
    frame = run_core._backend_error_frame(rewrite)
    assert frame["code"] == run_core.PROVIDER_SETUP_CODE
    for leak in ("llm.internal", "POCKETPAW_", "4000", "curl"):
        assert leak not in frame["message"]


async def test_a_key_echoed_in_an_auth_error_is_redacted():
    # Built at runtime so the diff secret scan doesn't see a literal key.
    key = "sk-or-v1-" + "0123456789abcdef0123456789abcdef"
    raw = f"Pydantic AI error: status_code: 401, body: Incorrect API key provided: {key}"
    frame = run_core._backend_error_frame(raw)
    assert frame["code"] == "agent.backend_error"
    assert key not in frame["message"]
    assert "Incorrect API key provided" in frame["message"]


async def test_an_internal_address_in_a_diagnostic_is_not_shown():
    raw = (
        "Pydantic AI error: status_code: 404, model 'gpt-9' not found at "
        "https://proxy.internal.example/v1/chat (10.0.3.7:4000), check POCKETPAW_LITELLM_API_BASE"
    )
    msg = run_core._backend_error_frame(raw)["message"]
    for leak in ("proxy.internal", "10.0.3.7", "4000", "POCKETPAW_"):
        assert leak not in msg
    assert "not found" in msg


async def test_a_comma_grouped_count_is_not_a_status_code():
    raw = "maximum context length is 128000 tokens, you requested 128,404 tokens"
    assert run_core._backend_error_frame(raw)["code"] == "agent.token_limit"


async def test_our_own_httpx_crash_stays_generic(monkeypatch, mongo_db):  # noqa: ARG001
    import httpx

    async def raising_events(spec, ctx):  # noqa: ARG001
        yield ("chunk", {"content": "Working on it. ", "type": "text"})
        raise httpx.ReadTimeout("timed out reading http://mongo-proxy.internal:27017/x")

    frames, _ = await _drive(monkeypatch, [], events_fn=raising_events)

    err = next(f.data for f in frames if f.event == "error")
    assert err["code"] == "agent.run_failed"
    assert "mongo-proxy.internal" not in err["message"]
    assert "27017" not in err["message"]
    # The run doc keeps the raw text for operators.
    assert "mongo-proxy.internal" in (await run_service.get_run("r1")).error


async def test_a_provider_crash_with_a_key_is_redacted(monkeypatch, mongo_db):  # noqa: ARG001
    # Built at runtime so the diff secret scan doesn't see a literal key.
    key = "sk-" + "abcdefghijklmnopqrstuvwxyz012345"

    async def raising_events(spec, ctx):  # noqa: ARG001
        yield ("chunk", {"content": "Working on it. ", "type": "text"})
        raise _ProviderError(f"status_code: 401, Incorrect API key provided: {key}")

    frames, _ = await _drive(monkeypatch, [], events_fn=raising_events)

    err = next(f.data for f in frames if f.event == "error")
    assert key not in err["message"]
    assert key in (await run_service.get_run("r1")).error


async def test_the_run_doc_keeps_the_raw_provider_text(monkeypatch, mongo_db):  # noqa: ARG001
    async def events_fn(spec, ctx):  # noqa: ARG001
        yield ("chunk", {"content": "Working on it. ", "type": "text"})
        ctx.backend_error_detail = _LITELLM_402
        yield ("error", run_core._backend_error_frame(_LITELLM_402))

    frames, _ = await _drive(monkeypatch, [], events_fn=events_fn)

    err = next(f.data for f in frames if f.event == "error")
    assert err["code"] == "agent.provider_out_of_credit"
    assert (await run_service.get_run("r1")).error == _LITELLM_402


async def test_the_agent_loop_records_the_raw_text_on_the_context(monkeypatch):
    class _ErrorPool:
        async def get(self, _agent_id):
            return SimpleNamespace(config={"backend": "pydantic_ai"})

        def run(self, *args, **kwargs):
            async def _events():
                yield SimpleNamespace(type="error", content=_LITELLM_402, metadata={})

            return _events()

        async def prewarm(self, *args, **kwargs):
            return None

    async def _fake_knowledge(*a, **k):
        return "KB"

    async def _is_cancelled():
        return False

    monkeypatch.setattr(run_core, "get_agent_pool", lambda: _ErrorPool())
    monkeypatch.setattr(run_core, "build_knowledge_context", _fake_knowledge)
    monkeypatch.setattr(run_core, "build_behavior_instructions", lambda *a, **k: "INSTR")
    monkeypatch.setattr(run_core, "attach_sse_event_sink", lambda *a, **k: None)
    monkeypatch.setattr(run_core, "attach_agent_identity", lambda **k: None)
    monkeypatch.setattr(run_core, "detach_sse_event_sink", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(run_core, "detach_agent_identity", lambda *a, **k: None, raising=False)

    ctx = _ctx()
    out = [
        ev
        async for ev in run_core._drive_agent_loop(
            ctx,
            user_content="hi",
            attachments_in=None,
            mentions_in=None,
            history=[],
            is_cancelled=_is_cancelled,
            emit_stream_start=False,
        )
    ]

    assert ("error", run_core._backend_error_frame(_LITELLM_402)) in out
    assert ctx.backend_error_detail == _LITELLM_402


class _ProviderError(Exception):
    """Stands in for a provider SDK exception."""


_ProviderError.__module__ = "pydantic_ai.exceptions"


async def test_a_provider_crash_reaches_chat_as_a_plain_sentence(monkeypatch, mongo_db):  # noqa: ARG001
    async def raising_events(spec, ctx):  # noqa: ARG001
        yield ("chunk", {"content": "Working on it. ", "type": "text"})
        raise _ProviderError(_VALIDATION)

    frames, _ = await _drive(monkeypatch, [], events_fn=raising_events)

    err = next(f.data for f in frames if f.event == "error")
    assert err["code"] == "agent.malformed_output"
    assert "validation error" in (await run_service.get_run("r1")).error


async def test_our_own_validation_error_stays_a_generic_internal_error(monkeypatch, mongo_db):  # noqa: ARG001
    from pydantic import BaseModel

    class _Doc(BaseModel):
        title: str

    async def raising_events(spec, ctx):  # noqa: ARG001
        yield ("chunk", {"content": "Working on it. ", "type": "text"})
        _Doc.model_validate({})

    frames, _ = await _drive(monkeypatch, [], events_fn=raising_events)

    err = next(f.data for f in frames if f.event == "error")
    assert err == {"code": "agent.run_failed", "message": run_core.INTERNAL_ERROR_MESSAGE}
    assert "validation error" in (await run_service.get_run("r1")).error


async def test_an_ordinary_internal_crash_keeps_its_text(monkeypatch, mongo_db):  # noqa: ARG001
    async def raising_events(spec, ctx):  # noqa: ARG001
        yield ("chunk", {"content": "Working on it. ", "type": "text"})
        raise RuntimeError("token limit bookkeeping broke")

    frames, _ = await _drive(monkeypatch, [], events_fn=raising_events)

    err = next(f.data for f in frames if f.event == "error")
    assert err == {"code": "agent.run_failed", "message": "token limit bookkeeping broke"}
