# test_codeagent_delegate_cross_process.py — the delegate rendezvous across
# two processes.
#
# Created 2026-08-07 (fix/code-delegate-cross-process). Reproduces the second
# layer of the Code Mode file-tool failure, the one that only appears in the
# DEPLOYED topology and never on a developer machine.
#
# THE TOPOLOGY. deploy/coolify/docker-compose.yaml runs two containers off one
# image with POCKETPAW_CLOUD_RUN_EXECUTOR=arq:
#
#     backend (web)   serves POST /codeagent/resolve
#     worker  (arq)   runs the chat turn and parks the 180s future
#
# PendingDelegates is in-process and single-loop by construction — its own
# docstring says a resolve landing on a different worker "finds no entry and is
# rejected as unknown". In this topology that is not an edge case, it is every
# delegate: the browser answers the WEB process, whose registry is empty, so the
# worker's future runs the full budget and the user is told the browser did not
# finish — about a browser that finished instantly.
#
# WHY IT HID. Local dev and every existing test run one process (the default
# `inprocess` executor), where park and resolve share a registry. The in-process
# discovery fix that shipped first (register_stream_sink) was verified on
# exactly that rig and still failed in production, because the two bugs were
# stacked: fixing the first exposed the second. The error class changing from an
# instant "no browser session" to a 180s timeout is what distinguishes them.
#
# HOW THESE TESTS MODEL IT. Two SEPARATE PendingDelegates instances stand in for
# the two processes — that is precisely what a second process is here, an
# unrelated in-memory registry — with a fake bridge as the shared Redis. If the
# park and the resolve could see one registry these tests would pass without the
# fix, so they use different ones on purpose.

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from pocketpaw_ee.cloud._core.errors import NotFound
from pocketpaw_ee.cloud.codeagent.bridge import NullDelegateBridge
from pocketpaw_ee.cloud.codeagent.delegates import (
    DELEGATE_EVENT,
    ERROR_TIMEOUT,
    PendingDelegates,
    delegate_call_to_browser,
    resolve_pending_anywhere,
)

WS = "ws-xproc"
OTHER_WS = "ws-someone-else"


class FakeBridge:
    """Stands in for Redis: one dict of pending records, one of result lists.

    Mirrors RedisDelegateBridge's contract exactly — durable delivery (a result
    delivered before anyone listens is still collected), tenancy checked on the
    pending record, and a miss for anything it has not been told about.
    """

    def __init__(self) -> None:
        self.pending: dict[str, str] = {}
        self.results: dict[str, list[dict]] = {}
        self.forgotten: list[str] = []

    @property
    def enabled(self) -> bool:
        return True

    async def announce(self, corr_id: str, workspace_id: str, *, ttl_seconds: int) -> None:
        self.pending[corr_id] = workspace_id

    async def listen(self, corr_id: str, *, timeout: float) -> dict[str, Any] | None:
        # Poll rather than block: same observable behaviour as BLPOP (returns as
        # soon as a value exists, including one pushed before the call).
        deadline = timeout
        while deadline > 0:
            queued = self.results.get(corr_id)
            if queued:
                return queued.pop(0)
            await asyncio.sleep(0.01)
            deadline -= 0.01
        return None

    async def deliver(self, corr_id: str, workspace_id: str, result: dict) -> bool:
        owner = self.pending.get(corr_id)
        if owner is None or owner != workspace_id:
            return False
        self.results.setdefault(corr_id, []).append(json.loads(json.dumps(result)))
        return True

    async def forget(self, corr_id: str) -> None:
        self.forgotten.append(corr_id)
        self.pending.pop(corr_id, None)
        self.results.pop(corr_id, None)


def _capture_push(sink: list[tuple[str, dict]]):
    def push(name: str, data: dict) -> None:
        sink.append((name, data))

    return push


@pytest.mark.asyncio
async def test_a_resolve_on_another_process_wakes_the_parked_turn() -> None:
    """The whole bug, in one test.

    The turn parks in the worker's registry; the browser answers the web
    process, which has a DIFFERENT registry. Without the bridge the web process
    404s and the worker times out after the full budget. With it, the answer
    crosses and the turn completes.
    """
    bridge = FakeBridge()
    worker_registry = PendingDelegates()  # the arq container
    web_registry = PendingDelegates()  # the backend container
    pushed: list[tuple[str, dict]] = []

    park = asyncio.create_task(
        delegate_call_to_browser(
            WS,
            "listDir",
            {"path": "."},
            timeout=5,
            registry=worker_registry,
            push=_capture_push(pushed),
            bridge=bridge,
        )
    )

    # Wait for the frame the browser would receive.
    for _ in range(200):
        if pushed:
            break
        await asyncio.sleep(0.01)
    assert pushed, "no code_delegate frame was pushed"
    name, frame = pushed[0]
    assert name == DELEGATE_EVENT
    corr_id = frame["corrId"]

    # The browser POSTs to the WEB process — a registry that never saw the park.
    await resolve_pending_anywhere(
        WS,
        corr_id,
        {"output": "Test.html", "isError": False},
        registry=web_registry,
        bridge=bridge,
    )

    outcome = await asyncio.wait_for(park, timeout=5)
    assert outcome.ok is True, f"parked turn did not receive the answer: {outcome}"
    assert outcome.result["output"] == "Test.html"


@pytest.mark.asyncio
async def test_without_the_bridge_the_same_sequence_times_out() -> None:
    """Pins WHY the bridge is needed rather than assuming it.

    Identical to the test above except the bridge is inert. This is the
    production behaviour before this change: the resolve finds nothing and the
    parked turn burns its budget. Kept so nobody 'simplifies' the bridge away
    and sees a green suite.
    """
    worker_registry = PendingDelegates()
    web_registry = PendingDelegates()
    pushed: list[tuple[str, dict]] = []

    park = asyncio.create_task(
        delegate_call_to_browser(
            WS,
            "listDir",
            {"path": "."},
            timeout=0.5,
            registry=worker_registry,
            push=_capture_push(pushed),
            bridge=NullDelegateBridge(),
        )
    )
    for _ in range(200):
        if pushed:
            break
        await asyncio.sleep(0.01)
    corr_id = pushed[0][1]["corrId"]

    with pytest.raises(NotFound):
        await resolve_pending_anywhere(
            WS, corr_id, {"output": "x"}, registry=web_registry, bridge=NullDelegateBridge()
        )

    outcome = await asyncio.wait_for(park, timeout=5)
    assert outcome.ok is False
    assert outcome.error == ERROR_TIMEOUT


@pytest.mark.asyncio
async def test_the_local_registry_is_tried_first() -> None:
    """Single-process deployments must not change behaviour.

    The `inprocess` executor and every existing test share one registry, so the
    answer has to be settled locally without the bridge being consulted at all.
    """
    bridge = FakeBridge()
    registry = PendingDelegates()
    pushed: list[tuple[str, dict]] = []

    park = asyncio.create_task(
        delegate_call_to_browser(
            WS,
            "listDir",
            {"path": "."},
            timeout=5,
            registry=registry,
            push=_capture_push(pushed),
            bridge=bridge,
        )
    )
    for _ in range(200):
        if pushed:
            break
        await asyncio.sleep(0.01)
    corr_id = pushed[0][1]["corrId"]

    await resolve_pending_anywhere(
        WS, corr_id, {"output": "local"}, registry=registry, bridge=bridge
    )

    outcome = await asyncio.wait_for(park, timeout=5)
    assert outcome.ok is True
    assert outcome.result["output"] == "local"
    assert bridge.results == {}, "the bridge was used when the local registry could answer"


@pytest.mark.asyncio
async def test_a_cross_process_resolve_from_the_wrong_workspace_is_a_miss() -> None:
    """Tenancy holds across the crossing too.

    The bridge is a process-global keyed by an unguessable id, which is exactly
    the shape `_Pending` warns about: "tenancy that rests on unguessability is
    not tenancy". A foreign workspace must get the same NotFound an unknown id
    gets, and the parked turn must stay parked.
    """
    bridge = FakeBridge()
    worker_registry = PendingDelegates()
    web_registry = PendingDelegates()
    pushed: list[tuple[str, dict]] = []

    park = asyncio.create_task(
        delegate_call_to_browser(
            WS,
            "listDir",
            {"path": "."},
            timeout=1.5,
            registry=worker_registry,
            push=_capture_push(pushed),
            bridge=bridge,
        )
    )
    for _ in range(200):
        if pushed:
            break
        await asyncio.sleep(0.01)
    corr_id = pushed[0][1]["corrId"]

    with pytest.raises(NotFound):
        await resolve_pending_anywhere(
            OTHER_WS, corr_id, {"output": "stolen"}, registry=web_registry, bridge=bridge
        )

    outcome = await asyncio.wait_for(park, timeout=5)
    assert outcome.ok is False
    assert outcome.error == ERROR_TIMEOUT, "a foreign resolve must not settle the park"


@pytest.mark.asyncio
async def test_an_unknown_correlation_id_still_raises_not_found() -> None:
    """The honest 404 survives. An id neither side has parked is still unknown,
    so the bridge cannot become a silent success for a bogus resolve."""
    bridge = FakeBridge()
    with pytest.raises(NotFound):
        await resolve_pending_anywhere(
            WS, "nope-not-a-real-id", {"output": "x"}, registry=PendingDelegates(), bridge=bridge
        )


@pytest.mark.asyncio
async def test_the_park_clears_its_bridge_record_on_the_way_out() -> None:
    """A leaked pending record is a correlation id a late browser could deliver
    into — the cross-process twin of the registry leak `wait`'s finally exists to
    prevent. Cleared on every exit, including the timeout path."""
    bridge = FakeBridge()
    pushed: list[tuple[str, dict]] = []

    outcome = await delegate_call_to_browser(
        WS,
        "listDir",
        {"path": "."},
        timeout=0.3,
        registry=PendingDelegates(),
        push=_capture_push(pushed),
        bridge=bridge,
    )

    assert outcome.ok is False
    assert outcome.error == ERROR_TIMEOUT
    assert bridge.pending == {}, "a timed-out park left its bridge record behind"
    assert bridge.forgotten, "forget() was never called"
