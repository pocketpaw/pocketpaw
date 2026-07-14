# tests/cloud/jobs/test_jobs.py
# Created: 2026-06-20 (feat/workspace-jobs, pp#1459) — TDD coverage for the
# workspace jobs primitive. Pins the nine acceptance criteria from the design:
#   1. registry lookup on an unknown job name → `job.unknown` (400)
#   2. dispatch creates a queued status doc + returns its job_id (ARQ pool mocked)
#   3. status poll returns the doc; wrong-workspace re-fetch → 404
#   4. on done the worker writes back via merge_spec under
#      user_id="system:workspace_job"
#   5. failure path writes {state: {<action>_status: "failed", ...}} so the
#      button never hangs
#   6. _validate_job_result rejects a result writing ui/actions/sources → failed
#   7. a cred-bearing param (api_key/token/secret) → 400 at dispatch
#   8. tenancy: poll with a mismatched workspace → 404
#   9. the xproc bridge: when the worker role is set, emit() routes a
#      PocketUpdated through publish_bus_envelope rather than the local bus
#
# These mirror the repo's pytest-asyncio + mongo_db fixture conventions
# (see tests/cloud/pockets/test_merge_spec_endpoint.py and conftest.py).
#
# Updated: 2026-06-20 (review fix pass) — adds coverage for the review
# findings:
#   - IMPORTANT 1: router HTTP-handler tests for the ``kind:"job"`` dispatch
#     (job_enqueued + job_id, requires_instinct → 400, no-kind back-compat).
#   - IMPORTANT 2: drives the REAL ARQ entrypoint with ``xproc.is_worker()``
#     True and asserts the worker→merge_spec→emit chain produces a
#     ``PocketUpdated`` through ``publish_bus_envelope`` (not just emit in
#     isolation).
#   - IMPORTANT 3: a job whose doc names a pocket in a DIFFERENT workspace →
#     job failed, NO merge_spec call (fail-closed tenancy re-check).
#   - MINOR A: non-empty client params on a job action → 400.
#   - MINOR B: a credential NESTED under a benign key → 400.
# A new ``seed_pocket`` fixture inserts a real Pocket doc so the worker's
# tenancy re-check has something to fetch.
#
# Updated: 2026-06-20 (audit-collision fix) — adds the C12 regression test
# ``test_lifecycle_audit_emit_succeeds`` that asserts the lifecycle audit emit
# actually reaches the logger. The earlier tests passed while the audit was
# silently broken (the `action` kwarg collided in AuditEvent.create and the
# best-effort except swallowed it); this test fails if that collision returns.
#
# Updated: 2026-06-20 (fix/jobs-arq-enqueue-contract — live-smoke showstopper):
#   - BUG 1 (arq enqueue contract): rewrote the enqueue assertion in
#     ``test_dispatch_creates_doc_and_returns_job_id`` to pin the CORRECT
#     contract (job id positional, NO ``queue``/``_queue_name`` kwarg — jobs
#     ride the shared chat-runs default queue) instead of codifying the
#     ``queue="paw:jobs"`` bug. Added
#     ``test_dispatch_enqueue_does_not_forward_stray_kwarg_to_job_fn`` — a pool
#     that mimics arq's real ``*args/**kwargs`` forwarding to the job function,
#     so a stray kwarg raises TypeError (the regression guard the mocked-pool
#     tests were missing).
#   - BUG 2 (writeback honors rejection): added
#     ``test_worker_success_writeback_rejection_marks_failed`` (merge_spec
#     ok:False on the success path → job ``failed`` + failed-state writeback)
#     and ``test_worker_success_writeback_ok_still_done`` (ok:True → still
#     ``done``, no regression).

from __future__ import annotations

from typing import Any

import pytest
import pytest_asyncio
from pocketpaw_ee.cloud.jobs import registry as jobs_registry
from pocketpaw_ee.cloud.jobs import service as jobs_service
from pocketpaw_ee.cloud.jobs import worker as jobs_worker
from pocketpaw_ee.cloud.jobs.domain import WORKSPACE_JOB_IDENTITY
from pocketpaw_ee.cloud.jobs.registry import (
    JobParamsError,
    JobResultError,
    UnknownJobError,
)

WS = "ws-jobs-1"
OTHER_WS = "ws-jobs-2"
POCKET = "pkt-jobs-1"
VIEWER = "viewer-1"


# ---------------------------------------------------------------------------
# Registry test-doubles — registered/cleared per test so the module-level
# registry never leaks state between tests.
# ---------------------------------------------------------------------------


class _EchoJob:
    """A registered job that echoes its params into a single state key."""

    name = "echo_job"

    async def __call__(
        self, *, workspace_id: str, pocket_id: str, job_id: str, params: dict
    ) -> dict:
        return {"state": {"echo_value": params.get("value", "")}}


class _BadResultJob:
    """A registered job that (incorrectly) tries to write a ui node."""

    name = "bad_result_job"

    async def __call__(
        self, *, workspace_id: str, pocket_id: str, job_id: str, params: dict
    ) -> dict:
        return {"ui": {"id": "x", "type": "text"}}


class _BoomJob:
    """A registered job that raises."""

    name = "boom_job"

    async def __call__(
        self, *, workspace_id: str, pocket_id: str, job_id: str, params: dict
    ) -> dict:
        raise RuntimeError("boom")


@pytest.fixture(autouse=True)
def _clean_registry():
    """Register the test doubles and restore the registry afterwards."""
    saved = dict(jobs_registry.get_job_registry())
    jobs_registry.get_job_registry().clear()
    jobs_registry.register_job(_EchoJob())
    jobs_registry.register_job(_BadResultJob())
    jobs_registry.register_job(_BoomJob())
    yield
    reg = jobs_registry.get_job_registry()
    reg.clear()
    reg.update(saved)


# ---------------------------------------------------------------------------
# 1. Registry lookup on an unknown job → UnknownJobError (400).
# ---------------------------------------------------------------------------


def test_unknown_job_raises_job_unknown() -> None:
    with pytest.raises(UnknownJobError) as exc:
        jobs_registry.resolve_job("does_not_exist")
    assert exc.value.code == "job.unknown"
    assert exc.value.status == 400


# ---------------------------------------------------------------------------
# 7. A cred-bearing param → JobParamsError (400). (Listed before #2 because
#    dispatch validates params, so this guards the dispatch path too.)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_key", ["api_key", "API_KEY", "token", "secret", "my_credential"])
def test_cred_bearing_param_rejected(bad_key: str) -> None:
    with pytest.raises(JobParamsError) as exc:
        jobs_registry.validate_job_params({bad_key: "leak-me", "batch_size": 10})
    assert exc.value.code == "job.params_forbidden"
    assert exc.value.status == 400


def test_clean_params_pass_validation() -> None:
    # No raise — a benign params dict is accepted untouched.
    jobs_registry.validate_job_params({"batch_size": 20, "connector": "snctm-api"})


@pytest.mark.parametrize(
    "nested_params",
    [
        {"config": {"api_key": "leak-me"}},
        {"outer": {"inner": {"token": "leak-me"}}},
        {"items": [{"name": "ok"}, {"secret": "leak-me"}]},
        {"a": [{"b": {"password": "leak-me"}}]},
    ],
)
def test_nested_cred_bearing_param_rejected(nested_params: dict) -> None:
    # MINOR B — the scrub recurses, so a credential buried under a benign key
    # (or inside a list of dicts) is rejected just like a top-level one.
    with pytest.raises(JobParamsError) as exc:
        jobs_registry.validate_job_params(nested_params)
    assert exc.value.code == "job.params_forbidden"
    assert exc.value.status == 400


def test_nested_clean_params_pass_validation() -> None:
    # Deeply nested but credential-free → accepted untouched.
    jobs_registry.validate_job_params(
        {"config": {"batch_size": 10, "rows": [{"id": "a1"}, {"id": "a2"}]}}
    )


# ---------------------------------------------------------------------------
# 6. _validate_job_result rejects ui/actions/sources/shape writes.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("forbidden_key", ["ui", "actions", "sources", "shape"])
def test_validate_job_result_rejects_template_owned_keys(forbidden_key: str) -> None:
    with pytest.raises(JobResultError) as exc:
        jobs_registry.validate_job_result({forbidden_key: {"anything": 1}})
    assert exc.value.code == "job.result_forbidden"


def test_validate_job_result_accepts_state_only() -> None:
    # State-only is the only legal shape — returns the result unchanged.
    out = jobs_registry.validate_job_result({"state": {"k": "v"}})
    assert out == {"state": {"k": "v"}}


# ---------------------------------------------------------------------------
# 2. Dispatch creates a queued doc + returns job_id (ARQ pool mocked).
# ---------------------------------------------------------------------------


class _FakePool:
    def __init__(self) -> None:
        self.calls: list[tuple[tuple, dict]] = []

    async def enqueue_job(self, *args: Any, **kwargs: Any) -> None:
        self.calls.append((args, kwargs))


@pytest_asyncio.fixture
async def fake_pool(monkeypatch: pytest.MonkeyPatch) -> _FakePool:
    pool = _FakePool()

    async def _get_pool() -> _FakePool:
        return pool

    monkeypatch.setattr(jobs_service, "_get_pool", _get_pool)
    return pool


@pytest_asyncio.fixture
async def seed_pocket(mongo_db: Any):  # noqa: ARG001 — mongo_db forces Beanie init
    """Insert a real Pocket doc and return its ObjectId string.

    The worker's fail-closed tenancy re-check (IMPORTANT 3) fetches the pocket
    the job names and asserts it lives in the job's workspace, so the worker
    tests need a persisted pocket whose id is a valid ObjectId and whose
    ``workspace`` matches the dispatch. Returns a factory so a test can seed a
    pocket in a chosen workspace (the tenancy-mismatch test seeds one in a
    DIFFERENT workspace than the job doc).
    """
    from pocketpaw_ee.cloud.models.pocket import Pocket as _PocketDoc

    async def _seed(*, workspace: str = WS, owner: str = "owner-1") -> str:
        doc = _PocketDoc(workspace=workspace, name="jobs-test-pocket", owner=owner)
        await doc.insert()
        return str(doc.id)

    return _seed


@pytest.mark.asyncio
async def test_dispatch_creates_doc_and_returns_job_id(mongo_db: Any, fake_pool: _FakePool) -> None:  # noqa: ARG001 — mongo_db forces Beanie init
    result = await jobs_service.dispatch_job(
        workspace_id=WS,
        pocket_id=POCKET,
        action="run_echo",
        job_name="echo_job",
        params={"value": "hi"},
        triggered_by=VIEWER,
    )
    assert result["ok"] is True
    assert result["code"] == "job_enqueued"
    job_id = result["job_id"]
    assert job_id

    # The status doc was persisted as `queued`.
    doc = await jobs_service.get_job(WS, job_id)
    assert doc is not None
    assert doc.status == "queued"
    assert doc.workspace == WS
    assert doc.job_name == "echo_job"
    assert doc.triggered_by == VIEWER

    # The ARQ pool was asked to enqueue exactly one job. arq 0.28's
    # ``enqueue_job(function, *args, _job_id=None, _queue_name=None, **kwargs)``
    # forwards ``**kwargs`` to the job FUNCTION as call args, so the enqueue
    # must pass the job id positionally and carry NO stray kwarg. Jobs ride the
    # shared chat-runs default queue on the one worker (single-process design),
    # so there is no queue selector here (``_queue_name`` would be the real one,
    # never ``queue``).
    assert len(fake_pool.calls) == 1
    (args, kwargs) = fake_pool.calls[0]
    assert args[0] == "execute_workspace_job"
    assert job_id in args
    assert "queue" not in kwargs
    assert "_queue_name" not in kwargs


@pytest.mark.asyncio
async def test_dispatch_unknown_job_raises_before_enqueue(
    mongo_db: Any, fake_pool: _FakePool
) -> None:  # noqa: ARG001
    with pytest.raises(UnknownJobError):
        await jobs_service.dispatch_job(
            workspace_id=WS,
            pocket_id=POCKET,
            action="run_x",
            job_name="not_a_real_job",
            params={},
            triggered_by=VIEWER,
        )
    # Nothing enqueued — the lookup gates before the pool is touched.
    assert fake_pool.calls == []


# ---------------------------------------------------------------------------
# BUG 1 regression guard — the arq enqueue contract. ``_FakePool`` records
# kwargs but never FORWARDS them, so it can't catch the real failure: arq's
# ``enqueue_job(function, *args, **kwargs)`` passes ``**kwargs`` straight to the
# job FUNCTION. A stray ``queue=`` kwarg therefore reaches
# ``execute_workspace_job(ctx, job_id, queue=...)`` → TypeError, crashing the
# worker on EVERY job. This pool mimics that forwarding so the dispatch can be
# proven against the real arq call shape.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_enqueue_does_not_forward_stray_kwarg_to_job_fn(
    mongo_db: Any, monkeypatch: pytest.MonkeyPatch
) -> None:  # noqa: ARG001 — mongo_db forces Beanie init
    """Reproduce the showstopper: any kwarg the dispatch passes to
    ``enqueue_job`` (other than arq's underscore-prefixed control kwargs) is
    forwarded to the job function and explodes it with a TypeError.

    FAILS on the unfixed code (``queue=JOBS_QUEUE`` is forwarded →
    ``TypeError: got an unexpected keyword argument 'queue'``); PASSES once the
    dispatch drops the ``queue=`` kwarg and matches the chat-runs contract.
    """
    forwarded: dict[str, Any] = {}

    # A stub job function with the SAME signature the real worker registers:
    # ``execute_workspace_job(ctx, job_id)`` — no ``queue`` parameter.
    async def _stub_execute_workspace_job(ctx: dict, job_id: str) -> None:
        forwarded["job_id"] = job_id

    class _ForwardingPool:
        """Mimics arq.ArqRedis.enqueue_job: strips the underscore control
        kwargs and forwards everything else to the registered job function."""

        async def enqueue_job(
            self,
            function: str,
            *args: Any,
            _job_id: Any = None,
            _queue_name: Any = None,
            _defer_until: Any = None,
            _defer_by: Any = None,
            _expires: Any = None,
            **kwargs: Any,
        ) -> None:
            # arq forwards (*args, **kwargs) to the job coroutine, prepending
            # the worker ctx. A stray ``queue=`` lands in **kwargs and detonates.
            await _stub_execute_workspace_job({}, *args, **kwargs)

    async def _get_pool() -> _ForwardingPool:
        return _ForwardingPool()

    monkeypatch.setattr(jobs_service, "_get_pool", _get_pool)

    # No TypeError → the dispatch passes only what the job function accepts.
    result = await jobs_service.dispatch_job(
        workspace_id=WS,
        pocket_id=POCKET,
        action="run_echo",
        job_name="echo_job",
        params={"value": "hi"},
        triggered_by=VIEWER,
    )
    assert result["ok"] is True
    # The forwarding pool actually reached the stub job with the job id.
    assert forwarded["job_id"] == result["job_id"]


# ---------------------------------------------------------------------------
# 3 + 8. Status poll returns the doc; wrong workspace → None (router 404s).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_job_enforces_tenancy(mongo_db: Any, fake_pool: _FakePool) -> None:  # noqa: ARG001
    result = await jobs_service.dispatch_job(
        workspace_id=WS,
        pocket_id=POCKET,
        action="run_echo",
        job_name="echo_job",
        params={"value": "hi"},
        triggered_by=VIEWER,
    )
    job_id = result["job_id"]

    # Right workspace → found.
    assert await jobs_service.get_job(WS, job_id) is not None
    # Wrong workspace → None even though the id is real (router maps to 404).
    assert await jobs_service.get_job(OTHER_WS, job_id) is None


# ---------------------------------------------------------------------------
# C12 (audit) — the lifecycle audit emit must actually SUCCEED, not be eaten
# by the best-effort except. Regression guard for the pp#1459 review: _audit
# passed `action` BOTH as AuditEvent.create's explicit param AND inside
# **context, so create() raised TypeError and every job audit record was
# silently dropped. The earlier tests passed *because* the failure was
# swallowed — this one asserts the success path so the collision can't return.
# ---------------------------------------------------------------------------


class _RecordingAuditLogger:
    """Captures the AuditEvents that reach the logger's ``.log``."""

    def __init__(self) -> None:
        self.events: list[Any] = []

    def log(self, event: Any) -> None:
        self.events.append(event)


@pytest.mark.asyncio
async def test_lifecycle_audit_emit_succeeds(
    mongo_db: Any,  # noqa: ARG001 — forces Beanie init
    fake_pool: _FakePool,
    seed_pocket,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    recorder = _RecordingAuditLogger()
    monkeypatch.setattr(jobs_service, "get_audit_logger", lambda: recorder)

    async def _fake_merge_spec(workspace_id, user_id, pocket_id, body):  # type: ignore[no-untyped-def]
        return {"ok": True}

    monkeypatch.setattr(jobs_worker, "merge_spec", _fake_merge_spec)

    pocket_id = await seed_pocket(workspace=WS)
    with caplog.at_level("WARNING"):
        result = await jobs_service.dispatch_job(
            workspace_id=WS,
            pocket_id=pocket_id,
            action="run_echo",
            job_name="echo_job",
            params={"value": "hi"},
            triggered_by=VIEWER,
        )
        await jobs_worker.execute_workspace_job({}, result["job_id"])

    # The best-effort except NEVER fired — i.e. AuditEvent.create did not raise.
    assert "jobs: audit emit failed" not in caplog.text
    # Both lifecycle steps audited: enqueue (success) + done.
    assert len(recorder.events) >= 2
    enqueue_event = recorder.events[0]
    # The audit event-type stays the stable string; the job's own action name
    # rides in context under `pocket_action` (renamed to dodge the create()
    # kwarg collision), so both survive into the record.
    assert enqueue_event.action == "workspace_job"
    assert enqueue_event.actor == WORKSPACE_JOB_IDENTITY
    assert enqueue_event.context.get("pocket_action") == "run_echo"
    # The literal `action` key must be gone from context (renamed), so it can
    # never shadow create()'s explicit param again.
    assert "action" not in enqueue_event.context


# ---------------------------------------------------------------------------
# 4. Worker writeback calls merge_spec with the synthetic identity.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_worker_writeback_uses_system_identity(
    mongo_db: Any, fake_pool: _FakePool, seed_pocket, monkeypatch: pytest.MonkeyPatch
) -> None:  # noqa: ARG001
    captured: dict[str, Any] = {}

    async def _fake_merge_spec(workspace_id, user_id, pocket_id, body):  # type: ignore[no-untyped-def]
        captured["workspace_id"] = workspace_id
        captured["user_id"] = user_id
        captured["pocket_id"] = pocket_id
        captured["body"] = body
        return {"ok": True}

    monkeypatch.setattr(jobs_worker, "merge_spec", _fake_merge_spec)

    # Seed a real pocket in WS so the worker's tenancy re-check passes.
    pocket_id = await seed_pocket(workspace=WS)
    result = await jobs_service.dispatch_job(
        workspace_id=WS,
        pocket_id=pocket_id,
        action="run_echo",
        job_name="echo_job",
        params={"value": "hello"},
        triggered_by=VIEWER,
    )
    job_id = result["job_id"]

    await jobs_worker.execute_workspace_job({}, job_id)

    assert captured["user_id"] == WORKSPACE_JOB_IDENTITY
    assert captured["user_id"] == "system:workspace_job"
    assert captured["workspace_id"] == WS
    assert captured["pocket_id"] == pocket_id
    # Writeback is a partial-spec merge carrying only state.
    assert captured["body"]["merge"]["state"]["echo_value"] == "hello"
    # Bug B — the WORKER (not the built-in) owns the success status flag, keyed
    # by the job's ACTION name. `echo_job` returns no status key; the worker
    # stamps `run_echo_status: "done"` so a status-bound widget lines up for any
    # action name.
    assert captured["body"]["merge"]["state"]["run_echo_status"] == "done"

    doc = await jobs_service.get_job(WS, job_id)
    assert doc is not None
    assert doc.status == "done"


# ---------------------------------------------------------------------------
# BUG 2 — the success-path writeback must HONOR merge_spec's rejection. When
# the catalog / action-wiring gate rejects the merge, merge_spec persists
# NOTHING and returns {ok: False, warnings: [...]}. The worker must NOT then
# mark the job ``done`` (the button would show done while the canvas never
# changed) — it must treat the rejected writeback as a failure: mark the job
# ``failed`` AND write the failed-state marker so the button stops spinning.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_worker_success_writeback_rejection_marks_failed(
    mongo_db: Any, fake_pool: _FakePool, seed_pocket, monkeypatch: pytest.MonkeyPatch
) -> None:  # noqa: ARG001
    """A job whose success writeback is REJECTED by merge_spec (ok:False) ends
    ``failed`` (not ``done``), and a failed-state writeback is attempted so the
    button un-hangs. FAILS on the unfixed worker, which discards merge_spec's
    return and marks the job ``done`` on a write that never persisted."""
    calls: list[dict] = []

    async def _rejecting_merge_spec(workspace_id, user_id, pocket_id, body):  # type: ignore[no-untyped-def]
        calls.append(dict(body))
        # First call is the success-path writeback — gate REJECTS it.
        # The failure-path writeback (the second call) must still succeed so
        # the button un-hangs; mimic merge_spec persisting the failed marker.
        if len(calls) == 1:
            return {"ok": False, "warnings": ["catalog: widget not allowed"]}
        return {"ok": True}

    monkeypatch.setattr(jobs_worker, "merge_spec", _rejecting_merge_spec)

    pocket_id = await seed_pocket(workspace=WS)
    result = await jobs_service.dispatch_job(
        workspace_id=WS,
        pocket_id=pocket_id,
        action="run_echo",
        job_name="echo_job",
        params={"value": "hi"},
        triggered_by=VIEWER,
    )
    job_id = result["job_id"]

    await jobs_worker.execute_workspace_job({}, job_id)

    # Two merge_spec calls: the rejected success writeback, then the
    # best-effort failed-state writeback.
    assert len(calls) == 2
    failed_state = calls[1]["merge"]["state"]
    assert failed_state["run_echo_status"] == "failed"
    assert "run_echo_error" in failed_state

    doc = await jobs_service.get_job(WS, job_id)
    assert doc is not None
    assert doc.status == "failed"
    assert doc.error


@pytest.mark.asyncio
async def test_worker_success_writeback_ok_still_done(
    mongo_db: Any, fake_pool: _FakePool, seed_pocket, monkeypatch: pytest.MonkeyPatch
) -> None:  # noqa: ARG001
    """Regression guard for BUG 2's fix: a writeback that merge_spec ACCEPTS
    (ok:True) still ends the job ``done`` — the ok-check escalation fires only
    on rejection, so the happy path is unchanged."""
    captured: dict[str, Any] = {}

    async def _accepting_merge_spec(workspace_id, user_id, pocket_id, body):  # type: ignore[no-untyped-def]
        captured["body"] = body
        return {"ok": True}

    monkeypatch.setattr(jobs_worker, "merge_spec", _accepting_merge_spec)

    pocket_id = await seed_pocket(workspace=WS)
    result = await jobs_service.dispatch_job(
        workspace_id=WS,
        pocket_id=pocket_id,
        action="run_echo",
        job_name="echo_job",
        params={"value": "hi"},
        triggered_by=VIEWER,
    )
    job_id = result["job_id"]

    await jobs_worker.execute_workspace_job({}, job_id)

    assert captured["body"]["merge"]["state"]["echo_value"] == "hi"
    doc = await jobs_service.get_job(WS, job_id)
    assert doc is not None
    assert doc.status == "done"


# ---------------------------------------------------------------------------
# 5. Failure path writes {state: {<action>_status: "failed", ...}}.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_worker_failure_writes_failed_state(
    mongo_db: Any, fake_pool: _FakePool, seed_pocket, monkeypatch: pytest.MonkeyPatch
) -> None:  # noqa: ARG001
    captured: dict[str, Any] = {}

    async def _fake_merge_spec(workspace_id, user_id, pocket_id, body):  # type: ignore[no-untyped-def]
        captured["body"] = body
        return {"ok": True}

    monkeypatch.setattr(jobs_worker, "merge_spec", _fake_merge_spec)

    pocket_id = await seed_pocket(workspace=WS)
    result = await jobs_service.dispatch_job(
        workspace_id=WS,
        pocket_id=pocket_id,
        action="run_boom",
        job_name="boom_job",
        params={},
        triggered_by=VIEWER,
    )
    job_id = result["job_id"]

    await jobs_worker.execute_workspace_job({}, job_id)

    # The button never hangs: a failed-state writeback fires under the action.
    state = captured["body"]["merge"]["state"]
    assert state["run_boom_status"] == "failed"
    assert "run_boom_error" in state

    doc = await jobs_service.get_job(WS, job_id)
    assert doc is not None
    assert doc.status == "failed"
    assert doc.error


@pytest.mark.asyncio
async def test_worker_rejects_template_owned_result(
    mongo_db: Any, fake_pool: _FakePool, seed_pocket, monkeypatch: pytest.MonkeyPatch
) -> None:  # noqa: ARG001
    captured: dict[str, Any] = {}

    async def _fake_merge_spec(workspace_id, user_id, pocket_id, body):  # type: ignore[no-untyped-def]
        captured["body"] = body
        return {"ok": True}

    monkeypatch.setattr(jobs_worker, "merge_spec", _fake_merge_spec)

    pocket_id = await seed_pocket(workspace=WS)
    result = await jobs_service.dispatch_job(
        workspace_id=WS,
        pocket_id=pocket_id,
        action="run_bad",
        job_name="bad_result_job",  # returns a `ui` write → must be rejected
        params={},
        triggered_by=VIEWER,
    )
    job_id = result["job_id"]

    await jobs_worker.execute_workspace_job({}, job_id)

    # The forbidden result was NOT written; a failed-state writeback fired.
    assert "ui" not in captured["body"]["merge"]
    state = captured["body"]["merge"]["state"]
    assert state["run_bad_status"] == "failed"

    doc = await jobs_service.get_job(WS, job_id)
    assert doc is not None
    assert doc.status == "failed"


@pytest.mark.asyncio
async def test_worker_tenancy_recheck_missing_doc_noop(
    mongo_db: Any, fake_pool: _FakePool, monkeypatch: pytest.MonkeyPatch
) -> None:  # noqa: ARG001
    """A worker handed an id with no matching doc must not call merge_spec."""
    called = {"merge": False}

    async def _fake_merge_spec(*a, **k):  # type: ignore[no-untyped-def]
        called["merge"] = True
        return {"ok": True}

    monkeypatch.setattr(jobs_worker, "merge_spec", _fake_merge_spec)
    # No doc was created for this id.
    await jobs_worker.execute_workspace_job({}, "no-such-job-id")
    assert called["merge"] is False


@pytest.mark.asyncio
async def test_worker_tenancy_mismatch_fails_without_writeback(
    mongo_db: Any, fake_pool: _FakePool, seed_pocket, monkeypatch: pytest.MonkeyPatch
) -> None:  # noqa: ARG001
    """IMPORTANT 3 — a job whose doc.pocket lives in a DIFFERENT workspace than
    doc.workspace is marked failed with NO merge_spec call (fail-closed)."""
    called = {"merge": False}

    async def _fake_merge_spec(*a, **k):  # type: ignore[no-untyped-def]
        called["merge"] = True
        return {"ok": True}

    monkeypatch.setattr(jobs_worker, "merge_spec", _fake_merge_spec)

    # The pocket really lives in OTHER_WS, but we dispatch the job under WS —
    # simulating a doc whose workspace no longer matches the pocket's tenancy.
    pocket_id = await seed_pocket(workspace=OTHER_WS)
    result = await jobs_service.dispatch_job(
        workspace_id=WS,
        pocket_id=pocket_id,
        action="run_echo",
        job_name="echo_job",
        params={"value": "hi"},
        triggered_by=VIEWER,
    )
    job_id = result["job_id"]

    await jobs_worker.execute_workspace_job({}, job_id)

    # No cross-workspace write fired.
    assert called["merge"] is False
    # The job is marked failed so the caller / poll sees the rejection.
    doc = await jobs_service.get_job(WS, job_id)
    assert doc is not None
    assert doc.status == "failed"
    assert doc.error


# ---------------------------------------------------------------------------
# 9. xproc bridge — when the worker role is set, emit routes a PocketUpdated
#    through publish_bus_envelope (not the local bus).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_emit_routes_through_xproc_when_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pocketpaw_ee.cloud._core.realtime import emit as emit_mod
    from pocketpaw_ee.cloud._core.realtime import xproc
    from pocketpaw_ee.cloud._core.realtime.events import PocketUpdated

    published: list[Any] = []

    async def _fake_publish(event):  # type: ignore[no-untyped-def]
        published.append(event)

    monkeypatch.setattr(xproc, "is_worker", lambda: True)
    monkeypatch.setattr(emit_mod.xproc, "publish_bus_envelope", _fake_publish)

    evt = PocketUpdated(data={"id": POCKET, "workspace": WS})
    await emit_mod.emit(evt)

    assert len(published) == 1
    assert published[0].type == "pocket.updated"
    assert published[0].data["id"] == POCKET


@pytest.mark.asyncio
async def test_worker_writeback_emits_pocket_updated_in_worker_context(
    mongo_db: Any, fake_pool: _FakePool, seed_pocket, monkeypatch: pytest.MonkeyPatch
) -> None:  # noqa: ARG001
    """IMPORTANT 2 — drive the REAL ARQ entrypoint with ``is_worker()`` True and
    a real ``merge_spec`` writeback, and assert the worker→merge_spec→emit chain
    publishes a ``PocketUpdated`` through ``publish_bus_envelope`` for the right
    pocket. Unlike ``test_emit_routes_through_xproc_when_worker`` (which hand-
    builds the event and calls ``emit`` in isolation), this exercises the
    writeback that PRODUCES the emit in worker context.
    """
    from pocketpaw_ee.cloud._core.realtime import emit as emit_mod
    from pocketpaw_ee.cloud._core.realtime import xproc
    from pocketpaw_ee.cloud.pockets import service as pockets_service

    published: list[Any] = []

    async def _fake_publish(event):  # type: ignore[no-untyped-def]
        published.append(event)

    # Catalog validation is orthogonal to the emit chain under test — no-op it
    # so a minimal seeded spec doesn't trip the strict gate. Everything else on
    # the merge_spec path (merge, normalize, save, _pocket_event_payload, emit)
    # runs for real, so the PocketUpdated is the one merge_spec actually fires.
    async def _noop_gate_catalog(*a, **k):  # type: ignore[no-untyped-def]
        return None

    monkeypatch.setattr(pockets_service, "_gate_catalog", _noop_gate_catalog)

    # Seed a real pocket in WS with a minimal valid rippleSpec so the state-only
    # job result merges cleanly.
    from pocketpaw_ee.cloud.models.pocket import Pocket as _PocketDoc

    pdoc = _PocketDoc(
        workspace=WS,
        name="jobs-emit-pocket",
        owner="owner-1",
        rippleSpec={"version": "1.0", "state": {}, "ui": {"id": "n_root0001", "type": "card"}},
    )
    await pdoc.insert()
    pocket_id = str(pdoc.id)

    result = await jobs_service.dispatch_job(
        workspace_id=WS,
        pocket_id=pocket_id,
        action="run_echo",
        job_name="echo_job",
        params={"value": "emitted"},
        triggered_by=VIEWER,
    )
    job_id = result["job_id"]

    # Flip to worker context ONLY for the worker run — dispatch above already
    # emitted (WorkspaceJobQueued) against the recording bus in web context.
    monkeypatch.setattr(xproc, "is_worker", lambda: True)
    monkeypatch.setattr(emit_mod.xproc, "publish_bus_envelope", _fake_publish)

    await jobs_worker.execute_workspace_job({}, job_id)

    # The writeback's merge_spec fired a PocketUpdated for THIS pocket, and it
    # routed over the xproc bridge (not the local bus) because is_worker()=True.
    pocket_updates = [e for e in published if e.type == "pocket.updated"]
    assert pocket_updates, f"expected a pocket.updated emit, saw: {[e.type for e in published]}"
    # merge_spec's PocketUpdated carries the id under ``pocket_id`` (see
    # _pocket_event_payload). Assert it names the pocket the job wrote to.
    assert any(e.data.get("pocket_id") == pocket_id for e in pocket_updates)

    # And the merge actually persisted the job's state-only result.
    refreshed = await _PocketDoc.get(pdoc.id)
    assert refreshed is not None
    assert refreshed.rippleSpec["state"]["echo_value"] == "emitted"


# ---------------------------------------------------------------------------
# Built-in PII allowlist — score_applications strips email/phone before the
# scored rows become broadcast state.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_score_applications_strips_pii() -> None:
    from pocketpaw_ee.cloud.jobs.builtin.score_applications import (
        ScoreApplicationsJob,
        project_row,
    )

    # The pure projection drops any non-allowlisted field — notably PII. `band`
    # is now part of the allowlist (the heuristic emits a Strong/Moderate/Weak
    # band alongside the score).
    projected = project_row(
        {
            "id": "a1",
            "name": "Jordan",
            "score": 60,
            "band": "Moderate",
            "stage": "scored",
            "email": "jordan@example.com",
            "phone": "+1-555-0100",
            "ssn": "000-00-0000",
        }
    )
    assert "email" not in projected
    assert "phone" not in projected
    assert "ssn" not in projected
    assert projected == {
        "id": "a1",
        "name": "Jordan",
        "score": 60,
        "band": "Moderate",
        "stage": "scored",
    }

    # End-to-end through the job (params.rows fallback): a row carrying PII
    # never reaches the result. The built-in NO LONGER returns a status key —
    # Bug B moved `<action>_status` ownership to the worker — so the result is
    # exactly `scored_rows` + the scored count.
    job = ScoreApplicationsJob()
    result = await job(
        workspace_id=WS,
        pocket_id=POCKET,
        job_id="j1",
        params={"rows": [{"id": "a1", "name": "Jordan", "email": "jordan@example.com"}]},
    )
    rows = result["state"]["scored_rows"]
    assert rows and all("email" not in r and "phone" not in r for r in rows)
    # The built-in itself stamps NO status key (the worker owns it now).
    assert "score_applications_status" not in result["state"]
    assert result["state"]["score_applications_scored_count"] == 1
    # The scored row carries the heuristic's score + band + stage.
    scored = rows[0]
    assert scored["id"] == "a1"
    assert isinstance(scored["score"], int) and 0 <= scored["score"] <= 100
    assert scored["band"] in ("Strong", "Moderate", "Weak")
    assert scored["stage"] == "scored"


# ---------------------------------------------------------------------------
# Writeback access — merge_spec's edit-access check ALLOWS the synthetic job
# identity on a private pocket (so the writeback never 403s + hangs), but
# still rejects an arbitrary user.
# ---------------------------------------------------------------------------


class _PrivatePocket:
    """Minimal stand-in carrying only the fields the access check reads."""

    id = "pkt-private"
    owner = "owner-1"
    team: tuple[str, ...] = ()
    shared_with: tuple[str, ...] = ()
    visibility = "private"


def test_system_identity_bypasses_edit_check_on_private_pocket() -> None:
    from pocketpaw_ee.cloud._core.errors import Forbidden
    from pocketpaw_ee.cloud.pockets.service import _check_domain_edit_access

    pocket = _PrivatePocket()
    # The synthetic job identity is allowed (no raise) even on a private pocket.
    _check_domain_edit_access(pocket, WORKSPACE_JOB_IDENTITY)  # type: ignore[arg-type]
    # An arbitrary user is still rejected.
    with pytest.raises(Forbidden):
        _check_domain_edit_access(pocket, "random-user")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# IMPORTANT 1 + MINOR A — router HTTP-handler coverage for the ``kind:"job"``
# dispatch in ``POST /pockets/{id}/actions/run``. Mirrors the app/auth-override
# pattern of tests/cloud/pockets/test_tools_run.py, but runs over an async
# httpx client so the real ``dispatch_job`` (which writes a Beanie doc) works
# against the ``mongo_db`` fixture. The ARQ pool is faked (``fake_pool``) so
# nothing is really enqueued. ``pockets_service.get`` is stubbed to return a
# pocket whose ``rippleSpec.actions`` carries the action under test, so the
# route reads the binding without a real Mongo pocket.
# ---------------------------------------------------------------------------

ROUTER_WS = "ws-jobs-1"
ROUTER_USER = "user-jobs-1"


def _job_action_spec(action: dict) -> dict:
    """A minimal pocket wire-dict whose rippleSpec.actions has one action."""
    return {"_id": "pkt-router-1", "rippleSpec": {"actions": {"act1": action}}}


@pytest_asyncio.fixture
async def jobs_router_client(monkeypatch: pytest.MonkeyPatch):
    """Mount the pockets router with auth/license overridden; yield an async
    client + a place to pin the pocket the route loads."""
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient
    from pocketpaw_ee.cloud._core.http import add_error_handler
    from pocketpaw_ee.cloud.license import require_license
    from pocketpaw_ee.cloud.pockets import service as pockets_service
    from pocketpaw_ee.cloud.pockets.router import router as pockets_router
    from pocketpaw_ee.cloud.shared.deps import (
        current_user_id,
        current_workspace_id,
        require_pocket_action_run,
    )

    state: dict[str, Any] = {"pocket": None}

    async def _get(pocket_id, user_id):  # type: ignore[no-untyped-def]
        return state["pocket"]

    monkeypatch.setattr(pockets_service, "get", _get)

    app = FastAPI()
    add_error_handler(app)
    app.include_router(pockets_router)
    app.dependency_overrides[require_license] = lambda: None
    app.dependency_overrides[require_pocket_action_run] = lambda: None
    app.dependency_overrides[current_user_id] = lambda: ROUTER_USER
    app.dependency_overrides[current_workspace_id] = lambda: ROUTER_WS

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        yield client, state


@pytest.mark.asyncio
async def test_route_kind_job_enqueues_and_returns_job_id(
    mongo_db: Any, fake_pool: _FakePool, jobs_router_client
) -> None:  # noqa: ARG001
    """(a) a kind:"job" action → code:"job_enqueued" + a job_id; the ARQ pool
    was asked to enqueue (faked, nothing real queued)."""
    client, state = jobs_router_client
    state["pocket"] = _job_action_spec({"kind": "job", "job": "echo_job"})

    res = await client.post("/pockets/pkt-router-1/actions/run", json={"action": "act1"})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["ok"] is True
    assert body["code"] == "job_enqueued"
    assert body["job_id"]
    # The faked pool received exactly one enqueue — nothing hit real Redis.
    assert len(fake_pool.calls) == 1
    assert fake_pool.calls[0][0][0] == "execute_workspace_job"


@pytest.mark.asyncio
async def test_route_kind_job_requires_instinct_rejected(
    mongo_db: Any, fake_pool: _FakePool, jobs_router_client
) -> None:  # noqa: ARG001
    """(b) a kind:"job" action with requires_instinct:true → HTTP 400 with
    code job.instinct_not_yet_supported; nothing enqueued."""
    client, state = jobs_router_client
    state["pocket"] = _job_action_spec(
        {"kind": "job", "job": "echo_job", "requires_instinct": True}
    )

    res = await client.post("/pockets/pkt-router-1/actions/run", json={"action": "act1"})
    assert res.status_code == 400, res.text
    assert res.json()["error"]["code"] == "job.instinct_not_yet_supported"
    assert fake_pool.calls == []


@pytest.mark.asyncio
async def test_route_kind_job_rejects_client_params(
    mongo_db: Any, fake_pool: _FakePool, jobs_router_client
) -> None:  # noqa: ARG001
    """MINOR A — non-empty client params on a kind:"job" run → HTTP 400 with
    code job.params_not_accepted; nothing enqueued (a clicker can't widen the
    job's scope)."""
    client, state = jobs_router_client
    state["pocket"] = _job_action_spec({"kind": "job", "job": "echo_job"})

    res = await client.post(
        "/pockets/pkt-router-1/actions/run",
        json={"action": "act1", "params": {"value": "smuggled"}},
    )
    assert res.status_code == 400, res.text
    assert res.json()["error"]["code"] == "job.params_not_accepted"
    assert fake_pool.calls == []


@pytest.mark.asyncio
async def test_route_no_kind_does_not_enqueue_job(
    mongo_db: Any, fake_pool: _FakePool, jobs_router_client, monkeypatch: pytest.MonkeyPatch
) -> None:  # noqa: ARG001
    """(c) an action with NO kind still hits the existing (non-job) action path
    (back-compat) — it does NOT enqueue a job nor return job_enqueued.

    We stub the credential reader to return None so the legacy path short-
    circuits at the "no backend configured" gate — that's enough to prove the
    request fell through to the HTTP-write branch rather than the job branch.
    """
    from pocketpaw_ee.cloud.pockets import service as pockets_service

    async def _no_creds(workspace_id, pocket_id):  # type: ignore[no-untyped-def]
        return None

    monkeypatch.setattr(pockets_service, "get_pocket_backend_for_executor", _no_creds)

    client, state = jobs_router_client
    # A classic HTTP-write action: no `kind`, carries a path.
    state["pocket"] = _job_action_spec({"path": "/things", "method": "POST"})

    res = await client.post("/pockets/pkt-router-1/actions/run", json={"action": "act1"})
    # The legacy path was taken: it reached the backend-config gate (400
    # pocket_backend.not_configured), NOT the job branch.
    assert res.status_code == 400, res.text
    assert res.json()["error"]["code"] == "pocket_backend.not_configured"
    # Crucially — no job was enqueued and no job_enqueued response was produced.
    assert fake_pool.calls == []


# ---------------------------------------------------------------------------
# CHANGE 2 — score_applications is a REAL data-backed job. With a
# `source_collection` it reads LIVE records from that Mongo collection in the
# cloud DB, scores the NEW ones with the real heuristic, SKIPS records already
# in the pocket's `scored_rows` (idempotent batch advancement), accumulates the
# rows so the canvas grows by a batch per run, and strips PII before broadcast.
# Seeds the source via the `mongo_db` fixture handle (Beanie is initialized
# against it, so the built-in's shared DB handle resolves to the same database).
# ---------------------------------------------------------------------------


async def _seed_source(
    mongo_db: Any, collection: str, records: list[dict], *, workspace: str = WS
) -> None:
    """Insert raw source records into a Mongo collection in the test DB.

    Each record is stamped with a ``workspace`` field (default :data:`WS`) so the
    tenancy-scoped read matches it — ``read_source_records`` now filters on
    ``{"workspace": workspace_id}``, so an untagged record would (correctly) read
    as nothing. A record that already carries its own ``workspace`` keeps it,
    which lets a test seed MULTIPLE tenants into one collection (the cross-tenant
    isolation test seeds both :data:`WS` and :data:`OTHER_WS` rows).
    """
    for rec in records:
        await mongo_db[collection].insert_one({"workspace": workspace, **rec})


async def _seed_pocket_with_scored_rows(*, workspace: str, scored_rows: list[dict]) -> str:
    """Insert a pocket whose rippleSpec.state.scored_rows is pre-populated, so
    the built-in's idempotent dedup has an existing batch to skip against."""
    from pocketpaw_ee.cloud.models.pocket import Pocket as _PocketDoc

    doc = _PocketDoc(
        workspace=workspace,
        name="score-apps-pocket",
        owner="owner-1",
        rippleSpec={"version": "1.0", "state": {"scored_rows": list(scored_rows)}},
    )
    await doc.insert()
    return str(doc.id)


@pytest.mark.asyncio
async def test_score_applications_source_backed_scores_new_records(
    mongo_db: Any,
) -> None:
    """A `source_collection`-backed run reads live records and scores the next
    `batch_size`, emitting a real heuristic score + band + stage per row."""
    from pocketpaw_ee.cloud.jobs.builtin.score_applications import ScoreApplicationsJob

    await _seed_source(
        mongo_db,
        "applications",
        [
            {
                "id": "app-1",
                "name": "Jordan Strong",
                "email": "jordan@acme.com",
                "message": "x" * 150,
                "referral": "linkedin.com/in/jordan",
            },
            {"id": "app-2", "name": "Sam Sparse"},
            {
                "id": "app-3",
                "name": "Casey Throwaway",
                "email": "casey@mailinator.com",
            },
        ],
    )
    pocket_id = await _seed_pocket_with_scored_rows(workspace=WS, scored_rows=[])

    job = ScoreApplicationsJob()
    result = await job(
        workspace_id=WS,
        pocket_id=pocket_id,
        job_id="j-src-1",
        params={"source_collection": "applications", "batch_size": 20},
    )

    rows = result["state"]["scored_rows"]
    assert {r["id"] for r in rows} == {"app-1", "app-2", "app-3"}
    assert result["state"]["score_applications_scored_count"] == 3
    by_id = {r["id"]: r for r in rows}
    # The complete, referral-bearing record outscores the disposable-email one.
    assert by_id["app-1"]["band"] == "Strong"
    assert by_id["app-3"]["score"] < by_id["app-1"]["score"]
    assert all(r["stage"] == "scored" for r in rows)
    # No status key from the built-in (the worker owns it).
    assert "score_applications_status" not in result["state"]


@pytest.mark.asyncio
async def test_score_applications_source_backed_is_idempotent(
    mongo_db: Any,
) -> None:
    """Re-running SKIPS records already in the pocket's `scored_rows` and pulls
    the NEXT batch, accumulating onto the existing rows (the canvas grows by a
    batch per run rather than re-scoring the same records)."""
    from pocketpaw_ee.cloud.jobs.builtin.score_applications import ScoreApplicationsJob

    await _seed_source(
        mongo_db,
        "applications",
        [
            {"id": "app-1", "name": "One", "email": "one@acme.com"},
            {"id": "app-2", "name": "Two", "email": "two@acme.com"},
            {"id": "app-3", "name": "Three", "email": "three@acme.com"},
        ],
    )
    # app-1 is already scored on the pocket.
    pocket_id = await _seed_pocket_with_scored_rows(
        workspace=WS,
        scored_rows=[
            {"id": "app-1", "name": "One", "score": 40, "band": "Moderate", "stage": "scored"}
        ],
    )

    job = ScoreApplicationsJob()
    result = await job(
        workspace_id=WS,
        pocket_id=pocket_id,
        job_id="j-src-2",
        params={"source_collection": "applications", "batch_size": 1},
    )

    rows = result["state"]["scored_rows"]
    ids = [r["id"] for r in rows]
    # app-1 (already scored) is preserved untouched; exactly ONE new record was
    # added (batch_size=1) — and it is NOT app-1.
    assert ids[0] == "app-1"
    assert len(rows) == 2
    new_id = ids[1]
    assert new_id in ("app-2", "app-3")
    assert new_id != "app-1"
    # The preserved row keeps its original score (not re-scored).
    assert rows[0]["score"] == 40
    assert result["state"]["score_applications_scored_count"] == 2


@pytest.mark.asyncio
async def test_score_applications_source_backed_strips_pii(
    mongo_db: Any,
) -> None:
    """The broadcast rows from a source-backed run carry ONLY the allowlist —
    no email / phone / raw source field reaches `state`."""
    from pocketpaw_ee.cloud.jobs.builtin.score_applications import ScoreApplicationsJob

    await _seed_source(
        mongo_db,
        "applications",
        [
            {
                "id": "app-1",
                "name": "Jordan",
                "email": "jordan@acme.com",
                "phone": "+1-555-0100",
                "ssn": "000-00-0000",
                "message": "Hello there, here is my note.",
            }
        ],
    )
    pocket_id = await _seed_pocket_with_scored_rows(workspace=WS, scored_rows=[])

    job = ScoreApplicationsJob()
    result = await job(
        workspace_id=WS,
        pocket_id=pocket_id,
        job_id="j-src-3",
        params={"source_collection": "applications", "batch_size": 20},
    )

    rows = result["state"]["scored_rows"]
    assert rows
    row = rows[0]
    assert "email" not in row
    assert "phone" not in row
    assert "ssn" not in row
    assert "message" not in row
    assert set(row.keys()) <= {"id", "name", "score", "band", "stage"}


@pytest.mark.asyncio
async def test_score_applications_missing_source_collection_returns_empty(
    mongo_db: Any,
) -> None:
    """A source collection that doesn't exist reads as 'no records' (the read
    is best-effort) — the job returns the existing rows unchanged, not a crash."""
    from pocketpaw_ee.cloud.jobs.builtin.score_applications import ScoreApplicationsJob

    pocket_id = await _seed_pocket_with_scored_rows(
        workspace=WS,
        scored_rows=[
            {"id": "app-1", "name": "One", "score": 40, "band": "Moderate", "stage": "scored"}
        ],
    )

    job = ScoreApplicationsJob()
    result = await job(
        workspace_id=WS,
        pocket_id=pocket_id,
        job_id="j-src-4",
        params={"source_collection": "does_not_exist", "batch_size": 20},
    )

    rows = result["state"]["scored_rows"]
    # Existing batch preserved; nothing new added.
    assert [r["id"] for r in rows] == ["app-1"]


# ---------------------------------------------------------------------------
# SECURITY (fix/jobs-source-collection-tenancy) — the source-collection read is
# TENANCY-SCOPED and DENYLISTED. Before the fix, `read_source_records` did an
# unscoped `db[name].find({})` over an AUTHOR-controlled collection name in the
# SHARED cloud DB, so a pocket author could set `source_collection: "users"` (or
# any collection) and pull EVERY tenant's rows — a cross-tenant leak. The fix
# scopes the read to the job's own workspace AND denylists credential/identity/
# system collections outright. These two tests FAIL on the pre-fix code (the
# unscoped read pulls the other tenant's rows / reads the denylisted collection)
# and PASS after the fix.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_score_applications_source_backed_is_workspace_scoped(
    mongo_db: Any,
) -> None:
    """A source-backed run reads ONLY records tagged with the job's own
    workspace — never another tenant's rows.

    Seeds ONE collection with records for TWO tenants (:data:`WS` and
    :data:`OTHER_WS`) and runs `score_applications` for :data:`WS`. The scored
    rows come only from the WS records; the OTHER_WS rows are never read. FAILS
    on the pre-fix code, whose unscoped `find({})` pulled every tenant's rows.
    """
    from pocketpaw_ee.cloud.jobs.builtin.score_applications import ScoreApplicationsJob

    await _seed_source(
        mongo_db,
        "applications",
        [
            {"id": "ws-a-1", "name": "Alice A", "email": "alice@acme.com"},
            {"id": "ws-a-2", "name": "Aaron A", "email": "aaron@acme.com"},
        ],
        workspace=WS,
    )
    await _seed_source(
        mongo_db,
        "applications",
        [
            {"id": "ws-b-1", "name": "Bianca B", "email": "bianca@other.com"},
            {"id": "ws-b-2", "name": "Ben B", "email": "ben@other.com"},
        ],
        workspace=OTHER_WS,
    )
    pocket_id = await _seed_pocket_with_scored_rows(workspace=WS, scored_rows=[])

    job = ScoreApplicationsJob()
    result = await job(
        workspace_id=WS,
        pocket_id=pocket_id,
        job_id="j-tenant-1",
        params={"source_collection": "applications", "batch_size": 10},
    )

    ids = {r["id"] for r in result["state"]["scored_rows"]}
    assert ids == {"ws-a-1", "ws-a-2"}
    assert not any(str(i).startswith("ws-b") for i in ids)


@pytest.mark.asyncio
async def test_score_applications_denylisted_collection_reads_nothing(
    mongo_db: Any,
) -> None:
    """A denylisted collection (credential/identity/system) is NEVER read, even
    for the job's own workspace.

    Seeds `workspace_connectors` rows tagged with the job's own :data:`WS` and
    points the job at it — nothing is scored (the denylist returns `[]` before
    any read). FAILS on the pre-fix code, which had no denylist and scored the
    connector rows (whose `config` holds secrets).
    """
    from pocketpaw_ee.cloud.jobs.builtin.score_applications import ScoreApplicationsJob

    await _seed_source(
        mongo_db,
        "workspace_connectors",
        [
            {"id": "conn-1", "name": "github", "config": {"token": "ghp_secret"}},
            {"id": "conn-2", "name": "slack", "config": {"token": "xoxb-secret"}},
        ],
        workspace=WS,
    )
    pocket_id = await _seed_pocket_with_scored_rows(workspace=WS, scored_rows=[])

    job = ScoreApplicationsJob()
    result = await job(
        workspace_id=WS,
        pocket_id=pocket_id,
        job_id="j-deny-1",
        params={"source_collection": "workspace_connectors", "batch_size": 10},
    )

    assert result["state"]["scored_rows"] == []
    assert result["state"]["score_applications_scored_count"] == 0


# ---------------------------------------------------------------------------
# CHANGE 2 (this PR) — score_applications CONNECTOR source mode. When `params`
# carries `connector` + `action`, the job reads its batch from the workspace's
# BOUND connector via `jobs.service.execute_connector_action` (which calls
# `connectors_service.execute`) and scores those records — same idempotent
# dedup, same heuristic + band, same PII allowlist as the Mongo path. Source
# precedence is connector > source_collection > rows. The connector call is
# mocked by monkeypatching `connectors_service.execute` to return sample
# records, so nothing real is dispatched.
# ---------------------------------------------------------------------------


def _connector_response(records: Any) -> Any:
    """Build a real ``ExecuteActionResponse`` (success) carrying ``records`` as
    the data payload — the shape ``execute_connector_action`` unwraps."""
    from pocketpaw_ee.cloud.connectors.dto import ExecuteActionResponse

    return ExecuteActionResponse(success=True, data=records, execution_mode="cloud")


def _patch_connector_execute(monkeypatch: pytest.MonkeyPatch, records: Any):
    """Monkeypatch ``connectors_service.execute`` to return ``records`` and
    capture the call args. ``execute_connector_action`` lazy-imports the
    connectors service inside the function, so patching the real module object
    is what the lazy import resolves to.

    Returns the ``captured`` dict so a test can assert the call ran under the
    workspace job identity with ``pocket_id=None``.
    """
    from pocketpaw_ee.cloud.connectors import service as connectors_service

    captured: dict[str, Any] = {}

    async def _fake_execute(workspace_id, name, body, *, user_id=None):  # type: ignore[no-untyped-def]
        captured["workspace_id"] = workspace_id
        captured["name"] = name
        captured["body"] = body
        captured["user_id"] = user_id
        return _connector_response(records)

    monkeypatch.setattr(connectors_service, "execute", _fake_execute)
    return captured


@pytest.mark.asyncio
async def test_score_applications_connector_backed_scores_records(
    mongo_db: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A connector-backed run scores the records the mocked
    ``connectors_service.execute`` returns, and the connector call runs under
    the workspace job identity with workspace-scope credentials
    (``pocket_id=None``)."""
    from pocketpaw_ee.cloud.jobs.builtin.score_applications import ScoreApplicationsJob

    captured = _patch_connector_execute(
        monkeypatch,
        [
            {
                "id": "lead-1",
                "name": "Jordan Strong",
                "email": "jordan@acme.com",
                "message": "x" * 150,
                "referral": "linkedin.com/in/jordan",
            },
            {"id": "lead-2", "name": "Sam Sparse"},
            {"id": "lead-3", "name": "Casey Throwaway", "email": "casey@mailinator.com"},
        ],
    )
    pocket_id = await _seed_pocket_with_scored_rows(workspace=WS, scored_rows=[])

    job = ScoreApplicationsJob()
    result = await job(
        workspace_id=WS,
        pocket_id=pocket_id,
        job_id="j-conn-1",
        params={"connector": "snctm-api", "action": "list_leads", "batch_size": 20},
    )

    rows = result["state"]["scored_rows"]
    assert {r["id"] for r in rows} == {"lead-1", "lead-2", "lead-3"}
    assert result["state"]["score_applications_scored_count"] == 3
    by_id = {r["id"]: r for r in rows}
    assert by_id["lead-1"]["band"] == "Strong"
    assert by_id["lead-3"]["score"] < by_id["lead-1"]["score"]
    assert all(r["stage"] == "scored" for r in rows)
    # No status key from the built-in (the worker owns it).
    assert "score_applications_status" not in result["state"]

    # The connector call ran under the synthetic job identity with
    # workspace-scope creds (no pocket binding, no params-borne tokens).
    assert captured["name"] == "snctm-api"
    assert captured["user_id"] == WORKSPACE_JOB_IDENTITY
    assert captured["body"].action == "list_leads"
    assert captured["body"].pocket_id is None


@pytest.mark.asyncio
async def test_score_applications_connector_backed_is_idempotent(
    mongo_db: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Re-running a connector-backed job SKIPS records already in the pocket's
    ``scored_rows`` and pulls the next ``batch_size``, accumulating onto the
    existing rows (idempotent batch advancement)."""
    from pocketpaw_ee.cloud.jobs.builtin.score_applications import ScoreApplicationsJob

    _patch_connector_execute(
        monkeypatch,
        [
            {"id": "lead-1", "name": "One", "email": "one@acme.com"},
            {"id": "lead-2", "name": "Two", "email": "two@acme.com"},
            {"id": "lead-3", "name": "Three", "email": "three@acme.com"},
        ],
    )
    # lead-1 is already scored on the pocket.
    pocket_id = await _seed_pocket_with_scored_rows(
        workspace=WS,
        scored_rows=[
            {"id": "lead-1", "name": "One", "score": 40, "band": "Moderate", "stage": "scored"}
        ],
    )

    job = ScoreApplicationsJob()
    result = await job(
        workspace_id=WS,
        pocket_id=pocket_id,
        job_id="j-conn-2",
        params={"connector": "snctm-api", "action": "list_leads", "batch_size": 1},
    )

    rows = result["state"]["scored_rows"]
    ids = [r["id"] for r in rows]
    assert ids[0] == "lead-1"
    assert len(rows) == 2  # one already-scored + exactly one NEW (batch_size=1)
    new_id = ids[1]
    assert new_id in ("lead-2", "lead-3")
    assert new_id != "lead-1"
    # The preserved row keeps its original score (not re-scored).
    assert rows[0]["score"] == 40
    assert result["state"]["score_applications_scored_count"] == 2


@pytest.mark.asyncio
async def test_score_applications_connector_backed_strips_pii(
    mongo_db: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The broadcast rows from a connector-backed run carry ONLY the allowlist —
    no email / phone / raw connector field reaches ``state``."""
    from pocketpaw_ee.cloud.jobs.builtin.score_applications import ScoreApplicationsJob

    _patch_connector_execute(
        monkeypatch,
        [
            {
                "id": "lead-1",
                "name": "Jordan",
                "email": "jordan@acme.com",
                "phone": "+1-555-0100",
                "ssn": "000-00-0000",
                "message": "Hello there, here is my note.",
            }
        ],
    )
    pocket_id = await _seed_pocket_with_scored_rows(workspace=WS, scored_rows=[])

    job = ScoreApplicationsJob()
    result = await job(
        workspace_id=WS,
        pocket_id=pocket_id,
        job_id="j-conn-3",
        params={"connector": "snctm-api", "action": "list_leads", "batch_size": 20},
    )

    rows = result["state"]["scored_rows"]
    assert rows
    row = rows[0]
    assert "email" not in row
    assert "phone" not in row
    assert "ssn" not in row
    assert "message" not in row
    assert set(row.keys()) <= {"id", "name", "score", "band", "stage"}


@pytest.mark.asyncio
async def test_score_applications_connector_precedence_over_collection(
    mongo_db: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Source precedence: when BOTH `connector` and `source_collection` are set,
    the connector wins — the Mongo collection is never read."""
    from pocketpaw_ee.cloud.jobs.builtin.score_applications import ScoreApplicationsJob

    _patch_connector_execute(monkeypatch, [{"id": "from-connector", "name": "Via Connector"}])
    # Seed a Mongo collection with a DIFFERENT id; if precedence were wrong the
    # collection's record would show up in the scored rows.
    await _seed_source(mongo_db, "applications", [{"id": "from-collection", "name": "Via Mongo"}])
    pocket_id = await _seed_pocket_with_scored_rows(workspace=WS, scored_rows=[])

    job = ScoreApplicationsJob()
    result = await job(
        workspace_id=WS,
        pocket_id=pocket_id,
        job_id="j-conn-4",
        params={
            "connector": "snctm-api",
            "action": "list_leads",
            "source_collection": "applications",
            "batch_size": 20,
        },
    )

    ids = {r["id"] for r in result["state"]["scored_rows"]}
    assert ids == {"from-connector"}
    assert "from-collection" not in ids


@pytest.mark.asyncio
async def test_score_applications_connector_error_propagates(
    mongo_db: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-success connector response raises ``CloudError(job.connector_error)``
    out of the job, so the worker's failure path marks the job failed + un-hangs
    the button. (The built-in does not swallow connector failures the way the
    best-effort Mongo read degrades to 'no records'.)"""
    from pocketpaw_ee.cloud._core.errors import CloudError
    from pocketpaw_ee.cloud.connectors import service as connectors_service
    from pocketpaw_ee.cloud.connectors.dto import ExecuteActionResponse
    from pocketpaw_ee.cloud.jobs.builtin.score_applications import ScoreApplicationsJob

    async def _failing_execute(workspace_id, name, body, *, user_id=None):  # type: ignore[no-untyped-def]
        return ExecuteActionResponse(success=False, data=None, error="upstream 500")

    monkeypatch.setattr(connectors_service, "execute", _failing_execute)
    pocket_id = await _seed_pocket_with_scored_rows(workspace=WS, scored_rows=[])

    job = ScoreApplicationsJob()
    with pytest.raises(CloudError) as exc:
        await job(
            workspace_id=WS,
            pocket_id=pocket_id,
            job_id="j-conn-5",
            params={"connector": "snctm-api", "action": "list_leads", "batch_size": 20},
        )
    assert exc.value.code == "job.connector_error"
