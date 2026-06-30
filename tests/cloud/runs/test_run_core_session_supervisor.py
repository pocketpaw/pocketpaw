# tests/cloud/runs/test_run_core_session_supervisor.py
# Created 2026-06-30 (feat/session-supervisor SS-5). Pins the SS-5 wiring in
# ``_drive_agent_loop``: behind the default-OFF ``POCKETPAW_SESSION_SUPERVISOR``
# flag the executor drives every supervised turn through the SessionSupervisor +
# the durable native-id mapping (SS-3) + the per-tenant Mongo transcript store
# (SS-2), so the agent RESUMES its native CLI session instead of replaying
# history. Hermetic: a capturing fake pool yields backend AgentEvents, the SS-3
# ``runtime_service`` + the ``MongoSessionStore`` are faked (no live mongod), and
# the supervisor is a thin recorder wrapping the REAL ``SessionSupervisor`` so we
# assert the real acquire/bracket/capture/crash behavior plus the call sequence.
#
# Coverage:
#   * Flag ON, turn 1 (no prior id): pool.run gets a SessionHandle carrying the
#     store + a None cli_session_id; mark_run_start/mark_run_end bracket the run;
#     the ("session_id", ...) event persists via set_cli_session_id +
#     record_cli_session_id (and is NOT yielded to the stream).
#   * Flag ON, turn 2 (prior id known): get_cli_session_id resolves it → the
#     SessionHandle carries resume (cli_session_id set).
#   * Flag OFF: pool.run is called WITHOUT a session_handle; zero
#     supervisor/store/mapping calls — the legacy path is byte-for-byte unchanged.
#   * Flag ON, backend error event: the run is flagged a crash → mark_crashed.

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from pocketpaw_ee.cloud.chat.agent_service import ScopeContext, ScopeKind
from pocketpaw_ee.cloud.chat.runs import run_core

from pocketpaw.agents.session_supervisor import SessionSupervisor

pytestmark = pytest.mark.asyncio


# --------------------------------------------------------------------------- #
# Fakes / harness                                                             #
# --------------------------------------------------------------------------- #


class _CapturePool:
    """Fake AgentPool: captures ``run`` kwargs and yields the given events."""

    def __init__(self, events: list[Any]) -> None:
        self._events = events
        self.run_kwargs: dict[str, Any] | None = None
        self.run_called = False

    async def get(self, _agent_id):
        return SimpleNamespace(config={"backend": "claude_agent_sdk"}, agent_name="A")

    def run(self, agent_id, content, session_key, **kwargs):
        self.run_called = True
        self.run_kwargs = kwargs

        async def _gen():
            for ev in self._events:
                yield ev

        return _gen()


class _FakeRuntimeService:
    """In-memory stand-in for the SS-3 durable ``runtime_service`` (no mongod)."""

    def __init__(self, prior: str | None = None) -> None:
        self._prior = prior
        self.get_calls: list[tuple[str, str, str]] = []
        self.set_calls: list[tuple[str, str, str, str, str | None]] = []

    async def get_cli_session_id(self, ws, session, agent):
        self.get_calls.append((ws, session, agent))
        return self._prior

    async def set_cli_session_id(self, ws, session, agent, cli_session_id, project_key=None):
        self.set_calls.append((ws, session, agent, cli_session_id, project_key))


class _FakeStore:
    """Stand-in for ``MongoSessionStore`` — construction only, no I/O."""

    def __init__(self, workspace_id: str) -> None:
        self.workspace_id = workspace_id


class _RecordingSupervisor:
    """Thin recorder delegating to a REAL ``SessionSupervisor``.

    Gives us the genuine acquire/bracket/capture/crash semantics (a real
    ``Acquisition`` + ``SessionRuntime`` whose state we can assert) AND an ordered
    log of which lifecycle hooks fired.
    """

    def __init__(self) -> None:
        self.inner = SessionSupervisor()
        self.calls: list[str] = []
        self.acq: Any = None

    def acquire(self, *a, **k):
        self.calls.append("acquire")
        self.acq = self.inner.acquire(*a, **k)
        return self.acq

    def mark_run_start(self, runtime):
        self.calls.append("mark_run_start")
        self.inner.mark_run_start(runtime)

    def mark_run_end(self, runtime):
        self.calls.append("mark_run_end")
        self.inner.mark_run_end(runtime)

    def record_cli_session_id(self, runtime, cli_session_id, project_key=None):
        self.calls.append("record_cli_session_id")
        self.inner.record_cli_session_id(runtime, cli_session_id, project_key=project_key)

    def mark_crashed(self, runtime):
        self.calls.append("mark_crashed")
        self.inner.mark_crashed(runtime)


def _scope_ctx() -> ScopeContext:
    return ScopeContext(
        kind=ScopeKind.SESSION,
        scope_id="s1",
        workspace_id="w1",
        user_id="u1",
        members=["u1"],
        target_agent_id="a1",
    )


async def _drive(
    monkeypatch,
    *,
    events: list[Any],
    flag_on: bool,
    runtime_service: _FakeRuntimeService | None = None,
    supervisor: _RecordingSupervisor | None = None,
) -> tuple[_CapturePool, list[tuple[str, dict]]]:
    """Run ``_drive_agent_loop`` hermetically; return (pool, produced events)."""
    if flag_on:
        monkeypatch.setenv("POCKETPAW_SESSION_SUPERVISOR", "true")
    else:
        monkeypatch.delenv("POCKETPAW_SESSION_SUPERVISOR", raising=False)

    pool = _CapturePool(events)
    monkeypatch.setattr(run_core, "get_agent_pool", lambda: pool)

    async def _fake_knowledge(*a, **k):
        return ""

    monkeypatch.setattr(run_core, "build_knowledge_context", _fake_knowledge)
    monkeypatch.setattr(run_core, "build_behavior_instructions", lambda *a, **k: "")
    monkeypatch.setattr(run_core, "attach_sse_event_sink", lambda *a, **k: None)
    monkeypatch.setattr(run_core, "attach_agent_identity", lambda **k: None)
    monkeypatch.setattr(run_core, "detach_sse_event_sink", lambda *a, **k: None)
    monkeypatch.setattr(run_core, "detach_agent_identity", lambda *a, **k: None)

    # SS-3 mapping + SS-2 store: faked so the path is hermetic (no live mongod).
    rs = runtime_service or _FakeRuntimeService()
    monkeypatch.setattr(run_core, "runtime_service", rs)
    monkeypatch.setattr(run_core, "MongoSessionStore", _FakeStore)
    sup = supervisor or _RecordingSupervisor()
    monkeypatch.setattr(run_core, "get_session_supervisor", lambda: sup)

    async def _never_cancelled():
        return False

    out: list[tuple[str, dict]] = []
    gen = run_core._drive_agent_loop(
        _scope_ctx(),
        user_content="hi",
        attachments_in=None,
        mentions_in=None,
        history=[],
        is_cancelled=_never_cancelled,
        emit_stream_start=False,
    )
    async for ev in gen:
        out.append(ev)
    return pool, out


def _turn1_events() -> list[Any]:
    return [
        SimpleNamespace(type="message", content="hi there", metadata={}),
        SimpleNamespace(
            type="session_id",
            content="",
            metadata={"session_id": "native-abc", "backend": "claude_agent_sdk"},
        ),
        SimpleNamespace(type="done", content=""),
    ]


# --------------------------------------------------------------------------- #
# Flag ON — turn 1: capture + handle + bracket                                #
# --------------------------------------------------------------------------- #


async def test_flag_on_turn1_threads_handle_brackets_and_captures(monkeypatch):
    rs = _FakeRuntimeService(prior=None)  # turn 1 → no prior native id
    sup = _RecordingSupervisor()
    pool, out = await _drive(
        monkeypatch, events=_turn1_events(), flag_on=True, runtime_service=rs, supervisor=sup
    )

    # 1. pool.run received a SessionHandle carrying the store + a turn-1 None id.
    assert pool.run_kwargs is not None
    handle = pool.run_kwargs.get("session_handle")
    assert handle is not None, "flag ON must thread a session_handle into pool.run"
    assert handle.cli_session_id is None, "turn 1 has no native id yet → resume is None"
    assert isinstance(handle.session_store, _FakeStore)
    assert handle.session_store.workspace_id == "w1"

    # 2. The prior id was looked up with the resolved identity (ws, scope_id, agent).
    assert rs.get_calls == [("w1", "s1", "a1")]

    # 3. The turn-1 ("session_id") event persisted the native id both durably and
    #    onto the runtime — and was NOT surfaced to the SSE stream.
    assert rs.set_calls == [("w1", "s1", "a1", "native-abc", None)]
    assert sup.acq.runtime.cli_session_id == "native-abc"
    assert "session_id" not in [name for name, _ in out]

    # 4. The run was bracketed and balanced; no crash.
    assert sup.calls == ["acquire", "mark_run_start", "record_cli_session_id", "mark_run_end"]
    assert sup.acq.runtime.active_runs == 0


# --------------------------------------------------------------------------- #
# Flag ON — turn 2: resume from the prior native id                           #
# --------------------------------------------------------------------------- #


async def test_flag_on_turn2_resumes_with_prior_id(monkeypatch):
    rs = _FakeRuntimeService(prior="native-xyz")  # turn 2 → prior id known
    sup = _RecordingSupervisor()
    events = [
        SimpleNamespace(type="message", content="again", metadata={}),
        SimpleNamespace(type="done", content=""),
    ]
    pool, _out = await _drive(
        monkeypatch, events=events, flag_on=True, runtime_service=rs, supervisor=sup
    )

    handle = pool.run_kwargs.get("session_handle")
    assert handle is not None
    # The SessionHandle carries the resume id recovered from the durable mapping.
    assert handle.cli_session_id == "native-xyz"
    # acquire saw the recovered id.
    assert sup.acq.cli_session_id == "native-xyz"
    # No new capture this turn → no set_cli_session_id write, no record call.
    assert rs.set_calls == []
    assert sup.calls == ["acquire", "mark_run_start", "mark_run_end"]


# --------------------------------------------------------------------------- #
# Flag OFF — legacy path is byte-for-byte unchanged                           #
# --------------------------------------------------------------------------- #


async def test_flag_off_legacy_path_no_handle_no_supervisor(monkeypatch):
    rs = _FakeRuntimeService(prior="native-should-not-be-read")
    sup = _RecordingSupervisor()
    pool, out = await _drive(
        monkeypatch, events=_turn1_events(), flag_on=False, runtime_service=rs, supervisor=sup
    )

    # pool.run was called WITHOUT a session_handle.
    assert pool.run_called
    assert pool.run_kwargs is not None
    assert "session_handle" not in pool.run_kwargs

    # Zero supervisor / store / mapping interaction on the legacy path.
    assert rs.get_calls == []
    assert rs.set_calls == []
    assert sup.calls == []


# --------------------------------------------------------------------------- #
# Flag ON — a backend error event flags the run as crashed                    #
# --------------------------------------------------------------------------- #


async def test_flag_on_backend_error_marks_crashed(monkeypatch):
    rs = _FakeRuntimeService(prior="native-xyz")
    sup = _RecordingSupervisor()
    events = [
        SimpleNamespace(type="message", content="partial", metadata={}),
        SimpleNamespace(type="error", content="backend blew up", metadata={}),
    ]
    _pool, out = await _drive(
        monkeypatch, events=events, flag_on=True, runtime_service=rs, supervisor=sup
    )

    # The error frame is surfaced to the stream...
    assert any(name == "error" for name, _ in out)
    # ...and the runtime is demoted (mark_crashed) then released (mark_run_end).
    assert "mark_crashed" in sup.calls
    assert sup.calls[-2:] == ["mark_crashed", "mark_run_end"]
    # mark_crashed keeps the resume id for the next turn.
    assert sup.acq.runtime.cli_session_id == "native-xyz"
    assert sup.acq.runtime.active_runs == 0
