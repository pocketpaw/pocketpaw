"""Tests for ``execute_run``.

Includes the RFC 13 M0 inline-spec contract coverage: ``_extract_ripple_attachment``
must pull a canonical ``ui-spec`` + ``{version, ui}`` block AND a transitional
legacy ``json`` + ``{widgets, lifecycle}`` block, both into a ripple attachment and
the ``ripple`` SSE event, while leaving a truncated / non-spec fence inline.
"""

from __future__ import annotations

import asyncio
from typing import Any

import fakeredis.aioredis
import pytest
from pocketpaw_ee.cloud.chat.agent_service import ScopeContext, ScopeKind
from pocketpaw_ee.cloud.chat.runs import run_core
from pocketpaw_ee.cloud.chat.runs.domain import RunSpec
from pocketpaw_ee.cloud.chat.runs.redis_stream import RedisStreamTransport
from pocketpaw_ee.cloud.surface import resolve_profile

pytestmark = pytest.mark.asyncio


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
        content="hi",
        history=[],
        intent=None,
    )


async def _noop(*a, **k):
    return None


async def _persist_stub(spec, ctx, full_text, attachments):
    return "assistant-msg-1"


async def fake_agent_events(spec, ctx):
    yield ("chunk", {"content": "Hello", "type": "text"})
    yield ("chunk", {"content": " world", "type": "text"})


async def fake_resolve_scope_context(**_):
    class _Ctx:
        kind = type("K", (), {"value": "session"})()
        scope_id = "s1"
        workspace_id = "w1"
        user_id = "u1"
        target_agent_id = "a1"
        members = ["u1"]
        session_id = None
        intent = None

    return _Ctx()


async def test_execute_run_writes_chunks_then_stream_end(monkeypatch):
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    transport = RedisStreamTransport(redis)

    monkeypatch.setattr(run_core, "_iter_agent_events", fake_agent_events)
    monkeypatch.setattr(run_core, "get_stream_transport", lambda: transport)
    monkeypatch.setattr(run_core, "_mark_running", _noop)
    monkeypatch.setattr(run_core, "_persist_and_complete", _persist_stub)
    monkeypatch.setattr(run_core, "_broadcast_agent_typing", _noop)
    monkeypatch.setattr(run_core, "resolve_scope_context", fake_resolve_scope_context)

    await run_core.execute_run(_spec())

    events = [e async for e in transport.read_events("r1", after="0", block_ms=10)]
    assert [e.event for e in events] == ["chunk", "chunk", "stream_end"]
    assert events[-1].data["assistant_message_id"] == "assistant-msg-1"
    assert events[-1].data["cancelled"] is False


async def test_execute_run_cancelled_does_not_persist(monkeypatch):
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    transport = RedisStreamTransport(redis)
    await transport.request_cancel("r1")  # cancel BEFORE the run starts

    persisted: list[str] = []

    async def _track_persist(*a, **k):
        persisted.append("called")
        return "should-not-happen"

    monkeypatch.setattr(run_core, "_iter_agent_events", fake_agent_events)
    monkeypatch.setattr(run_core, "get_stream_transport", lambda: transport)
    monkeypatch.setattr(run_core, "_mark_running", _noop)
    monkeypatch.setattr(run_core, "_persist_and_complete", _track_persist)
    monkeypatch.setattr(run_core, "_broadcast_agent_typing", _noop)
    monkeypatch.setattr(run_core, "resolve_scope_context", fake_resolve_scope_context)
    # cancel + mark_terminal path also touches run_service.mark_terminal
    monkeypatch.setattr("pocketpaw_ee.cloud.chat.runs.run_core.run_service.mark_terminal", _noop)

    await run_core.execute_run(_spec())

    events = [e async for e in transport.read_events("r1", after="0", block_ms=10)]
    assert events[-1].event == "stream_end"
    assert events[-1].data["cancelled"] is True
    assert events[-1].data["assistant_message_id"] is None
    assert persisted == []


async def fake_agent_events_empty(spec, ctx):
    # Tool-only turn: the agent runs to completion but produces no text.
    yield ("tool_start", {"tool": "noop", "input": {}})
    yield ("tool_result", {"tool": "noop", "output": {}})


async def test_execute_run_empty_text_marks_completed(monkeypatch):
    """Regression: a non-cancelled run with no assistant text must still
    flip the ChatRunDoc out of ``running`` — without this, the sweeper
    eventually marks it ``interrupted`` (semantically wrong) and until
    then ``active_run`` ghosts on the client."""
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    transport = RedisStreamTransport(redis)

    persisted: list[str] = []
    mark_calls: list[dict[str, Any]] = []

    async def _track_persist(*a, **k):
        persisted.append("called")
        return "should-not-happen"

    async def _track_completed(run_id, *, assistant_message_id, partial_text):
        mark_calls.append(
            {
                "fn": "mark_completed",
                "run_id": run_id,
                "assistant_message_id": assistant_message_id,
                "partial_text": partial_text,
            }
        )

    async def _track_terminal(run_id, *, status, partial_text="", **k):
        mark_calls.append(
            {
                "fn": "mark_terminal",
                "run_id": run_id,
                "status": status,
                "partial_text": partial_text,
            }
        )

    monkeypatch.setattr(run_core, "_iter_agent_events", fake_agent_events_empty)
    monkeypatch.setattr(run_core, "get_stream_transport", lambda: transport)
    monkeypatch.setattr(run_core, "_mark_running", _noop)
    monkeypatch.setattr(run_core, "_persist_and_complete", _track_persist)
    monkeypatch.setattr(run_core, "_broadcast_agent_typing", _noop)
    monkeypatch.setattr(run_core, "resolve_scope_context", fake_resolve_scope_context)
    monkeypatch.setattr(
        "pocketpaw_ee.cloud.chat.runs.run_core.run_service.mark_completed", _track_completed
    )
    monkeypatch.setattr(
        "pocketpaw_ee.cloud.chat.runs.run_core.run_service.mark_terminal", _track_terminal
    )

    await run_core.execute_run(_spec())

    events = [e async for e in transport.read_events("r1", after="0", block_ms=10)]
    assert events[-1].event == "stream_end"
    assert events[-1].data["cancelled"] is False
    assert events[-1].data["assistant_message_id"] is None
    assert persisted == []
    assert mark_calls == [
        {
            "fn": "mark_completed",
            "run_id": "r1",
            "assistant_message_id": None,
            "partial_text": "",
        }
    ]


async def fake_agent_events_backend_error(spec, ctx):
    # Backend-yielded error (e.g. codex_cli without ``openai-codex-sdk``),
    # surfaced through _drive_agent_loop's ``elif etype == "error"`` branch.
    yield ("error", {"code": "agent.backend_error", "message": "codex sdk missing"})


async def test_execute_run_backend_error_marks_failed(monkeypatch):
    """Regression for PR #1191's fix, ported into _drive_agent_loop: when
    the backend yields an error event, the doc must end up ``failed`` (not
    silently ``completed`` via the empty-text path).

    Replaces the wire-shape test ``tests/cloud/test_agent_router_backend_error.py``
    that PR #1191 added against the now-deleted ``_run_agent_stream``.
    """
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    transport = RedisStreamTransport(redis)

    persisted: list[str] = []
    mark_calls: list[dict[str, Any]] = []

    async def _track_persist(*a, **k):
        persisted.append("called")
        return "should-not-happen"

    async def _track_terminal(run_id, *, status, partial_text="", error=None, **k):
        mark_calls.append(
            {"run_id": run_id, "status": status, "partial_text": partial_text, "error": error}
        )

    async def _track_completed(*a, **k):
        mark_calls.append({"fn": "mark_completed"})

    monkeypatch.setattr(run_core, "_iter_agent_events", fake_agent_events_backend_error)
    monkeypatch.setattr(run_core, "get_stream_transport", lambda: transport)
    monkeypatch.setattr(run_core, "_mark_running", _noop)
    monkeypatch.setattr(run_core, "_persist_and_complete", _track_persist)
    monkeypatch.setattr(run_core, "_broadcast_agent_typing", _noop)
    monkeypatch.setattr(run_core, "resolve_scope_context", fake_resolve_scope_context)
    monkeypatch.setattr(
        "pocketpaw_ee.cloud.chat.runs.run_core.run_service.mark_terminal", _track_terminal
    )
    monkeypatch.setattr(
        "pocketpaw_ee.cloud.chat.runs.run_core.run_service.mark_completed", _track_completed
    )

    await run_core.execute_run(_spec())

    events = [e async for e in transport.read_events("r1", after="0", block_ms=10)]
    # ``error`` is terminal — read_events stops here, and we MUST NOT have
    # appended a stream_end frame after it.
    assert [e.event for e in events] == ["error"]
    assert events[0].data["message"] == "codex sdk missing"
    assert persisted == []
    assert mark_calls == [
        {
            "run_id": "r1",
            "status": "failed",
            "partial_text": "",
            "error": "codex sdk missing",
        }
    ]


async def fake_agent_events_cancelled(spec, ctx):
    yield ("chunk", {"content": "partial ", "type": "text"})
    raise asyncio.CancelledError()


async def test_execute_run_propagates_cancellation(monkeypatch):
    """When the task is cancelled mid-stream (arq worker shutdown), the
    agent loop must (a) NOT swallow CancelledError, (b) mark the run
    ``interrupted`` with the partial text preserved, (c) append a terminal
    event to the stream so live SSE subscribers finalise, and (d) re-raise
    so the arq worker actually exits."""
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    transport = RedisStreamTransport(redis)

    persisted: list[str] = []
    mark_calls: list[dict[str, Any]] = []

    async def _track_persist(*a, **k):
        persisted.append("called")
        return "should-not-happen"

    async def _track_terminal(run_id, *, status, partial_text="", error=None, **k):
        mark_calls.append(
            {"run_id": run_id, "status": status, "partial_text": partial_text, "error": error}
        )

    monkeypatch.setattr(run_core, "_iter_agent_events", fake_agent_events_cancelled)
    monkeypatch.setattr(run_core, "get_stream_transport", lambda: transport)
    monkeypatch.setattr(run_core, "_mark_running", _noop)
    monkeypatch.setattr(run_core, "_persist_and_complete", _track_persist)
    monkeypatch.setattr(run_core, "_broadcast_agent_typing", _noop)
    monkeypatch.setattr(run_core, "resolve_scope_context", fake_resolve_scope_context)
    monkeypatch.setattr(
        "pocketpaw_ee.cloud.chat.runs.run_core.run_service.mark_terminal", _track_terminal
    )

    with pytest.raises(asyncio.CancelledError):
        await run_core.execute_run(_spec())

    events = [e async for e in transport.read_events("r1", after="0", block_ms=10)]
    # The chunk made it through, then a terminal `interrupted` frame.
    assert events[0].event == "chunk"
    assert events[-1].event == "interrupted"
    assert events[-1].is_terminal
    assert persisted == []
    assert mark_calls == [
        {
            "run_id": "r1",
            "status": "interrupted",
            "partial_text": "partial ",
            "error": None,
        }
    ]


async def test_execute_run_cancellation_preserves_original_exception(monkeypatch):
    """Review finding #6 — the host-cancellation re-raise must use the
    original CancelledError instance (bare ``raise``), not a fresh one, so
    arq sees the cancel reason it sent and the original traceback survives.
    """
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    transport = RedisStreamTransport(redis)

    async def long_running_events(spec, ctx):
        yield ("chunk", {"content": "x", "type": "text"})
        # Block long enough for the cancel to land in this await.
        await asyncio.sleep(5)

    monkeypatch.setattr(run_core, "_iter_agent_events", long_running_events)
    monkeypatch.setattr(run_core, "get_stream_transport", lambda: transport)
    monkeypatch.setattr(run_core, "_mark_running", _noop)
    monkeypatch.setattr(run_core, "_persist_and_complete", _persist_stub)
    monkeypatch.setattr(run_core, "_broadcast_agent_typing", _noop)
    monkeypatch.setattr(run_core, "resolve_scope_context", fake_resolve_scope_context)
    monkeypatch.setattr("pocketpaw_ee.cloud.chat.runs.run_core.run_service.mark_terminal", _noop)

    task = asyncio.create_task(run_core.execute_run(_spec()))
    # Give the first chunk a moment to flow through; then cancel with a
    # specific reason that we expect to survive the re-raise.
    await asyncio.sleep(0.05)
    task.cancel("worker SIGTERM, graceful shutdown")

    with pytest.raises(asyncio.CancelledError) as excinfo:
        await task

    # The cancel-reason supplied via task.cancel(msg) survives the cleanup
    # path. A fresh ``raise asyncio.CancelledError()`` would drop the args.
    assert excinfo.value.args == ("worker SIGTERM, graceful shutdown",)


async def test_execute_run_cancellation_cleanup_survives_second_cancel(monkeypatch):
    """Review finding #3 — when a second cancel arrives during the
    interrupted cleanup (SIGKILL grace window), ``asyncio.shield`` must keep
    mark_terminal + append + set_ttl running to completion so the doc isn't
    stranded in ``running`` with no terminal stream frame."""
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    transport = RedisStreamTransport(redis)

    mark_started = asyncio.Event()
    mark_finished = asyncio.Event()

    async def slow_mark_terminal(run_id, **kwargs):
        mark_started.set()
        # The cleanup is in flight; arrange for the OUTER task to be
        # cancelled while we're awaiting this sleep.
        await asyncio.sleep(0.1)
        mark_finished.set()

    async def fake_events(spec, ctx):
        yield ("chunk", {"content": "partial ", "type": "text"})
        raise asyncio.CancelledError()

    monkeypatch.setattr(run_core, "_iter_agent_events", fake_events)
    monkeypatch.setattr(run_core, "get_stream_transport", lambda: transport)
    monkeypatch.setattr(run_core, "_mark_running", _noop)
    monkeypatch.setattr(run_core, "_persist_and_complete", _persist_stub)
    monkeypatch.setattr(run_core, "_broadcast_agent_typing", _noop)
    monkeypatch.setattr(run_core, "resolve_scope_context", fake_resolve_scope_context)
    monkeypatch.setattr(
        "pocketpaw_ee.cloud.chat.runs.run_core.run_service.mark_terminal",
        slow_mark_terminal,
    )

    task = asyncio.create_task(run_core.execute_run(_spec()))
    await asyncio.wait_for(mark_started.wait(), timeout=1.0)

    # Second cancel arrives while mark_terminal is still running. Without
    # shield this would abort the cleanup mid-flight; with shield, the
    # cleanup task continues to completion in the background.
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    await asyncio.wait_for(mark_finished.wait(), timeout=2.0)


async def fake_agent_events_raising(spec, ctx):
    yield ("chunk", {"content": "partial ", "type": "text"})
    raise RuntimeError("boom")


async def test_execute_run_failure_marks_failed_with_error(monkeypatch):
    """When the agent loop raises, execute_run must (a) write an ``error``
    SSE frame, (b) mark the doc ``failed`` with the error message, and
    (c) preserve any partial text already produced."""
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    transport = RedisStreamTransport(redis)

    persisted: list[str] = []
    mark_calls: list[dict[str, Any]] = []

    async def _track_persist(*a, **k):
        persisted.append("called")
        return "should-not-happen"

    async def _track_terminal(run_id, *, status, partial_text="", error=None, **k):
        mark_calls.append(
            {
                "run_id": run_id,
                "status": status,
                "partial_text": partial_text,
                "error": error,
            }
        )

    monkeypatch.setattr(run_core, "_iter_agent_events", fake_agent_events_raising)
    monkeypatch.setattr(run_core, "get_stream_transport", lambda: transport)
    monkeypatch.setattr(run_core, "_mark_running", _noop)
    monkeypatch.setattr(run_core, "_persist_and_complete", _track_persist)
    monkeypatch.setattr(run_core, "_broadcast_agent_typing", _noop)
    monkeypatch.setattr(run_core, "resolve_scope_context", fake_resolve_scope_context)
    monkeypatch.setattr(
        "pocketpaw_ee.cloud.chat.runs.run_core.run_service.mark_terminal", _track_terminal
    )

    await run_core.execute_run(_spec())

    events = [e async for e in transport.read_events("r1", after="0", block_ms=10)]
    event_names = [e.event for e in events]
    assert "error" in event_names
    err = next(e for e in events if e.event == "error")
    assert err.data["code"] == "agent.run_failed"
    assert "boom" in err.data["message"]
    assert persisted == []
    assert mark_calls == [
        {
            "run_id": "r1",
            "status": "failed",
            "partial_text": "partial ",
            "error": "boom",
        }
    ]


# ---------------------------------------------------------------------------
# RFC 13 M0 — inline-spec contract unification.
#
# The cloud extractor now treats ``ui-spec`` + ``{version, ui}`` as canonical
# (the prompt's contract, the shape ``MarkdownRenderer`` tokenizes) and keeps a
# transitional branch for the deprecated ``json`` + ``{widgets, lifecycle}``
# shape. These tests pin both paths plus the truncated-fence recovery contract.
# ---------------------------------------------------------------------------

_CANONICAL_UI_SPEC_BLOCK = (
    "Here are your numbers:\n\n"
    "```ui-spec\n"
    '{"version": "1.0", "ui": {"type": "stat", '
    '"props": {"label": "Revenue", "value": "$42k"}}}\n'
    "```\n\nLet me know if you want a breakdown."
)

_LEGACY_JSON_BLOCK = (
    "Dashboard:\n\n"
    "```json\n"
    '{"widgets": [{"type": "metric", "name": "Sales"}], '
    '"lifecycle": {"type": "persistent", "id": "p1"}}\n'
    "```"
)


async def test_extract_canonical_ui_spec_block():
    """Canonical ``ui-spec`` + ``{version, ui}`` extracts, normalizes, strips."""
    remaining, spec = run_core._extract_ripple_attachment(_CANONICAL_UI_SPEC_BLOCK)

    assert spec is not None
    # Envelope + tree survive normalization.
    assert spec["version"] == "1.0"
    assert spec["ui"]["type"] == "stat"
    assert spec["ui"]["props"]["label"] == "Revenue"
    # The fence is stripped out of the message body; the prose stays.
    assert "```ui-spec" not in remaining
    assert "Here are your numbers:" in remaining
    assert "Let me know if you want a breakdown." in remaining


async def test_extract_legacy_json_widgets_block_still_works():
    """Transitional path: deprecated ``json`` + ``{widgets, lifecycle}`` extracts."""
    remaining, spec = run_core._extract_ripple_attachment(_LEGACY_JSON_BLOCK)

    assert spec is not None
    # The legacy widgets shape passes through the normalizer's widgets branch.
    assert "widgets" in spec
    assert spec["widgets"][0]["name"] == "Sales"
    assert "```json" not in remaining
    assert remaining == "Dashboard:"


async def test_extract_truncated_ui_spec_leaves_text_inline():
    """A truncated / unparseable ``ui-spec`` fence returns no attachment and
    leaves the text untouched, so the frontend's ``ui-spec-error`` path owns it."""
    truncated = (
        'Oops:\n\n```ui-spec\n{"version": "1.0", "ui": {"type": "stat", "props": {"label":\n```'
    )
    remaining, spec = run_core._extract_ripple_attachment(truncated)

    assert spec is None
    assert remaining == truncated


async def test_extract_non_spec_json_fence_is_not_attached():
    """A plain ``json`` object that is not a ripple spec must not be extracted."""
    plain = 'Config:\n\n```json\n{"foo": 1, "bar": 2}\n```'
    remaining, spec = run_core._extract_ripple_attachment(plain)

    assert spec is None
    assert remaining == plain


async def test_extract_non_spec_ui_spec_fence_does_not_fall_back():
    """A ``ui-spec`` fence whose body is not a spec is left inline and does NOT
    silently fall through to the legacy ``json`` path."""
    nonspec = 'X:\n\n```ui-spec\n{"foo": 1}\n```'
    remaining, spec = run_core._extract_ripple_attachment(nonspec)

    assert spec is None
    assert remaining == nonspec


async def test_extract_prefers_canonical_over_legacy_when_both_present():
    """If a reply carries both fences, the canonical ``ui-spec`` wins."""
    both = _CANONICAL_UI_SPEC_BLOCK + "\n\n" + _LEGACY_JSON_BLOCK
    remaining, spec = run_core._extract_ripple_attachment(both)

    assert spec is not None
    # Canonical extracted: it has a ``ui`` tree, not the legacy widgets list.
    assert spec["ui"]["type"] == "stat"
    assert "widgets" not in spec
    # Only the canonical fence is stripped; the legacy block stays in the body.
    assert "```ui-spec" not in remaining
    assert "```json" in remaining


def _fenced_reply_events(block: str):
    async def _gen(spec, ctx):
        yield ("chunk", {"content": block, "type": "text"})

    return _gen


async def _run_and_collect_ripple_event(monkeypatch, block: str):
    """Drive ``execute_run`` with a single chunk carrying ``block`` and return
    the appended ``ripple`` SSE event payload (or ``None``)."""
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    transport = RedisStreamTransport(redis)

    monkeypatch.setattr(run_core, "_iter_agent_events", _fenced_reply_events(block))
    monkeypatch.setattr(run_core, "get_stream_transport", lambda: transport)
    monkeypatch.setattr(run_core, "_mark_running", _noop)
    monkeypatch.setattr(run_core, "_persist_and_complete", _persist_stub)
    monkeypatch.setattr(run_core, "_broadcast_agent_typing", _noop)
    monkeypatch.setattr(run_core, "resolve_scope_context", fake_resolve_scope_context)

    await run_core.execute_run(_spec())

    events = [e async for e in transport.read_events("r1", after="0", block_ms=10)]
    ripple = next((e for e in events if e.event == "ripple"), None)
    return ripple, events


async def test_execute_run_emits_ripple_event_for_canonical_block(monkeypatch):
    """End-to-end: a canonical ``ui-spec`` reply produces a ``ripple`` SSE event
    carrying the normalized spec, and the fence is gone from the persisted text."""
    ripple, events = await _run_and_collect_ripple_event(monkeypatch, _CANONICAL_UI_SPEC_BLOCK)

    assert ripple is not None
    assert ripple.data["spec"]["ui"]["type"] == "stat"
    assert ripple.data["spec"]["version"] == "1.0"
    # ``ripple`` fires before the terminal ``stream_end``.
    assert [e.event for e in events][-1] == "stream_end"


async def test_execute_run_emits_ripple_event_for_legacy_block(monkeypatch):
    """End-to-end (transitional): a legacy ``json`` + ``{widgets}`` reply still
    produces a ``ripple`` SSE event so in-flight conversations keep rendering."""
    ripple, events = await _run_and_collect_ripple_event(monkeypatch, _LEGACY_JSON_BLOCK)

    assert ripple is not None
    assert "widgets" in ripple.data["spec"]
    assert ripple.data["spec"]["widgets"][0]["name"] == "Sales"
    assert [e.event for e in events][-1] == "stream_end"


async def test_execute_run_no_ripple_event_for_truncated_block(monkeypatch):
    """A truncated ``ui-spec`` reply emits no ``ripple`` event; the text streams
    through and the run still completes via ``stream_end``."""
    truncated = (
        'Oops:\n\n```ui-spec\n{"version": "1.0", "ui": {"type": "stat", "props": {"label":\n```'
    )
    ripple, events = await _run_and_collect_ripple_event(monkeypatch, truncated)

    assert ripple is None
    assert [e.event for e in events][-1] == "stream_end"


# ---------------------------------------------------------------------------
# RFC 13 M3 — the `start_flow` authoring tool emits a doc that rides this M0
# contract. A flow scaffolded by the builder, dropped into a ``ui-spec`` fence,
# must extract through the same canonical path (the whole point of unifying the
# envelope at M0 before layering flows at M3).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("flow_type", ["onboarding_wizard", "due_diligence_intake"])
async def test_start_flow_doc_extracts_through_canonical_path(flow_type):
    from pocketpaw.tools.builtin.flow_tool import StartFlowTool

    doc_str = await StartFlowTool().execute(flow_type=flow_type)
    message = f"Here's your flow:\n\n```ui-spec\n{doc_str}\n```"

    stripped, attachment = run_core._extract_ripple_attachment(message)

    # The flow doc is pulled out as the ripple attachment...
    assert attachment is not None
    assert attachment["version"] == "1.0"
    assert isinstance(attachment["ui"], dict)
    # ...the nested Chain Flow survives intact (root branches via chain_map)...
    assert isinstance(attachment["ui"].get("chain_map"), dict)
    assert attachment["ui"].get("flowId")
    # ...and the fence is stripped from the persisted message body.
    assert "ui-spec" not in stripped


# --- surface_context survives the RunSpec/executor boundary -----------------


def _sites_svelte_spec() -> RunSpec:
    """A /sites svelte-CREATE turn: ``surface="sites"`` + ``engine="svelte"``,
    no ``pocket_id`` (so the resolver picks the svelte-create profile that
    denies the two ripple-create tools)."""
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
        content="build a dentist landing site",
        history=[],
        intent=None,
        surface="sites",
        surface_meta={"engine": "svelte"},
    )


def _scope_only_ctx() -> ScopeContext:
    """A real ``ScopeContext`` exactly as ``resolve_scope_context`` builds it:
    scope/tenancy fields populated, ``surface_context`` left ``None``. This is
    the executor's starting point — the bug is that nothing re-populates
    ``surface_context`` from the spec before the agent loop reads it."""
    return ScopeContext(
        kind=ScopeKind.SESSION,
        scope_id="s1",
        workspace_id="w1",
        user_id="u1",
        members=["u1"],
        target_agent_id="a1",
    )


async def test_execute_run_threads_surface_meta_into_ctx(monkeypatch):
    """Regression: the /sites SurfaceProfile gate must survive the
    RunSpec/executor boundary.

    The HTTP handler resolves ``ctx.surface_context`` but submits a
    ``RunSpec`` to the executor; the executor rebuilds its OWN ctx via
    ``resolve_scope_context`` (scope only) and used to leave
    ``surface_context`` as ``None``. That silently no-ops the entire
    SurfaceProfile mechanism (tool-deny, ripple-block omission, preamble,
    skill) on the real ``/agent`` path. Here we assert the executor
    re-resolves ``surface_context`` from the spec, so the resolved deny set
    reaching the agent loop is the non-empty ripple-create pair.
    """
    captured: dict[str, Any] = {}

    async def _capture_ctx(spec, ctx):
        # Intercept exactly where the executor hands its ctx to the agent
        # loop. By this point ``execute_run`` must have re-resolved
        # ``surface_context`` from the spec.
        captured["surface_context"] = ctx.surface_context
        captured["resolved_profile"] = ctx.resolved_profile
        return
        yield  # pragma: no cover - make this an async generator

    async def _fake_resolve_scope_context(**_):
        return _scope_only_ctx()

    async def _noop_ensure(**_):
        return None

    monkeypatch.setattr(run_core, "_iter_agent_events", _capture_ctx)
    monkeypatch.setattr(run_core, "resolve_scope_context", _fake_resolve_scope_context)
    monkeypatch.setattr(run_core, "_mark_running", _noop)
    monkeypatch.setattr(run_core, "_persist_and_complete", _persist_stub)
    monkeypatch.setattr(run_core, "_broadcast_agent_typing", _noop)
    monkeypatch.setattr("pocketpaw_ee.cloud.sessions.service.ensure_for_agent_scope", _noop_ensure)

    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    transport = RedisStreamTransport(redis)
    monkeypatch.setattr(run_core, "get_stream_transport", lambda: transport)

    await run_core.execute_run(_sites_svelte_spec())

    surface_context = captured.get("surface_context")
    # BEFORE the fix this is ``None`` (surface info dropped at the boundary).
    assert surface_context is not None, (
        "executor dropped surface_context — RunSpec carried no surface/surface_meta"
    )
    deny = resolve_profile(surface_context.kind, surface_context.meta).deny_mcp_tool_ids
    # The two ripple-create tools the /sites svelte-create profile forbids.
    assert deny == frozenset(
        {
            "mcp__pocketpaw_sites_manager__create_landing_site",
            "mcp__pocketpaw_pocket_specialist__create",
        }
    )
    # entity-rooms chunk ①: the deny now reaches the loop via the once-per-run
    # ``ctx.resolved_profile`` (resolved in execute_run). For a no-pocket /sites
    # svelte-create turn the resolved profile == the surface base, so its deny
    # matches the surface deny above.
    resolved = captured.get("resolved_profile")
    assert resolved is not None, "execute_run must stash ctx.resolved_profile"
    assert resolved.deny_mcp_tool_ids == deny


async def test_execute_run_legacy_path_leaves_surface_context_none(monkeypatch):
    """A spec with no surface hint (older clients) must keep the legacy path:
    ``surface_context`` resolves to a GENERIC context with an empty deny set,
    so non-/sites turns are unchanged."""
    captured: dict[str, Any] = {}

    async def _capture_ctx(spec, ctx):
        captured["surface_context"] = ctx.surface_context
        captured["resolved_profile"] = ctx.resolved_profile
        return
        yield  # pragma: no cover - make this an async generator

    async def _fake_resolve_scope_context(**_):
        return _scope_only_ctx()

    async def _noop_ensure(**_):
        return None

    monkeypatch.setattr(run_core, "_iter_agent_events", _capture_ctx)
    monkeypatch.setattr(run_core, "resolve_scope_context", _fake_resolve_scope_context)
    monkeypatch.setattr(run_core, "_mark_running", _noop)
    monkeypatch.setattr(run_core, "_persist_and_complete", _persist_stub)
    monkeypatch.setattr(run_core, "_broadcast_agent_typing", _noop)
    monkeypatch.setattr("pocketpaw_ee.cloud.sessions.service.ensure_for_agent_scope", _noop_ensure)

    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    transport = RedisStreamTransport(redis)
    monkeypatch.setattr(run_core, "get_stream_transport", lambda: transport)

    await run_core.execute_run(_spec())  # _spec() has no surface fields

    surface_context = captured.get("surface_context")
    # resolve_surface_context never raises; a missing hint -> GENERIC, no deny.
    assert surface_context is not None
    deny = resolve_profile(surface_context.kind, surface_context.meta).deny_mcp_tool_ids
    assert deny == frozenset()
    # entity-rooms chunk ①: the legacy / no-pocket path still resolves a profile
    # (the GENERIC base) — its deny is empty, so the run is unchanged.
    resolved = captured.get("resolved_profile")
    assert resolved is not None
    assert resolved.deny_mcp_tool_ids == frozenset()
