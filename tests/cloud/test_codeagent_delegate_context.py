# test_codeagent_delegate_context.py — the delegate channel must survive the
# task boundary that the pooled SDK client puts between the stream loop and the
# tool.
#
# Created 2026-08-07 (fix/code-delegate-pooled-context). Reproduces a defect
# observed end to end: every Code Mode file tool fails with
# `code_delegate.no_client` — "No browser session is attached to this
# conversation" — even though the browser is attached and streaming.
#
# WHY THE EXISTING SUITE MISSES IT: test_codeagent_delegates.py injects `push`
# on every call, and `delegate_call_to_browser` only consults
# `has_sse_event_sink()` when `push is None`. The production path is therefore
# the one path the suite never exercises.
#
# THE MECHANISM, from run_core.py:
#
#   attach_agent_identity   is bound at TWO sites — 1054 (inside
#                           _prewarm_session) and 1166 (the stream loop)
#   attach_sse_event_sink   is bound at ONE  site — 1164 (the stream loop)
#
# The SDK client is pooled and prewarmed. `_prewarm_session` fires in its own
# `create_task` context, which is created BEFORE the stream loop binds anything,
# and a child task inherits a COPY of the context as it stood at creation. The
# MCP tool in agent/mcp_servers/code.py runs in that pooled task, so it reads
# identity (bound during prewarm) but never sees the sink (bound afterwards, in
# a context it already copied). ART-2 hit exactly this wall on 2026-06-26 with
# the per-tenant cwd jail and fixed it by adding the second identity bind; the
# sink never got the same treatment.
#
# The observed signature is the proof: the failure logs a REAL workspace id
# (`code.listDir delegate failed ws=69d0f4d0…`) while the sink reads as absent.
# One ContextVar from the module is visible and its neighbour is not, which only
# happens when the two were bound at different times relative to the task.
#
# A third bind is the WRONG fix: prewarm has no live stream, so it would capture
# a queue belonging to no turn, and a stale queue is worse than no queue. The
# channel has to be resolvable by IDENTITY rather than by inheritance —
# `delegate_call_to_browser` already receives the workspace id, and identity
# also carries `session_mongo_id`, which is the correct key because one
# workspace can hold two streams open in two windows and a workspace-keyed
# lookup would deliver a file operation to the wrong one.
from __future__ import annotations

import asyncio

import pytest
from pocketpaw_ee.cloud.chat.agent_service import (
    attach_agent_identity,
    attach_sse_event_sink,
    detach_agent_identity,
    detach_sse_event_sink,
    register_stream_sink,
    unregister_stream_sink,
)
from pocketpaw_ee.cloud.codeagent.delegates import (
    ERROR_NO_CLIENT,
    PendingDelegates,
    delegate_call_to_browser,
)

WS = "ws-pooled-1"
SESSION = "session-pooled-1"


@pytest.fixture
def reg() -> PendingDelegates:
    return PendingDelegates()


async def _run_in_task_created_before(
    bind: asyncio.Event,
    released: asyncio.Event,
    out: dict,
    reg: PendingDelegates,
) -> None:
    """Stand in for the pooled SDK client's tool task.

    Created before the sink is bound, so it carries the prewarm-era context —
    the same position agent/mcp_servers/code.py runs from.
    """
    bind.set()
    await released.wait()
    out["outcome"] = await delegate_call_to_browser(
        WS,
        "listDir",
        {"path": "."},
        timeout=0.25,
        registry=reg,
        # push deliberately left as None: this is the production path, the one
        # that consults has_sse_event_sink().
    )


@pytest.mark.asyncio
async def test_delegate_reaches_a_stream_bound_after_the_tool_task_was_created(
    reg: PendingDelegates,
) -> None:
    """A live stream must be reachable from a task that predates its binding.

    This is the end-to-end failure reduced to its mechanism. The browser IS
    attached — the sink is bound and a queue is waiting — but the tool runs in a
    task whose context was copied before that bind, so the ContextVar lookup
    misses and the channel refuses a browser that is right there.

    The assertion is deliberately weak: NOT no_client. Whether the call then
    times out waiting for a resolve is irrelevant here — a timeout proves the
    frame was pushed and the caller parked, which is the behaviour under test.
    """
    queue: asyncio.Queue[tuple[str, dict]] = asyncio.Queue()
    bind = asyncio.Event()
    released = asyncio.Event()
    out: dict = {}

    # Prewarm binds identity (ART-2, run_core ~1054) BEFORE the pooled task
    # exists, which is why the tool can read a session id but not the sink.
    identity = attach_agent_identity(workspace_id=WS, user_id="user-1", session_mongo_id=SESSION)
    try:
        # Created FIRST — snapshots the context as it stands now: identity, no sink.
        task = asyncio.create_task(_run_in_task_created_before(bind, released, out, reg))
        await bind.wait()

        # The stream binds its sink and publishes itself only now, exactly as
        # _drive_agent_loop does — after the pooled task already exists.
        token = attach_sse_event_sink(queue)
        register_stream_sink(SESSION, queue)
        try:
            released.set()
            await asyncio.wait_for(task, timeout=5)
        finally:
            detach_sse_event_sink(token)
            unregister_stream_sink(SESSION)
    finally:
        detach_agent_identity(identity)

    outcome = out["outcome"]
    assert outcome.error != ERROR_NO_CLIENT, (
        "The delegate refused a browser that is attached and streaming. The sink "
        "was bound before the call ran, but in a context the tool's task had "
        "already copied — so the channel must resolve by identity, not by "
        "context inheritance."
    )


@pytest.mark.asyncio
async def test_delegate_still_refuses_when_no_stream_exists_at_all(
    reg: PendingDelegates,
) -> None:
    """The fast refusal must SURVIVE the fix.

    A CLI run, a background job or a test genuinely has no browser, and the
    delegate must keep failing immediately instead of parking for the full
    budget. Pinned here so a fix for the case above cannot make every caller
    wait out the timeout to discover nobody is listening.
    """
    outcome = await delegate_call_to_browser(
        WS, "listDir", {"path": "."}, timeout=0.25, registry=reg
    )

    assert outcome.ok is False
    assert outcome.error == ERROR_NO_CLIENT
    assert len(reg) == 0, "a refused delegate must not leak a registry slot"


@pytest.mark.asyncio
async def test_a_finished_run_does_not_unregister_a_concurrent_run_on_the_same_session() -> None:
    """Teardown must not delete a stream that belongs to somebody else.

    Nothing serializes runs per session: a second tab on the same conversation,
    or a second send while the first stream is still tailing, gives two
    concurrent `_drive_agent_loop` runs with the same scope id, and the later
    `register_stream_sink` overwrites the earlier entry. If teardown popped by
    key alone, whichever run finished FIRST would delete the entry belonging to
    the run still streaming — resurrecting the exact "no browser session is
    attached" failure this registry exists to fix, for the rest of that turn.
    """
    queue_a: asyncio.Queue[tuple[str, dict]] = asyncio.Queue()
    queue_b: asyncio.Queue[tuple[str, dict]] = asyncio.Queue()

    register_stream_sink(SESSION, queue_a)
    register_stream_sink(SESSION, queue_b)  # run B overwrites run A's entry

    # Run A finishes first and tears down. Its queue is no longer the
    # registered one, so this must be a no-op.
    unregister_stream_sink(SESSION, queue_a)

    from pocketpaw_ee.cloud.chat.agent_service import stream_sink_for_session

    assert stream_sink_for_session(SESSION) is queue_b, (
        "run A's teardown deleted run B's live stream"
    )

    # B's own teardown still clears it.
    unregister_stream_sink(SESSION, queue_b)
    assert stream_sink_for_session(SESSION) is None
