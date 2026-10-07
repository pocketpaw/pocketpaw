# Tests for Daytona capacity handling in the site-build lanes: the narrow limit-error
# classifier, the jittered retry schedule, each lane's wait / give-up / superseded
# behaviour (publish, preview, html verify, project), the honest copy, and the sandbox
# lifetime every Daytona create caller passes.
from __future__ import annotations

import random
from types import SimpleNamespace
from typing import Any

import pytest
from arq.jobs import JobStatus
from arq.worker import Retry
from daytona import DaytonaRateLimitError, DaytonaValidationError
from pocketpaw_ee.cloud.models.site import Site
from pocketpaw_ee.sites import build_job as bj
from pocketpaw_ee.sites import capacity, project_build, verify

from tests.ee.sites.faults import DaytonaUnavailable, FaultyDaytonaClient
from tests.ee.sites.test_build_job import FakeRunner, _input, _insert_site, _reread
from tests.ee.sites.test_preview_build_lane import (
    MemoryArtifactStore,
    _preview_input,
    _sandbox,
)

PROD_MESSAGE = "Failed to create sandbox: Total memory limit exceeded. Maximum allowed: 10GiB."


def _capacity_error() -> DaytonaValidationError:
    return DaytonaValidationError(PROD_MESSAGE)


class _Records:
    def __init__(self) -> None:
        self.data: dict[tuple[str, str], dict] = {}

    def read(self, pocket_id: str, key: str) -> dict | None:
        return self.data.get((pocket_id, key))

    def write(self, pocket_id: str, key: str, record: dict) -> None:
        self.data[(pocket_id, key)] = dict(record)


class _Redis:
    """The bit of arq's ctx["redis"] the supersede check reads."""

    def __init__(self, current: str | None) -> None:
        self.current = current

    async def get(self, key: str) -> Any:
        return self.current.encode() if self.current else None


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


class TestClassification:
    @pytest.mark.parametrize(
        "message",
        [
            PROD_MESSAGE,
            "Total CPU limit exceeded. Maximum allowed: 4",
            "Total disk limit exceeded. Maximum allowed: 30GiB",
            "Concurrent sandbox limit exceeded",
        ],
    )
    def test_org_limit_errors_are_capacity(self, message: str) -> None:
        assert capacity.is_capacity_error(DaytonaValidationError(message))

    def test_a_wrapped_limit_error_is_still_capacity(self) -> None:
        try:
            try:
                raise _capacity_error()
            except DaytonaValidationError as inner:
                raise RuntimeError("create failed") from inner
        except RuntimeError as outer:
            assert capacity.is_capacity_error(outer)

    @pytest.mark.parametrize(
        "exc",
        [
            DaytonaValidationError("Invalid image name"),
            DaytonaValidationError("cpu must be a positive integer"),
            RuntimeError(PROD_MESSAGE),  # right words, wrong type
            DaytonaRateLimitError("Total memory limit exceeded"),  # a different error class
            DaytonaUnavailable(status=503),
            RuntimeError("Daytona is not configured (DAYTONA_API_URL / DAYTONA_API_KEY unset)"),
        ],
    )
    def test_everything_else_is_not(self, exc: BaseException) -> None:
        assert not capacity.is_capacity_error(exc)

    def test_none_is_not(self) -> None:
        assert not capacity.is_capacity_error(None)


class TestRetrySchedule:
    def test_one_delay_per_try_then_none(self) -> None:
        rng = random.Random(1)
        delays = [capacity.next_retry_delay({"job_try": n}, rng=rng) for n in range(1, 9)]
        assert all(d is not None for d in delays[: len(capacity.RETRY_DELAYS_SECONDS)])
        assert delays[len(capacity.RETRY_DELAYS_SECONDS) :] == [None, None]
        assert capacity.CAPACITY_MAX_TRIES == len(capacity.RETRY_DELAYS_SECONDS) + 1

    def test_delays_are_jittered_within_a_quarter(self) -> None:
        for n, base in enumerate(capacity.RETRY_DELAYS_SECONDS, start=1):
            for seed in range(20):
                d = capacity.next_retry_delay({"job_try": n}, rng=random.Random(seed))
                assert base * 0.75 <= d <= base * 1.25

    def test_the_whole_budget_is_about_five_minutes(self) -> None:
        total = sum(capacity.RETRY_DELAYS_SECONDS)
        assert 200 <= total * 0.75 and total * 1.25 <= 400

    @pytest.mark.parametrize("ctx", [{}, None, {"job_try": 0}, {"job_try": "1"}])
    def test_no_arq_try_means_no_retry(self, ctx: Any) -> None:
        assert capacity.next_retry_delay(ctx) is None


class TestCopy:
    def test_waiting(self) -> None:
        assert capacity.reason_message("waiting_for_capacity") == capacity.CAPACITY_WAITING_MESSAGE
        assert "queued" in capacity.CAPACITY_WAITING_MESSAGE

    def test_exhausted(self) -> None:
        msg = capacity.reason_message("sandbox_unavailable:capacity")
        assert msg == capacity.CAPACITY_EXHAUSTED_MESSAGE
        assert "try again in a few minutes" in msg

    @pytest.mark.parametrize(
        "reason",
        ["sandbox_unavailable", "sandbox_unavailable:no_sandbox", "sandbox_unavailable:job_raised"],
    )
    def test_no_sandbox_keeps_its_own_message(self, reason: str) -> None:
        assert capacity.reason_message(reason) == capacity.NO_SANDBOX_MESSAGE

    @pytest.mark.parametrize("reason", [None, "", "timeout", "build_failed:install_failed"])
    def test_other_reasons_are_left_to_their_callers(self, reason: Any) -> None:
        assert capacity.reason_message(reason) is None

    def test_capacity_never_says_the_server_is_down(self) -> None:
        for msg in (capacity.CAPACITY_WAITING_MESSAGE, capacity.CAPACITY_EXHAUSTED_MESSAGE):
            assert "down" not in msg.lower()

    def test_the_verify_verdict_carries_the_sentence(self) -> None:
        def verdict(reason: str) -> dict:
            layer = {"name": "build", "status": "unverified", "reason": reason}
            return verify._verdict(content_hash="h", layers=[layer])

        assert verdict("sandbox_unavailable:capacity")["message"] == (
            capacity.CAPACITY_EXHAUSTED_MESSAGE
        )
        assert verdict("waiting_for_capacity")["message"] == capacity.CAPACITY_WAITING_MESSAGE
        assert verdict("sandbox_unavailable")["message"] == capacity.NO_SANDBOX_MESSAGE
        assert "message" not in verdict("timeout")


# ---------------------------------------------------------------------------
# Preview lane (react / svelte / ripple drafts)
# ---------------------------------------------------------------------------


async def _preview(ctx: dict, client: Any, records: _Records | None = None) -> dict:
    async def _no_harness(*_a: Any, **_k: Any) -> None:
        return None

    return await bj.run_site_preview_build(
        ctx,
        "pk1",
        "c0ffee",
        _preview_input(),
        "react",
        600,
        _runner=FakeRunner(),
        _client=client,
        _store=MemoryArtifactStore(),
        _verify_store=records if records is not None else _Records(),
        _harness=_no_harness,
    )


def _ctx(job_try: int, job_id: str = "site-preview-pk1-c0ffee", current: str | None = None):
    return {"job_try": job_try, "job_id": job_id, "redis": _Redis(current or job_id)}


class TestPreviewLane:
    async def test_capacity_retries_then_builds(self) -> None:
        client = _sandbox(fail_at="create", error=_capacity_error(), fail_times=1)
        with pytest.raises(Retry):
            await _preview(_ctx(1), client)
        out = await _preview(_ctx(2), client)
        assert out["status"] == "built"
        assert client.failures == {"create": 1}

    async def test_exhausted_budget_fails_with_the_capacity_reason(self) -> None:
        records = _Records()
        out = await _preview(_ctx(capacity.CAPACITY_MAX_TRIES), _sandbox(
            fail_at="create", error=_capacity_error()
        ), records)  # fmt: skip
        assert out["status"] == "failed"
        assert out["reason"] == "sandbox_unavailable:capacity"
        assert out["layers"]["build"]["reason"] == "sandbox_unavailable:capacity"
        # Not cached: the next verify of the same source must try again.
        assert records.data == {}

    async def test_a_superseded_job_stops_retrying(self) -> None:
        client = _sandbox(fail_at="create", error=_capacity_error())
        out = await _preview(_ctx(1, current="site-preview-pk1-newer"), client)
        assert out["reason"] == bj.SUPERSEDED_REASON

    async def test_a_non_capacity_failure_still_raises_without_retry(self) -> None:
        client = _sandbox(fail_at="create", error=DaytonaUnavailable(status=503))
        with pytest.raises(DaytonaUnavailable):
            await _preview(_ctx(1), client)

    async def test_a_waiting_job_reads_as_queued_for_capacity(self, monkeypatch) -> None:
        class _Job:
            def __init__(self, *_a: Any, **_k: Any) -> None:
                pass

            async def status(self) -> JobStatus:
                return JobStatus.deferred

        monkeypatch.setattr(bj, "Job", _Job)
        assert await bj._preview_job_outcome(object(), "j") == ("queued", "waiting_for_capacity")

    async def test_verify_timeout_on_a_waiting_job_says_waiting(self, monkeypatch) -> None:
        import arq.jobs

        class _Job:
            def __init__(self, *_a: Any, **_k: Any) -> None:
                pass

            async def status(self) -> JobStatus:
                return JobStatus.deferred

        monkeypatch.setattr(arq.jobs, "Job", _Job)
        assert await verify._timeout_reason(object(), "j") == "waiting_for_capacity"


class TestHtmlVerifyLane:
    async def test_capacity_retries_and_then_gives_up(self) -> None:
        async def _full(*_a: Any, **_k: Any) -> Any:
            raise _capacity_error()

        async def _run(job_try: int) -> dict:
            return await bj.run_site_html_verify(
                _ctx(job_try, job_id="site-verify-html-pk1-h"),
                "pk1",
                "h",
                {"engine": "html"},
                _runner=FakeRunner({"index.html": b"<h1>x</h1>"}),
                _verify_store=_Records(),
                _harness=_full,
            )

        with pytest.raises(Retry):
            await _run(1)
        out = await _run(capacity.CAPACITY_MAX_TRIES)
        assert out["reason"] == "sandbox_unavailable:capacity"
        assert out["layers"]["build"]["status"] == "skipped"


# ---------------------------------------------------------------------------
# Project lane (where prod reported ``no_sandbox``)
# ---------------------------------------------------------------------------

GEN = [(b"{}", "/tmp/paw-sites-gen/package.json")]
SOURCE = {"package.json": '{"name":"p"}', "src/index.ts": "export {}"}


class _RaisingRunner:
    def __init__(self, exc: BaseException) -> None:
        self.exc = exc
        self.calls = 0

    async def __call__(self, files: Any, **kw: Any) -> Any:
        self.calls += 1
        raise self.exc


async def _project(ctx: dict, runner: Any, records: _Records) -> dict:
    return await project_build.run_project_preview_build(
        ctx, "pk1", "h1", {"source": SOURCE}, 600,
        _runner=runner, _store=MemoryArtifactStore(), _verify_store=records, _gen_uploads=GEN,
    )  # fmt: skip


def _project_record(records: _Records) -> dict | None:
    job_id = bj._preview_job_id("pk1", "h1")
    return records.read("pk1", project_build.build_record_key(job_id))


class TestProjectLane:
    JOB = "site-preview-pk1-h1"

    async def test_capacity_keeps_the_build_queued_and_retries(self) -> None:
        records = _Records()
        with pytest.raises(Retry):
            await _project(_ctx(1, job_id=self.JOB), _RaisingRunner(_capacity_error()), records)
        record = _project_record(records)
        assert (record["status"], record["reason"]) == ("queued", "waiting_for_capacity")

    async def test_exhausted_budget_records_capacity_not_no_sandbox(self) -> None:
        records = _Records()
        out = await _project(
            _ctx(capacity.CAPACITY_MAX_TRIES, job_id=self.JOB),
            _RaisingRunner(_capacity_error()),
            records,
        )
        assert out["reason"] == "sandbox_unavailable:capacity"
        assert _project_record(records)["status"] == "failed"
        assert _project_record(records)["reason"] == "sandbox_unavailable:capacity"

    async def test_a_superseded_job_stops_and_records_nothing_new(self) -> None:
        records = _Records()
        out = await _project(
            _ctx(1, job_id=self.JOB, current="site-preview-pk1-newer"),
            _RaisingRunner(_capacity_error()),
            records,
        )
        assert out["reason"] == bj.SUPERSEDED_REASON
        assert _project_record(records)["status"] == "building"  # untouched since start

    async def test_unreachable_daytona_is_still_no_sandbox(self) -> None:
        records = _Records()
        with pytest.raises(DaytonaUnavailable):
            await _project(_ctx(1, job_id=self.JOB), _RaisingRunner(DaytonaUnavailable()), records)
        assert _project_record(records)["reason"] == "sandbox_unavailable:no_sandbox"


# ---------------------------------------------------------------------------
# Publish lane (Site row)
# ---------------------------------------------------------------------------


async def _publish(site: Site, ctx: dict, client: Any) -> None:
    await bj.run_site_build(
        ctx, "ws1", str(site.id), _input(), "react", 600, _runner=FakeRunner(), _client=client
    )


class TestPublishLane:
    async def test_capacity_waits_queued_then_builds(self, beanie_test_db) -> None:
        site = await _insert_site(build_job_id="job-1", build_status="queued")
        client = FaultyDaytonaClient(fail_at="create", error=_capacity_error(), fail_times=1)
        with pytest.raises(Retry):
            await _publish(site, {"job_try": 1, "job_id": "job-1"}, client)
        row = await _reread(site)
        assert (row.build_status, row.build_reason) == ("queued", "waiting_for_capacity")

        await _publish(site, {"job_try": 2, "job_id": "job-1"}, client)
        row = await _reread(site)
        assert row.build_status == "built"

    async def test_exhausted_budget_fails_with_the_capacity_reason(self, beanie_test_db) -> None:
        site = await _insert_site(build_job_id="job-1", build_status="queued")
        client = FaultyDaytonaClient(fail_at="create", error=_capacity_error())
        await _publish(site, {"job_try": capacity.CAPACITY_MAX_TRIES, "job_id": "job-1"}, client)
        row = await _reread(site)
        assert (row.build_status, row.build_reason) == ("failed", "sandbox_unavailable:capacity")

    async def test_a_newer_publish_supersedes_the_waiting_one(self, beanie_test_db) -> None:
        site = await _insert_site(build_job_id="job-2", build_status="queued")
        client = FaultyDaytonaClient(fail_at="create", error=_capacity_error())
        await _publish(site, {"job_try": 1, "job_id": "job-1"}, client)  # no Retry raised
        row = await _reread(site)
        assert row.build_reason != "waiting_for_capacity"
        assert row.build_job_id == "job-2"

    async def test_unreachable_daytona_still_fails_fast(self, beanie_test_db) -> None:
        site = await _insert_site(build_job_id="job-1", build_status="queued")
        client = FaultyDaytonaClient(fail_at="create", error=DaytonaUnavailable(status=503))
        with pytest.raises(DaytonaUnavailable):
            await _publish(site, {"job_try": 1, "job_id": "job-1"}, client)
        row = await _reread(site)
        assert row.build_reason == "sandbox_unavailable:run_build_raised"


# ---------------------------------------------------------------------------
# Sandbox lifetimes, per create_sandbox caller
# ---------------------------------------------------------------------------


class TestLifetimes:
    async def test_site_build_sandbox_self_deletes(self) -> None:
        from pocketpaw_ee.sites.daytona_runner import run_build

        client = FaultyDaytonaClient(fail_at="create")
        with pytest.raises(DaytonaUnavailable):
            await run_build(
                {"package.json": "{}"}, engine="react", timeout_seconds=600, client=client
            )
        kw = client.create_kwargs
        assert (kw["cpu"], kw["memory"], kw["disk"]) == (2, 4, 10)
        assert kw["auto_stop_interval"] == 20  # build budget (10 min) + 10
        assert kw["auto_delete_interval"] == 0

    async def test_html_verify_sandbox_self_deletes(self) -> None:
        from pocketpaw_ee.sites import browser_check

        client = FaultyDaytonaClient(fail_at="create")
        with pytest.raises(DaytonaUnavailable):
            await browser_check.run_standalone({"index.html": "x"}, client=client, harness={})
        kw = client.create_kwargs
        assert kw["auto_delete_interval"] == 0
        assert kw["auto_stop_interval"] <= 30

    def test_code_mode_sandbox_is_ephemeral(self) -> None:
        from pocketpaw_ee.cloud.websandbox import provision

        assert (
            provision._AUTO_STOP_MINUTES,
            provision._AUTO_ARCHIVE_MINUTES,
            provision._AUTO_DELETE_MINUTES,
        ) == (5, 5, 0)

    @pytest.mark.parametrize(
        ("config", "stop"),
        [
            ({}, 30),
            (None, 30),
            ({"auto_stop_interval": 1800}, 30),  # the new default, in seconds
            ({"auto_stop_interval": 3600}, 60),  # legacy rows: 60 min, not 60 hours
            ({"auto_stop_interval": 10}, 5),  # floor
            ({"auto_stop_interval": 10**7}, 24 * 60),  # ceiling
            ({"auto_stop_interval": "junk"}, 30),
        ],
    )
    def test_workspace_vm_stops_idle_and_is_archived_never_deleted(self, config, stop) -> None:
        from pocketpaw_ee.cloud.daytona.store import workspace_vm_lifecycle

        assert workspace_vm_lifecycle(config) == {
            "auto_stop_interval": stop,
            "auto_archive_interval": 24 * 60,
            "auto_delete_interval": -1,
        }

    async def test_workspace_vm_auto_provision_passes_the_lifecycle(self, monkeypatch) -> None:
        from pocketpaw_ee.cloud.daytona import router as router_mod
        from pocketpaw_ee.cloud.daytona import store

        created: dict[str, Any] = {}

        class _Client:
            async def create_sandbox(self, **kw: Any) -> Any:
                created.update(kw)
                return SimpleNamespace(id="sb1", name=kw["name"], state="creating")

        async def _none(_ws: str) -> None:
            return None

        async def _config(_ws: str) -> dict:
            return {"cpu": 2, "memory": 4, "disk": 10, "auto_stop_interval": 3600}

        async def _set(*_a: Any) -> None:
            return None

        async def _client() -> Any:
            return _Client()

        monkeypatch.setattr(store, "get_workspace_vm_sandbox_id", _none)
        monkeypatch.setattr(store, "get_workspace_vm_config", _config)
        monkeypatch.setattr(store, "set_workspace_vm", _set)
        monkeypatch.setattr(router_mod, "daytona_enabled", lambda: True)
        monkeypatch.setattr(router_mod, "_require_daytona", _client)
        monkeypatch.setattr(router_mod.asyncio, "create_task", lambda coro: coro.close())

        await router_mod.get_workspace_vm(workspace_id="ws1")
        assert created["auto_stop_interval"] == 60
        assert created["auto_archive_interval"] == 24 * 60
        assert created["auto_delete_interval"] == -1

    def test_the_client_default_is_minutes_not_hours(self) -> None:
        import inspect

        from pocketpaw_ee.cloud.daytona.client import DaytonaClient

        default = inspect.signature(DaytonaClient.create_sandbox).parameters["auto_stop_interval"]
        assert default.default == 30


class TestWorkspaceVmResumes:
    """The VM now auto-stops after 30 idle minutes, so the agent must wake it."""

    def _client(self, state: str) -> Any:
        calls: list[str] = []

        class _Client:
            async def get_sandbox_by_id(self, sandbox_id: str) -> Any:
                calls.append("get")
                return SimpleNamespace(state=state)

            async def start_sandbox(self, sandbox_id: str) -> None:
                calls.append("start")

            async def wait_for_sandbox(self, sandbox_id: str, **kw: Any) -> None:
                calls.append("wait")

        client = _Client()
        client.calls = calls  # type: ignore[attr-defined]
        return client

    @pytest.mark.parametrize("state", ["stopped", "archived"])
    async def test_a_sleeping_vm_is_started(self, state: str) -> None:
        from pocketpaw_ee.cloud.daytona import context

        context._last_seen_started.clear()
        client = self._client(state)
        await context._ensure_started(client, f"sb-{state}")
        assert client.calls == ["get", "start", "wait"]

    async def test_a_running_vm_is_left_alone_and_not_rechecked(self) -> None:
        from pocketpaw_ee.cloud.daytona import context

        context._last_seen_started.clear()
        client = self._client("started")
        await context._ensure_started(client, "sb-up")
        await context._ensure_started(client, "sb-up")
        assert client.calls == ["get"]

    async def test_an_old_vm_gets_the_new_lifecycle_once(self, monkeypatch: Any) -> None:
        from pocketpaw_ee.cloud.daytona import context, store

        applied: list[dict] = []

        class _Client:
            async def set_sandbox_lifecycle(self, sandbox_id: str, **kw: Any) -> None:
                applied.append(kw)

        async def _config(_ws: str) -> dict:
            return {"auto_stop_interval": 3600}  # seconds: one hour

        monkeypatch.setattr(store, "get_workspace_vm_config", _config)
        context._lifecycle_applied.clear()
        await context._ensure_lifecycle(_Client(), "sb-old", "ws1")
        await context._ensure_lifecycle(_Client(), "sb-old", "ws1")
        assert applied == [
            {"auto_stop_interval": 60, "auto_archive_interval": 1440, "auto_delete_interval": -1}
        ]
