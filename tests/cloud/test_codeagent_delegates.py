# test_codeagent_delegates.py — The browser-delegate channel (CD-1).
#
# Created 2026-07-22 (feat/code-delegate-channel). Covers the rendezvous that
# lets a BACKEND tool have work done in the USER'S BROWSER: park on a future,
# push a `code_delegate` SSE frame, wait for `POST /codeagent/resolve`.
#
# The property most of these tests exist to defend is "a parked caller never
# hangs". Timeout, an abort, a push that failed, no stream at all — each has to
# end as a `DelegateOutcome` the tool can report, and each has to leave the
# registry empty. Every test therefore asserts on the outcome AND on `len(reg)`;
# a leaked entry is a correlation id a late browser could resolve into a future
# nobody is reading.
#
# Kept apart from test_codeagent*.py (the model turn) — those drive a fake
# Anthropic client, these drive no model at all.
from __future__ import annotations

import asyncio

import pytest
from pocketpaw_ee.cloud._core.errors import CloudError
from pocketpaw_ee.cloud.codeagent import delegates
from pocketpaw_ee.cloud.codeagent import service as codeagent_service
from pocketpaw_ee.cloud.codeagent.delegates import (
    DELEGATE_EVENT,
    ERROR_ABORTED,
    ERROR_NO_CLIENT,
    ERROR_TIMEOUT,
    MAX_DELEGATE_RESULT_CHARS,
    DelegateOutcome,
    PendingDelegates,
    delegate_to_browser,
    get_pending_delegates,
    resolve_pending,
)
from pocketpaw_ee.cloud.codeagent.dto import DelegateResolveRequest

WS = "ws-1"
OTHER_WS = "ws-2"
USER = "user-1"

# Short enough that a "does it time out" test costs milliseconds, long enough
# that a slow CI box does not trip it on the paths that should NOT time out.
FAST = 0.05
SLOW = 5.0


# ── Helpers ─────────────────────────────────────────────────────────────────


class _Recorder:
    """Stands in for ``push_sse_event``. Records frames so a test can read the
    corrId the backend minted — the browser's only handle on the parked turn."""

    def __init__(self) -> None:
        self.frames: list[tuple[str, dict]] = []

    def __call__(self, name: str, data: dict) -> None:
        self.frames.append((name, data))

    @property
    def corr_id(self) -> str:
        return self.frames[0][1]["corrId"]


async def _park(
    reg: PendingDelegates,
    push: object,
    *,
    workspace_id: str = WS,
    task: str = "add a test",
    mode: str = "ask",
    timeout: float = SLOW,
) -> asyncio.Task[DelegateOutcome]:
    """Start a delegation and wait until it is actually parked.

    The poll matters: without it a test can resolve a corrId before the caller
    has reached ``wait``, which passes for the wrong reason. It watches for the
    count to GROW rather than to be non-zero, so a test that parks twice does
    not have its second call satisfied by the first one's entry.
    """
    before = len(reg)
    task_handle = asyncio.create_task(
        delegate_to_browser(
            workspace_id,
            task,
            mode,
            timeout=timeout,
            registry=reg,
            push=push,  # type: ignore[arg-type]
        )
    )
    for _ in range(200):
        if len(reg) > before:
            return task_handle
        await asyncio.sleep(0)
    task_handle.cancel()
    raise AssertionError("delegation never parked")


@pytest.fixture
def reg() -> PendingDelegates:
    return PendingDelegates()


@pytest.fixture
def push() -> _Recorder:
    return _Recorder()


# ── The happy path ──────────────────────────────────────────────────────────


async def test_park_then_resolve_returns_the_payload(reg, push):
    handle = await _park(reg, push)

    resolve_pending(WS, push.corr_id, {"answer": "done", "files": ["a.py"]}, registry=reg)
    outcome = await asyncio.wait_for(handle, timeout=SLOW)

    assert outcome.ok is True
    assert outcome.result == {"answer": "done", "files": ["a.py"]}
    assert outcome.error == ""
    assert len(reg) == 0


async def test_push_carries_corr_id_task_and_mode(reg, push):
    handle = await _park(reg, push, task="rename the handler", mode="edit")

    name, data = push.frames[0]
    assert name == DELEGATE_EVENT
    assert data["task"] == "rename the handler"
    assert data["mode"] == "edit"
    assert data["corrId"]

    resolve_pending(WS, data["corrId"], {}, registry=reg)
    await asyncio.wait_for(handle, timeout=SLOW)


async def test_result_is_forwarded_verbatim(reg, push):
    """The channel does not interpret the payload — the shape belongs to the
    Code Mode client, and a backend that reshaped it would be a second place to
    change every time the sub-agent learns a new answer shape."""
    handle = await _park(reg, push)
    payload = {"nested": {"diff": [1, 2, 3]}, "ok": False, "n": None}

    resolve_pending(WS, push.corr_id, payload, registry=reg)
    outcome = await asyncio.wait_for(handle, timeout=SLOW)

    assert outcome.result == payload


async def test_two_delegations_get_distinct_ids(reg, push):
    first = await _park(reg, push)
    second_push = _Recorder()
    second = await _park(reg, second_push)

    assert push.corr_id != second_push.corr_id
    assert len(reg) == 2

    # Resolving one must not disturb the other — the registry is keyed by
    # correlation id precisely so one turn can delegate more than once.
    resolve_pending(WS, second_push.corr_id, {"which": "second"}, registry=reg)
    assert (await asyncio.wait_for(second, timeout=SLOW)).result == {"which": "second"}
    assert len(reg) == 1

    resolve_pending(WS, push.corr_id, {"which": "first"}, registry=reg)
    assert (await asyncio.wait_for(first, timeout=SLOW)).result == {"which": "first"}
    assert len(reg) == 0


# ── Timeout: an error result, never a hang ──────────────────────────────────


async def test_timeout_returns_an_error_outcome_and_does_not_hang(reg, push):
    handle = await _park(reg, push, timeout=FAST)

    # No resolve ever arrives. asyncio.wait_for here is the "does not hang"
    # assertion: it is 100x the park budget, so a caller that failed to give up
    # fails the test rather than stalling the suite.
    outcome = await asyncio.wait_for(handle, timeout=SLOW)

    assert outcome.ok is False
    assert outcome.error == ERROR_TIMEOUT
    assert outcome.message
    assert len(reg) == 0


async def test_resolve_after_timeout_is_rejected(reg, push):
    handle = await _park(reg, push, timeout=FAST)
    await asyncio.wait_for(handle, timeout=SLOW)

    with pytest.raises(CloudError) as exc:
        resolve_pending(WS, push.corr_id, {"late": True}, registry=reg)

    assert exc.value.code == "code_delegate.not_found"
    assert exc.value.status_code == 404


# ── Unknown, duplicate, and cross-tenant resolves ───────────────────────────


async def test_unknown_corr_id_is_rejected(reg):
    with pytest.raises(CloudError) as exc:
        resolve_pending(WS, "nope-not-a-real-id", {}, registry=reg)

    assert exc.value.code == "code_delegate.not_found"
    assert exc.value.status_code == 404


async def test_duplicate_resolve_is_rejected(reg, push):
    handle = await _park(reg, push)

    resolve_pending(WS, push.corr_id, {"answer": "first"}, registry=reg)
    outcome = await asyncio.wait_for(handle, timeout=SLOW)
    assert outcome.result == {"answer": "first"}

    with pytest.raises(CloudError) as exc:
        resolve_pending(WS, push.corr_id, {"answer": "second"}, registry=reg)

    assert exc.value.code == "code_delegate.not_found"


async def test_resolve_from_another_workspace_is_rejected(reg, push):
    """The corrId is unguessable, but tenancy that rests on unguessability is
    not tenancy. The entry stays parked, so the real owner can still answer."""
    handle = await _park(reg, push)

    with pytest.raises(CloudError) as exc:
        resolve_pending(OTHER_WS, push.corr_id, {"stolen": True}, registry=reg)
    assert exc.value.code == "code_delegate.not_found"

    assert len(reg) == 1
    resolve_pending(WS, push.corr_id, {"mine": True}, registry=reg)
    assert (await asyncio.wait_for(handle, timeout=SLOW)).result == {"mine": True}


async def test_oversized_result_is_rejected(reg, push):
    handle = await _park(reg, push, timeout=FAST)

    with pytest.raises(CloudError) as exc:
        resolve_pending(
            WS,
            push.corr_id,
            {"answer": "x" * (MAX_DELEGATE_RESULT_CHARS + 1)},
            registry=reg,
        )

    assert exc.value.code == "code_delegate.result_too_large"
    assert exc.value.status_code == 422
    # The caller is untouched by a rejected payload and still times out cleanly.
    assert (await asyncio.wait_for(handle, timeout=SLOW)).error == ERROR_TIMEOUT


# ── Abort and cancellation: a disconnected client strands nothing ───────────


async def test_abort_wakes_the_caller_with_an_error(reg, push):
    handle = await _park(reg, push)

    assert reg.abort(push.corr_id, message="stream closed") is True
    outcome = await asyncio.wait_for(handle, timeout=SLOW)

    assert outcome.ok is False
    assert outcome.error == ERROR_ABORTED
    assert outcome.message == "stream closed"
    assert len(reg) == 0


async def test_abort_all_wakes_every_parked_caller(reg, push):
    first = await _park(reg, push)
    second_push = _Recorder()
    second = await _park(reg, second_push)

    assert reg.abort_all() == 2
    for handle in (first, second):
        assert (await asyncio.wait_for(handle, timeout=SLOW)).error == ERROR_ABORTED
    assert len(reg) == 0


async def test_abort_of_an_unknown_id_is_a_no_op(reg):
    assert reg.abort("nope") is False
    assert reg.abort_all() == 0


async def test_cancelling_the_caller_removes_the_registry_entry(reg, push):
    """A disconnected client tears down the run task. If the entry survived
    that, the correlation id would outlive its future forever."""
    handle = await _park(reg, push)

    handle.cancel()
    with pytest.raises(asyncio.CancelledError):
        await handle

    assert len(reg) == 0
    with pytest.raises(CloudError):
        resolve_pending(WS, push.corr_id, {}, registry=reg)


async def test_a_failed_push_gives_up_immediately(reg):
    def _explode(_name: str, _data: dict) -> None:
        raise RuntimeError("sink is gone")

    outcome = await asyncio.wait_for(
        delegate_to_browser(WS, "task", registry=reg, push=_explode, timeout=SLOW),
        timeout=SLOW,
    )

    assert outcome.ok is False
    assert outcome.error == ERROR_NO_CLIENT
    assert len(reg) == 0


# ── The real SSE seam ───────────────────────────────────────────────────────


async def test_no_sse_stream_fails_fast_rather_than_parking(reg):
    """No ``push=`` here on purpose: this drives the REAL
    ``push_sse_event`` / ``has_sse_event_sink`` pair. Outside a stream the push
    is a documented no-op, so parking would burn the full budget on a failure
    that was knowable at the push."""
    outcome = await asyncio.wait_for(
        delegate_to_browser(WS, "task", registry=reg, timeout=SLOW),
        timeout=SLOW,
    )

    assert outcome.ok is False
    assert outcome.error == ERROR_NO_CLIENT
    assert len(reg) == 0


async def test_frame_reaches_a_real_attached_sse_sink(reg):
    """End to end over the actual transport: bind a queue the way the chat
    stream does, delegate, and read the frame off the queue."""
    from pocketpaw_ee.cloud.chat.agent_service import (
        attach_sse_event_sink,
        detach_sse_event_sink,
    )

    queue: asyncio.Queue = asyncio.Queue()
    token = attach_sse_event_sink(queue)
    try:
        handle = asyncio.create_task(
            delegate_to_browser(WS, "refactor it", "edit", timeout=SLOW, registry=reg)
        )
        name, data = await asyncio.wait_for(queue.get(), timeout=SLOW)
        assert name == DELEGATE_EVENT
        assert data["task"] == "refactor it"
        assert data["mode"] == "edit"

        resolve_pending(WS, data["corrId"], {"answer": "ok"}, registry=reg)
        assert (await asyncio.wait_for(handle, timeout=SLOW)).result == {"answer": "ok"}
    finally:
        detach_sse_event_sink(token)


# ── Wiring: the DTO, the service, and the singleton ─────────────────────────


async def test_service_resolve_delegate_wakes_the_parked_caller(reg, push, monkeypatch):
    """The route's path, one layer down: the singleton registry is what both
    ends actually share in production, so pin that it is the one used."""
    monkeypatch.setattr(delegates, "_registry", reg)
    handle = await _park(reg, push)

    response = await codeagent_service.resolve_delegate(
        WS, USER, {"corrId": push.corr_id, "result": {"answer": "via service"}}
    )

    assert response.accepted is True
    assert (await asyncio.wait_for(handle, timeout=SLOW)).result == {"answer": "via service"}


async def test_service_resolve_delegate_rejects_an_unknown_id(reg, monkeypatch):
    monkeypatch.setattr(delegates, "_registry", reg)

    with pytest.raises(CloudError) as exc:
        await codeagent_service.resolve_delegate(WS, USER, {"corrId": "ghost", "result": {}})

    assert exc.value.code == "code_delegate.not_found"


def test_resolve_request_requires_a_corr_id():
    with pytest.raises(ValueError):
        DelegateResolveRequest(corrId="", result={})


def test_resolve_request_defaults_result_to_empty():
    assert DelegateResolveRequest(corrId="abc").result == {}


def test_get_pending_delegates_is_a_singleton():
    assert get_pending_delegates() is get_pending_delegates()


def test_outcome_to_dict_splits_success_from_failure():
    ok = DelegateOutcome(ok=True, result={"a": 1}).to_dict()
    assert ok == {"ok": True, "result": {"a": 1}}

    failed = DelegateOutcome(ok=False, error=ERROR_TIMEOUT, message="slow").to_dict()
    assert failed == {"ok": False, "error": ERROR_TIMEOUT, "message": "slow"}
