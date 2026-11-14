# ee/pocketpaw_ee/cloud/growth/worker.py — arq worker for the dedicated
# ``growth`` queue.
#
# One job and six crons. ``growth.dispatch`` delivers a draft whose
# ``_growth_send`` proposal a human approved (the approval is the only producer
# of the job): email through ``email_dispatch``, WhatsApp through ``whatsapp``
# (hard opt-in guard and hourly cap), LinkedIn stays manual and only logs. The
# crons, all ``unique=True`` so a scaled fleet runs each tick once, in UTC:
# 05:00 the Pulse refresh, 05:30 the Growth › Social daily X drafts (drafts
# only, never posts), 06:00 discovery, 06:30 the daily copy of each product
# repo's stars and traffic, 07:00 the Growth › Social post metrics (reads
# Postiz and X, never writes to X), 13:00 the follow-up sweep (proposes, never
# sends). The worker process never runs ``cloud.__init__``'s wiring, so the
# Pulse, daily-drafts, repo-metrics and post-metrics jobs install their
# production fns when missing.
#
# Growth gets its OWN queue and worker process so a burst of outbound work can
# never starve interactive chat runs. Enqueue with
# ``_queue_name=GROWTH_QUEUE_NAME`` (arq's selector is the underscore-prefixed
# kwarg; a bare ``queue=`` is forwarded to the job function and crashes it).
#
# Deploy as a separate process alongside the web service::
#
#     arq pocketpaw_ee.cloud.growth.worker.WorkerSettings
#
# Startup pins the xproc role BEFORE anything emits, then inits the cloud DB and
# realtime bus. ``redis_settings`` is evaluated eagerly at class-body time
# because arq reads ``settings_cls.__dict__`` directly (bypassing descriptors);
# tests set a stub ``POCKETPAW_REDIS_URL`` in ``tests/cloud/conftest.py``.

"""arq worker entry point for the dedicated ``growth`` queue."""

from __future__ import annotations

import logging
import os
from typing import Any

from arq import cron
from arq.connections import RedisSettings
from arq.worker import func

from pocketpaw_ee.cloud import init_realtime
from pocketpaw_ee.cloud._core.realtime import xproc
from pocketpaw_ee.cloud.growth.discovery import discovery_sweep
from pocketpaw_ee.cloud.growth.domain import (
    GROWTH_DISCOVERY_SWEEP_JOB_NAME,
    GROWTH_DISPATCH_JOB_NAME,
    GROWTH_PULSE_SWEEP_JOB_NAME,
    GROWTH_QUEUE_NAME,
    GROWTH_SOCIAL_DAILY_DRAFTS_JOB_NAME,
    GROWTH_SOCIAL_POST_METRICS_JOB_NAME,
    GROWTH_SOCIAL_REPO_METRICS_JOB_NAME,
)
from pocketpaw_ee.cloud.growth.followups import (
    GROWTH_FOLLOWUP_SWEEP_JOB_NAME,
    followup_sweep,
)
from pocketpaw_ee.cloud.shared.db import close_cloud_db, init_cloud_db


async def pulse_sweep(ctx: dict[str, Any]) -> dict[str, int]:
    """Refresh the Pulses people used this week (Growth › Social and hunt market pulses).

    The worker process never runs ``cloud.__init__``'s wiring, so the production Pulse fn is
    installed here when missing; otherwise the sweep would be a silent no-op."""
    from pocketpaw_ee.cloud.growth.social import pulse
    from pocketpaw_ee.cloud.growth.social.service import refresh_due_pulses

    if pulse.resolve_pulse_fn() is None:
        pulse.set_production_pulse_fn(pulse.agent_build_pulse)
    return await refresh_due_pulses()


async def social_daily_drafts(ctx: dict[str, Any]) -> dict[str, int]:
    """Draft today's X posts for every active Growth › Social product. Drafts only.

    Installs the production ideas and GitHub changes fns when missing, like ``pulse_sweep``."""
    from pocketpaw_ee.cloud.growth.social import changes, ideas
    from pocketpaw_ee.cloud.growth.social.service import run_daily_drafts

    if ideas.resolve_ideas_fn() is None:
        ideas.set_production_ideas_fn(ideas.agent_generate_ideas)
    if changes.resolve_changes_fn() is None:
        changes.set_production_changes_fn(changes.github_changes)
    return await run_daily_drafts()


async def social_repo_metrics(ctx: dict[str, Any]) -> dict[str, int]:
    """Store yesterday's stars, forks and traffic for every active product's repos.

    Installs the production GitHub reader when missing, like ``pulse_sweep``."""
    from pocketpaw_ee.cloud.growth.social import repo_metrics
    from pocketpaw_ee.cloud.growth.social.service import run_repo_metrics

    if repo_metrics.resolve_repo_metrics_fn() is None:
        repo_metrics.set_production_repo_metrics_fn(repo_metrics.github_repo_metrics)
    return await run_repo_metrics()


async def social_post_metrics(ctx: dict[str, Any]) -> dict[str, int]:
    """Record published X posts and snapshot their metrics. Reads only, never posts.

    Installs the production metrics fn when missing, like ``pulse_sweep``."""
    from pocketpaw_ee.cloud.growth.social import metrics
    from pocketpaw_ee.cloud.growth.social.service import run_post_metrics

    if metrics.resolve_metrics_fn() is None:
        metrics.set_production_metrics_fn(metrics.x_post_metrics)
    return await run_post_metrics()


logger = logging.getLogger(__name__)

# 30 minutes. arq's own default is 300s, which cancels the discovery sweep
# after the first two or three ICPs — see the note on ``job_timeout`` below.
_DEFAULT_JOB_TIMEOUT_SECONDS = 1800

# One template/email POST. Unchanged from arq's default on purpose.
_DISPATCH_TIMEOUT_SECONDS = 300


async def dispatch(ctx: dict[str, Any], draft_id: str, channel: str) -> None:
    """Dispatch an APPROVED outbound draft.

    This job is enqueued ONLY by ``executor.execute_approved_growth_send``
    after a human approved the draft's ``_growth_send`` Instinct proposal —
    there is no other producer, so a job here IS the approval record's
    downstream. Each channel owns its own delivery module and re-checks that
    the draft is still ``approved`` before anything reaches a provider.

    ``email`` is live (G-5) and ``whatsapp`` is live (G-6). WhatsApp
    additionally enforces the hard opt-in guard, the hourly rate cap, and
    RAISES on every refusal (``max_tries = 1``, so the job lands as a failed
    job an operator can see) while writing its own compliance row — a refused
    send is never a silent success.

    ``linkedin`` keeps the stub on purpose: it is manual by design, sent by
    hand from G-8's queue and recorded through the mark-sent route.
    """
    if channel == "email":
        # Lazy import — keeps the arq worker's boot free of the HTTP client
        # stack until an email job actually lands.
        from pocketpaw_ee.cloud.growth.email_dispatch import dispatch_email

        await dispatch_email(draft_id)
        return

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


def _job_timeout_seconds() -> int | None:
    """Per-job ceiling for the growth queue, in seconds.

    Default 30 minutes: the discovery sweep works ICPs sequentially and each
    one is a real agent run. Override with ``GROWTH_JOB_TIMEOUT_SECONDS`` when
    a deployment runs more hunts than that comfortably fits.
    """
    # ``0`` DISABLES the ceiling, matching this package's own convention
    # (``discovery.py``'s monthly max, and the agent-jail knobs in CLAUDE.md).
    # An earlier version ran it through ``max(int(raw), 60)``, so an operator
    # setting 0 to mean "no limit" got 60s — a 30x TIGHTENING, and one whose
    # cancellations are invisible because CancelledError is a BaseException.
    raw = os.environ.get("GROWTH_JOB_TIMEOUT_SECONDS", "").strip()
    if raw:
        try:
            parsed = int(raw)
            if parsed <= 0:
                return None
            return max(parsed, 60)
        except ValueError:
            logger.warning(
                "growth worker: GROWTH_JOB_TIMEOUT_SECONDS=%r is not an integer — using %d",
                raw,
                _DEFAULT_JOB_TIMEOUT_SECONDS,
            )
    return _DEFAULT_JOB_TIMEOUT_SECONDS


class WorkerSettings:
    """arq worker configuration for the ``growth`` queue.

    Loaded by ``arq pocketpaw_ee.cloud.growth.worker.WorkerSettings``.
    """

    queue_name = GROWTH_QUEUE_NAME
    # ``func(..., name=...)`` pins the job's wire name to the explicit dotted
    # constant so the executor's ``enqueue_job(GROWTH_DISPATCH_JOB_NAME, ...)``
    # always matches, independent of the Python function's name.
    # The dispatch job keeps arq's 300s. The long ``job_timeout`` below exists
    # for the discovery sweep, which works ICPs sequentially; applying it here
    # too would let ten hung provider sockets hold every worker slot for half
    # an hour with human-approved outbound queued behind them, and max_tries=1
    # means no recovery.
    functions = [func(dispatch, name=GROWTH_DISPATCH_JOB_NAME, timeout=_DISPATCH_TIMEOUT_SECONDS)]
    # G-7 — one daily tick at 13:00 UTC (business hours across US/EU, and well
    # clear of the midnight batch traffic every other scheduler piles onto).
    # ``unique=True`` (arq's default, explicit here because it is the property
    # that matters) means a horizontally-scaled growth worker fleet still runs
    # the sweep exactly once per tick. The sweep only ever PROPOSES — it
    # cannot send, so a duplicate tick would at worst be a no-op anyway (the
    # open-follow-up guard makes it idempotent).
    cron_jobs = [
        cron(
            followup_sweep,
            name=GROWTH_FOLLOWUP_SWEEP_JOB_NAME,
            hour=13,
            minute=0,
            unique=True,
            run_at_startup=False,
        ),
        # G-13 — discovery runs at 06:00 UTC, seven hours AHEAD of the
        # follow-up sweep rather than near it. Two reasons, in order of
        # importance. First, they must not contend: discovery is the expensive
        # tick (an agent research run per due ICP) and the follow-up sweep is
        # the time-sensitive one, so stacking them would let a slow hunt delay
        # outreach that is already overdue. Second, discovery FEEDS the list a
        # human then triages — landing it at the start of the working day in
        # Europe and India means the morning's finds are waiting when someone
        # sits down, instead of arriving mid-afternoon behind the day's other
        # work. ``unique=True`` for the same reason as above: a scaled worker
        # fleet must run one sweep per tick, and discovery WRITES prospects, so
        # a duplicate tick would double-file rather than no-op.
        cron(
            discovery_sweep,
            name=GROWTH_DISCOVERY_SWEEP_JOB_NAME,
            hour=6,
            minute=0,
            unique=True,
            run_at_startup=False,
        ),
        # The daily Pulse refresh at 05:00 UTC, an hour ahead of discovery so a hunt's market
        # pulse is fresh when its prospects land. Only Pulses a person refreshed this week.
        cron(
            pulse_sweep,
            name=GROWTH_PULSE_SWEEP_JOB_NAME,
            hour=5,
            minute=0,
            unique=True,
            run_at_startup=False,
        ),
        # Yesterday's repo stars and traffic at 06:30 UTC (GitHub keeps traffic 14 days only).
        cron(
            social_repo_metrics,
            name=GROWTH_SOCIAL_REPO_METRICS_JOB_NAME,
            hour=6,
            minute=30,
            unique=True,
            run_at_startup=False,
        ),
        # Post metrics at 07:00 UTC: record what got published, snapshot X metrics (reads only).
        cron(
            social_post_metrics,
            name=GROWTH_SOCIAL_POST_METRICS_JOB_NAME,
            hour=7,
            minute=0,
            unique=True,
            run_at_startup=False,
        ),
        # The daily X drafts at 05:30 UTC, after the Pulse refresh so drafts read a fresh Pulse.
        cron(
            social_daily_drafts,
            name=GROWTH_SOCIAL_DAILY_DRAFTS_JOB_NAME,
            hour=5,
            minute=30,
            unique=True,
            run_at_startup=False,
        ),
    ]
    # arq's default job_timeout is 300s, which is far too short for the
    # discovery sweep: each ICP awaits a full WebSearch/WebFetch agent run, so
    # a fleet of tenants is cancelled mid-loop after the first two or three.
    # Worse, the cancellation is invisible — CancelledError is a BaseException,
    # so none of the sweep's careful `except Exception` handling catches it,
    # the closing summary log never fires, and max_tries=1 means no retry. The
    # chat-runs worker already learned this (POCKETPAW_CLOUD_RUN_JOB_TIMEOUT
    # exists for the same reason); this is the growth queue's version.
    job_timeout = _job_timeout_seconds()

    on_startup = _startup
    on_shutdown = _shutdown
    # Crash policy mirrors the chat-runs worker: no auto-retry. Outbound work
    # must never double-send on a retry; failed jobs surface for manual review.
    max_tries = 1
    # Eager: arq reads __dict__, which bypasses descriptors. See `_redis_settings`.
    redis_settings = _redis_settings()


__all__ = [
    "GROWTH_DISPATCH_JOB_NAME",
    "GROWTH_FOLLOWUP_SWEEP_JOB_NAME",
    "GROWTH_QUEUE_NAME",
    "WorkerSettings",
    "dispatch",
    "followup_sweep",
]
