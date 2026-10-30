# tests/ee/sites/test_sandbox_image_poison.py — reproduction tests for the poisoned
# build-sandbox image: every Daytona create fails in under a second with
# "SandboxState.BUILD_FAILED, error reason: context canceled".
#
# Build boxes use a dynamic ``daytona.Image`` that Daytona builds server-side during
# ``daytona.create()`` and caches by dockerfile. Deleting the box (or abandoning the
# request) mid-build leaves that cached build ``BUILD_FAILED``, and every later create
# of the same image fails at once. We cause the mid-build delete two ways, and then
# retry the poisoned create as if it were a transient outage:
#
#   * Cancel: a superseded preview job's ``CancelledError`` lands in
#     ``admission.create_admitted``, whose ``except BaseException`` runs
#     ``sandbox_reaper.teardown_unreturned`` straight away, deleting the box mid-build.
#     Intended: the create keeps running in the background (shielded); the cancel
#     propagates at once; the box is deleted only after create returns.
#   * Timeout: ``DaytonaClient.create_sandbox`` leaves the SDK's 60s create timeout in
#     place, shorter than a cold image build. Intended: an explicit, generous timeout.
#   * Classification: a BUILD_FAILED create reads ``sandbox_unavailable:no_sandbox`` and
#     spends the "raised" retry budget. Intended: a distinct, non-retryable cause,
#     ``sandbox_unavailable:image_build_failed`` (name assumed; see TestBuildFailed).
#
# Fakes only: no network, no real Daytona.
from __future__ import annotations

import asyncio
import pickle
from types import SimpleNamespace
from typing import Any

import pytest
from daytona import DaytonaError
from pocketpaw_ee.cloud.daytona.client import DaytonaClient, SandboxInfo
from pocketpaw_ee.sites import admission
from pocketpaw_ee.sites import build_job as bj

from tests.ee.sites.test_preview_build_lane import _sandbox
from tests.ee.sites.test_sandbox_capacity import _ctx, _preview

BUILD_FAILED_MESSAGE = (
    "Failed to create sandbox: Failure during waiting for sandbox to start: Sandbox "
    "3f2a9c1e-1111-2222-3333-444455556666 entered error state: SandboxState.BUILD_FAILED, "
    "error reason: context canceled"
)
#: The cause the implementer is expected to add (``capacity.IMAGE_BUILD_FAILED_CAUSE``
#: or similar); the full reason is ``sandbox_unavailable:image_build_failed``.
IMAGE_BUILD_FAILED_REASON = f"{bj.RUNG_SANDBOX_UNAVAILABLE}:image_build_failed"


# ---------------------------------------------------------------------------
# A. Cancel during create must not delete the box while its image is building
# ---------------------------------------------------------------------------


class _BuildingDaytona:
    """Daytona with a create that blocks while the image builds.

    The box is registered (findable by name) as soon as create starts, as the real API
    does at POST time. ``finish_build`` lets create return. ``deleted_mid_build`` records
    every delete that arrived while create was still in flight: that delete is what
    poisons the cached image build.
    """

    def __init__(self) -> None:
        self.create_started = asyncio.Event()
        self._build_done = asyncio.Event()
        self.create_returned = False
        self.deleted: list[str] = []
        self.deleted_mid_build: list[str] = []
        self.box: SandboxInfo | None = None

    def finish_build(self) -> None:
        self._build_done.set()

    async def create_sandbox(self, **kwargs: Any) -> SandboxInfo:
        self.box = SandboxInfo(id="sb-1", name=kwargs["name"], state="pending_build", memory=4)
        self.create_started.set()
        await self._build_done.wait()
        self.box.state = "started"
        self.create_returned = True
        return self.box

    async def find_sandbox_by_name(self, name: str) -> SandboxInfo | None:
        return self.box if self.box is not None and self.box.name == name else None

    async def delete_sandbox(self, sandbox_id: str) -> None:
        if not self.create_returned:
            self.deleted_mid_build.append(sandbox_id)
        self.deleted.append(sandbox_id)


async def _until(cond: Any, timeout: float = 2.0) -> None:
    async with asyncio.timeout(timeout):
        while not cond():
            await asyncio.sleep(0.01)


class TestCancelDuringCreate:
    async def test_cancel_lets_the_build_finish_then_deletes_the_box(self) -> None:
        """Superseding a preview (arq ``Job.abort``) cancels the job while
        ``daytona.create`` is still building the dynamic image.

        Intended: ``create_admitted`` re-raises ``CancelledError`` to its caller at once
        (the caller never waits out the build) but does NOT delete the box while create
        is in flight. The create carries on in a shielded background task, and once it
        returns, the box it made is deleted, so nothing leaks.

        Today the ``except BaseException`` calls ``teardown_unreturned`` immediately,
        which finds the box by name and deletes it mid-build. Daytona then caches that
        image build as ``BUILD_FAILED: context canceled`` and every later create of the
        image fails in under a second.
        """
        client = _BuildingDaytona()
        task = asyncio.create_task(
            admission.create_admitted(client, {"name": "paw-build-poison", "memory": 4})
        )
        await asyncio.wait_for(client.create_started.wait(), timeout=2)

        task.cancel()
        # Prompt: the build is still blocked, so a caller that waited it out would hang.
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=1)

        assert client.deleted_mid_build == [], (
            f"deleted {client.deleted_mid_build} while its image was still building: "
            "that delete poisons Daytona's cached image build (BUILD_FAILED: context canceled)"
        )
        assert not client.create_returned

        client.finish_build()
        await _until(lambda: client.deleted, timeout=2)
        assert client.deleted == ["sb-1"], "the box the background create made must not leak"
        assert client.deleted_mid_build == []


# ---------------------------------------------------------------------------
# B. The create call must not use the SDK's 60s default timeout
# ---------------------------------------------------------------------------

#: Lower bound for an explicit create timeout. A cold build of the paw dev image takes
#: well over the SDK's 60s default; 0 (no SDK timeout) is also acceptable.
MIN_CREATE_TIMEOUT_SECONDS = 600


class _RecordingAsyncDaytona:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, dict[str, Any]]] = []

    async def create(self, params: Any, **kwargs: Any) -> Any:
        self.calls.append((params, kwargs))
        return SimpleNamespace(id="sb-1")


class TestCreateTimeout:
    async def test_create_sandbox_passes_a_generous_timeout_to_the_sdk(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Expectation: ``DaytonaClient.create_sandbox`` calls ``daytona.create(params,
        timeout=T)`` with T == 0 (no SDK timeout) or T >= 600 seconds. A constant or an
        env knob is fine; the default must clear a cold dynamic-image build.

        Today it calls ``daytona.create(params)`` and inherits the SDK's ``timeout=60``.
        The SDK raises after 60s while the image is still building, ``create_admitted``
        then deletes the box mid-build, and the cached image build is poisoned.
        """
        monkeypatch.setenv("DAYTONA_API_KEY", "test-key")
        fake = _RecordingAsyncDaytona()
        client = DaytonaClient()

        async def _ensure() -> _RecordingAsyncDaytona:
            return fake

        async def _parse(_sb: Any) -> SandboxInfo:
            return SandboxInfo(id="sb-1", name="paw-build-timeout", state="started")

        monkeypatch.setattr(client, "_ensure_daytona", _ensure)
        monkeypatch.setattr(client, "_parse_sandbox", _parse)

        # image=None resolves to the dynamic paw dev image, the one that builds server-side.
        await client.create_sandbox(name="paw-build-timeout", memory=4)

        assert len(fake.calls) == 1
        _params, kwargs = fake.calls[0]
        assert "timeout" in kwargs, (
            "daytona.create() was called without timeout=, so the SDK's 60s default applies"
        )
        timeout = kwargs["timeout"]
        assert timeout == 0 or timeout >= MIN_CREATE_TIMEOUT_SECONDS, (
            f"create timeout {timeout}s is shorter than a cold image build"
        )


# ---------------------------------------------------------------------------
# C. A poisoned image is its own, non-retryable cause
# ---------------------------------------------------------------------------


def _build_failed() -> DaytonaError:
    return DaytonaError(BUILD_FAILED_MESSAGE)


class TestBuildFailedClassification:
    """Seams assumed: ``bj.raised_job_reason`` (reason read back for a raised job) and
    ``bj._retry_kind`` (which retry budget a stored result spends). The preview lane
    still raises a ``SandboxUnavailableError`` (or a subclass of it) for a BUILD_FAILED
    create; the implementer may classify by subclass or by the wrapped message, but the
    classification must survive arq pickling the exception into Redis."""

    def test_a_build_failed_create_reads_as_image_build_failed(self) -> None:
        """Today: ``sandbox_unavailable:no_sandbox``, indistinguishable from Daytona
        being down, so nobody can tell the image itself is poisoned."""
        wrapped = bj.SandboxUnavailableError.wrapping(_build_failed())
        stored = pickle.loads(pickle.dumps(wrapped))  # arq stores the raise in Redis
        assert bj.raised_job_reason(stored) == IMAGE_BUILD_FAILED_REASON

    def test_a_build_failed_create_does_not_spend_the_retry_budget(self) -> None:
        """A poisoned image fails every create instantly, so re-queuing the render just
        burns three attempts in ~3s (prod log: "retry budget spent"). Intended:
        sticky (``_retry_kind`` returns None), not ``"raised"``."""
        wrapped = bj.SandboxUnavailableError.wrapping(_build_failed())
        info = SimpleNamespace(success=False, result=pickle.loads(pickle.dumps(wrapped)))
        assert bj._retry_kind(info) is None

    async def test_the_preview_lane_raises_it_as_image_build_failed(self) -> None:
        """End to end through ``run_site_preview_build``: a create that fails with
        BUILD_FAILED surfaces as a raise read back as ``image_build_failed``."""
        client = _sandbox(fail_at="create", error=_build_failed())
        with pytest.raises(bj.SandboxUnavailableError) as raised:
            await _preview(_ctx(1), client)
        assert isinstance(raised.value.__cause__, DaytonaError)
        assert bj.raised_job_reason(raised.value) == IMAGE_BUILD_FAILED_REASON
