# scheduler.py — Periodic background sweep for the Firestore→Fabric worker.
# Created: 2026-06-11 — generic Firestore→Fabric ingestion worker.
#
# Mirrors the MemberIngestScheduler / ChatRunDoc sweeper shape (the cloud's
# established periodic-task pattern): a class with a testable ``tick()`` (one
# sweep, no background task), a ``_run_loop()`` that wakes on a timeout OR a
# stop event so ``stop()`` returns promptly, ``start()``/``stop()`` lifecycle,
# and a module singleton wired onto ``app.state`` by ``mount_cloud`` under the
# ``POCKETPAW_CLOUD_SCHEDULER_ENABLED`` gate (so pytest runs never spawn a loop
# that outlives the test).
#
# Cadence: every 5 minutes by default. Each tick runs ``run_ingest_sweep``
# across every configured (workspace, collection) pair — backfill on first
# sight of a source, incremental thereafter. Override the interval via
# POCKETPAW_FABRIC_INGEST_INTERVAL_SECONDS.

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from typing import Any

from fastapi import FastAPI

logger = logging.getLogger(__name__)

_TASK_KEY = "_fabric_ingest_scheduler"
_DEFAULT_INTERVAL_SECONDS = 300  # 5 minutes — same cadence as the member sweep
_ENV_INTERVAL = "POCKETPAW_FABRIC_INGEST_INTERVAL_SECONDS"


def _interval_seconds() -> int:
    """Read the sweep interval from env, default 300s (5 min)."""
    raw = os.environ.get(_ENV_INTERVAL, "").strip()
    if not raw:
        return _DEFAULT_INTERVAL_SECONDS
    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "%s=%r is not an int — falling back to %d seconds",
            _ENV_INTERVAL,
            raw,
            _DEFAULT_INTERVAL_SECONDS,
        )
        return _DEFAULT_INTERVAL_SECONDS
    return max(1, value)


class FabricIngestScheduler:
    """In-process periodic sweep for Firestore→Fabric mirroring.

    Use:
        scheduler = FabricIngestScheduler()
        await scheduler.start()   # spawn the background loop
        ...
        await scheduler.stop()    # cancel + await cleanly

    For unit tests:
        scheduler = FabricIngestScheduler()
        await scheduler.tick()    # one sweep, no background task
    """

    def __init__(self, interval_seconds: int | None = None) -> None:
        self._interval = interval_seconds if interval_seconds is not None else _interval_seconds()
        self._task: asyncio.Task[None] | None = None
        self._stop_event: asyncio.Event = asyncio.Event()

    @property
    def interval_seconds(self) -> int:
        return self._interval

    async def tick(self) -> dict[str, Any]:
        """Run a single ingest sweep. Safe to call from tests without
        ``start()``. Returns the sweep summary; never raises — a sweep failure
        is logged and reported as an empty summary so the loop survives a
        transient DB/Firestore hiccup."""
        # Import inside so a test exercising ``tick()`` directly doesn't have to
        # bootstrap the whole cloud module, and so a monkeypatch of
        # ``service.run_ingest_sweep`` is observed (late binding).
        from pocketpaw_ee.cloud.fabric_ingest import service as ingest_service

        try:
            summary = await ingest_service.run_ingest_sweep()
            logger.info(
                "fabric_ingest.scheduler: tick sources=%s ok=%s errors=%s",
                summary.get("sources"),
                summary.get("ok"),
                summary.get("errors"),
            )
            return summary
        except Exception:  # noqa: BLE001 — keep the loop alive across hiccups
            logger.exception("fabric_ingest.scheduler: sweep tick raised")
            return {"sources": 0, "ok": 0, "errors": 0}

    async def _run_loop(self) -> None:
        """Background loop body. Wakes on the interval OR the stop event."""
        logger.info("fabric_ingest.scheduler: loop started (interval=%ds)", self._interval)
        while not self._stop_event.is_set():
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=self._interval)
                # Reached here without TimeoutError → stop was signalled.
                break
            except TimeoutError:
                pass
            except asyncio.CancelledError:
                logger.info("fabric_ingest.scheduler: loop cancelled — exiting")
                raise
            await self.tick()

    async def start(self) -> None:
        """Spawn the background loop. Idempotent."""
        if self._task is not None and not self._task.done():
            return
        self._stop_event = asyncio.Event()
        self._task = asyncio.create_task(self._run_loop(), name="fabric-ingest-scheduler")

    async def stop(self) -> None:
        """Cancel + await the loop. Safe to call multiple times."""
        if self._task is None:
            return
        self._stop_event.set()
        if not self._task.done():
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
        self._task = None


# ---------------------------------------------------------------------------
# Module-level singleton — mount_cloud installs one onto app.state.
# ---------------------------------------------------------------------------


_SCHEDULER: FabricIngestScheduler | None = None


def get_scheduler() -> FabricIngestScheduler:
    """Return the process singleton, constructing one lazily if needed."""
    global _SCHEDULER
    if _SCHEDULER is None:
        _SCHEDULER = FabricIngestScheduler()
    return _SCHEDULER


def reset_scheduler_for_tests() -> None:
    """Drop the singleton so each test gets a fresh instance."""
    global _SCHEDULER
    _SCHEDULER = None


async def start_fabric_ingest(app: FastAPI) -> None:
    """Wire the singleton onto ``app.state`` and spawn the loop.
    Mirrors ``member_ingest.scheduler.start_member_ingest``."""
    scheduler = get_scheduler()
    setattr(app.state, _TASK_KEY, scheduler)
    await scheduler.start()


async def stop_fabric_ingest(app: FastAPI) -> None:
    """Cancel + await the loop attached to ``app.state``."""
    scheduler: FabricIngestScheduler | None = getattr(app.state, _TASK_KEY, None)
    if scheduler is None:
        return
    await scheduler.stop()
    setattr(app.state, _TASK_KEY, None)


__all__ = [
    "FabricIngestScheduler",
    "get_scheduler",
    "reset_scheduler_for_tests",
    "start_fabric_ingest",
    "stop_fabric_ingest",
]
