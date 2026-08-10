# ee/pocketpaw_ee/sites/build_job.py — the ephemeral-build lane's WORKER SIDE: the arq
# job that carries ONE site build from scaffold to verified artifact, plus the enqueue
# helper that starts one.
#
# Created 2026-08-10 (SL-2 slice 2). The lane's two halves already existed and had no
# way to meet: ``daytona_runner.run_build`` could produce a verdict and nothing consumed
# it, ``build_state`` could decide what to write and nothing called it. This is the
# module that joins them, and it is the FIRST caller ``run_build`` has ever had.
#
# WHAT THIS DELIBERATELY DOES NOT DO: change the publish path. ``service.publish`` still
# builds synchronously through the local generator. Flipping it to enqueue-and-return is
# a separate slice gated on a frontend that can render a queued build — and shipping the
# flip before that frontend exists would show every publisher a finished-looking page
# for a site that has not built yet. So this module is complete and callable, and the
# only thing that calls it today is its tests.
#
# ┌───────────────────────────────────────────────────────────────────────────────────┐
# │ WHY A DEDICATED ARQ FUNCTION AND NOT THE WORKSPACE-JOBS REGISTRY.                  │
# └───────────────────────────────────────────────────────────────────────────────────┘
#
# The registry route (``jobs.service.dispatch_job`` → ``execute_workspace_job`` →
# ``resolve_job``) is the established way to run durable work here, and the D1 provision
# job rides it. Three facts ruled it out for a build, in increasing order of weight:
#
#   1. ITS RESULT CONTRACT IS A POCKET-SPEC WRITEBACK. ``execute_workspace_job`` passes
#      a job's return through ``validate_job_result`` (state-only) and merges it into the
#      POCKET's rippleSpec, then stamps ``<action>_status``. A build's outcome belongs on
#      the SITE row (``build_status`` / ``build_reason``); riding the registry would mean
#      inventing a meaningless pocket partial to satisfy a contract we do not want.
#
#   2. ITS FAILURE PATH DELIBERATELY DESTROYS THE REASON. ``_safe_failure_message``
#      collapses any non-``CloudError`` raise to the fixed string ``"job failed"`` —
#      correct there, because a workspace-custom job's exception text is untrusted. But
#      SL-2 exists precisely to record WHICH rung failed, and a generic message is the
#      unactionable ``failed`` the whole slice is meant to eliminate.
#
#   3. THE TIMEOUT WOULD BE WRONG, AND IT IS ALREADY WRONG AT THE DEFAULTS. Every
#      registry job shares ONE budget: ``jobs.domain.job_timeout_seconds()``
#      (``POCKETPAW_JOB_TIMEOUT_SECONDS``, default 900s). A build needs
#      :func:`site_build_job_timeout_seconds` — the widest per-engine build budget plus
#      ``run_build``'s own exec slack plus the phases that sit outside the sandbox —
#      which is 1020s at today's defaults, i.e. ALREADY over the shared 900s. An arq
#      cancellation at 900s kills the job before the in-sandbox ``timeout(1)`` fires, so
#      the sentinel is never written and a healthy-but-slow build classifies as
#      ``infra_lost``: exactly the mis-report ``daytona_build``'s evidence design exists
#      to prevent. A dedicated ``arq.worker.func(timeout=...)`` derived from the same env
#      knob keeps the two coupled, which no registry job can do.
#
# It registers into the SAME worker process (``chat/runs/worker.py`` ``WorkerSettings``),
# which is the one arq entrypoint that is actually deployed — ``docs/internal/
# 2026-05-resumable-runs-deploy.md`` gives the start command and the dynamic-sites
# runbook confirms the provision job rides it. ``cloud/jobs/worker.py`` is not a second
# worker; it is the registry's entrypoint function, registered into that same settings
# class. So this costs no new deploy artifact, exactly like workspace jobs.
#
# ── THE ORDER OF THE BODY IS THE CONTRACT ───────────────────────────────────────────
#
#   1. SCAFFOLD LOCALLY (``_runner.generate``). Cheap, offline, no install — and doing
#      it here rather than in the sandbox means a scaffold failure costs no sandbox.
#   2. READ THE TREE into the ``{path: contents}`` map ``run_build`` uploads.
#   3. BUILD IN A FRESH SANDBOX (``run_build``), which installs, builds, tars, and
#      classifies against the sentinel.
#   4. GATE ON ``BuildRunResult.ok`` — never on ``classification.deployable``. That flag
#      is derived from the sentinel alone and stays True when the download returns zero
#      bytes, so gating on it would deploy nothing over something that was working. This
#      is wiring contract #1 from the SG-7 fault-ladder findings, discharged here.
#   5. SETTLE via ``build_state.settle`` and persist ``build_status`` + ``build_reason``.
#
# A THROWAWAY TEMP DIR, NOT THE PER-POCKET BUILD HOME. ``GeneratorClient`` scaffolds into
# a stable ``build_home()/<pocket_id>/`` so ``bun install`` caches (PERF-3). This lane
# must not: that directory carries the PREVIOUS build's ``node_modules`` and
# ``.svelte-kit``, and step 2 uploads what step 1 leaves on disk — so the cache would put
# a stale artifact and a 500 MB dependency tree on the wire. Its per-pocket lock is also
# per-PROCESS, and the worker is a different process from the web app, so the lock does
# not serialise us against a concurrent local build anyway. The install we pay for lives
# in the sandbox; there is nothing here worth caching.
#
# ``building`` COVERS THE SCAFFOLD, not just the sandbox. ``build_state``'s vocabulary
# says ``building`` means "a sandbox exists", and strictly the scaffold happens before
# one does. The row flips on CONSUMPTION instead, because the distinction that earns a
# state is "is this still waiting in the queue" — that is what ``queued`` was added for —
# and a fourth state for a step measured in single-digit seconds would buy a UI nothing.
# The staleness window covers the scaffold either way.
#
# ┌───────────────────────────────────────────────────────────────────────────────────┐
# │ ``build_reason`` IS ALWAYS ``"<rung>:<cause>"`` AND NEVER RAW STDERR.              │
# └───────────────────────────────────────────────────────────────────────────────────┘
#
# Both halves come from closed sets: the rung is a ``BuildOutcome`` (or one of the
# pre-sandbox rungs below), and the cause is ``BuildClassification.reason``, which the
# F7 ladder already proves is a lowercase machine-readable identifier for every
# condition the classifier can reach. ONE format rather than "rung, plus a cause when
# the user is to blame" so a consumer parses one shape.
#
# The stderr tail is NEVER one of those halves. A build's error text is the user's own
# code and can carry anything a config file can — a pasted token, an absolute path, a
# customer's source. It goes to the log, which is where an operator needs it, and the
# row carries only the fixed name. Same reasoning as ``jobs/worker.py``'s
# ``_safe_failure_message``, reached from the opposite direction: it had to throw the
# detail away, this has a bounded vocabulary to keep instead.
#
# ┌───────────────────────────────────────────────────────────────────────────────────┐
# │ THE PER-SITE CAPTURE KEY IS SCRUBBED BEFORE THE INPUT LEAVES THIS PROCESS.         │
# └───────────────────────────────────────────────────────────────────────────────────┘
#
# ``daytona_runner``'s header records this as an obligation owed by whoever adds the
# lane's first caller — which is this module. A svelte scaffold substitutes the real
# per-site ``captureSignedKey`` into ``src/routes/api/submit/+server.ts``, and a canary
# build found it there and in the compiled server bundle. The decision that the key's
# exposure is acceptable rests entirely on it living only in a container that is then
# destroyed; uploading it into a third-party sandbox (and, before that, into a Redis
# payload) is a different question than the one this lane was cleared for.
#
# So :func:`scrub_build_input` blanks it, in BOTH the enqueue helper (so it never enters
# Redis) and the job (so a direct caller cannot skip the scrub). This costs nothing that
# is served: the compiled server route is absent from the deployable artifact on the
# svelte track, and react emits no server route at all. If lead capture comes back, the
# key must be injected at DEPLOY (wrangler ``[vars]``) rather than restored here.
"""SL-2 — the site-build arq job and its enqueue helper."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from arq import create_pool
from arq.connections import ArqRedis, RedisSettings

from pocketpaw_ee.sites import service as sites_service
from pocketpaw_ee.sites.build_state import BuildStatus, settle, should_enqueue
from pocketpaw_ee.sites.daytona_build import resolve_build_timeout_seconds
from pocketpaw_ee.sites.daytona_runner import (
    EXEC_TIMEOUT_SLACK_SECONDS,
    BuildRunResult,
    run_build,
)
from pocketpaw_ee.sites.engines import needs_node_build, normalize_engine, static_output_rel

logger = logging.getLogger(__name__)

#: The name the enqueue writes and ``WorkerSettings`` registers. Pinned as a constant
#: rather than left to ``__qualname__`` for the same reason ``execute_workspace_job``
#: pins its own: an enqueue name and a registration name that drift produce a job that
#: sits in Redis forever with no worker willing to claim it, and no error anywhere.
ARQ_FUNCTION_NAME = "run_site_build"

#: Engines this lane can build. Every one needs a per-site Node build AND emits its
#: output into a SUBDIRECTORY — ``artifact_tar_command`` refuses an engine whose output
#: is the project root, because an include-list cannot then exclude ``node_modules``.
#: html satisfies neither and needs no build at all, so it never belongs here.
#: ``tests/ee/sites/test_build_job.py`` fails if ``engines.py`` gains a buildable engine
#: this tuple misses, which is the drift a hand-written list would otherwise hide.
BUILDABLE_ENGINES: tuple[str, ...] = ("ripple", "svelte", "react")

#: Added to the widest in-sandbox budget (plus ``run_build``'s own exec slack) to size
#: the arq function timeout. Covers everything OUTSIDE the sandbox's clock: the local
#: scaffold, sandbox create + wait, the upload, the artifact download, teardown.
#: Measured overhead is ~5s per build (react: 8.7s total against a 2.9s build), so this
#: is generous by two orders of magnitude on purpose — the same posture as
#: ``build_state.STALE_MARGIN``. An arq timeout's job is to reap a wedged job, and if it
#: fires first the sentinel is lost and a real verdict becomes ``infra_lost``.
OUT_OF_SANDBOX_MARGIN_SECONDS = 300

#: Directory names pruned from the uploaded tree. NOT belt-and-braces: a scaffold shares
#: a filesystem with whatever else wrote there, and both of these are the kind of thing
#: that turns a 200 KB upload into a 500 MB one (``node_modules``) or leaks history into
#: a sandbox (``.git``). Pruned during the walk rather than filtered after it, so a large
#: tree costs nothing to skip. The sandbox runs its own install; there is nothing here we
#: want to carry over.
SKIPPED_TREE_DIRS: frozenset[str] = frozenset({"node_modules", ".git"})

#: The generator-input key holding the per-site capture secret — see the module header
#: for why it never reaches a sandbox.
_SECRET_INPUT_KEYS: tuple[str, ...] = ("captureSignedKey",)

# ---------------------------------------------------------------------------
# Rungs this job reaches on its OWN, before the lane's classifier sees anything
# ---------------------------------------------------------------------------
#
# Deliberately NOT folded into ``BuildOutcome``. Those four describe what happened to a
# build that ran; these describe why one never did, and giving them borrowed names would
# make ``infra_lost`` mean both "we lost the container" (retry it) and "the engine cannot
# build here" (a routing bug — retrying it forever is the harm).

#: The engine cannot build in this lane at all. A routing bug, not a build failure.
RUNG_ENGINE_NOT_BUILDABLE = "engine_not_buildable"
#: The local scaffold raised. Nothing was billed and no sandbox was created.
RUNG_SCAFFOLD_FAILED = "scaffold_failed"
#: The scaffold reported success and wrote nothing. The empty-deploy failure, caught one
#: step earlier than ``artifact_empty`` catches it — before a sandbox exists to pay for.
RUNG_SCAFFOLD_EMPTY = "scaffold_empty"
#: ``run_build`` raised rather than returning a verdict: Daytona unconfigured, or the
#: sandbox could not be created. Nothing ran, so it is ours and retryable.
RUNG_SANDBOX_UNAVAILABLE = "sandbox_unavailable"
#: The classification cleared the build and the download delivered no bytes. Distinct
#: from ``artifact_empty`` (which the sentinel catches) because this one is a property of
#: the TRANSFER, so it is worth another attempt.
RUNG_ARTIFACT_MISSING = "artifact_missing"
#: The enqueue itself failed after the row was already stamped ``queued``. Written by the
#: enqueue helper so the row lands TERMINAL instead of pinned in flight.
RUNG_ENQUEUE_FAILED = "enqueue_failed"


@dataclass(frozen=True)
class BuildSettlement:
    """What one attempt's verdict writes to the Site row.

    ``status`` is ``None`` when the attempt must STAY IN FLIGHT (a retryable rung with
    attempts left) — ``settle``'s contract, carried through unchanged, because writing a
    terminal status between attempts invites a second sandbox on top of the retry.
    ``reason`` is still populated in that case: it is what a log line needs even when
    nothing is persisted.
    """

    status: BuildStatus | None
    reason: str


def _settlement(
    rung: str,
    cause: str,
    *,
    retryable: bool,
    attempts_left: int,
) -> BuildSettlement:
    """Pair a rung with the status ``settle`` says it should leave on the row."""
    return BuildSettlement(
        status=settle(rung, retryable=retryable, attempts_left=attempts_left),
        reason=f"{rung}:{cause}",
    )


def resolve_build_settlement(result: BuildRunResult, *, attempts_left: int = 0) -> BuildSettlement:
    """Turn a finished ``run_build`` into the status + reason to persist.

    GATES ON ``result.ok``, NOT on ``result.classification.deployable``. The two disagree
    in exactly one case and it is the dangerous one: the sentinel promised bytes and the
    download delivered none, so ``deployable`` still reads True while ``ok`` reads False.
    A caller that trusted ``deployable`` would settle that row as ``built`` and hand a
    zero-byte artifact to the deploy, replacing a working site with a blank one.

    That case gets its OWN rung rather than the classifier's ``completed_ok``, because
    "the build worked and the bytes did not arrive" is retryable while ``completed_ok``
    is not — and a rung that lies about retryability either burns a publish that a second
    attempt would have fixed, or retries something no attempt can fix.

    Truncation — a payload that arrives SHORTER than the sentinel's ``artifact_bytes`` —
    is NOT distinguished here. That needs the promised size threaded off the sentinel,
    which is banked on ``spike/sites-artifact-verification`` along with the rest of the
    four-way artifact classification. Until it lands, a truncated download settles as
    ``built``; the gap is real, is not new, and is recorded rather than papered over.
    """
    classification = result.classification
    if result.ok:
        return _settlement(
            classification.outcome,
            classification.reason,
            retryable=classification.retryable,
            attempts_left=attempts_left,
        )
    if classification.deployable:
        return _settlement(
            RUNG_ARTIFACT_MISSING,
            "download_delivered_no_bytes",
            retryable=True,
            attempts_left=attempts_left,
        )
    return _settlement(
        classification.outcome,
        classification.reason,
        retryable=classification.retryable,
        attempts_left=attempts_left,
    )


def site_build_job_timeout_seconds() -> int:
    """The arq ``job_timeout`` for :func:`run_site_build`.

    The WIDEST engine's budget, not a per-call one: ``arq.worker.func`` takes a single
    number evaluated once at worker import, so a value sized on the narrowest engine
    would cancel the widest engine's healthy builds. See the module header for why this
    must not be the shared workspace-jobs timeout.

    Reads the same env knobs ``resolve_build_timeout_seconds`` reads, so an operator who
    lengthens a slow engine's build lengthens this too — on the next worker restart,
    which a deploy does anyway.
    """
    widest = max(resolve_build_timeout_seconds(engine) for engine in BUILDABLE_ENGINES)
    return widest + EXEC_TIMEOUT_SLACK_SECONDS + OUT_OF_SANDBOX_MARGIN_SECONDS


def is_buildable_engine(engine: str | None) -> bool:
    """Can this lane build ``engine`` at all?

    Two independent conditions, both from ``engines.py``: the engine must need a Node
    build, and its output must land in a subdirectory. An engine failing either has no
    business reaching a sandbox — ``artifact_tar_command`` would refuse it anyway, but it
    would refuse it after the job had already been consumed and the row moved to
    ``building``, so the check belongs before any of that.
    """
    return needs_node_build(engine) and static_output_rel(engine) != "."


def scrub_build_input(generator_input: dict[str, Any]) -> dict[str, Any]:
    """A copy of ``generator_input`` with the per-site capture secret blanked.

    See the module header for the full reasoning. Returns a NEW dict and never mutates
    the caller's — a publish holds the same input for its own local path, and quietly
    blanking a shared dict would strip the key from a build that is entitled to it.

    Only ``siteConfig`` is copied, because it is the only key mutated. A key the caller
    never set stays unset, so this does not add ``captureSignedKey: ""`` to an input that
    had no ``siteConfig`` at all.
    """
    raw_config = generator_input.get("siteConfig")
    if not isinstance(raw_config, dict):
        return dict(generator_input)
    site_config = dict(raw_config)
    for key in _SECRET_INPUT_KEYS:
        if site_config.get(key):
            site_config[key] = ""
    scrubbed = dict(generator_input)
    scrubbed["siteConfig"] = site_config
    return scrubbed


def read_generated_tree(project_dir: str) -> dict[str, str | bytes]:
    """Read a scaffolded project into the ``{relative_path: contents}`` map to upload.

    Bytes, never decoded text: a scaffold carries binary members (an imported site's
    assets, a lockfile), and ``run_build`` accepts either — so decoding would only create
    a way to fail on a file we do not need to read.

    Paths are POSIX-relative because they are joined onto the sandbox's project dir.
    A Windows-separated key would land as a single file with backslashes in its name and
    the build would fail looking for a directory that was never created.

    Symlinks are skipped rather than followed: the target is a file this function never
    inspected, and uploading it would put something outside the scaffold into the sandbox.
    """
    root = Path(project_dir)
    files: dict[str, str | bytes] = {}
    for current, dirnames, filenames in os.walk(root):
        # Prune, don't filter — descending into a cached node_modules to discard it is
        # the cost this exists to avoid.
        dirnames[:] = [name for name in dirnames if name not in SKIPPED_TREE_DIRS]
        for name in filenames:
            path = Path(current, name)
            if path.is_symlink() or not path.is_file():
                continue
            files[path.relative_to(root).as_posix()] = path.read_bytes()
    return files


# ---------------------------------------------------------------------------
# The job
# ---------------------------------------------------------------------------


async def run_site_build(
    ctx: dict[str, Any],
    workspace_id: str,
    site_id: str,
    generator_input: dict[str, Any],
    engine: str,
    timeout_seconds: int,
    *,
    attempts_left: int = 0,
    _runner: Any = None,
    _client: Any = None,
) -> None:
    """arq job: scaffold, build in an ephemeral sandbox, and record the outcome.

    ``ctx`` is the arq worker context and is unused — every input the build needs is in
    the payload. ``timeout_seconds`` is passed rather than re-derived so the budget the
    enqueue measured its staleness window against is the SAME number the sandbox is held
    to; two independent reads of an env var can disagree across a config change and leave
    a live build outside its own window.

    ``attempts_left`` is what the caller has, not what it wishes it had — no retry loop
    exists in this lane, so it is 0 on every real enqueue and a retryable rung settles
    immediately. It is a parameter rather than a literal so ``settle``'s stay-in-flight
    branch is reachable the day an attempt loop lands, and testable before then.

    ``_runner`` / ``_client`` are the test-injection seams (the convention the rest of
    ``sites`` uses): a generator runner exposing ``generate``, and a Daytona client.
    None means the real thing.

    NEVER RAISES FOR A BUILD OUTCOME — a failed build, a timeout and a lost sandbox are
    all results, and the row is where they get recorded. It DOES re-raise when the sandbox
    could not be reached at all, after writing a terminal row: that is a condition an
    operator needs in the worker log, and the row is what keeps the site republishable
    meanwhile.
    """
    site = await sites_service.load_build_site(workspace_id, site_id)
    if site is None:
        # The site was deleted, or the id is bogus. Nothing to record on, and nothing to
        # be gained by raising — mirrors ``execute_workspace_job``'s missing-doc no-op.
        logger.warning("sites.build: no site %s in workspace %s — no-op", site_id, workspace_id)
        return

    if not is_buildable_engine(engine):
        # Refused before the row leaves ``queued``-or-``building`` for a sandbox: this is
        # a routing bug, and spending a sandbox to discover it would bill for a mistake
        # that was knowable from the payload.
        await _record(
            site,
            _settlement(
                RUNG_ENGINE_NOT_BUILDABLE,
                normalize_engine(engine),
                retryable=False,
                attempts_left=attempts_left,
            ),
        )
        logger.error("sites.build: engine %r cannot build in this lane (site %s)", engine, site_id)
        return

    await sites_service.mark_build_running(site)

    work_dir = tempfile.mkdtemp(prefix=f"paw-build-{site_id}-")
    try:
        try:
            project_dir = await _scaffold(generator_input, work_dir, runner=_runner)
        except Exception:
            # The generator's own stderr can name paths and carry the user's content, so
            # the row gets the rung and the log gets the detail.
            logger.exception("sites.build: scaffold failed for site %s", site_id)
            await _record(
                site,
                _settlement(
                    RUNG_SCAFFOLD_FAILED,
                    "generator_raised",
                    retryable=False,
                    attempts_left=attempts_left,
                ),
            )
            return

        files = read_generated_tree(project_dir)
        if not files:
            logger.error("sites.build: scaffold of site %s produced no files", site_id)
            await _record(
                site,
                _settlement(
                    RUNG_SCAFFOLD_EMPTY,
                    "no_files_generated",
                    retryable=False,
                    attempts_left=attempts_left,
                ),
            )
            return

        logger.info(
            "sites.build: site %s scaffolded %d files (%d bytes) for a %s build",
            site_id,
            len(files),
            sum(len(contents) for contents in files.values()),
            normalize_engine(engine),
        )

        try:
            result = await run_build(
                files,
                engine=normalize_engine(engine),
                timeout_seconds=timeout_seconds,
                client=_client,
            )
        except Exception:
            # Nothing ran: Daytona unconfigured, or the sandbox could not be created.
            # Record it (so the site is immediately republishable and the row says why)
            # and re-raise so the worker log carries the real exception.
            logger.exception("sites.build: no sandbox for site %s", site_id)
            await _record(
                site,
                _settlement(
                    RUNG_SANDBOX_UNAVAILABLE,
                    "run_build_raised",
                    retryable=True,
                    attempts_left=attempts_left,
                ),
            )
            raise
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)

    settlement = resolve_build_settlement(result, attempts_left=attempts_left)
    _log_outcome(site_id, result, settlement)
    await _record(site, settlement)


async def _scaffold(generator_input: dict[str, Any], out_dir: str, *, runner: Any = None) -> str:
    """Run the pure scaffold step and return the generated project dir.

    Scrubbed AGAIN here, not only at the enqueue: a direct caller (a test, a future
    re-drive of a stored payload) must not be able to skip the scrub by not going through
    the helper. Doing it twice is free; doing it once in the wrong place is a leak.
    """
    if runner is None:
        from pocketpaw_ee.sites.generator_client import _SubprocessRunner

        runner = _SubprocessRunner()
    generated = await runner.generate(scrub_build_input(generator_input), out_dir)
    return str(generated["projectDir"])


async def _record(site: Any, settlement: BuildSettlement) -> None:
    """Persist a settlement, or persist NOTHING when the attempt stays in flight.

    ``status is None`` is the stay-in-flight case, and writing anything there would be
    the bug ``settle`` returns an optional to prevent: a terminal status between attempts
    reads to ``should_enqueue`` as "free to re-publish" and invites a second sandbox on
    top of the retry.
    """
    if settlement.status is None:
        logger.info(
            "sites.build: %s stays in flight (%s) — no status written",
            site.id,
            settlement.reason,
        )
        return
    await sites_service.record_build_outcome(
        site, status=settlement.status, reason=settlement.reason
    )


def _log_outcome(site_id: str, result: BuildRunResult, settlement: BuildSettlement) -> None:
    """Log the verdict, its timings, and the stderr tail the row must not carry."""
    classification = result.classification
    logger.info(
        "sites.build: site %s → %s (%s) artifact=%dB timings=%s",
        site_id,
        settlement.status,
        settlement.reason,
        result.artifact_bytes,
        result.timings.as_dict(),
    )
    if classification.stderr_tail and settlement.status != "built":
        # The one place the build's own error text belongs: an operator (or the user,
        # via a support path) needs it to act, and the row cannot carry it safely.
        logger.warning(
            "sites.build: site %s stderr tail (%s): %s",
            site_id,
            settlement.reason,
            classification.stderr_tail,
        )


# ---------------------------------------------------------------------------
# The enqueue
# ---------------------------------------------------------------------------

_pool: ArqRedis | None = None
_pool_lock = asyncio.Lock()


async def _get_pool() -> ArqRedis:
    """The process's arq pool — the same lazy double-checked pattern the chat-runs
    executor and the jobs service both use, for the same reason: one pool per process,
    and concurrent first-enqueues must not leak two."""
    global _pool
    if _pool is None:
        async with _pool_lock:
            if _pool is None:
                url = os.environ.get("POCKETPAW_REDIS_URL", "").strip()
                if not url:
                    raise RuntimeError(
                        "POCKETPAW_REDIS_URL is not set — the site-build lane needs Redis."
                    )
                _pool = await create_pool(RedisSettings.from_dsn(url))
    return _pool


def _reset_for_tests() -> None:
    global _pool
    _pool = None


def _mint_job_id(site_id: str) -> str:
    """A unique arq job id, minted BEFORE the enqueue so it can be persisted with the
    queued stamp in one write.

    Deliberately NOT deterministic per site. arq refuses an enqueue whose id already has
    a job or a RESULT in Redis (results live for ``keep_result``, an hour by default), so
    a stable id would silently refuse every rebuild for an hour after a build finished —
    a single-flight guard we did not ask for, enforced in the wrong layer, and invisible
    because the refusal is a ``None`` return rather than an error.
    """
    return f"site-build-{site_id}-{uuid.uuid4().hex}"


async def enqueue_site_build(
    site: Any,
    *,
    engine: str,
    generator_input: dict[str, Any],
    timeout_seconds: int | None = None,
    _pool_override: Any = None,
) -> str | None:
    """Enqueue a build for ``site``; return the job id, or ``None`` when one is in flight.

    The workspace and site id are read OFF THE DOC rather than taken as parameters. That
    is a tenancy decision, not a convenience: a caller that could pass a workspace
    alongside a doc could pass a mismatched pair, and the job would then load — and write
    — under the workspace it was told rather than the one the row belongs to.

    Order, and why each step is where it is:

      1. GATE on ``should_enqueue``. A build genuinely in flight must not get a second
         sandbox; a stale one must not block forever. Both live in ``build_state``.
      2. STAMP ``queued`` + the clock + the job id, in ONE write, BEFORE the enqueue.
         Before, because a worker that claimed the job first would write a terminal
         status and then have this stamp land on top of it, pinning a finished build in
         ``queued`` forever. One write, because the job id is minted here rather than
         read back off the enqueue, so there is no second write to race with the job.
      3. ENQUEUE. On ANY failure — a dead Redis, or arq refusing the id — roll the row to
         a terminal status and re-raise. Skipping the rollback is what pins a row in
         ``queued`` behind an enqueue that never happened, and ``should_enqueue`` would
         then no-op every publish of this site until the staleness window lapsed.

    ``timeout_seconds`` defaults to the engine's resolved budget and is passed on to the
    job, so the window the guard measures and the budget the sandbox is held to are one
    number decided once.
    """
    workspace_id = site.workspace
    site_id = str(site.id)
    timeout = (
        timeout_seconds if timeout_seconds is not None else resolve_build_timeout_seconds(engine)
    )

    if not should_enqueue(site, timeout):
        logger.info(
            "sites.build: site %s already has a build in flight (%s) — not enqueueing",
            site_id,
            getattr(site, "build_status", None),
        )
        return None

    job_id = _mint_job_id(site_id)
    await sites_service.mark_build_queued(site, job_id=job_id)

    try:
        pool = _pool_override or await _get_pool()
        job = await pool.enqueue_job(
            ARQ_FUNCTION_NAME,
            workspace_id,
            site_id,
            scrub_build_input(generator_input),
            normalize_engine(engine),
            timeout,
            _job_id=job_id,
        )
        if job is None:
            # arq returns None when the id already exists. With a uuid-tailed id that
            # should be impossible, so treat it as a failed enqueue rather than assuming
            # a build is coming — a row left in ``queued`` for a job nobody will run is
            # the exact failure the rollback below exists to prevent.
            raise RuntimeError(f"arq refused job id {job_id!r} — a job with that id exists")
    except Exception:
        logger.exception("sites.build: enqueue failed for site %s", site_id)
        # Best-effort, and its own suppression: the raise below is what the caller acts
        # on, and a rollback that failed must not replace it with a second error.
        with contextlib.suppress(Exception):
            await sites_service.record_build_outcome(
                site,
                status="failed",
                reason=f"{RUNG_ENQUEUE_FAILED}:pool_or_enqueue_raised",
            )
        raise

    logger.info("sites.build: queued build %s for site %s (%ds budget)", job_id, site_id, timeout)
    return job_id


__all__ = [
    "ARQ_FUNCTION_NAME",
    "BUILDABLE_ENGINES",
    "OUT_OF_SANDBOX_MARGIN_SECONDS",
    "RUNG_ARTIFACT_MISSING",
    "RUNG_ENGINE_NOT_BUILDABLE",
    "RUNG_ENQUEUE_FAILED",
    "RUNG_SANDBOX_UNAVAILABLE",
    "RUNG_SCAFFOLD_EMPTY",
    "RUNG_SCAFFOLD_FAILED",
    "SKIPPED_TREE_DIRS",
    "BuildSettlement",
    "enqueue_site_build",
    "is_buildable_engine",
    "read_generated_tree",
    "resolve_build_settlement",
    "run_site_build",
    "scrub_build_input",
    "site_build_job_timeout_seconds",
]
