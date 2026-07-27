# ee/pocketpaw_ee/cloud/growth/worker.py — arq worker for the dedicated
# ``growth`` queue.
#
# Created 2026-07-27 (feat/growth-g1): registration seam only (a no-op
# heartbeat so arq would boot).
# Updated 2026-07-27 (feat/growth-g4): the heartbeat is replaced by the real
# ``growth.dispatch`` job entrypoint — registered under its explicit dotted
# name via ``arq.worker.func`` so the enqueue side
# (``growth.executor.execute_approved_growth_send``) and the worker agree on
# the name. The job body is a deliberate STUB in this slice: it logs and marks
# NOTHING sent — G-5 (email via provider) and G-6 (follow-ups) fill in the
# actual delivery and the draft's ``sent`` flip (via the service's
# ``gate_transition`` seam). The job only ever EXISTS for a draft whose
# ``_growth_send`` proposal a human approved — that is the whole gate.
# Updated 2026-07-27 (feat/growth-g6): the ``channel="whatsapp"`` branch is
# live — it delegates to ``growth.whatsapp.dispatch_whatsapp``, which enforces
# the hard ``prospect.opted_in`` guard (no opt-in ⇒ NO provider call at all),
# the hourly ``GROWTH_WHATSAPP_MAX_PER_HOUR`` cap, the MSG91 send, and the
# approved→sent flip through ``service.gate_transition``. Other channels keep
# the stub.
#
# Unlike workspace jobs (which ride arq's DEFAULT queue on the shared chat-runs
# worker — see ``jobs/domain.py``), growth gets its OWN queue + worker process
# so a burst of outbound work can never starve interactive chat runs. Enqueue
# with ``_queue_name=GROWTH_QUEUE_NAME`` (arq's selector is the
# underscore-prefixed kwarg; a bare ``queue=`` is forwarded to the job function
# and crashes it — see the jobs/domain.py history).
#
# Deploy as a separate process alongside the web service::
#
#     arq pocketpaw_ee.cloud.growth.worker.WorkerSettings
#
# The startup/shutdown pair mirrors ``chat/runs/worker.py``: pin the xproc role
# BEFORE anything emits, init the cloud DB + realtime bus, close the DB on the
# way out. ``redis_settings`` is evaluated eagerly at class-body time because
# arq reads ``settings_cls.__dict__`` directly (bypassing descriptors) — the
# same shape + rationale as the chat-runs worker; tests set a stub
# ``POCKETPAW_REDIS_URL`` in ``tests/cloud/conftest.py`` before import.

"""arq worker entry point for the dedicated ``growth`` queue."""

from __future__ import annotations

import logging
import os
from typing import Any

from arq.connections import RedisSettings
from arq.worker import func

from pocketpaw_ee.cloud import init_realtime
from pocketpaw_ee.cloud._core.realtime import xproc
from pocketpaw_ee.cloud.growth.domain import GROWTH_DISPATCH_JOB_NAME, GROWTH_QUEUE_NAME
from pocketpaw_ee.cloud.shared.db import close_cloud_db, init_cloud_db

logger = logging.getLogger(__name__)


async def dispatch(ctx: dict[str, Any], draft_id: str, channel: str) -> None:
    """Dispatch an APPROVED outbound draft.

    This job is enqueued ONLY by ``executor.execute_approved_growth_send``
    after a human approved the draft's ``_growth_send`` Instinct proposal —
    there is no other producer, so a job here IS the approval record's
    downstream.

    ``whatsapp`` is live (G-6): it enforces the hard opt-in guard, the hourly
    rate cap, sends via MSG91, and flips the draft approved→sent through the
    gate seam. Every refusal RAISES (``max_tries = 1``, so the job lands as a
    failed job an operator can see) and writes its own compliance row — a
    refused send is never a silent success.

    The other channels remain the G-4 logging stub until their slices land
    (G-5 email); they mark nothing sent.
    """
    if channel == "whatsapp":
        # Imported lazily so the worker module stays importable (and arq's
        # settings class evaluable) without the provider stack.
        from pocketpaw_ee.cloud.growth.whatsapp import dispatch_whatsapp

        await dispatch_whatsapp(draft_id)
        return

    logger.info(
        "growth worker: dispatch STUB — draft=%s channel=%s (approved send queued; "
        "no delivery for this channel yet, nothing marked sent)",
        draft_id,
        channel,
    )


async def _startup(ctx: dict[str, Any]) -> None:
    """Boot the worker: pin role, init the DB + realtime bus.

    ``xproc.set_role("worker")`` must run before any code emits, so ``emit()``
    routes over the cross-process bridge instead of into the worker's empty
    local bus — same ordering as the chat-runs worker.
    """
    xproc.set_role("worker")
    mongo_uri = os.environ.get("CLOUD_MONGODB_URI", "mongodb://localhost:27017/paw-enterprise")
    await init_cloud_db(mongo_uri)
    init_realtime()


async def _shutdown(ctx: dict[str, Any]) -> None:
    await close_cloud_db()


def _redis_settings() -> RedisSettings:
    """Resolve arq RedisSettings from ``POCKETPAW_REDIS_URL``.

    Eager (called at class-body evaluation) because arq's ``get_kwargs`` reads
    ``settings_cls.__dict__`` directly, bypassing the descriptor protocol —
    same rationale as ``chat/runs/worker.py``. Fails loud when the env var is
    missing rather than silently falling back to localhost.
    """
    url = os.environ.get("POCKETPAW_REDIS_URL", "").strip()
    if not url:
        raise RuntimeError("POCKETPAW_REDIS_URL must be set to run the growth arq worker")
    return RedisSettings.from_dsn(url)


class WorkerSettings:
    """arq worker configuration for the ``growth`` queue.

    Loaded by ``arq pocketpaw_ee.cloud.growth.worker.WorkerSettings``.
    """

    queue_name = GROWTH_QUEUE_NAME
    # ``func(..., name=...)`` pins the job's wire name to the explicit dotted
    # constant so the executor's ``enqueue_job(GROWTH_DISPATCH_JOB_NAME, ...)``
    # always matches, independent of the Python function's name.
    functions = [func(dispatch, name=GROWTH_DISPATCH_JOB_NAME)]
    on_startup = _startup
    on_shutdown = _shutdown
    # Crash policy mirrors the chat-runs worker: no auto-retry. Outbound work
    # must never double-send on a retry; failed jobs surface for manual review.
    max_tries = 1
    # Eager: arq reads __dict__, which bypasses descriptors. See `_redis_settings`.
    redis_settings = _redis_settings()


__all__ = ["GROWTH_DISPATCH_JOB_NAME", "GROWTH_QUEUE_NAME", "WorkerSettings", "dispatch"]
