# tests/cloud/runs/test_run_core_model_availability.py
# A run that fails because the provider is out of credit marks that provider out
# of credit for the run's workspace (catalog.availability), so the model picker
# can say so before the next build. A run that completes clears the mark for its
# provider. Other failures (busy, token limit) mark nothing. The agent loop
# stamps the model it ran and whether a BYOK key paid onto the context, which is
# what the mark is keyed on.
from __future__ import annotations

from types import SimpleNamespace

import fakeredis
import fakeredis.aioredis
import pytest
from pocketpaw_ee.catalog import availability
from pocketpaw_ee.cloud._core import redis_client
from pocketpaw_ee.cloud.chat.agent_service import ScopeContext, ScopeKind
from pocketpaw_ee.cloud.chat.runs import run_core
from pocketpaw_ee.cloud.chat.runs import service as run_service
from pocketpaw_ee.cloud.chat.runs.domain import RunSpec
from pocketpaw_ee.cloud.chat.runs.redis_stream import RedisStreamTransport

pytestmark = pytest.mark.asyncio

_DEEPSEEK_402 = (
    "status_code: 402, model_name: deepseek/deepseek-v4-flash, body: "
    "{'error': {'message': 'Insufficient Balance', 'type': 'unknown_error'}}"
)
_FLASH = "deepseek/deepseek-v4-flash"


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
        content="build me a pottery site",
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


@pytest.fixture
def fake_redis(monkeypatch):
    server = fakeredis.FakeServer()
    monkeypatch.setattr(
        redis_client,
        "get_redis",
        lambda: fakeredis.aioredis.FakeRedis(server=server, decode_responses=True),
    )
    return server


async def _drive(monkeypatch, events_fn):
    transport = RedisStreamTransport(fakeredis.aioredis.FakeRedis(decode_responses=True))
    await run_service.create_run(_spec())
    ctx = _ctx()

    async def fake_resolve_scope_context(**_):
        return ctx

    class _FakePool:
        async def observe(self, *a, **k):
            return None

    monkeypatch.setattr(run_core, "_iter_agent_events", events_fn)
    monkeypatch.setattr(run_core, "get_stream_transport", lambda: transport)
    monkeypatch.setattr(run_core, "_mark_running", _noop)
    monkeypatch.setattr(run_core, "_broadcast_agent_typing", _noop)
    monkeypatch.setattr(run_core, "_broadcast_message_new", _noop)
    monkeypatch.setattr(run_core, "get_agent_pool", lambda: _FakePool())
    monkeypatch.setattr(run_core, "resolve_scope_context", fake_resolve_scope_context)

    await run_core.execute_run(_spec())


def _status(marks, model_id: str, provider: str) -> str:
    entry = SimpleNamespace(id=model_id, provider=provider)
    return availability.availability_for(entry, marks).status


async def test_an_out_of_credit_error_marks_the_provider(monkeypatch, mongo_db, fake_redis):  # noqa: ARG001
    async def events_fn(spec, ctx):  # noqa: ARG001
        ctx.turn_model = _FLASH
        ctx.backend_error_detail = _DEEPSEEK_402
        yield ("error", run_core._backend_error_frame(_DEEPSEEK_402))

    await _drive(monkeypatch, events_fn)

    marks = await availability.read_marks("w1")
    assert _status(marks, "deepseek/deepseek-v4-pro", "deepseek") == "out_of_credit"
    assert _status(marks, "anthropic/claude-sonnet-4-6", "anthropic") == "ok"
    assert await availability.read_marks("w2") == {}


async def test_an_out_of_credit_crash_marks_the_provider(monkeypatch, mongo_db, fake_redis):  # noqa: ARG001
    class _ProviderError(Exception):
        pass

    _ProviderError.__module__ = "pydantic_ai.exceptions"

    async def events_fn(spec, ctx):  # noqa: ARG001
        ctx.turn_model = _FLASH
        yield ("chunk", {"content": "Working on it. ", "type": "text"})
        raise _ProviderError(_DEEPSEEK_402)

    await _drive(monkeypatch, events_fn)

    marks = await availability.read_marks("w1")
    assert _status(marks, _FLASH, "deepseek") == "out_of_credit"


async def test_a_byok_out_of_credit_error_marks_every_model(monkeypatch, mongo_db, fake_redis):  # noqa: ARG001
    async def events_fn(spec, ctx):  # noqa: ARG001
        ctx.turn_model = _FLASH
        ctx.turn_byok = True
        yield ("error", run_core._backend_error_frame(_DEEPSEEK_402))

    await _drive(monkeypatch, events_fn)

    marks = await availability.read_marks("w1")
    assert _status(marks, "anthropic/claude-sonnet-4-6", "anthropic") == "out_of_credit"


@pytest.mark.parametrize(
    "raw",
    [
        "status_code: 429, Rate limit exceeded, try again later",
        "This model's maximum context length is 128000 tokens",
    ],
)
async def test_other_failures_mark_nothing(monkeypatch, mongo_db, fake_redis, raw):  # noqa: ARG001
    async def events_fn(spec, ctx):  # noqa: ARG001
        ctx.turn_model = _FLASH
        yield ("error", run_core._backend_error_frame(raw))

    await _drive(monkeypatch, events_fn)

    assert await availability.read_marks("w1") == {}


async def test_a_completed_run_clears_the_mark(monkeypatch, mongo_db, fake_redis):  # noqa: ARG001
    await availability.record_out_of_credit("w1", "deepseek/deepseek-v4-pro", byok=False)

    async def events_fn(spec, ctx):  # noqa: ARG001
        ctx.turn_model = _FLASH
        yield ("chunk", {"content": "Here is your site.", "type": "text"})

    await _drive(monkeypatch, events_fn)

    assert await availability.read_marks("w1") == {}


async def test_a_redis_outage_never_breaks_the_run(monkeypatch, mongo_db):  # noqa: ARG001
    def _boom():
        raise RuntimeError("redis down")

    monkeypatch.setattr(redis_client, "get_redis", _boom)

    async def events_fn(spec, ctx):  # noqa: ARG001
        ctx.turn_model = _FLASH
        yield ("error", run_core._backend_error_frame(_DEEPSEEK_402))

    await _drive(monkeypatch, events_fn)

    assert (await run_service.get_run("r1")).status == "failed"


async def test_the_agent_loop_stamps_the_turn_model(monkeypatch):
    class _Pool:
        async def get(self, _agent_id):
            return SimpleNamespace(config={"backend": "pydantic_ai", "model": _FLASH})

        def run(self, *args, **kwargs):
            async def _events():
                yield SimpleNamespace(type="error", content=_DEEPSEEK_402, metadata={})

            return _events()

        async def prewarm(self, *args, **kwargs):
            return None

    async def _fake_knowledge(*a, **k):
        return "KB"

    async def _is_cancelled():
        return False

    monkeypatch.setattr(run_core, "get_agent_pool", lambda: _Pool())
    monkeypatch.setattr(run_core, "build_knowledge_context", _fake_knowledge)
    monkeypatch.setattr(run_core, "build_behavior_instructions", lambda *a, **k: "INSTR")
    monkeypatch.setattr(run_core, "attach_sse_event_sink", lambda *a, **k: None)
    monkeypatch.setattr(run_core, "attach_agent_identity", lambda **k: None)
    monkeypatch.setattr(run_core, "detach_sse_event_sink", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(run_core, "detach_agent_identity", lambda *a, **k: None, raising=False)

    ctx = _ctx()
    _ = [
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

    assert ctx.turn_model == _FLASH
    assert ctx.turn_byok is False
