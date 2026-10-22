# tests/ee/sites/test_build_admission.py — reproduction tests for site builds
# overcommitting the Daytona org's memory cap.
#
# Hypothesis: the sites lane runs ``_sites_max_jobs()`` (4) builds at once, each asking
# Daytona for 4 GiB, against a 10 GiB org cap that idle workspace VMs (4 GiB each,
# never deleted, 30 min auto-stop) also draw from. Nothing on our side admits a build
# against that budget: every build just tries create and, on refusal, backs off and
# retries until the budget is spent, then reports "Build capacity is full right now;
# try again in a few minutes".
#
# Intended seam (does not exist yet): ``pocketpaw_ee.sites.admission``
#   * ``memory_lease(gib: int)`` — async context manager. Blocks (does not raise) until
#     ``gib`` fits in the sites memory budget, then holds it until exit. Budget comes
#     from ``PAW_SITES_DAYTONA_MEMORY_BUDGET_GIB`` (the org cap). ``run_build`` and
#     ``browser_check.run_standalone`` hold a lease for ``memory_gb`` from before create
#     until after delete. Cross-process (Redis-backed) in production; these tests use
#     one process, so a process-local fallback must satisfy them.
#   * ``free_capacity(client, *, need_gib: int, idle_for_seconds: int = 600)
#     -> list[str]`` — on a capacity refusal, stops started ``paw-ws-*`` VMs idle for at
#     least ``idle_for_seconds``, least-recently-active first, until ``need_gib`` is
#     free; returns the stopped ids. ``run_build`` calls it once on a refusal and
#     retries the create before raising.
#
# Uses ``LedgerDaytona`` from test_build_sandbox_teardown.py: a fake org with a real
# memory ledger that raises the production ``DaytonaValidationError`` on overcommit.
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from pocketpaw_ee.sites import capacity
from pocketpaw_ee.sites import daytona_runner as dr

from tests.ee.sites.test_build_sandbox_teardown import ORG_MEMORY_GIB, REACT_FILES, LedgerDaytona


@pytest.fixture(autouse=True)
def _memory_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PAW_SITES_DAYTONA_MEMORY_BUDGET_GIB", str(ORG_MEMORY_GIB))
    monkeypatch.delenv("POCKETPAW_SITES_ARQ_MAX_JOBS", raising=False)
    # build_worker builds its arq settings at import time; same pattern as
    # test_delete_job.py.
    monkeypatch.setenv("POCKETPAW_REDIS_URL", "redis://localhost:6379")


def _sites_max_jobs() -> int:
    from pocketpaw_ee.sites.build_worker import _sites_max_jobs as resolve

    return resolve()


async def _build(client: LedgerDaytona, n: int) -> dr.BuildRunResult:
    return await dr.run_build(
        REACT_FILES,
        engine="react",
        timeout_seconds=600,
        client=client,
        sandbox_name=f"paw-build-{n}",
    )


class TestAdmissionAgainstTheOrgCap:
    async def test_a_full_lane_of_builds_all_complete_under_a_10gib_cap(self) -> None:
        """Hypothesis: overcommit vs the org cap.

        ``_sites_max_jobs()`` concurrent builds x 4 GiB = 16 GiB against a 10 GiB cap.
        Today two creates succeed and the rest are refused with "Total memory limit
        exceeded", which the job turns into capacity retries and finally "Build
        capacity is full right now; try again in a few minutes". Intended: each build
        takes an ``admission.memory_lease(4)`` first, so the extra builds WAIT for a
        lease and every one completes without Daytona ever refusing a create.
        """
        jobs = _sites_max_jobs()
        assert jobs * 4 > ORG_MEMORY_GIB, "precondition: the lane can overcommit the cap"
        client = LedgerDaytona(step_delay=0.02)

        results = await asyncio.gather(
            *(_build(client, n) for n in range(jobs)), return_exceptions=True
        )

        refused = [
            r for r in results if isinstance(r, BaseException) and capacity.is_capacity_error(r)
        ]
        assert not refused, (
            f"{len(refused)}/{jobs} concurrent builds were refused for capacity "
            f"(peak {client.peak_memory_gib} GiB of {ORG_MEMORY_GIB})"
        )
        assert all(isinstance(r, dr.BuildRunResult) and r.ok for r in results), results
        assert client.capacity_refusals == 0
        assert client.peak_memory_gib <= ORG_MEMORY_GIB

    async def test_memory_lease_waits_instead_of_failing(self) -> None:
        """Hypothesis: overcommit vs the org cap.

        Contract of the intended seam: with a 10 GiB budget, two 4 GiB leases are
        granted at once, a third blocks until one is released, and none raises.
        Fails today with ImportError (``pocketpaw_ee.sites.admission`` absent).
        """
        from pocketpaw_ee.sites.admission import memory_lease

        held = asyncio.Event()
        release = asyncio.Event()
        third_in = asyncio.Event()

        async def holder() -> None:
            async with memory_lease(4):
                held.set()
                await release.wait()

        async def third() -> None:
            async with memory_lease(4):
                third_in.set()

        holders = [asyncio.create_task(holder()) for _ in range(2)]
        await asyncio.wait_for(held.wait(), timeout=1)
        await asyncio.sleep(0.01)
        waiter = asyncio.create_task(third())
        await asyncio.sleep(0.05)
        assert not third_in.is_set(), "a third 4 GiB lease was granted over a 10 GiB budget"
        release.set()
        await asyncio.wait_for(waiter, timeout=1)
        await asyncio.gather(*holders)
        assert third_in.is_set()


class TestIdleWorkspaceVmsYield:
    async def test_capacity_refusal_stops_the_least_recently_active_idle_workspace_vm(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Hypothesis: idle workspace VMs hold the quota builds need.

        Two idle ``paw-ws-*`` VMs (4 GiB each, auto-provisioned by get_workspace_vm and
        never deleted) leave 2 GiB of a 10 GiB org, so a 4 GiB build is refused. Today
        nothing reclaims them and the build ends in "Build capacity is full right now".
        Intended: on the refusal, ``admission.free_capacity`` stops the
        least-recently-active idle workspace VM (only as many as needed), then the
        create is retried and the build completes. The VM resumes on next use via
        ``context._ensure_started``.
        """
        # Eviction is opt-in (it can stop a VM running a process the agent left).
        monkeypatch.setenv("PAW_SITES_EVICT_IDLE_WORKSPACE_VMS", "1")
        now = datetime.now(UTC)
        client = LedgerDaytona()
        oldest = client.add_box(
            "paw-ws-aaaaaaaaaaaa",
            created_at=now - timedelta(days=2),
            last_activity_at=now - timedelta(hours=2),
        )
        recent = client.add_box(
            "paw-ws-bbbbbbbbbbbb",
            created_at=now - timedelta(days=1),
            last_activity_at=now - timedelta(minutes=20),
        )

        try:
            result = await _build(client, 0)
        except Exception as exc:  # noqa: BLE001 — the failure under test
            pytest.fail(
                f"build refused with {type(exc).__name__}: {exc}; "
                f"stopped={client.stopped} (expected [{oldest.id}])"
            )
        assert result.ok
        assert client.stopped == [oldest.id]
        assert client.boxes[recent.id].state == "started"
