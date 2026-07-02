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
#
# WH-3 (feat/warm-reuse) coverage added 2026-06-30:
#   * Flag ON, turn 1: pool.run gets an ``on_client_built`` callback; invoking it
#     (as the backend would after a fresh build) calls ``bind_warm_slot`` with a
#     ``LeasedClient`` wrapping the built client + its options key.
#   * Flag ON, turn 2 WARM (a live slot is bound): pool.run gets
#     ``warm_client=<that slot>`` and the SessionHandle WITHHOLDS the resume id
#     (cli_session_id is None — the live client already holds the conversation;
#     the store is still threaded) so the backend's ``not resume_active`` gate
#     permits warm reuse. This is the skill-free supervised warm turn the WH-1
#     carried concern asks for (skills+leased lifecycle is a documented follow-up).
#   * Flag ON, after reap (COLD): the slot was reaped → warm_reuse=False → NO
#     warm_client kwarg; the SessionHandle still carries the cli_session_id
#     (cold-resume) and on_client_built is present to rebind a fresh slot.
#   * Flag OFF: NEITHER warm_client NOR on_client_built is added (legacy identical).
#
# WARM no-op regression (live smoke 2026-06-30) added 2026-06-30:
#   The WARM tier was a NO-OP live — every supervised turn rebuilt a fresh
#   ``claude`` client and turn 1's warm slot was torn down before turn 2. Root
#   cause: ``_drive_agent_loop`` flagged the run a CRASH (``mark_crashed`` →
#   ``_drop_slot`` + teardown) on ANY backend ``error`` event, including a benign
#   trailing one (the real ``claude_sdk`` yields ``error`` THEN ``done`` when a
#   ResultMessage carries ``is_error`` on a turn that still produced a response —
#   the leased client stays healthy). These two-turn tests drive the REAL
#   ``_drive_agent_loop`` against the REAL ``SessionSupervisor`` with a realistic
#   leased-backend fake pool (it BINDS via ``on_client_built`` like the backend
#   does after a fresh ``connect()``) and assert: a clean run keeps the slot warm
#   (baseline — passed pre-fix, isolating the trigger to the error event); a
#   benign trailing ``error`` + ``done`` MUST keep the slot warm (the repro —
#   FAILED pre-fix); and a genuine crash (``error`` with NO successful
#   completion) MUST still drop the slot → COLD cold-resume next turn (no
#   regression of the v1 crash semantics).

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from pocketpaw_ee.cloud.chat.agent_service import ScopeContext, ScopeKind
from pocketpaw_ee.cloud.chat.runs import run_core

from pocketpaw.agents.backend import LeasedClient
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
        self.bound_slot: Any = None

    def acquire(self, *a, **k):
        self.calls.append("acquire")
        self.acq = self.inner.acquire(*a, **k)
        return self.acq

    def bind_warm_slot(self, runtime, slot, teardown=None):
        self.calls.append("bind_warm_slot")
        self.bound_slot = slot
        self.inner.bind_warm_slot(runtime, slot, teardown)

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

    # WH-3: neither warm-reuse kwarg is added on the legacy path — byte-identical.
    assert "warm_client" not in pool.run_kwargs
    assert "on_client_built" not in pool.run_kwargs

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


# --------------------------------------------------------------------------- #
# WH-3 — turn 1: the executor hands the backend an on_client_built callback    #
# that binds the freshly-built client to the supervisor as the warm slot.      #
# --------------------------------------------------------------------------- #


async def test_wh3_turn1_provides_on_client_built_that_binds_warm_slot(monkeypatch):
    rs = _FakeRuntimeService(prior=None)  # turn 1 → fresh build, no warm slot
    sup = _RecordingSupervisor()
    pool, _out = await _drive(
        monkeypatch, events=_turn1_events(), flag_on=True, runtime_service=rs, supervisor=sup
    )

    # Turn 1 is a fresh build: no warm slot to lease.
    assert "warm_client" not in pool.run_kwargs
    # The executor ALWAYS hands the backend a binding callback on the supervised
    # path so the next turn can reuse the client this turn builds.
    on_built = pool.run_kwargs.get("on_client_built")
    assert callable(on_built), "flag ON must thread an on_client_built callback"

    # The supervisor has not been asked to bind anything yet (the fake pool never
    # invokes the callback) — turn 1 just BUILDS the client.
    assert "bind_warm_slot" not in sup.calls

    # Simulate the backend calling the callback after its fresh connect(): the
    # executor must register the built client as the session's warm slot.
    built_client = SimpleNamespace(name="freshly-built-claude-client")
    teardown_marker = SimpleNamespace(kind="teardown")
    on_built(built_client, "options-key-abc", teardown_marker)

    assert "bind_warm_slot" in sup.calls
    assert isinstance(sup.bound_slot, LeasedClient)
    assert sup.bound_slot.client is built_client
    assert sup.bound_slot.options_key == "options-key-abc"
    # The slot is now bound onto THIS turn's runtime, ready for warm reuse next turn.
    assert sup.acq.runtime.warm_slot is sup.bound_slot
    assert sup.acq.runtime.teardown is teardown_marker


# --------------------------------------------------------------------------- #
# WH-3 — turn 2 (WARM): a live, key-eligible slot is leased to the backend and  #
# the resume id is WITHHELD so the backend's warm-reuse gate fires.             #
# --------------------------------------------------------------------------- #


async def test_wh3_turn2_warm_leases_slot_and_withholds_resume(monkeypatch):
    # Pre-seed the supervisor exactly as turn 1 would have: a live warm slot bound
    # for (w1, s1) with a known native id. The seeding uses the inner supervisor
    # directly so it does NOT pollute the recorded call log.
    sup = _RecordingSupervisor()
    seed = sup.inner.acquire("w1", "s1", "a1", cli_session_id=None)  # turn-1 runtime
    sup.inner.record_cli_session_id(seed.runtime, "native-xyz")
    live_slot = LeasedClient(client=SimpleNamespace(name="live-warm-client"), options_key="k-live")
    sup.inner.bind_warm_slot(seed.runtime, live_slot, None)

    rs = _FakeRuntimeService(prior="native-xyz")  # the durable mapping resolves it
    events = [
        SimpleNamespace(type="message", content="again", metadata={}),
        SimpleNamespace(type="done", content=""),
    ]
    pool, _out = await _drive(
        monkeypatch, events=events, flag_on=True, runtime_service=rs, supervisor=sup
    )

    # acquire saw a live, eligible warm slot → WARM reuse.
    assert sup.acq.warm_reuse is True
    assert sup.acq.slot is live_slot

    # The live slot is leased to the backend for direct reuse.
    assert pool.run_kwargs.get("warm_client") is live_slot

    # The resume id is WITHHELD on a warm-reuse turn (the live client already
    # holds the conversation; threading resume would demote warm reuse to a cold
    # re-materialize). The store is still threaded.
    handle = pool.run_kwargs.get("session_handle")
    assert handle is not None
    assert handle.cli_session_id is None, "warm reuse must withhold the resume id"
    assert isinstance(handle.session_store, _FakeStore)
    assert handle.session_store.workspace_id == "w1"

    # The binding callback is still provided (a no-op on a pure warm reuse, but it
    # rebinds if the backend has to rebuild on a key drift).
    assert callable(pool.run_kwargs.get("on_client_built"))

    # No new native id this turn → no durable write, just the bracket.
    assert rs.set_calls == []
    assert sup.calls == ["acquire", "mark_run_start", "mark_run_end"]


# --------------------------------------------------------------------------- #
# WH-3 — after reap (COLD): the warm slot is gone → fall back to cold-resume.   #
# --------------------------------------------------------------------------- #


async def test_wh3_after_reap_cold_resume_no_warm_client(monkeypatch):
    # Pre-seed a warm slot, then drop it (mark_crashed models a reaped/failed slot:
    # warm_slot cleared, cli_session_id retained for resume).
    sup = _RecordingSupervisor()
    seed = sup.inner.acquire("w1", "s1", "a1", cli_session_id=None)
    sup.inner.record_cli_session_id(seed.runtime, "native-xyz")
    reaped_slot = LeasedClient(client=SimpleNamespace(name="reaped"), options_key="k-old")
    sup.inner.bind_warm_slot(seed.runtime, reaped_slot, None)
    sup.inner.mark_crashed(seed.runtime)  # the reaper / a failure dropped the slot
    assert seed.runtime.warm_slot is None

    rs = _FakeRuntimeService(prior="native-xyz")
    events = [
        SimpleNamespace(type="message", content="again", metadata={}),
        SimpleNamespace(type="done", content=""),
    ]
    pool, _out = await _drive(
        monkeypatch, events=events, flag_on=True, runtime_service=rs, supervisor=sup
    )

    # No live slot → fresh launch, NOT warm reuse.
    assert sup.acq.warm_reuse is False
    assert "warm_client" not in pool.run_kwargs

    # The SessionHandle carries the resume id (cold-resume from the store).
    handle = pool.run_kwargs.get("session_handle")
    assert handle is not None
    assert handle.cli_session_id == "native-xyz"

    # The binding callback is present so the freshly-built client rebinds a slot.
    assert callable(pool.run_kwargs.get("on_client_built"))


# --------------------------------------------------------------------------- #
# WARM no-op regression — two full turns through the REAL supervisor.          #
# A leased-backend fake pool that BINDS the slot (turn 1) and REUSES it        #
# (turn 2) exactly as the claude_sdk leased path does.                         #
# --------------------------------------------------------------------------- #


class _RecordingTeardown:
    """A leased-client teardown that records whether it fired.

    ``True`` means the supervisor tore the warm ``claude`` subprocess down
    (``mark_crashed`` → ``_drop_slot`` → ``_run_teardown``). On a healthy run the
    slot must SURVIVE, so this must stay ``False``.
    """

    def __init__(self) -> None:
        self.torn_down = False

    def __call__(self) -> None:
        self.torn_down = True


class _StatefulRuntimeService:
    """SS-3 stand-in that PERSISTS the captured native id across turns.

    Unlike ``_FakeRuntimeService`` (fixed ``prior``), this returns on turn 2
    whatever turn 1's ``("session_id", ...)`` event wrote — modelling the real
    durable mapping so the two-turn resume/warm decision is exercised end-to-end.
    """

    def __init__(self) -> None:
        self._id: str | None = None
        self.get_calls = 0
        self.set_calls = 0

    async def get_cli_session_id(self, ws, session, agent):
        self.get_calls += 1
        return self._id

    async def set_cli_session_id(self, ws, session, agent, cli_session_id, project_key=None):
        self.set_calls += 1
        self._id = cli_session_id


class _LeasedBackendPool:
    """Realistic fake AgentPool mirroring the claude_sdk leased path.

    * Supervised FRESH-BUILD turn (``on_client_built`` set, no ``warm_client``):
      build a fake client and BIND it via ``on_client_built`` (as the backend
      does right after its fresh ``connect()``), stream a normal response + the
      turn-1 ``session_id`` capture, then emit ``tail_events`` at end-of-stream
      to mirror whatever the real backend trails the leased-fresh stream with
      (the live trigger under test), then ``done`` — unless ``omit_done`` models
      a genuine crash (an error with NO successful completion).
    * WARM-REUSE turn (``warm_client`` leased): drive the leased client directly
      — NO new build, NO bind — and stream a short response.
    """

    def __init__(self, *, tail_events=None, omit_done=False) -> None:
        self.tail_events = list(tail_events or [])
        self.omit_done = omit_done
        self.turns: list[dict[str, Any]] = []
        self.builds = 0
        self.reuses = 0
        self.teardowns: list[_RecordingTeardown] = []

    async def get(self, _agent_id):
        return SimpleNamespace(config={"backend": "claude_agent_sdk"}, agent_name="A")

    def run(self, agent_id, content, session_key, **kwargs):
        self.turns.append(kwargs)
        warm_client = kwargs.get("warm_client")
        on_built = kwargs.get("on_client_built")

        async def _gen():
            if warm_client is not None:
                # Warm reuse: the live subprocess serves this turn directly.
                self.reuses += 1
                yield SimpleNamespace(type="message", content="reused-warm", metadata={})
                yield SimpleNamespace(type="done", content="")
                return
            # Supervised fresh build: mirror the backend binding the new client.
            if on_built is not None:
                client = SimpleNamespace(name=f"claude-client-{self.builds}")
                teardown = _RecordingTeardown()
                on_built(client, "k-options", teardown)
                self.teardowns.append(teardown)
            self.builds += 1
            yield SimpleNamespace(type="message", content="hi there", metadata={})
            yield SimpleNamespace(
                type="session_id",
                content="",
                metadata={"session_id": "native-abc", "backend": "claude_agent_sdk"},
            )
            for ev in self.tail_events:
                yield ev
            if not self.omit_done:
                yield SimpleNamespace(type="done", content="")

        return _gen()


async def _drive_turn(monkeypatch, *, pool, sup, rs) -> list[tuple[str, dict]]:
    """Drive ONE supervised turn of the REAL ``_drive_agent_loop`` against the
    given (shared-across-turns) ``pool`` / ``sup`` / ``rs``."""
    monkeypatch.setenv("POCKETPAW_SESSION_SUPERVISOR", "true")
    monkeypatch.setattr(run_core, "get_agent_pool", lambda: pool)

    async def _fake_knowledge(*a, **k):
        return ""

    monkeypatch.setattr(run_core, "build_knowledge_context", _fake_knowledge)
    monkeypatch.setattr(run_core, "build_behavior_instructions", lambda *a, **k: "")
    monkeypatch.setattr(run_core, "attach_sse_event_sink", lambda *a, **k: None)
    monkeypatch.setattr(run_core, "attach_agent_identity", lambda **k: None)
    monkeypatch.setattr(run_core, "detach_sse_event_sink", lambda *a, **k: None)
    monkeypatch.setattr(run_core, "detach_agent_identity", lambda *a, **k: None)
    monkeypatch.setattr(run_core, "runtime_service", rs)
    monkeypatch.setattr(run_core, "MongoSessionStore", _FakeStore)
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
    return out


async def test_warm_slot_survives_clean_run_two_turns(monkeypatch):
    """Baseline: a CLEAN leased-fresh turn 1 keeps its warm slot, so turn 2 reuses
    it. This PASSED before the fix — it isolates the no-op trigger to the backend
    error event (a clean fake does not reproduce the bug)."""
    sup = _RecordingSupervisor()
    rs = _StatefulRuntimeService()
    pool = _LeasedBackendPool(tail_events=[])  # clean stream: message, session_id, done

    await _drive_turn(monkeypatch, pool=pool, sup=sup, rs=rs)  # turn 1 (fresh build + bind)
    # Turn 1 captured + persisted the native id and bound a live warm slot.
    assert rs._id == "native-abc"
    assert pool.builds == 1 and pool.teardowns and pool.teardowns[0].torn_down is False

    await _drive_turn(monkeypatch, pool=pool, sup=sup, rs=rs)  # turn 2

    # Turn 2 reused the warm slot — warm_reuse True, the slot was leased, no rebuild.
    assert sup.acq.warm_reuse is True
    assert pool.turns[1].get("warm_client") is not None
    assert pool.reuses == 1
    assert pool.builds == 1  # no second build
    assert pool.teardowns[0].torn_down is False  # turn-1 client never torn down


async def test_warm_slot_survives_benign_trailing_error_two_turns(monkeypatch):
    """THE REPRODUCTION: the real claude_sdk yields a benign ``error`` (ResultMessage
    ``is_error`` on a turn that still produced a response) FOLLOWED by ``done``. The
    leased ``claude`` client is healthy, so the warm slot must SURVIVE and turn 2
    must reuse it. Pre-fix, ``_drive_agent_loop`` flagged the run a crash on the
    error event and dropped the slot → turn 2 ``warm_reuse=False`` (the no-op)."""
    sup = _RecordingSupervisor()
    rs = _StatefulRuntimeService()
    # Mirror the backend: message, session_id, error (benign), done.
    pool = _LeasedBackendPool(
        tail_events=[SimpleNamespace(type="error", content="benign result error", metadata={})]
    )

    out1 = await _drive_turn(monkeypatch, pool=pool, sup=sup, rs=rs)  # turn 1
    # The error is still surfaced to the stream (diagnostic preserved)...
    assert any(name == "error" for name, _ in out1)
    # ...but the warm slot must NOT be torn down (the client is healthy).
    assert pool.teardowns[0].torn_down is False, "benign trailing error dropped the warm slot"

    await _drive_turn(monkeypatch, pool=pool, sup=sup, rs=rs)  # turn 2

    # The headline assertion: turn 2 reuses the warm slot.
    assert sup.acq.warm_reuse is True, "WARM no-op: turn 2 rebuilt instead of reusing"
    assert pool.turns[1].get("warm_client") is not None
    assert pool.reuses == 1
    assert pool.builds == 1  # no second build


async def test_genuine_crash_two_turns_demotes_to_cold(monkeypatch):
    """No regression: a GENUINE crash (an ``error`` with NO successful completion —
    no ``done``) MUST still drop the warm slot → COLD, and turn 2 cold-resumes from
    the store (resume id threaded, no warm_client)."""
    sup = _RecordingSupervisor()
    rs = _StatefulRuntimeService()
    pool = _LeasedBackendPool(
        tail_events=[SimpleNamespace(type="error", content="backend blew up", metadata={})],
        omit_done=True,  # crash: the stream ends without a successful completion
    )

    await _drive_turn(monkeypatch, pool=pool, sup=sup, rs=rs)  # turn 1 crashes
    # A real crash tears the warm slot down (demote to COLD).
    assert pool.teardowns[0].torn_down is True
    assert "mark_crashed" in sup.calls

    await _drive_turn(monkeypatch, pool=pool, sup=sup, rs=rs)  # turn 2

    # No warm reuse — fresh launch that cold-resumes from the durable mapping.
    assert sup.acq.warm_reuse is False
    assert "warm_client" not in pool.turns[1]
    handle = pool.turns[1].get("session_handle")
    assert handle is not None and handle.cli_session_id == "native-abc"
    assert pool.builds == 2  # turn 2 rebuilt (correct for a crashed/COLD runtime)
