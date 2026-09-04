# Tests for the request-log write path's backpressure and path skipping.
#
# Created 2026-09-04 alongside the fix that made telemetry writes safe.
# Rewritten the same day, when the write path changed shape: it no longer
# spawns one task per request, it queues one entry per request for a single
# consumer to batch. The properties being pinned did not change, but where
# they live did.
#
#   * The write must be strongly referenced. The original code used
#     asyncio.ensure_future and kept no reference, so the loop's weak
#     reference was the only one and a pending write could be collected
#     mid-await, silently. Now there is exactly one long-lived consumer, and
#     it is the module that holds it.
#   * The backlog must be bounded. Unbounded, a Mongo stall turns into
#     unbounded memory growth in the web process instead of backpressure.
#     The ceiling moved from in-flight tasks to queued entries.
#   * The high-volume, no-audit-value paths must never reach any of this.
#
# The batching itself is covered in tests/cloud/test_request_log_batching.py.
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
    """Each test starts with no consumer, no queue and no drop count."""
    request_log._reset_for_tests()
    yield
    request_log._reset_for_tests()


async def _stop_consumer() -> None:
    """Reset while a loop is still running, then let the cancel land.

    The autouse fixture is synchronous, so its reset happens after the loop is
    gone and the cancelled consumer is finalized by the garbage collector
    instead - which CPython reports as an unraisable "Event loop is closed"
    against whichever test happens to run next.
    """
    request_log._reset_for_tests()
    await asyncio.sleep(0)


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


class TestConsumerLifetime:
    @pytest.mark.asyncio
    async def test_the_consumer_is_strongly_referenced(self, monkeypatch):
        """The module holds the consumer, not the loop's weak reference."""
        started = asyncio.Event()
        release = asyncio.Event()

        async def _slow_record_many(entries):  # noqa: ARG001
            started.set()
            await release.wait()
            return len(entries)

        monkeypatch.setattr(
            "pocketpaw_ee.cloud.request_log.service.record_many",
            _slow_record_many,
            raising=False,
        )
        monkeypatch.setattr(request_log, "_LINGER_SECONDS", 0.0)

        _log_once()
        await started.wait()

        assert request_log._drain_task is not None, "the consumer is not referenced"
        assert not request_log._drain_task.done()

        release.set()
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        # Still referenced and still running: one consumer serves every
        # request for the lifetime of the loop.
        assert request_log._drain_task is not None
        assert not request_log._drain_task.done()

        await _stop_consumer()

    @pytest.mark.asyncio
    async def test_entries_are_dropped_once_the_queue_is_full(self, monkeypatch):
        """Past the ceiling, shed telemetry rather than queue without bound.

        Nothing awaits inside the loop, so the consumer never gets a turn and
        the queue genuinely fills - the same shape as a Mongo stall.
        """
        monkeypatch.setattr(request_log, "_QUEUE_MAX", 8)
        request_log._reset_for_tests()

        for _ in range(request_log._QUEUE_MAX):
            _log_once()

        assert request_log._queue is not None
        assert request_log._queue.qsize() == request_log._QUEUE_MAX
        assert request_log._dropped == 0

        # One more must be shed, not queued.
        _log_once()

        assert request_log._queue.qsize() == request_log._QUEUE_MAX
        assert request_log._dropped == 1

        await _stop_consumer()

    @pytest.mark.asyncio
    async def test_capacity_recovers_after_the_queue_drains(self, monkeypatch):
        """Shedding is transient: once the backlog clears, logging resumes."""
        written: list[int] = []

        async def _record_many(entries):  # noqa: ANN001
            written.append(len(entries))
            return len(entries)

        monkeypatch.setattr(
            "pocketpaw_ee.cloud.request_log.service.record_many", _record_many, raising=False
        )
        monkeypatch.setattr(request_log, "_LINGER_SECONDS", 0.0)
        monkeypatch.setattr(request_log, "_QUEUE_MAX", 8)
        request_log._reset_for_tests()

        for _ in range(request_log._QUEUE_MAX + 1):
            _log_once()
        assert request_log._dropped == 1

        for _ in range(10):
            await asyncio.sleep(0)

        assert sum(written) == request_log._QUEUE_MAX
        assert request_log._queue is not None
        assert request_log._queue.qsize() == 0

        # A fresh entry is accepted again rather than shed.
        _log_once()
        assert request_log._dropped == 1, "drop counter must not rise on an accepted entry"

        for _ in range(10):
            await asyncio.sleep(0)
        assert sum(written) == request_log._QUEUE_MAX + 1

        await _stop_consumer()
