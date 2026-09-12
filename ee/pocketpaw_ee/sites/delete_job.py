# ee/pocketpaw_ee/sites/delete_job.py — the site-delete lane's WORKER SIDE: the arq job
# that carries ONE site from "the owner pressed delete" to "there is no Site document",
# plus the enqueue helper that starts one.
#
# Created 2026-09-12 (sites lifecycle wave 1, feat/sites-delete-endpoint). The two halves
# of this lane already existed and had no way to meet: ``delete_cascade.run_cascade``
# could tear a site down in a survivable order and nothing called it, and
# ``service.create_site_export`` could capture the customer's data and nothing required
# it. This module is what joins them, and it is the FIRST caller either has ever had.
#
# THE FORCED EXPORT IS STEP ZERO OF THIS JOB, AND NOT A PRECONDITION ON THE ENDPOINT.
# That is the load-bearing decision in this file, so the reasoning is here rather than in
# a commit message:
#
#   * The shipped client has an ``exporting`` status (``delete-types.ts``,
#     ``SiteDeleteStatus``) and a failure sentence keyed on the ``export`` STEP
#     (``deleteFailureMessage``'s ``STEP_MESSAGES``). Both are read from the POLL, off
#     ``Site.delete_status`` / ``Site.delete_reason``. A synchronous export inside the
#     DELETE request could never produce either: the status would never be observable,
#     and an export failure would arrive as a 4xx on the delete call instead of as the
#     ``export:<cause>`` reason the client is written to render.
#   * An export PAGES a D1 500 rows at a time and walks every captured lead. On a busy
#     dynamic site that is minutes of work. Holding an HTTP request open for it is the
#     exact thing the 202 contract exists to avoid, and it would die at the proxy long
#     before it died honestly.
#   * What the cascade's header actually demands is that the export resolve "before the
#     cascade is ever enqueued" — i.e. before the first DESTRUCTIVE step runs, because an
#     export taken between steps could fail after something irreversible had already
#     happened. This job satisfies that guarantee exactly: the export completes and is
#     recorded on the row BEFORE ``run_cascade`` is entered. Nothing destructive runs
#     ahead of it.
#
# A STALE EXPORT IS NOT REUSED. If the owner took an export last week, the leads captured
# since then are not in it, and a delete that pointed at it would satisfy the gate while
# destroying data nobody kept. A fresh one is taken on every attempt — EXCEPT on a resume,
# where ``delete_export_id`` already names a ``ready`` row taken by this same delete. That
# is the export's equivalent of the cascade's ledger skip, and it is the only case where
# the existing bytes are provably a copy of the site as this delete found it.
#
# ``max_tries = 1``, LIKE EVERY OTHER LANE IN THIS TREE — and here it is not just house
# style. ``run_cascade`` is resumable from its ledger, but the resume decision belongs to
# a PERSON, not to arq. A cascade that stopped at ``script`` because Cloudflare was
# returning 500s would be re-entered by an automatic retry seconds later, into the same
# outage, spending the ledger's one cheap resume on a failure that has not cleared. The
# row settles at ``failed`` with the step that stopped it, the owner presses delete again,
# and ``claim_delete_queued`` lets them — ``failed`` is deliberately not an in-flight
# status — at which point the cascade skips everything the first attempt finished.
#
# IT RIDES THE SITE-BUILD QUEUE rather than opening a third one. The sites lane
# (``build_worker.WorkerSettings``) is already the consumer of site work, and a third
# arq service cannot be added to the Coolify compose file anyway — a third ``build:``
# blew the SSH argv limit and broke every deploy (paw-workspace #193/#194). A delete is
# bounded work that shares the lane's four slots with builds, which is the right trade:
# the alternative is a queue with no consumer, which is worse than no split at all.

"""The delete job: force the export, run the cascade, remove the row."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import uuid
from typing import Any

from arq import create_pool
from arq.connections import ArqRedis, RedisSettings

from pocketpaw_ee.cloud._core.errors import CloudError
from pocketpaw_ee.sites import service as sites_service
from pocketpaw_ee.sites.build_job import SITE_BUILD_QUEUE_NAME

# ``_classify`` is imported ACROSS the underscore on purpose. ``delete_reason`` has one
# vocabulary of causes, owned by the cascade, and the export step's cause has to be drawn
# from it or an operator grouping failures by cause is grouping two different alphabets.
# Re-implementing the same rule here is how the two would drift.
from pocketpaw_ee.sites.delete_cascade import CascadeStepFailed, _classify, run_cascade

logger = logging.getLogger(__name__)

#: The arq-registered name of the delete job. Pinned explicitly rather than left to
#: ``__qualname__`` so the enqueue and the registration cannot drift apart the way a
#: rename would silently let them.
SITE_DELETE_FUNCTION_NAME = "run_site_delete"

#: The step name a forced-export failure is reported under. It is NOT a member of
#: ``delete_cascade.CASCADE_STEPS`` — the export is not a cascade step, it is the
#: precondition the cascade is gated on — but it shares the ``"<step>:<cause>"`` shape so
#: one consumer parses every ``delete_reason``, and the shipped client keys its most
#: reassuring sentence ("nothing was deleted, your site is untouched") on exactly this
#: token.
STEP_EXPORT = "export"

_DEFAULT_DELETE_TIMEOUT_SECONDS = 900
_TIMEOUT_ENV = "POCKETPAW_SITES_DELETE_TIMEOUT_SEC"


def site_delete_job_timeout_seconds() -> int:
    """The arq ``job_timeout`` for :func:`run_site_delete`, and the window the claim
    measures staleness against.

    Fifteen minutes, well past the design's targets (under a minute for a static site,
    under five for a dynamic one with data) and past the client's own six-minute poll
    ceiling. Sized long deliberately: the budget's job is to release a slot held by a
    genuinely dead worker, and a delete that arq cancels mid-cascade is strictly worse
    than one that takes longer than expected — the ledger records completed steps, but
    the row is left mid-teardown with a live cancellation nobody logged a reason for.

    Same fail-soft contract as the other lanes: an unparseable or non-positive value
    falls back to the default rather than reaching arq, where ``0`` means "no timeout at
    all" and a negative one is nonsense.
    """
    raw = os.environ.get(_TIMEOUT_ENV, "").strip()
    if not raw:
        return _DEFAULT_DELETE_TIMEOUT_SECONDS
    try:
        val = int(raw)
    except ValueError:
        logger.warning(
            "%s=%r is not an int; using default %ds",
            _TIMEOUT_ENV,
            raw,
            _DEFAULT_DELETE_TIMEOUT_SECONDS,
        )
        return _DEFAULT_DELETE_TIMEOUT_SECONDS
    if val <= 0:
        logger.warning(
            "%s=%d is not positive; using default %ds",
            _TIMEOUT_ENV,
            val,
            _DEFAULT_DELETE_TIMEOUT_SECONDS,
        )
        return _DEFAULT_DELETE_TIMEOUT_SECONDS
    return val


class CloudflareUnavailable(Exception):
    """A cascade step needed Cloudflare and this deployment has no client for it.

    Raised rather than skipped. "There is no Cloudflare here" and "there was nothing to
    remove" are different facts, and recording the second for the first is how a cascade
    reports a completed teardown over a Worker that is still serving — the same mistake
    ``_delete_script`` avoids by reading ``deploy_target`` instead of the configured mode.
    """


class _NoCloudflare:
    """Stands in for the client on a deployment that has none, and refuses honestly."""

    def __getattr__(self, name: str) -> Any:
        async def _refuse(*_args: Any, **_kwargs: Any) -> None:
            raise CloudflareUnavailable(name)

        return _refuse


class _DeleteDeps:
    """The side effects ``run_cascade`` calls, resolved once per job.

    Assembled here rather than inside the cascade so the ordering and the ledger stay
    testable without Cloudflare, a bucket, or a database — which is the whole reason
    ``run_cascade`` takes ``deps`` at all.
    """

    def __init__(self, *, cloudflare: Any, assets: Any) -> None:
        self.cloudflare = cloudflare
        self.assets = assets

    async def purge_records(self, *, workspace_id: str, site_id: str) -> None:
        await sites_service.purge_site_records(workspace_id=workspace_id, site_id=site_id)


def _build_deps() -> _DeleteDeps:
    from pocketpaw_ee.sites.public_assets import public_asset_store

    cloudflare: Any
    if sites_service._local_mode():
        cloudflare = _NoCloudflare()
    else:
        try:
            cloudflare = sites_service._cf_client()
        except Exception as exc:  # noqa: BLE001 - an unconfigured deployment, not a bug
            logger.warning("sites.delete: no Cloudflare client available (%s)", exc)
            cloudflare = _NoCloudflare()
    # ``None`` is what the cascade's R2 step reads as "no public rail on this
    # deployment", which is a deployment fact and a legitimate skip. Distinct from an
    # adapter that exists and cannot list, which ``PublicAssetStore.purge`` raises on.
    return _DeleteDeps(cloudflare=cloudflare, assets=public_asset_store())


def _cause_of(exc: Exception) -> str:
    """A short machine token for ``delete_reason``, never raw provider text.

    A ``CloudError`` already carries a curated machine code (``sites.export_unavailable``)
    and that is strictly more useful to an operator than the exception's class name, so it
    is preferred and flattened into the same alphabet. Everything else falls through to
    the cascade's own classifier, which is what keeps one vocabulary across both halves.
    """
    if isinstance(exc, CloudError):
        code = getattr(exc, "code", "") or ""
        flattened = "".join(c.lower() if c.isalnum() else "_" for c in code).strip("_")
        if flattened:
            return flattened
    return _classify(exc)


async def _force_export(site: Any) -> str:
    """Capture the customer's data, and return the export id. Raises if it cannot.

    Resumes rather than repeats: an export already named by ``delete_export_id`` and
    still ``ready`` was taken by THIS delete, before anything was destroyed, so it is a
    faithful copy of the site as the delete found it and re-taking it would only cost a
    second full read of a database that is about to be deleted anyway.

    Every other case takes a fresh one — see the module header on why a pre-existing
    export from last week is not reusable.
    """
    existing_id = (getattr(site, "delete_export_id", "") or "").strip()
    if existing_id and await sites_service.site_export_is_ready(
        workspace_id=site.workspace, export_id=existing_id
    ):
        logger.info(
            "sites.delete: reusing this attempt's export %s for site %s", existing_id, site.id
        )
        return existing_id

    export = await sites_service.create_site_export(
        workspace_id=site.workspace, user_id=site.owner, site_id=str(site.id)
    )
    if export.status != "ready":
        # ``create_site_export`` raises on a failure it detected, so reaching here means
        # a row came back in some other state. Treated as a hard stop: an export that is
        # not ``ready`` has no bytes, and the whole point of the gate is that "we kept a
        # copy" is never assumed.
        raise CascadeStepFailed(STEP_EXPORT, "export_not_ready")
    return export.id


async def run_site_delete(
    ctx: dict[str, Any],
    workspace_id: str,
    site_id: str,
    *,
    _deps: Any = None,
) -> None:
    """arq job: export the site's data, tear it down in order, then delete the row.

    ``ctx`` is the arq worker context and is unused — everything the job needs is in the
    payload, and the workspace rides along so the row can be read back under the tenancy
    it belongs to rather than by id alone.

    A MISSING SITE IS A SUCCESS, NOT AN ERROR. The last thing a completed delete does is
    remove this row, so a job that wakes to find nothing has found its own earlier
    attempt's success — or a row that was already gone. Either way there is nothing left
    to tear down and nothing to record it on.

    Nothing here raises on an operational failure. The row settles at ``failed`` carrying
    the step that stopped it, which is what the client polls for; raising as well would
    only add an arq-level error beside a row that already says what happened.
    """
    site = await sites_service.load_build_site(workspace_id, site_id)
    if site is None:
        # ``load_build_site`` is named for the lane that introduced it; the read it
        # performs — workspace-scoped, ``None`` for a deleted or malformed id — is
        # exactly what this job needs, so it is reused rather than duplicated.
        logger.info("sites.delete: site %s is already gone; nothing to do", site_id)
        return

    # ── The forced export: step zero, before anything destructive ────────────
    await sites_service.mark_delete_stage(site, status="exporting")
    try:
        export_id = await _force_export(site)
    except Exception as exc:  # noqa: BLE001 - classified into the row, never re-raised
        reason = (
            exc.reason if isinstance(exc, CascadeStepFailed) else f"{STEP_EXPORT}:{_cause_of(exc)}"
        )
        logger.warning(
            "sites.delete: export failed for site %s (%s); NOTHING was deleted",
            site_id,
            reason,
            exc_info=True,
        )
        await sites_service.record_delete_failure(site, reason=reason)
        return

    await site.set({"delete_export_id": export_id})
    site.delete_export_id = export_id

    # ── The cascade: everything from here is destructive ─────────────────────
    await sites_service.mark_delete_stage(site, status="tearing_down")
    deps = _deps if _deps is not None else _build_deps()
    try:
        await run_cascade(site=site, deps=deps, save=sites_service.record_delete_progress)
    except CascadeStepFailed as exc:
        logger.warning("sites.delete: cascade stopped for site %s at %s", site_id, exc.reason)
        await sites_service.record_delete_failure(site, reason=exc.reason)
        return
    except Exception as exc:  # noqa: BLE001 - an unclassified stop is still a stop
        # The cascade classifies its own step failures, so reaching here means something
        # outside a step raised — the ledger write itself, most likely. Recorded rather
        # than swallowed: a row left in ``tearing_down`` with no reason makes the client
        # poll a delete nobody is running until its deadline lapses.
        logger.exception("sites.delete: unexpected failure tearing down site %s", site_id)
        await sites_service.record_delete_failure(site, reason=f"cascade:{_cause_of(exc)}")
        return

    # ── The row, last ────────────────────────────────────────────────────────
    # Deleting this is the success signal: the status endpoint 404s from here, which is
    # what the client reads as "it finished". See ``service.delete_site_document``.
    await sites_service.delete_site_document(site)
    logger.info("sites.delete: site %s deleted (export %s)", site_id, export_id)


# ---------------------------------------------------------------------------
# The enqueue
# ---------------------------------------------------------------------------

_pool: ArqRedis | None = None
_pool_lock = asyncio.Lock()


async def _get_pool() -> ArqRedis:
    """The process's arq pool — the same lazy double-checked pattern the build lane
    uses, for the same reason: one pool per process, and concurrent first-enqueues must
    not leak two."""
    global _pool
    if _pool is None:
        async with _pool_lock:
            if _pool is None:
                url = os.environ.get("POCKETPAW_REDIS_URL", "").strip()
                if not url:
                    raise RuntimeError(
                        "POCKETPAW_REDIS_URL is not set — the site-delete lane needs Redis."
                    )
                _pool = await create_pool(RedisSettings.from_dsn(url))
    return _pool


def _reset_for_tests() -> None:
    global _pool
    _pool = None


def _mint_job_id(site_id: str) -> str:
    """A unique arq job id, minted BEFORE the enqueue so it can be persisted with the
    queued stamp in one write.

    Uuid-tailed rather than deterministic, for the reason the build lane documents: arq
    refuses an enqueue whose id already holds a job OR a RESULT, and results outlive the
    job by ``keep_result``. A stable ``site-delete-<id>`` would therefore refuse the
    RETRY of a failed delete for an hour after it failed — a single-flight guard nobody
    asked for, enforced in the wrong layer, and invisible because arq's refusal is a
    ``None`` return rather than an error. The real single-flight guard is the conditional
    claim below.
    """
    return f"site-delete-{site_id}-{uuid.uuid4().hex}"


async def enqueue_site_delete(site: Any, *, _pool_override: Any = None) -> str | None:
    """Queue the delete of ``site``; return the job id, or ``None`` when one is in flight.

    The workspace and site id are read OFF THE DOC rather than taken as parameters, which
    is a tenancy decision and not a convenience: a caller that could pass a workspace
    alongside a doc could pass a mismatched pair, and the job would then destroy a site
    under the workspace it was told rather than the one the row belongs to.

    Order, and why:

      1. CLAIM the slot — stamp ``queued`` + the clock + the job id in ONE CONDITIONAL
         write, BEFORE the enqueue. Conditional because a read-then-write gate loses the
         race the build lane measured at 8 sandboxes for 8 concurrent publishes; here the
         same race is two workers running two cascades over one site, which is worse than
         a wasted sandbox. Before the enqueue, because a worker that picked the job up
         first would write a real status and then have this stamp land on top of it,
         pinning a finished delete in ``queued`` forever.
      2. ENQUEUE. On ANY failure — dead Redis, or arq refusing the id — settle the row at
         ``failed`` and re-raise. Skipping that rollback is what pins a row in ``queued``
         behind a job that never existed, and the claim would then refuse every delete of
         this site until the staleness window lapsed.

    ``None`` is not an error. It means another attempt owns the slot, and the caller
    reports that attempt rather than starting a second teardown of one site.
    """
    workspace_id = site.workspace
    site_id = str(site.id)
    timeout = site_delete_job_timeout_seconds()

    job_id = _mint_job_id(site_id)
    claimed = await sites_service.claim_delete_queued(site, job_id=job_id, timeout_seconds=timeout)
    if not claimed:
        logger.info(
            "sites.delete: site %s already has a delete in flight (%s) — not enqueueing",
            site_id,
            getattr(site, "delete_status", None),
        )
        return None

    try:
        pool = _pool_override or await _get_pool()
        job = await pool.enqueue_job(
            SITE_DELETE_FUNCTION_NAME,
            workspace_id,
            site_id,
            _job_id=job_id,
            _queue_name=SITE_BUILD_QUEUE_NAME,
        )
        if job is None:
            # arq answers None when the id already exists. With a uuid tail that should be
            # impossible, so it is a failed enqueue rather than evidence a delete is
            # coming — a row left in ``queued`` for a job nobody will run is exactly what
            # the rollback below exists to prevent.
            raise RuntimeError(f"arq refused job id {job_id!r} — a job with that id exists")
    except Exception:
        logger.exception("sites.delete: enqueue failed for site %s", site_id)
        # Best-effort and separately suppressed: the raise below is what the caller acts
        # on, and a failed rollback must not replace it with a second, less useful error.
        with contextlib.suppress(Exception):
            await sites_service.record_delete_failure(site, reason="enqueue:pool_or_enqueue_raised")
        raise

    logger.info("sites.delete: queued delete %s for site %s (%ds budget)", job_id, site_id, timeout)
    return job_id


__all__ = [
    "SITE_DELETE_FUNCTION_NAME",
    "STEP_EXPORT",
    "CloudflareUnavailable",
    "enqueue_site_delete",
    "run_site_delete",
    "site_delete_job_timeout_seconds",
]
