# Tests for the request-log write path's task handling and path skipping.
#
# Created 2026-09-04 alongside the fix. Two behaviours are pinned here because
# both failed silently in production shape and neither is visible from a
# response:
#
#   * A telemetry write task must be strongly referenced. The old code used
#     asyncio.ensure_future and kept no reference, so the loop's weak
#     reference was the only one and a pending write could be collected
#     mid-await. Nothing surfaces when that happens — the row is simply
#     missing, sometimes.
#   * The number of in-flight writes must be bounded. Unbounded, a Mongo stall
#     turns into unbounded memory growth in the web process instead of
#     backpressure.
#
# NOTE: this file lives under tests/cloud/, which the CI lanes exclude, so it
# does not run on a pull request today. That exclusion is a known pre-existing
# gap (CI names 7 of 685 cloud test files), not something this change made.

from __future__ import annotations

import asyncio

import pytest
from pocketpaw_ee.cloud._core import request_log


@pytest.fixture(autouse=True)
def _clean_module_state():
    """Each test starts with an empty pending set and drop counter."""
    request_log._pending.clear()
    request_log._dropped = 0
    yield
    request_log._pending.clear()
    request_log._dropped = 0


def _log_once(**overrides):
    kwargs = {
        "method": "GET",
        "path": "/api/v1/pockets",
        "status_code": 200,
        "duration_ms": 1.0,
        "actor_id": "u1",
        "workspace_id": "w1",
        "is_error": False,
        "user_agent": "pytest",
        "ip": "203.0.113.9",
    }
    kwargs.update(overrides)
    request_log._log_request(**kwargs)


class TestSkippedPaths:
    """The high-volume, no-audit-value paths must not reach the write path."""

    @pytest.mark.parametrize(
        "path",
        [
            "/health",
            "/version",
            "/api/v1/health",
            "/api/v1/version",
            "/api/v1/auth/csrf",
            "/static/app.js",
            "/uploads/avatars/abc.png",
            "/assets/logo.svg",
        ],
    )
    def test_noise_paths_are_skipped(self, path):
        assert request_log._is_skipped(path) is True

    @pytest.mark.parametrize(
        "path",
        [
            "/api/v1/pockets",
            "/api/v1/chat/send",
            "/api/v1/workspaces/w1/audit",
            # Near-misses: a real route must not be skipped just because it
            # shares a prefix with a skipped one.
            "/api/v1/healthchecks",
            "/versions/list",
        ],
    )
    def test_real_routes_are_not_skipped(self, path):
        assert request_log._is_skipped(path) is False


class TestTaskLifetime:
    @pytest.mark.asyncio
    async def test_pending_task_is_strongly_referenced_until_it_finishes(self, monkeypatch):
        """The write task must be held, not left to the loop's weak reference."""
        started = asyncio.Event()
        release = asyncio.Event()

        async def _slow_record(**_kwargs):
            started.set()
            await release.wait()

        monkeypatch.setattr(
            "pocketpaw_ee.cloud.request_log.service.record", _slow_record, raising=False
        )

        _log_once()
        await started.wait()

        assert len(request_log._pending) == 1, "in-flight write is not referenced"

        release.set()
        # Let the task finish and its done-callback run.
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        assert request_log._pending == set(), "completed write was not released"

    @pytest.mark.asyncio
    async def test_writes_are_dropped_once_the_ceiling_is_reached(self, monkeypatch):
        """Past the ceiling, shed telemetry rather than queue without bound."""
        release = asyncio.Event()

        async def _blocked_record(**_kwargs):
            await release.wait()

        monkeypatch.setattr(
            "pocketpaw_ee.cloud.request_log.service.record", _blocked_record, raising=False
        )

        for _ in range(request_log._MAX_PENDING):
            _log_once()
        await asyncio.sleep(0)

        assert len(request_log._pending) == request_log._MAX_PENDING
        assert request_log._dropped == 0

        # One more must be shed, not queued.
        _log_once()
        assert len(request_log._pending) == request_log._MAX_PENDING
        assert request_log._dropped == 1

        release.set()
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    @pytest.mark.asyncio
    async def test_capacity_recovers_after_writes_drain(self, monkeypatch):
        """Shedding is transient: once the backlog clears, logging resumes."""
        release = asyncio.Event()

        async def _blocked_record(**_kwargs):
            await release.wait()

        monkeypatch.setattr(
            "pocketpaw_ee.cloud.request_log.service.record", _blocked_record, raising=False
        )

        for _ in range(request_log._MAX_PENDING):
            _log_once()
        await asyncio.sleep(0)
        _log_once()
        assert request_log._dropped == 1

        release.set()
        for _ in range(5):
            await asyncio.sleep(0)

        assert request_log._pending == set()

        # A fresh write is accepted again rather than shed.
        release_2 = asyncio.Event()

        async def _record_2(**_kwargs):
            await release_2.wait()

        monkeypatch.setattr(
            "pocketpaw_ee.cloud.request_log.service.record", _record_2, raising=False
        )
        _log_once()
        await asyncio.sleep(0)
        assert len(request_log._pending) == 1
        assert request_log._dropped == 1, "drop counter must not rise on an accepted write"

        release_2.set()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
