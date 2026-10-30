# test_sse_push_pooled_context.py — ``push_sse_event`` must reach the live stream
# from a task that predates the sink's binding.
#
# Created 2026-08-21 (fix/sites-draft-does-not-open). Reproduces a defect observed
# in the DEPLOYED environment: describing a site on /sites creates the draft — it
# appears in the gallery — but the builder never opens on its own, so the user is
# left on the gallery with no sign that the thing they asked for is ready to edit.
#
# THE MECHANISM IS ONE THIS REPO HAS ALREADY DIAGNOSED ONCE. See
# test_codeagent_delegate_context.py and the 2026-08-07 fix/code-delegate-pooled-context
# header in cloud/chat/agent_service.py:
#
#   attach_agent_identity   is bound at TWO sites — inside _prewarm_session, and
#                           again in the stream loop
#   attach_sse_event_sink   is bound at ONE  site — the stream loop
#
# The SDK client is pooled and prewarmed, and a child task inherits a COPY of the
# context as it stood when it was created. So an in-process MCP tool running in the
# pooled task reads identity (bound during prewarm) and never sees the sink (bound
# afterwards, into a context it had already copied). ``push_sse_event`` consults ONLY
# the ContextVar, so it silently no-ops.
#
# WHY THAT LOSES THE BUILDER. The /sites create tools call
# ``sites_create._bind_session_and_emit``, which pushes ``pocket_created``. The
# gallery route sets ``pendingPocketCloudId`` from that frame and EVERY downstream
# step keys on it: the "building your site" cell, the run-completion publish
# fallback, and the $effect that navigates to /sites/<id> once the new row appears.
# No frame, no navigation. The draft still LISTS, because ``site.created`` is a
# realtime websocket event on a completely different channel that never touches this
# ContextVar — which is exactly why the bug reads as "it was created but it didn't
# open" rather than as "nothing happened".
#
# WHY IT LOOKS LOCAL-CLEAN. A cold local run creates its client inside the stream
# loop, after the sink is bound, so the tool inherits a context that has one. The
# pooled/prewarmed path is the deployed steady state.
#
# The fix belongs in ``push_sse_event`` rather than in each caller: eight call sites
# across five modules push from inside in-process MCP tools, and every one of them
# runs from the same pooled task. The registry the 2026-08-07 fix built
# (``register_stream_sink`` / ``stream_sink_for_session``) is precisely the
# resolve-by-identity channel this needs; it was simply never wired into the push.
from __future__ import annotations

import asyncio

import pytest
from pocketpaw_ee.cloud.chat.agent_service import (
    attach_agent_identity,
    attach_sse_event_sink,
    detach_agent_identity,
    detach_sse_event_sink,
    push_sse_event,
    register_stream_sink,
    unregister_stream_sink,
)

WS = "ws-pooled-sse"
SESSION = "session-pooled-sse"

pytestmark = pytest.mark.asyncio


async def _push_from_pooled_task(
    created: asyncio.Event,
    released: asyncio.Event,
    name: str = "pocket_created",
) -> None:
    """Stand in for an in-process MCP tool handler.

    Created before the sink is bound, so it carries the prewarm-era context — the
    same position ``agent/mcp_servers/sites_create.py`` pushes from.
    """
    created.set()
    await released.wait()
    push_sse_event(name, {"pocket_id": "pkt-1", "session_id": SESSION})


async def test_a_pooled_tool_task_reaches_the_stream_bound_after_it():
    """THE BUG. The stream is live and the browser is listening, but the tool runs
    in a task whose context was copied before the sink was bound — so the push
    lands nowhere and /sites never learns a pocket was created."""
    queue: asyncio.Queue[tuple[str, dict]] = asyncio.Queue()
    created = asyncio.Event()
    released = asyncio.Event()

    # Prewarm binds identity BEFORE the pooled task exists, which is why the tool
    # can read a session id but not the sink.
    identity = attach_agent_identity(workspace_id=WS, user_id="u1", session_mongo_id=SESSION)
    try:
        task = asyncio.create_task(_push_from_pooled_task(created, released))
        await created.wait()

        # The stream binds its sink and publishes itself only now — exactly the
        # order _drive_agent_loop runs in.
        token = attach_sse_event_sink(queue)
        register_stream_sink(SESSION, queue, WS)
        try:
            released.set()
            await asyncio.wait_for(task, timeout=5)
        finally:
            detach_sse_event_sink(token)
            unregister_stream_sink(SESSION, queue)
    finally:
        detach_agent_identity(identity)

    assert not queue.empty(), (
        "pocket_created was pushed into nothing. The sink was bound before the push "
        "ran, but into a context the tool's task had already copied — so the push "
        "must resolve the stream by identity, not by context inheritance. Without "
        "the frame, /sites never sets pendingPocketCloudId and the new draft's "
        "builder never opens."
    )
    name, data = queue.get_nowait()
    assert name == "pocket_created"
    assert data["pocket_id"] == "pkt-1"


async def test_the_in_context_sink_still_wins():
    """The ContextVar remains the primary path. A caller that CAN see its own sink
    must use it and never consult a process-global dict — that lookup is the
    fallback, not the mechanism."""
    own: asyncio.Queue[tuple[str, dict]] = asyncio.Queue()
    other: asyncio.Queue[tuple[str, dict]] = asyncio.Queue()

    identity = attach_agent_identity(workspace_id=WS, user_id="u1", session_mongo_id=SESSION)
    token = attach_sse_event_sink(own)
    register_stream_sink(SESSION, other, WS)
    try:
        push_sse_event("pocket_created", {"pocket_id": "pkt-2"})
    finally:
        detach_sse_event_sink(token)
        unregister_stream_sink(SESSION, other)
        detach_agent_identity(identity)

    assert not own.empty(), "the caller's own sink must receive the frame"
    assert other.empty(), "the registry must not be consulted when a sink is in scope"


async def test_a_run_with_no_stream_anywhere_is_still_a_silent_no_op():
    """The no-op must SURVIVE the fix. A CLI run, a background job or a unit test
    genuinely has no stream, and ``push_sse_event`` is documented as a deliberate
    no-op there — an observability frame nobody is obliged to see must never become
    an exception in a tool handler."""
    identity = attach_agent_identity(workspace_id=WS, user_id="u1", session_mongo_id=SESSION)
    try:
        push_sse_event("pocket_created", {"pocket_id": "pkt-3"})  # must not raise
    finally:
        detach_agent_identity(identity)


async def test_a_push_with_no_identity_at_all_is_a_no_op():
    """No sink and no session id — the plain unit-test / CLI case. Nothing to
    resolve, nothing to raise."""
    push_sse_event("pocket_created", {"pocket_id": "pkt-4"})  # must not raise


async def test_the_fallback_refuses_to_cross_a_tenant_boundary():
    """TENANCY. The registry is a PROCESS-GLOBAL dict, so a lookup keyed on a
    session id can get tenancy wrong in a way the ContextVar path could not — the
    sink used to be the caller's own stream by construction. A push whose bound
    workspace disagrees with the stream's owner must land nowhere rather than in
    another tenant's stream."""
    victim: asyncio.Queue[tuple[str, dict]] = asyncio.Queue()
    register_stream_sink(SESSION, victim, "ws-owner")

    # Same session id, different tenant — the shape an id collision or a stale
    # identity would take.
    identity = attach_agent_identity(
        workspace_id="ws-attacker", user_id="u1", session_mongo_id=SESSION
    )
    try:
        push_sse_event("pocket_created", {"pocket_id": "pkt-5"})
    finally:
        detach_agent_identity(identity)
        unregister_stream_sink(SESSION, victim)

    assert victim.empty(), "a cross-tenant push must not reach another tenant's stream"
