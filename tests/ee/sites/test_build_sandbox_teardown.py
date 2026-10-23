# tests/ee/sites/test_build_sandbox_teardown.py — reproduction tests for orphaned
# build / verify sandboxes holding the Daytona org's memory quota.
#
# Hypothesis: a site build or html verify that dies between "Daytona registered the
# box" and "we learned its id" (the SDK's 60s create-wait times out, or arq cancels the
# job while it is blocked in create) leaves a 2-4 GiB sandbox behind. Nothing names it
# in our state, nothing reaps ``paw-build-*`` / ``paw-verify-*``, so a few of them fill
# the 10 GiB org cap and every later build reads "Build capacity is full right now; try
# again in a few minutes".
#
# ``LedgerDaytona`` is a fake client with a real memory ledger: create registers a box
# BEFORE it can fail (as the SDK does), delete frees it, and ``find_sandbox_by_name`` /
# ``list_sandboxes`` see every registered box. test_build_admission.py reuses it.
#
# The intended seams these tests name (they do not exist yet):
#   * ``daytona_runner.run_build`` / ``browser_check.run_standalone``: when create raises
#     or is cancelled before an id comes back, look the box up by its unique name
#     (``client.find_sandbox_by_name``) and delete it; stamp ``labels`` on every create.
#   * ``pocketpaw_ee.sites.sandbox_reaper.reap_orphan_build_sandboxes`` plus an arq cron
#     in ``build_worker.WorkerSettings.cron_jobs`` that runs it.
from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from daytona import DaytonaTimeoutError, DaytonaValidationError
from pocketpaw_ee.cloud.daytona.client import SandboxInfo
from pocketpaw_ee.sites import browser_check as bc
from pocketpaw_ee.sites import daytona_build as db
from pocketpaw_ee.sites import daytona_runner as dr

from tests.ee.sites.faults import clean_artifact, ok_sentinel

REACT_FILES = {"src/App.tsx": "export default function App() { return <p>hi</p>; }"}
HTML_FILES = {"index.html": "<!doctype html><title>hi</title>"}
HARNESS = {"check.mjs": b"// harness stub"}

ORG_MEMORY_GIB = 10
CAPACITY_MESSAGE = (
    "Failed to create sandbox: Total memory limit exceeded. Maximum allowed: {cap}GiB."
)


class LedgerDaytona:
    """A fake Daytona org with a memory cap and a list of live sandboxes.

    ``fail_after_register`` makes the next create register the box and then raise
    (the SDK's create-wait timing out on a box that does exist). ``block_create`` makes
    create register the box and then hang until ``create_gate`` is set (the image still
    building), then return it. ``step_delay`` yields inside
    wait / exec so concurrent builds overlap the way real ones do.
    """

    def __init__(self, *, memory_cap_gib: int = ORG_MEMORY_GIB, step_delay: float = 0.0) -> None:
        self.memory_cap_gib = memory_cap_gib
        self.step_delay = step_delay
        self.boxes: dict[str, SandboxInfo] = {}
        self.labels: dict[str, dict[str, str]] = {}
        self.create_kwargs: list[dict[str, Any]] = []
        self.deleted: list[str] = []
        self.stopped: list[str] = []
        self.capacity_refusals = 0
        self.peak_memory_gib = 0
        self.fail_after_register: BaseException | None = None
        self.block_create = False
        self.create_gate = asyncio.Event()
        self.registered = asyncio.Event()
        self._seq = 0

    # -- ledger --------------------------------------------------------------
    def used_memory_gib(self) -> int:
        return sum(b.memory for b in self.boxes.values() if b.state == "started")

    def add_box(
        self,
        name: str,
        *,
        memory: int = 4,
        state: str = "started",
        created_at: datetime | None = None,
        last_activity_at: datetime | None = None,
    ) -> SandboxInfo:
        self._seq += 1
        sid = f"sb-{self._seq}"
        created = created_at or datetime.now(UTC)
        info = SandboxInfo(
            id=sid,
            name=name,
            state=state,
            memory=memory,
            created_at=created.isoformat(),
            last_activity_at=(last_activity_at or created).isoformat(),
        )
        self.boxes[sid] = info
        self.peak_memory_gib = max(self.peak_memory_gib, self.used_memory_gib())
        return info

    # -- DaytonaClient surface ----------------------------------------------
    async def create_sandbox(self, **kwargs: Any) -> SandboxInfo:
        self.create_kwargs.append(kwargs)
        memory = int(kwargs.get("memory", 4))
        if self.used_memory_gib() + memory > self.memory_cap_gib:
            self.capacity_refusals += 1
            raise DaytonaValidationError(CAPACITY_MESSAGE.format(cap=self.memory_cap_gib))
        info = self.add_box(kwargs["name"], memory=memory)
        self.labels[info.id] = dict(kwargs.get("labels") or {})
        self.registered.set()
        if self.block_create:
            await self.create_gate.wait()  # the SDK's create-wait, image still building
        if self.fail_after_register is not None:
            exc, self.fail_after_register = self.fail_after_register, None
            raise exc
        return info

    async def wait_for_sandbox(self, sandbox_id: str, target_state: str = "started") -> None:
        await asyncio.sleep(self.step_delay)

    async def bulk_upload(self, sandbox_id: str, files: list[tuple[Any, str]]) -> None:
        return None

    async def execute_command(self, sandbox_id: str, command: str, timeout: int = 30) -> Any:
        await asyncio.sleep(self.step_delay)
        return object()

    async def download_file(self, sandbox_id: str, remote_path: str) -> bytes:
        if remote_path.endswith(db.BUILD_RESULT_FILENAME):
            return json.dumps(ok_sentinel()).encode()
        if remote_path == dr.SANDBOX_ARTIFACT_PATH:
            return clean_artifact()
        raise FileNotFoundError(remote_path)

    async def delete_sandbox(self, sandbox_id: str) -> None:
        self.deleted.append(sandbox_id)
        self.boxes.pop(sandbox_id, None)

    async def stop_sandbox(self, sandbox_id: str) -> None:
        self.stopped.append(sandbox_id)
        if sandbox_id in self.boxes:
            self.boxes[sandbox_id].state = "stopped"

    async def start_sandbox(self, sandbox_id: str) -> None:
        if sandbox_id in self.boxes:
            self.boxes[sandbox_id].state = "started"

    async def list_sandboxes(self) -> list[SandboxInfo]:
        return list(self.boxes.values())

    async def find_sandbox_by_name(self, name: str) -> SandboxInfo | None:
        return next((b for b in self.boxes.values() if b.name == name), None)

    async def get_sandbox_by_id(self, sandbox_id: str) -> SandboxInfo:
        return self.boxes[sandbox_id]


def _create_timeout() -> DaytonaTimeoutError:
    return DaytonaTimeoutError(
        "Failed to create sandbox: Sandbox failed to become ready within the 60s timeout period"
    )


async def _build(client: LedgerDaytona, **kw: Any) -> dr.BuildRunResult:
    return await dr.run_build(REACT_FILES, engine="react", timeout_seconds=600, client=client, **kw)


async def _verify(client: LedgerDaytona) -> bc.BrowserCheckResult:
    return await bc.run_standalone(HTML_FILES, client=client, harness=HARNESS)


# ---------------------------------------------------------------------------
# Create registers the box, then raises
# ---------------------------------------------------------------------------


class TestCreateTimeoutStillTearsDown:
    async def test_run_build_deletes_the_box_a_timed_out_create_registered(self) -> None:
        """Hypothesis: orphaned build sandboxes hold quota.

        The SDK create POSTs, then waits 60s for ``started``; on timeout the box exists
        but create raises, so ``run_build`` never learns ``sandbox_id`` and its
        ``finally`` skips the delete. Intended: run_build finds the box by its unique
        name and deletes it. Symptom: each slow create leaks a 4 GiB box until the org
        hits 10 GiB and builds report "Build capacity is full right now".
        """
        client = LedgerDaytona()
        client.fail_after_register = _create_timeout()
        with pytest.raises(DaytonaTimeoutError):
            await _build(client, sandbox_name="paw-build-timeout")
        assert client.boxes == {}, (
            f"timed-out create leaked {[b.name for b in client.boxes.values()]}; "
            f"deleted={client.deleted}"
        )

    async def test_run_standalone_deletes_the_box_a_timed_out_create_registered(self) -> None:
        """Hypothesis: orphaned verify sandboxes hold quota.

        Same gap in the html verify lane: ``browser_check.run_standalone`` only deletes
        when ``sandbox_id`` is set. Intended: look the ``paw-verify-*`` box up by name
        and delete it. Symptom: verify runs leak 2 GiB boxes that starve later builds.
        """
        client = LedgerDaytona()
        client.fail_after_register = _create_timeout()
        with pytest.raises(DaytonaTimeoutError):
            await _verify(client)
        assert client.boxes == {}, (
            f"timed-out verify create leaked {[b.name for b in client.boxes.values()]}"
        )


# ---------------------------------------------------------------------------
# Task cancelled while blocked (preview supersede / arq abort / job timeout)
# ---------------------------------------------------------------------------


async def _cancel_once_registered(coro: Any, client: LedgerDaytona) -> None:
    task = asyncio.create_task(coro)
    await asyncio.wait_for(client.registered.wait(), timeout=2)
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=2)


async def finish_create_and_settle(client: LedgerDaytona) -> None:
    """Let the blocked create return, then wait for the background delete of its box.
    The box must still exist first: a delete mid-create poisons Daytona's image build."""
    assert client.boxes, "the box was deleted while its create was still in flight"
    client.create_gate.set()
    async with asyncio.timeout(2):
        while client.boxes:
            await asyncio.sleep(0.01)


class TestCancellationStillTearsDown:
    async def test_run_build_cancelled_inside_create_deletes_the_box(self) -> None:
        """Hypothesis: orphaned build sandboxes hold quota.

        ``_supersede_previous_job`` aborts the previous preview job, and arq cancels a
        job past its timeout. When that lands while create is still waiting for
        ``started``, the box exists but ``sandbox_id`` is None, so nothing deletes it.
        Intended: the cancel propagates at once, the create finishes in the background
        (deleting mid-create would poison the image build) and its box is then deleted.
        Symptom: fast successive edits (each superseding the last preview) leak one 4 GiB
        box apiece and the next build hits "Build capacity is full".
        """
        client = LedgerDaytona()
        client.block_create = True
        await _cancel_once_registered(_build(client, sandbox_name="paw-build-cancel"), client)
        await finish_create_and_settle(client)
        assert client.boxes == {}, (
            f"cancelled create leaked {[b.name for b in client.boxes.values()]}"
        )

    async def test_run_standalone_cancelled_inside_create_deletes_the_box(self) -> None:
        """Hypothesis: orphaned verify sandboxes hold quota.

        A superseded html verify job is aborted the same way. Intended: delete the
        ``paw-verify-*`` box create registered. Symptom: as above, 2 GiB per abort.
        """
        client = LedgerDaytona()
        client.block_create = True
        await _cancel_once_registered(_verify(client), client)
        await finish_create_and_settle(client)
        assert client.boxes == {}, (
            f"cancelled verify create leaked {[b.name for b in client.boxes.values()]}"
        )

    async def test_run_build_cancelled_inside_wait_deletes_the_box(self) -> None:
        """Hypothesis: orphaned build sandboxes hold quota (GUARD, passes today).

        Once create returned an id, a cancel during ``wait_for_sandbox`` reaches the
        ``finally`` with ``sandbox_id`` set, so the delete runs. Kept so the fix for the
        create-window cases does not regress this window.
        """
        client = LedgerDaytona(step_delay=30)
        task = asyncio.create_task(_build(client, sandbox_name="paw-build-wait"))
        await asyncio.wait_for(client.registered.wait(), timeout=2)
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert client.boxes == {}


# ---------------------------------------------------------------------------
# Labels — so a reaper (and a human in the Daytona console) can find our boxes
# ---------------------------------------------------------------------------


def _refusing_client() -> LedgerDaytona:
    # Cap 0: create records its kwargs and refuses, so the test reads the request
    # without running the rest of the lane.
    return LedgerDaytona(memory_cap_gib=0)


class TestSandboxesCarryLabels:
    async def test_run_build_labels_its_sandbox_with_the_lane(self) -> None:
        """Hypothesis: orphaned build sandboxes hold quota and cannot be told apart.

        Intended: every ``run_build`` create passes ``labels`` containing
        ``{"paw_lane": "site-build"}``. Symptom today: the only identity is the
        ``paw-build-<ms>`` name, so neither a reaper nor an operator can map a box in
        the Daytona console back to a lane or a job.
        """
        client = _refusing_client()
        with pytest.raises(DaytonaValidationError):
            await _build(client)
        labels = client.create_kwargs[0].get("labels")
        assert isinstance(labels, dict) and labels.get("paw_lane") == "site-build", (
            f"run_build created with labels={labels!r}"
        )

    async def test_run_build_forwards_caller_labels(self) -> None:
        """Hypothesis: orphaned build sandboxes cannot be traced to their job.

        Intended: ``run_build(..., labels={"paw_pocket": ..., "paw_job": ...})`` merges
        the caller's labels over the lane label. The build jobs pass the pocket id and
        arq job id. Fails today with TypeError (no ``labels`` parameter).
        """
        client = _refusing_client()
        with pytest.raises(DaytonaValidationError):
            await _build(client, labels={"paw_pocket": "p-1", "paw_job": "j-1"})
        labels = client.create_kwargs[0].get("labels") or {}
        assert labels.get("paw_pocket") == "p-1"
        assert labels.get("paw_job") == "j-1"
        assert labels.get("paw_lane") == "site-build"

    async def test_run_standalone_labels_its_sandbox_with_the_lane(self) -> None:
        """Hypothesis: orphaned verify sandboxes cannot be told apart.

        Intended: ``run_standalone`` creates with ``labels`` containing
        ``{"paw_lane": "site-verify"}`` (and accepts a ``labels=`` kwarg like
        ``run_build``). Symptom: unlabelled ``paw-verify-*`` boxes in the console.
        """
        client = _refusing_client()
        with pytest.raises(DaytonaValidationError):
            await _verify(client)
        labels = client.create_kwargs[0].get("labels")
        assert isinstance(labels, dict) and labels.get("paw_lane") == "site-verify", (
            f"run_standalone created with labels={labels!r}"
        )


# ---------------------------------------------------------------------------
# The reaper
# ---------------------------------------------------------------------------


class TestOrphanReaper:
    def test_build_worker_schedules_a_build_sandbox_reaper(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Hypothesis: orphaned build sandboxes hold quota because nothing reaps them.

        Intended: ``build_worker.WorkerSettings.cron_jobs`` carries an arq cron whose
        coroutine lives in ``pocketpaw_ee.sites.sandbox_reaper`` and calls
        ``reap_orphan_build_sandboxes``. Today the only cron is the AI-visibility sweep,
        so a leaked box lives until Daytona's own auto-stop (build timeout + 10 min) at
        best, and forever if it never left ``creating``.
        """
        # build_worker builds its arq settings at import time (see test_delete_job.py).
        monkeypatch.setenv("POCKETPAW_REDIS_URL", "redis://localhost:6379")
        from pocketpaw_ee.sites.build_worker import WorkerSettings

        modules = [getattr(cj.coroutine, "__module__", "") for cj in WorkerSettings.cron_jobs]
        assert "pocketpaw_ee.sites.sandbox_reaper" in modules, (
            f"cron_jobs coroutines come from {modules}; no build-sandbox reaper"
        )

    async def test_reaper_deletes_only_old_build_and_verify_boxes(self) -> None:
        """Hypothesis: orphaned build sandboxes hold quota.

        Intended contract::

            async def reap_orphan_build_sandboxes(
                client: DaytonaClient,
                *,
                now: datetime | None = None,
                max_age_seconds: int | None = None,
            ) -> list[str]   # ids deleted

        Deletes every sandbox named ``paw-build-*`` or ``paw-verify-*`` whose
        ``created_at`` is older than ``max_age_seconds`` (default: the longest sites
        job timeout plus a margin under an hour), in any state. Leaves younger build
        boxes (a build may be running in them), workspace VMs (``paw-ws-*``) and any
        other name alone. A failing delete is logged and skipped, never raised.
        Fails today with ImportError (module absent).
        """
        from pocketpaw_ee.sites.build_job import (
            site_build_job_timeout_seconds,
            site_preview_job_timeout_seconds,
        )
        from pocketpaw_ee.sites.sandbox_reaper import reap_orphan_build_sandboxes

        now = datetime.now(UTC)
        old = now - timedelta(
            seconds=max(site_build_job_timeout_seconds(), site_preview_job_timeout_seconds()) + 3600
        )
        young = now - timedelta(seconds=60)
        client = LedgerDaytona(memory_cap_gib=100)
        old_build = client.add_box("paw-build-1", created_at=old)
        old_creating = client.add_box("paw-build-2", state="creating", created_at=old)
        old_verify = client.add_box("paw-verify-3", memory=2, created_at=old)
        young_build = client.add_box("paw-build-4", created_at=young)
        workspace_vm = client.add_box("paw-ws-abcdef012345", created_at=old)
        other = client.add_box("someone-elses-box", created_at=old)

        deleted = await reap_orphan_build_sandboxes(client, now=now)

        assert sorted(deleted) == sorted([old_build.id, old_creating.id, old_verify.id])
        assert set(client.boxes) == {young_build.id, workspace_vm.id, other.id}
