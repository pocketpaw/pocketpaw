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

    # The ARQ pool was asked to enqueue exactly one job, on the jobs queue.
    assert len(fake_pool.calls) == 1
    (args, kwargs) = fake_pool.calls[0]
    assert args[0] == "execute_workspace_job"
    assert job_id in args
    assert kwargs.get("queue") == "paw:jobs"


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
# 4. Worker writeback calls merge_spec with the synthetic identity.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_worker_writeback_uses_system_identity(
    mongo_db: Any, fake_pool: _FakePool, monkeypatch: pytest.MonkeyPatch
) -> None:  # noqa: ARG001
    captured: dict[str, Any] = {}

    async def _fake_merge_spec(workspace_id, user_id, pocket_id, body):  # type: ignore[no-untyped-def]
        captured["workspace_id"] = workspace_id
        captured["user_id"] = user_id
        captured["pocket_id"] = pocket_id
        captured["body"] = body
        return {"ok": True}

    monkeypatch.setattr(jobs_worker, "merge_spec", _fake_merge_spec)

    result = await jobs_service.dispatch_job(
        workspace_id=WS,
        pocket_id=POCKET,
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
    assert captured["pocket_id"] == POCKET
    # Writeback is a partial-spec merge carrying only state.
    assert captured["body"]["merge"]["state"]["echo_value"] == "hello"

    doc = await jobs_service.get_job(WS, job_id)
    assert doc is not None
    assert doc.status == "done"


# ---------------------------------------------------------------------------
# 5. Failure path writes {state: {<action>_status: "failed", ...}}.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_worker_failure_writes_failed_state(
    mongo_db: Any, fake_pool: _FakePool, monkeypatch: pytest.MonkeyPatch
) -> None:  # noqa: ARG001
    captured: dict[str, Any] = {}

    async def _fake_merge_spec(workspace_id, user_id, pocket_id, body):  # type: ignore[no-untyped-def]
        captured["body"] = body
        return {"ok": True}

    monkeypatch.setattr(jobs_worker, "merge_spec", _fake_merge_spec)

    result = await jobs_service.dispatch_job(
        workspace_id=WS,
        pocket_id=POCKET,
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
    mongo_db: Any, fake_pool: _FakePool, monkeypatch: pytest.MonkeyPatch
) -> None:  # noqa: ARG001
    captured: dict[str, Any] = {}

    async def _fake_merge_spec(workspace_id, user_id, pocket_id, body):  # type: ignore[no-untyped-def]
        captured["body"] = body
        return {"ok": True}

    monkeypatch.setattr(jobs_worker, "merge_spec", _fake_merge_spec)

    result = await jobs_service.dispatch_job(
        workspace_id=WS,
        pocket_id=POCKET,
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

    # The pure projection drops any non-allowlisted field — notably PII.
    projected = project_row(
        {
            "id": "a1",
            "name": "Jordan",
            "score": 6,
            "stage": "scored",
            "email": "jordan@example.com",
            "phone": "+1-555-0100",
            "ssn": "000-00-0000",
        }
    )
    assert "email" not in projected
    assert "phone" not in projected
    assert "ssn" not in projected
    assert projected == {"id": "a1", "name": "Jordan", "score": 6, "stage": "scored"}

    # End-to-end through the job: a row carrying PII never reaches the result.
    job = ScoreApplicationsJob()
    result = await job(
        workspace_id=WS,
        pocket_id=POCKET,
        job_id="j1",
        params={"rows": [{"id": "a1", "name": "Jordan", "email": "jordan@example.com"}]},
    )
    rows = result["state"]["scored_rows"]
    assert rows and all("email" not in r and "phone" not in r for r in rows)
    assert result["state"]["score_applications_status"] == "done"


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
