# tests/ee/sites/test_admission_lease.py — the edges of ``pocketpaw_ee.sites.admission``
# that test_build_admission.py's three contract tests do not pin: lease accounting on
# every exit path (error, cancel, a timed-out wait), FIFO order, an oversized request,
# the Redis-down fallback, ``free_capacity``'s eviction rules, and the preview status a
# poller sees while a build waits for a lease.
#
# All process-local: CI has no Redis. The Lua script was exercised by hand against a
# local Redis 7 (grant two, queue the third, cancel and bounded-wait release the queue
# entry); the fake below only proves the Python around it falls back when Redis errors.
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from arq.jobs import JobStatus
from daytona import DaytonaValidationError
from pocketpaw_ee.sites import admission
from pocketpaw_ee.sites import build_job as bj

from tests.ee.sites.test_build_sandbox_teardown import (
    CAPACITY_MESSAGE,
    ORG_MEMORY_GIB,
    REACT_FILES,
    LedgerDaytona,
)


@pytest.fixture(autouse=True)
def _budget(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(admission.BUDGET_ENV, str(ORG_MEMORY_GIB))
    admission.use_redis(None)


async def _granted_now(gib: int) -> bool:
    """True when a ``gib`` lease is granted without waiting."""
    async with admission.memory_lease(gib, max_wait_seconds=0.05) as ok:
        return ok


class TestOffByDefault:
    @pytest.mark.parametrize("raw", [None, "", "  ", "lots", "0", "-4"])
    async def test_no_valid_budget_is_a_pass_through(self, monkeypatch, raw) -> None:
        if raw is None:
            monkeypatch.delenv(admission.BUDGET_ENV, raising=False)
        else:
            monkeypatch.setenv(admission.BUDGET_ENV, raw)
        calls: list[str] = []

        class _RecordingRedis:
            def __getattr__(self, name: str) -> Any:
                calls.append(name)
                raise AssertionError(f"admission off must not touch Redis ({name})")

        admission.use_redis(_RecordingRedis())
        inside = 0
        release = asyncio.Event()

        async def hold() -> None:
            nonlocal inside
            async with admission.memory_lease(ORG_MEMORY_GIB * 5, max_wait_seconds=5) as ok:
                assert ok is True
                inside += 1
                await release.wait()

        try:
            tasks = [asyncio.create_task(hold()) for _ in range(3)]
            await asyncio.sleep(0.01)
            assert inside == 3, "a pass-through lease must never wait"
            release.set()
            await asyncio.gather(*tasks)
        finally:
            admission.use_redis(None)
        assert calls == []


class TestCreateFailureLogging:
    async def test_a_capacity_refusal_logs_the_full_message_and_the_limit(self, caplog) -> None:
        from pocketpaw_ee.sites import daytona_runner as dr

        client = LedgerDaytona()
        client.add_box("paw-ws-busy0000000", memory=8)
        with caplog.at_level("WARNING", logger="pocketpaw_ee.sites.admission"):
            with pytest.raises(DaytonaValidationError):
                await dr.run_build(
                    REACT_FILES,
                    engine="react",
                    timeout_seconds=600,
                    client=client,
                    labels={"paw_pocket": "pk1", "paw_job": "job1"},
                )
        lines = [r.getMessage() for r in caplog.records if "failed" in r.getMessage()]
        assert len(lines) == 1, lines
        assert "limit=memory" in lines[0]
        assert CAPACITY_MESSAGE.format(cap=ORG_MEMORY_GIB) in lines[0]
        assert "pocket=pk1 job=job1" in lines[0]

    def test_secrets_are_redacted_and_the_limit_named(self) -> None:
        from pocketpaw_ee.sites import capacity

        exc = DaytonaValidationError(
            "Failed to create sandbox: Total disk limit exceeded. Maximum allowed: 30GiB. "
            "api_key=dtn_0123456789abcdef0123456789abcdef01 Authorization: Bearer abc.def"
        )
        limit, message = capacity.describe_refusal(exc)
        assert limit == "disk"
        assert "Total disk limit exceeded. Maximum allowed: 30GiB." in message
        assert "dtn_0123456789abcdef" not in message
        assert "abc.def" not in message
        assert capacity.describe_refusal(RuntimeError("boom")) == (None, "boom")


class TestLeaseAccounting:
    async def test_an_error_inside_the_lease_releases_it(self) -> None:
        with pytest.raises(RuntimeError):
            async with admission.memory_lease(ORG_MEMORY_GIB):
                raise RuntimeError("build blew up")
        assert await _granted_now(ORG_MEMORY_GIB)

    async def test_released_memory_is_reusable_beside_a_live_holder(self) -> None:
        async with admission.memory_lease(8):
            pass
        # Two 4 GiB leases fit a 10 GiB budget only if the released 8 GiB came back.
        async with admission.memory_lease(4, max_wait_seconds=0.05) as first:
            assert first is True
            assert await _granted_now(4)

    async def test_a_cancel_inside_the_lease_releases_it(self) -> None:
        inside = asyncio.Event()

        async def hold() -> None:
            async with admission.memory_lease(ORG_MEMORY_GIB):
                inside.set()
                await asyncio.Event().wait()

        task = asyncio.create_task(hold())
        await asyncio.wait_for(inside.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert await _granted_now(ORG_MEMORY_GIB)

    async def test_a_cancelled_waiter_neither_holds_nor_blocks_the_queue(self) -> None:
        release = asyncio.Event()

        async def hold(gib: int) -> None:
            async with admission.memory_lease(gib):
                await release.wait()

        holder = asyncio.create_task(hold(8))
        await asyncio.sleep(0)
        waiter = asyncio.create_task(hold(8))
        await asyncio.sleep(0.01)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        release.set()
        await holder
        assert await _granted_now(ORG_MEMORY_GIB)

    async def test_a_bounded_wait_proceeds_unleased_and_takes_nothing(self) -> None:
        release = asyncio.Event()

        async def hold() -> None:
            async with admission.memory_lease(8):
                await release.wait()

        holder = asyncio.create_task(hold())
        await asyncio.sleep(0)
        async with admission.memory_lease(4, max_wait_seconds=0.05) as ok:
            assert ok is False
        release.set()
        await holder
        assert await _granted_now(ORG_MEMORY_GIB)

    async def test_waiters_are_served_first_come_first_served(self) -> None:
        release = asyncio.Event()
        order: list[str] = []

        async def take(tag: str, gib: int, hold: bool = False) -> None:
            async with admission.memory_lease(gib):
                order.append(tag)
                if hold:
                    await release.wait()

        holder = asyncio.create_task(take("holder", 8, hold=True))
        await asyncio.sleep(0)
        big = asyncio.create_task(take("big", 8))
        await asyncio.sleep(0)
        small = asyncio.create_task(take("small", 2))
        await asyncio.sleep(0.02)
        # 2 GiB would fit beside the holder, but the 8 GiB request queued first.
        assert order == ["holder"]
        release.set()
        await asyncio.gather(holder, big, small)
        assert order == ["holder", "big", "small"]

    async def test_a_request_over_the_whole_budget_runs_alone(self) -> None:
        release = asyncio.Event()
        got: list[str] = []

        async def hold() -> None:
            async with admission.memory_lease(4):
                await release.wait()

        holder = asyncio.create_task(hold())
        await asyncio.sleep(0)

        async def huge() -> None:
            async with admission.memory_lease(ORG_MEMORY_GIB * 2):
                got.append("huge")

        task = asyncio.create_task(huge())
        await asyncio.sleep(0.02)
        assert got == []
        release.set()
        await asyncio.wait_for(asyncio.gather(holder, task), timeout=1)
        assert got == ["huge"]

    async def test_redis_errors_fall_back_to_the_local_budget(self) -> None:
        class _DownRedis:
            async def eval(self, *_a: Any) -> Any:
                raise ConnectionError("redis down")

            def pipeline(self, **_k: Any) -> Any:
                raise ConnectionError("redis down")

            async def delete(self, *_a: Any) -> Any:
                raise ConnectionError("redis down")

        admission.use_redis(_DownRedis())
        try:
            async with admission.memory_lease(8) as ok:
                assert ok is True
                assert not await _granted_now(4), "the fallback must still count the lease"
        finally:
            admission.use_redis(None)

    async def test_a_redis_error_mid_wait_does_not_restart_the_wait(self, monkeypatch) -> None:
        """The local fallback gets only what is left of the wait, so the total stays
        within the one ``MAX_WAIT_SECONDS`` the job timeout allows for."""
        import time

        monkeypatch.setattr(admission, "_POLL_SECONDS", 0.05)
        release = asyncio.Event()

        async def hold() -> None:
            async with admission.memory_lease(ORG_MEMORY_GIB):
                await release.wait()

        holder = asyncio.create_task(hold())
        await asyncio.sleep(0)
        t0 = time.monotonic()

        class _FlakyRedis:
            async def eval(self, *_a: Any) -> Any:
                if time.monotonic() - t0 > 0.8:
                    raise ConnectionError("redis went away")
                return 0

            async def set(self, *_a: Any, **_k: Any) -> Any:
                return True

            async def delete(self, *_a: Any) -> Any:
                return 1

            def pipeline(self, **_k: Any) -> Any:
                raise ConnectionError("redis went away")

        admission.use_redis(_FlakyRedis())
        try:
            async with admission.memory_lease(4, max_wait_seconds=1.0) as ok:
                elapsed = time.monotonic() - t0
        finally:
            admission.use_redis(None)
            release.set()
            await holder
        assert ok is False
        assert elapsed < 1.4, f"waited {elapsed:.2f}s against a 1.0s budget"


def _ledger_with_vms(now: datetime) -> LedgerDaytona:
    client = LedgerDaytona(memory_cap_gib=100)
    client.add_box("paw-ws-recent00000", last_activity_at=now - timedelta(minutes=40))
    client.add_box("paw-ws-oldest00000", last_activity_at=now - timedelta(hours=5))
    client.add_box("paw-ws-middle00000", last_activity_at=now - timedelta(hours=1))
    client.add_box("paw-ws-under300000", last_activity_at=now - timedelta(minutes=20))
    client.add_box("paw-ws-active00000", last_activity_at=now - timedelta(minutes=2))
    client.add_box("paw-ws-nosize00000", memory=0, last_activity_at=now - timedelta(days=2))
    client.add_box("paw-ws-asleep00000", state="stopped", last_activity_at=now - timedelta(days=3))
    client.add_box("paw-build-1", last_activity_at=now - timedelta(days=1))
    client.add_box("paw-verify-1", memory=2, last_activity_at=now - timedelta(days=1))
    client.add_box("websandbox-abc", memory=1, last_activity_at=now - timedelta(days=1))
    return client


def _ids_by_name(client: LedgerDaytona) -> dict[str, str]:
    return {b.name: b.id for b in client.boxes.values()}


@pytest.fixture
def _evict_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(admission.EVICT_ENV, "1")


@pytest.fixture(autouse=True)
def _no_own_signal(monkeypatch: pytest.MonkeyPatch) -> None:
    from pocketpaw_ee.cloud.daytona import context

    monkeypatch.setattr(context, "_last_seen_started", {})


class TestFreeCapacity:
    async def test_off_by_default_nothing_is_stopped(self, monkeypatch) -> None:
        monkeypatch.delenv(admission.EVICT_ENV, raising=False)
        now = datetime.now(UTC)
        client = _ledger_with_vms(now)

        assert await admission.free_capacity(client, need_gib=1000, now=now) == []
        assert client.stopped == []

    @pytest.mark.usefixtures("_evict_on")
    async def test_stops_least_recently_active_idle_vms_until_enough_is_freed(self) -> None:
        now = datetime.now(UTC)
        client = _ledger_with_vms(now)
        ids = _ids_by_name(client)

        stopped = await admission.free_capacity(client, need_gib=8, now=now)

        assert stopped == [ids["paw-ws-oldest00000"], ids["paw-ws-middle00000"]]
        assert client.stopped == stopped

    @pytest.mark.usefixtures("_evict_on")
    async def test_respects_the_auto_stop_floor_and_never_touches_other_boxes(self) -> None:
        now = datetime.now(UTC)
        client = _ledger_with_vms(now)
        ids = _ids_by_name(client)

        # A caller asking for a shorter threshold still gets the 30 min floor.
        stopped = await admission.free_capacity(client, need_gib=1000, idle_for_seconds=60, now=now)

        assert set(stopped) == {
            ids["paw-ws-oldest00000"],
            ids["paw-ws-middle00000"],
            ids["paw-ws-recent00000"],
        }
        assert client.deleted == []

    @pytest.mark.usefixtures("_evict_on")
    async def test_our_own_recent_use_keeps_a_vm_daytona_calls_idle(self) -> None:
        import time

        from pocketpaw_ee.cloud.daytona import context

        now = datetime.now(UTC)
        client = _ledger_with_vms(now)
        ids = _ids_by_name(client)
        context._last_seen_started[ids["paw-ws-oldest00000"]] = time.monotonic() - 60

        stopped = await admission.free_capacity(client, need_gib=4, now=now)

        assert stopped == [ids["paw-ws-middle00000"]]

    @pytest.mark.usefixtures("_evict_on")
    async def test_a_failed_stop_is_skipped_and_the_next_vm_tried(self) -> None:
        now = datetime.now(UTC)
        client = _ledger_with_vms(now)
        ids = _ids_by_name(client)
        real_stop = client.stop_sandbox

        async def flaky_stop(sandbox_id: str) -> None:
            if sandbox_id == ids["paw-ws-oldest00000"]:
                raise RuntimeError("daytona 500")
            await real_stop(sandbox_id)

        client.stop_sandbox = flaky_stop  # type: ignore[method-assign]
        stopped = await admission.free_capacity(client, need_gib=4, now=now)
        assert stopped == [ids["paw-ws-middle00000"]]


class TestRunBuildAdmission:
    async def test_a_create_error_releases_the_lease(self) -> None:
        from daytona import DaytonaTimeoutError
        from pocketpaw_ee.sites import daytona_runner as dr

        client = LedgerDaytona()
        client.fail_after_register = DaytonaTimeoutError("create wait timed out")
        with pytest.raises(DaytonaTimeoutError):
            await dr.run_build(REACT_FILES, engine="react", timeout_seconds=600, client=client)
        assert client.boxes == {}
        assert await _granted_now(ORG_MEMORY_GIB)

    async def test_a_cancel_inside_create_tears_down_and_releases(self) -> None:
        from pocketpaw_ee.sites import daytona_runner as dr

        client = LedgerDaytona()
        client.block_create = True
        task = asyncio.create_task(
            dr.run_build(REACT_FILES, engine="react", timeout_seconds=600, client=client)
        )
        await asyncio.wait_for(client.registered.wait(), timeout=2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert client.boxes == {}
        assert await _granted_now(ORG_MEMORY_GIB)

    async def test_a_verify_create_error_releases_the_lease(self) -> None:
        from daytona import DaytonaTimeoutError
        from pocketpaw_ee.sites import browser_check as bc

        from tests.ee.sites.test_build_sandbox_teardown import HARNESS, HTML_FILES

        client = LedgerDaytona()
        client.fail_after_register = DaytonaTimeoutError("create wait timed out")
        with pytest.raises(DaytonaTimeoutError):
            await bc.run_standalone(HTML_FILES, client=client, harness=HARNESS)
        assert client.boxes == {}
        assert await _granted_now(ORG_MEMORY_GIB)

    async def test_a_refusal_with_nothing_to_evict_raises_without_a_second_create(self) -> None:
        from pocketpaw_ee.sites import capacity
        from pocketpaw_ee.sites import daytona_runner as dr

        client = LedgerDaytona()
        client.add_box("paw-ws-busy0000000", memory=8)  # active: never evicted
        with pytest.raises(DaytonaValidationError) as caught:
            await dr.run_build(REACT_FILES, engine="react", timeout_seconds=600, client=client)
        assert capacity.is_capacity_error(caught.value)
        assert str(caught.value) == CAPACITY_MESSAGE.format(cap=ORG_MEMORY_GIB)
        assert len(client.create_kwargs) == 1
        assert client.stopped == []
        assert await _granted_now(ORG_MEMORY_GIB)


class TestWaitingStatus:
    async def test_a_running_job_waiting_for_a_lease_reads_queued(self, monkeypatch) -> None:
        class _Job:
            def __init__(self, *_a: Any, **_k: Any) -> None:
                pass

            async def status(self) -> JobStatus:
                return JobStatus.in_progress

        class _Pool:
            def __init__(self, waiting: set[str]) -> None:
                self.waiting = waiting

            async def exists(self, key: str) -> int:
                return int(key.rsplit(":", 1)[-1] in self.waiting)

        monkeypatch.setattr(bj, "Job", _Job)
        assert await bj._preview_job_outcome(_Pool({"j1"}), "j1") == (
            "queued",
            "waiting_for_capacity",
        )
        assert await bj._preview_job_outcome(_Pool(set()), "j1") == ("building", None)
