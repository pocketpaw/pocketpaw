# ee/pocketpaw_ee/sites/build_job.py — the ephemeral-build lane's WORKER SIDE: the arq
# job that carries ONE site build from scaffold to verified artifact, plus the enqueue
# helper that starts one.
#
# Created 2026-08-10 (SL-2 slice 2). The lane's two halves already existed and had no
# way to meet: ``daytona_runner.run_build`` could produce a verdict and nothing consumed
# it, ``build_state`` could decide what to write and nothing called it. This is the
# module that joins them, and it is the FIRST caller ``run_build`` has ever had.
#
# Edited 2026-08-10 (SL-3): THIS MODULE NOW HAS A PRODUCTION CALLER, and it finishes the
# publish rather than only recording a verdict. ``service._enqueue_static_build`` queues a
# build for the engines whose artifact can be deployed from this lane, and on a clean
# build the job materialises the artifact and calls ``service.deploy_prebuilt_site`` — the
# SAME deploy tail the inline path runs (concierge embed → deploy → canonical upsert →
# knowledge sync + screenshot). One deploy implementation, two places the build can have
# happened.
#
# THE DEPLOY RUNS BEFORE THE ROW SETTLES. ``built`` must never mean "built but not
# serving": a client reads that as done. So a deploy failure after a clean build settles
# as :data:`RUNG_DEPLOY_FAILED` instead — its own rung, because nothing about the user's
# build was wrong and blaming it would send them to debug a site that compiles.
#
# ``deploy_inputs=None`` KEEPS THE SLICE-2 SHAPE REACHABLE: build, classify, record, deploy
# nothing. That is not dead code — a build queued to verify an artifact is a real use of
# this lane, and it is what the fault-ladder tests drive.
#
# Edited 2026-08-24 (SP-2): THE LANE GREW A SECOND JOB — :func:`run_site_preview_build`,
# for the DRAFT PREVIEW the native editor shadow-renders. Preview used to build inline in
# the API container (``service._build_native_artifact`` → ``generator.build`` → ``bun``),
# which in the deployed container fails and surfaces as ``sites.generator_failed``. It now
# rides the same sandbox this module already drives.
#
# IT IS A SEPARATE JOB RATHER THAN A FLAG ON ``run_site_build``, and the reason is the
# thing a preview does NOT have: a Site row. ``run_site_build`` opens by loading one
# (``load_build_site``) and every step after that writes to it — ``mark_build_running``,
# ``_record``, and the ``claim_build_queued`` its enqueue depends on. A DRAFT that was
# never published has no Site row at all, which is precisely why ``get_native_artifact``
# works on one. Threading "sometimes there is no row" through the publish job would put a
# None-check on every write in the lane's most load-bearing function, to serve a caller
# that also wants a different OUTPUT (``{body_html, css}`` in the artifact store, not a
# deploy) and a different SINGLE-FLIGHT KEY (the content hash, not the site).
#
# THE SINGLE-FLIGHT KEY IS THE CONTENT HASH, AND IT IS THE arq JOB ID. The publish lane
# guards with a conditional write to the Site row because that is the state it owns; with
# no row, this lane spends the id instead: ``_preview_job_id`` is deterministic over
# ``(pocket_id, content_hash)``, so arq itself refuses a second enqueue of a render that
# is already in flight. That is not a lucky reuse of a refusal — it is the same guard,
# keyed on the thing that actually distinguishes one preview build from another. Without
# it a client polling a 15s build every 2s would open a sandbox per poll.
#
# ``_mint_job_id``'s warning about deterministic ids still stands and is answered rather
# than ignored: a completed job's RESULT holds the id for ``keep_result`` (an hour), so a
# refused enqueue is inspected (:func:`_preview_job_outcome`) instead of assumed to be in
# flight. A build that already FINISHED and left the store empty is reported as failed,
# with its reason — not as a build that is still coming. Re-enqueueing there instead
# would put a polling client in a rebuild loop, one sandbox per build duration, on inputs
# that just failed.
#
# WHICH ENGINES ARRIVE HERE IS DECIDED IN ``service.build_runs_async``, not here, and it is
# react-only today. The reason is the artifact, not the queue: an adapter-cloudflare build
# (ripple, dynamic svelte) emits pages rendered by a ``_worker.js`` whose imports sit
# outside the tarred directory, so the artifact cannot serve — which is why ``truth_lane``
# refuses to preview one. Widen that predicate only together with the artifact.
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
#
# Edited 2026-09-04 (fix/queue-lanes, backend-perf C1): BOTH LANES NOW ENQUEUE ONTO
# THEIR OWN QUEUE, :data:`SITE_BUILD_QUEUE_NAME`, instead of arq's default. Nothing
# about a build changed; what changed is which ``max_jobs`` ceiling it competes for.
# Sharing the default queue meant sharing ONE cluster-wide limit of 10 with chat runs,
# workspace jobs and both /ship jobs, so ten concurrent publishes left chat with no
# slot at all and the eleventh request hung silently for up to half an hour.
#
# Three call sites move together and MUST stay together: both enqueues and the
# :func:`_preview_job_outcome` status read. arq scopes a job id to a queue, so a read
# left pointing at the default queue would find nothing, report ``building`` forever,
# and the client would poll a job that finished minutes ago.
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
from arq.jobs import Job, JobStatus

from pocketpaw_ee.sites import service as sites_service
from pocketpaw_ee.sites.build_state import BuildStatus, settle
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

#: The preview lane's own registered name (SP-2). Separate from the publish job's for the
#: same reason the function is: a worker that registered one name for both would run a
#: preview payload through the publish job's signature.
PREVIEW_ARQ_FUNCTION_NAME = "run_site_preview_build"

#: The dedicated arq queue both site-build lanes ride (backend-perf C1).
#:
#: Before this, every lane the cluster runs — chat runs, workspace jobs, both
#: /ship jobs and both site builds — shared ONE queue and therefore ONE
#: ``max_jobs`` ceiling, default 10. Ten concurrent publishes left zero slots for
#: chat, and job 11 did not error: it sat in Redis behind a ``job_timeout`` of up
#: to 30 minutes while the user watched an SSE stream emit nothing. Builds are the
#: lane that bursts (a publish storm is one customer pressing Publish on ten sites)
#: and the lane that is slowest (1020s of budget at today's defaults), so builds are
#: what starves everything else rather than the other way round.
#:
#: A queue is only half a lane; the other half is a consumer. The settings live in
#: ``pocketpaw_ee.sites.build_worker`` and the process that runs both lanes in one
#: container is ``pocketpaw_ee.cloud.worker_supervisor``. Enqueueing here with no
#: consumer running is WORSE than sharing a queue: the job waits forever and
#: nothing anywhere says so.
#:
#: Namespaced, unlike ``GROWTH_QUEUE_NAME`` ("growth"), because arq uses this
#: string as the Redis key verbatim and this database also holds the realtime
#: bridge's keys. The growth name is left alone rather than made consistent —
#: renaming a live queue strands whatever is already sitting in it.
SITE_BUILD_QUEUE_NAME = "arq:queue:sites"

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
#: from the sentinel's ``artifact_empty`` because this one is a property of the TRANSFER,
#: so it is worth another attempt.
#:
#: Updated 2026-08-11: ``run_build`` now verifies the bytes and reports that same
#: condition as ``infra_lost:artifact_empty``, so this rung is no longer reached from that
#: path. It remains the guard for any OTHER result carrying ``deployable`` with no bytes —
#: see :func:`resolve_build_settlement` for why that is kept rather than removed.
RUNG_ARTIFACT_MISSING = "artifact_missing"
#: The enqueue itself failed after the row was already stamped ``queued``. Written by the
#: enqueue helper so the row lands TERMINAL instead of pinned in flight.
RUNG_ENQUEUE_FAILED = "enqueue_failed"
#: SL-3. The build produced a deployable artifact and the DEPLOY failed. Deliberately its
#: own rung rather than folded into ``build_failed``: nothing about the user's build was
#: wrong, so blaming it would send them to debug a site that compiles. Retryable — a
#: failed wrangler run or an unreachable Cloudflare is worth another publish.
RUNG_DEPLOY_FAILED = "deploy_failed"
#: SP-2. The preview build was clean and the artifact could not be turned into
#: ``{body_html, css}`` — a tar that unpacked to no ``index.html``, or a store write that
#: raised. Its own rung for the same reason :data:`RUNG_DEPLOY_FAILED` is: the user's
#: build compiled, so naming this a build failure would send them to debug working code.
RUNG_PREVIEW_UNREADABLE = "preview_unreadable"


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

    Updated 2026-08-11: THE ``deployable``-BUT-NOT-``ok`` CASE NO LONGER ARRIVES FROM
    ``run_build``. That runner verifies the downloaded bytes itself and demotes the
    classification, so an empty download now reaches here already named
    ``infra_lost:artifact_empty`` — and a TRUNCATED one, which used to settle as ``built``
    because nothing compared the promise against what arrived, reaches here as
    ``infra_lost:artifact_truncated``. Both are more precise than this rung could be, so
    the first branch handles them.

    THE ``artifact_missing`` BRANCH STAYS ANYWAY, and not as decoration: this function's
    contract is over a ``BuildRunResult``, not over "a result that came from ``run_build``".
    A result assembled anywhere else — a future caller, a partial re-implementation, a test
    — can still carry ``deployable`` with no bytes, and the branch is what stops that being
    settled as ``built``. Deleting it would make the safe reading depend on an invariant
    held one module away.
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
    deploy_inputs: dict[str, Any] | None = None,
    attempts_left: int = 0,
    _runner: Any = None,
    _client: Any = None,
    _deployer: Any = None,
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

    ``deploy_inputs`` is what the publish captured, carried through so the DEPLOY runs
    with the inputs of the publish that queued it rather than with whatever the pocket's
    draft has become since. When it is None the job builds and records the outcome and
    deploys nothing — the shape slice 2 shipped, still reachable and still tested,
    because a build whose result is only being verified is a real use of this lane.

    ``_runner`` / ``_client`` / ``_deployer`` are the test-injection seams (the convention
    the rest of ``sites`` uses): a generator runner exposing ``generate``, a Daytona
    client, and the deploy callback. None means the real thing.

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

    # SL-4 — decided ONCE, from the payload, and used by both the tar's include-list and
    # the deploy's unpack. Deriving it twice is how the two end up disagreeing, and a
    # disagreement here unpacks an artifact into a directory the deployer does not read.
    from pocketpaw_ee.sites.generator_client import expected_static_output_rel

    artifact_rel = expected_static_output_rel(engine, generator_input)

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
                artifact_rel=artifact_rel,
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

    # SL-3: a build that produced a deployable artifact still has to become a LIVE site.
    # The deploy runs BEFORE the row settles, so ``built`` never means "built but not
    # serving" — a status a client would reasonably read as done.
    if settlement.status == "built" and deploy_inputs is not None:
        try:
            await _deploy_built_artifact(
                result.artifact,
                engine=engine,
                deploy_inputs=deploy_inputs,
                deployer=_deployer,
                output_rel=artifact_rel,
            )
        except Exception:
            # The build worked and the deploy did not, so this is NOT a build failure —
            # but from the publisher's side nothing went live, and the row is the only
            # place that can say so. A terminal status is also what keeps the site
            # republishable; leaving ``built`` on a site that never deployed would read
            # as success forever.
            logger.exception("sites.build: deploy failed for site %s after a clean build", site_id)
            await _record(
                site,
                _settlement(
                    RUNG_DEPLOY_FAILED,
                    "deploy_raised",
                    retryable=True,
                    attempts_left=attempts_left,
                ),
            )
            raise

    await _record(site, settlement)


async def _deploy_built_artifact(
    artifact: bytes | None,
    *,
    engine: str,
    deploy_inputs: dict[str, Any],
    deployer: Any = None,
    output_rel: str | None = None,
) -> None:
    """Materialise the artifact into a project-dir shape and run the deploy tail.

    THE ARTIFACT IS UNPACKED THROUGH ``artifact_preview.unpack_artifact``, not through a
    second extractor written here. That function is hardened against the two things a tar
    from customer content will eventually contain — a member that escapes its root
    (``../``, absolute, drive-qualified, a symlink) and a zip bomb — and every one of
    those guards has a mutation proving it fires. Hand-rolling an extractor for the deploy
    path would mean the deploy got the unproven copy.

    The tree is extracted UNDER the engine's static-output rel, because the deploy targets
    resolve their source as ``<project_dir>/<static_output_rel>`` while the tar is rooted
    AT that directory's contents. Extracting flat would deploy an empty dir.

    ``output_rel`` IS THE SAME PREDICTION THE TAR USED (SL-4), passed down rather than
    recomputed. svelte has two output dirs and the deploy targets probe for them in a
    fixed order (``engines.resolve_static_output_rel``), so extracting an adapter-static
    tree under the nominal ``.svelte-kit/cloudflare`` would still be FOUND — by accident,
    because the probe falls through to it — while reporting the wrong adapter to anything
    that asks. Threading the one value the tar was built from keeps the pack and the
    unpack unable to disagree. ``None`` keeps the nominal value, so react is unchanged.

    ── THE SERVER-ENTRY REFUSAL (the obligation this docstring used to only record) ─────

    ``unpack_artifact``'s skip list DROPS ``_worker.js``. That list is written for a
    PREVIEW, where a worker is noise the preview server cannot run anyway — and it was
    harmless here only while this path was react-only, since react emits no server entry.
    Widening the gate past react is exactly the event the previous version of this
    docstring warned about, so the warning is now a check.

    An artifact carrying a server entry is REFUSED rather than unpacked, because for an
    engine whose worker IS the site, silently dropping it deploys a shell that cannot
    start — a working site replaced by a broken one, with every status reporting success.
    Refusing settles the row as a deploy failure instead, which is recoverable and true.

    It should be unreachable: ``service.build_runs_async`` keeps dynamic svelte and ripple
    out of this lane precisely because their artifacts are worker-rendered. "Unreachable"
    is the reason to check it, not a reason to skip it — the gate reads a prediction, and
    this reads what actually arrived.
    """
    if not artifact:
        # ``settle`` only reaches ``built`` via ``BuildRunResult.ok``, which requires
        # bytes, so this is unreachable rather than tolerated — and it is checked anyway,
        # because "deploy whatever arrived" is how an empty deploy happens.
        raise RuntimeError("refusing to deploy an empty artifact")

    from pocketpaw_ee.sites import artifact_preview
    from pocketpaw_ee.sites import service as _service
    from pocketpaw_ee.sites.engines import static_output_rel

    _refuse_server_entry(artifact, engine=engine, site_id=deploy_inputs.get("site_id"))

    rel = output_rel or static_output_rel(engine)
    deploy = deployer or _service.deploy_prebuilt_site
    project_dir = tempfile.mkdtemp(prefix="paw-deploy-")
    try:
        unpacked = artifact_preview.unpack_artifact(artifact, Path(project_dir, rel))
        logger.info(
            "sites.build: materialised %d entries (%d bytes) under %s for the deploy of site %s",
            unpacked.entries,
            unpacked.bytes_written,
            rel,
            deploy_inputs.get("site_id"),
        )
        await deploy(project_dir=project_dir, deploy_inputs=deploy_inputs)
    finally:
        shutil.rmtree(project_dir, ignore_errors=True)


def _refuse_server_entry(artifact: bytes, *, engine: str, site_id: Any) -> None:
    """Raise when the artifact carries a ``_worker.js`` the unpack would silently drop.

    Reads the tar's MEMBER NAMES only — no extraction, no decompression of contents beyond
    what the index needs — so this cannot itself become the zip-bomb surface
    ``unpack_artifact`` is hardened against.

    ``_worker.js`` is matched as a path COMPONENT rather than a filename, because
    adapter-cloudflare emits it as a DIRECTORY (``_worker.js/chunks/0.js``) once an app is
    large enough. ``engines.resolve_emits_server_worker`` had to learn the same thing from
    the other side; a check keyed on a file would report "no worker" for exactly the
    biggest, most broken-if-dropped sites.

    An UNREADABLE archive is not this function's failure to report: ``verify_artifact``
    already rejects one upstream with a reason of its own, so re-raising here would
    relabel a known condition. Pass and let the unpack speak.
    """
    import tarfile
    from io import BytesIO

    try:
        with tarfile.open(fileobj=BytesIO(artifact), mode="r:gz") as tar:
            names = tar.getnames()
    except Exception:  # noqa: BLE001 — see the docstring; not our condition to report
        return
    for name in names:
        if any(part == "_worker.js" for part in name.replace("\\", "/").split("/")):
            raise RuntimeError(
                f"refusing to deploy a {normalize_engine(engine)} artifact carrying a "
                f"server entry ({name}) — the unpack would drop it and deploy a shell "
                f"that cannot start (site {site_id})"
            )


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
    deploy_inputs: dict[str, Any] | None = None,
    timeout_seconds: int | None = None,
    _pool_override: Any = None,
) -> str | None:
    """Enqueue a build for ``site``; return the job id, or ``None`` when one is in flight.

    The workspace and site id are read OFF THE DOC rather than taken as parameters. That
    is a tenancy decision, not a convenience: a caller that could pass a workspace
    alongside a doc could pass a mismatched pair, and the job would then load — and write
    — under the workspace it was told rather than the one the row belongs to.

    Order, and why each step is where it is:

      1. CLAIM the slot: stamp ``queued`` + the clock + the job id in ONE CONDITIONAL
         write, BEFORE the enqueue. Conditional because the gate and the stamp used to be
         two steps — read ``should_enqueue``, then write — and every publish arriving
         between them read the same pre-stamp row, passed the gate correctly, and opened
         its own sandbox: 8 concurrent publishes of one site produced 8 sandboxes. The
         precondition (``build_state.claim_precondition``) puts the decision inside the
         write so the database picks one winner; the losers return ``None`` having written
         nothing. Before the enqueue, because a worker that claimed the job first would
         write a terminal status and then have this stamp land on top of it, pinning a
         finished build in ``queued`` forever. One write, because the job id is minted here
         rather than read back off the enqueue, so there is no second write to race with
         the job.
      2. ENQUEUE. On ANY failure — a dead Redis, or arq refusing the id — roll the row to
         a terminal status and re-raise. Skipping the rollback is what pins a row in
         ``queued`` behind an enqueue that never happened, and the claim would then refuse
         every publish of this site until the staleness window lapsed.

    ``timeout_seconds`` defaults to the engine's resolved budget and is passed on to the
    job, so the window the guard measures and the budget the sandbox is held to are one
    number decided once.
    """
    workspace_id = site.workspace
    site_id = str(site.id)
    timeout = (
        timeout_seconds if timeout_seconds is not None else resolve_build_timeout_seconds(engine)
    )

    # The claim IS the gate. There is deliberately no ``should_enqueue`` pre-check in
    # front of it: a second reader of the same rule would be free but would also invite
    # the next reader of this code to believe the read is what protects the site, which is
    # the belief that cost 8 sandboxes. One gate, and it is the conditional write.
    job_id = _mint_job_id(site_id)
    claimed = await sites_service.claim_build_queued(site, job_id=job_id, timeout_seconds=timeout)
    if not claimed:
        logger.info(
            "sites.build: site %s already has a build in flight (%s) — not enqueueing",
            site_id,
            getattr(site, "build_status", None),
        )
        return None

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
            _queue_name=SITE_BUILD_QUEUE_NAME,
            # Rides as a kwarg so the positional payload stays exactly what slice 2
            # shipped, and a build queued for verification alone (no deploy) simply omits
            # it. arq forwards any non-underscore kwarg to the function.
            deploy_inputs=deploy_inputs,
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


# ---------------------------------------------------------------------------
# The PREVIEW lane (SP-2) — same sandbox, no Site row, and the artifact becomes
# ``{body_html, css}`` in the native-artifact store instead of a deploy.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PreviewBuildEnqueue:
    """What :func:`enqueue_preview_build` tells its caller to put on the wire.

    ``status`` reuses the publish lane's vocabulary verbatim (``queued`` / ``building`` /
    ``failed``) because the frontend already codes to it, and SL-3's contract that an
    UNRECOGNISED status means in-progress only holds while nobody mints a second
    vocabulary for the same idea.

    ``reason`` is populated on ``failed`` only, and carries a RUNG — never stderr. The
    same rule ``Site.build_reason`` follows, for the same reason: this value crosses to a
    client, and a build's error text is the user's own content.
    """

    job_id: str
    status: str
    reason: str | None = None


def _preview_job_id(pocket_id: str, content_hash: str) -> str:
    """The arq id for one pocket's one render — and, being unique per render, the lane's
    single-flight guard.

    DELIBERATELY DETERMINISTIC, which is the opposite of :func:`_mint_job_id` and for a
    reason that inverts its argument. There the id names a SITE, so a stable one refuses
    every rebuild of that site for as long as a result lives. Here it names the exact
    ``(pocket, render inputs)`` pair the store is keyed on, so "a job with this id already
    exists" is exactly the question the caller is asking: is this render already being
    built? A uuid tail would answer "no" every time and open a sandbox per poll.

    The content hash is a sha256 hex digest, so the id is bounded and contains nothing
    that needs escaping.
    """
    return f"site-preview-{pocket_id}-{content_hash}"


async def _preview_job_outcome(pool: Any, job_id: str) -> tuple[str, str | None]:
    """Read a REFUSED enqueue: is that id an in-flight build, or one that already ended?

    Looked up on the module by :func:`enqueue_preview_build` (the ``_default_prewarm_scheduler``
    convention) so a test can substitute it without faking arq's Redis surface.

    Three answers, and the middle one is why this function exists at all:

      * still queued / running → ``building``. The polling case, and the common one.
      * COMPLETE → the store missed, so whatever that job did it did not leave a usable
        artifact: report ``failed`` with its rung. Reporting ``building`` here would spin
        a client forever on a build that is over; re-enqueueing would rebuild identical
        inputs on a loop.
      * gone (a result that expired between the enqueue and this read) → ``building``.
        A pure race, and the next poll enqueues cleanly because the id is free again.
    """
    job = Job(job_id, pool, _queue_name=SITE_BUILD_QUEUE_NAME)
    status = await job.status()
    if status is not JobStatus.complete:
        return "building", None
    info = await job.result_info()
    if info is None:
        return "building", None
    if not info.success:
        # The job raised. ``run_site_preview_build`` only re-raises for a sandbox it
        # never reached, and the exception text can name paths, so the rung is all that
        # travels.
        return "failed", f"{RUNG_SANDBOX_UNAVAILABLE}:job_raised"
    result = info.result
    if isinstance(result, dict):
        return str(result.get("status") or "failed"), result.get("reason")
    return "failed", "preview_result_unreadable"


def _store_preview_artifact(
    artifact: bytes,
    *,
    engine: str,
    pocket_id: str,
    content_hash: str,
    output_rel: str,
    store: Any,
) -> None:
    """Turn the built tar into ``{body_html, css}`` and cache it under the content hash.

    The preview lane's answer to :func:`_deploy_built_artifact`, and it borrows that
    function's two hard-won decisions rather than re-deciding them: the tar is unpacked
    through ``artifact_preview.unpack_artifact`` (the extractor whose path-escape and
    zip-bomb guards each have a mutation proving they fire), and it is unpacked UNDER
    ``output_rel`` — the same rel the tar was PACKED from — because the reader probes for
    the engine's static output dir and a flat extraction leaves it nothing to find.

    THE SERVER-ENTRY REFUSAL THE DEPLOY MAKES IS DELIBERATELY ABSENT. There it matters
    because dropping ``_worker.js`` deploys a shell that cannot start. Here nothing is
    deployed and the reader only ever opens ``index.html``, which is the same file the
    inline arm build read off disk — so a worker-rendered site previews exactly as
    poorly as it did before this lane existed, and no worse.

    Synchronous, and deliberately: every step is blocking work (untar, read, write) and
    the store seam (``service._default_artifact_store``) is sync — the same one
    ``get_native_artifact`` reads through, so an S3 or other backend swapped in there is
    picked up here for free. An ``async def`` with no awaits would only add indirection.

    Raises on an unreadable tree or a failed read; the caller settles that as
    :data:`RUNG_PREVIEW_UNREADABLE`.
    """
    from pocketpaw_ee.sites import artifact_preview

    project_dir = tempfile.mkdtemp(prefix="paw-preview-")
    try:
        unpacked = artifact_preview.unpack_artifact(artifact, Path(project_dir, output_rel))
        body_html, css = sites_service._read_native_artifact(project_dir, engine)
        logger.info(
            "sites.preview: materialised %d entries (%d bytes) under %s for pocket %s",
            unpacked.entries,
            unpacked.bytes_written,
            output_rel,
            pocket_id,
        )
    finally:
        shutil.rmtree(project_dir, ignore_errors=True)
    store.write(pocket_id, content_hash, body_html, css)


async def run_site_preview_build(
    ctx: dict[str, Any],
    pocket_id: str,
    content_hash: str,
    generator_input: dict[str, Any],
    engine: str,
    timeout_seconds: int,
    *,
    _runner: Any = None,
    _client: Any = None,
    _store: Any = None,
) -> dict[str, str]:
    """arq job: build a pocket's ARMED draft in a sandbox and cache the native artifact.

    The same five steps :func:`run_site_build` runs — scaffold, refuse an empty tree,
    build, classify, act on the verdict — with the last step writing ``{body_html, css}``
    to the native-artifact store instead of deploying. ``ctx`` is unused; everything the
    build needs rides the payload.

    ``content_hash`` is carried rather than recomputed. It is the store's key AND this
    job's id, and it was computed in the web process from the pocket read that decided to
    enqueue. Recomputing it here from the payload would let a source that changed between
    the enqueue and the run write this build's output under the NEW hash — caching a
    render of the old source as if it were the new one.

    RETURNS THE SETTLEMENT RATHER THAN WRITING IT. With no Site row there is nowhere to
    record, so the outcome lives in the arq result — which is what a refused enqueue
    reads (:func:`_preview_job_outcome`) to tell a poller "this render already failed"
    instead of spinning it. The returned ``reason`` is a rung and never stderr, because
    it crosses to a client.

    NEVER RAISES FOR A BUILD OUTCOME, matching the publish job: a failed build, a timeout
    and a lost sandbox are results. It DOES re-raise when the sandbox could not be reached
    at all, after the settlement is already lost to the raise — the worker log is where
    that condition belongs, and ``_preview_job_outcome`` maps the failed job back to the
    ``sandbox_unavailable`` rung.
    """
    if not is_buildable_engine(engine):
        # A routing bug — ``service.get_native_artifact`` gates on ``has_native_edit_lane``
        # and every engine that passes it also builds here. Checked anyway rather than
        # spending a sandbox to discover the day that stops being true.
        logger.error(
            "sites.preview: engine %r cannot build in this lane (pocket %s)", engine, pocket_id
        )
        return {
            "status": "failed",
            "reason": f"{RUNG_ENGINE_NOT_BUILDABLE}:{normalize_engine(engine)}",
        }

    from pocketpaw_ee.sites.generator_client import expected_static_output_rel

    artifact_rel = expected_static_output_rel(engine, generator_input)
    store = _store if _store is not None else sites_service._default_artifact_store()

    work_dir = tempfile.mkdtemp(prefix=f"paw-preview-{pocket_id}-")
    try:
        try:
            project_dir = await _scaffold(generator_input, work_dir, runner=_runner)
        except Exception:
            logger.exception("sites.preview: scaffold failed for pocket %s", pocket_id)
            return {"status": "failed", "reason": f"{RUNG_SCAFFOLD_FAILED}:generator_raised"}

        files = read_generated_tree(project_dir)
        if not files:
            logger.error("sites.preview: scaffold of pocket %s produced no files", pocket_id)
            return {"status": "failed", "reason": f"{RUNG_SCAFFOLD_EMPTY}:no_files_generated"}

        try:
            result = await run_build(
                files,
                engine=normalize_engine(engine),
                timeout_seconds=timeout_seconds,
                client=_client,
                artifact_rel=artifact_rel,
            )
        except Exception:
            logger.exception("sites.preview: no sandbox for pocket %s", pocket_id)
            raise
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)

    settlement = resolve_build_settlement(result)
    _log_outcome(f"preview:{pocket_id}", result, settlement)
    if settlement.status != "built":
        # ``settle`` can answer None to keep a publish attempt in flight between retries.
        # This lane has no attempt loop and no row to leave in flight, so the caller gets
        # a terminal answer — a poller with nothing coming must not be told to keep
        # waiting.
        return {"status": settlement.status or "failed", "reason": settlement.reason}

    try:
        _store_preview_artifact(
            result.artifact or b"",
            engine=engine,
            pocket_id=pocket_id,
            content_hash=content_hash,
            output_rel=artifact_rel,
            store=store,
        )
    except Exception:
        logger.exception(
            "sites.preview: pocket %s built cleanly and the artifact could not be read",
            pocket_id,
        )
        return {"status": "failed", "reason": f"{RUNG_PREVIEW_UNREADABLE}:read_or_store_raised"}

    return {"status": "built", "reason": settlement.reason}


async def enqueue_preview_build(
    *,
    pocket_id: str,
    content_hash: str,
    engine: str,
    generator_input: dict[str, Any],
    timeout_seconds: int | None = None,
    _pool_override: Any = None,
) -> PreviewBuildEnqueue:
    """Queue a preview build for one render, or report the one already running.

    No claim write, because there is no row to claim: the deterministic job id IS the
    single-flight guard (see :func:`_preview_job_id`). arq refuses a duplicate id by
    returning ``None``, and that refusal is inspected rather than assumed — a render that
    already finished and left the store empty comes back ``failed``, not ``building``.

    THE FAILURE MODE THIS EXISTS TO PREVENT IS A SILENT ONE. A dead Redis, or an arq that
    cannot take the job, must NOT return a job id and a ``queued`` status — a client that
    got one would poll a build nobody will run, forever, with the endpoint reporting
    progress the whole time. So an enqueue failure RAISES, and the service turns it into
    an error the user sees.
    """
    if not is_buildable_engine(engine):
        raise RuntimeError(
            f"engine {normalize_engine(engine)!r} cannot build in the preview lane "
            f"(pocket {pocket_id})"
        )

    timeout = (
        timeout_seconds if timeout_seconds is not None else resolve_build_timeout_seconds(engine)
    )
    job_id = _preview_job_id(pocket_id, content_hash)
    pool = _pool_override or await _get_pool()
    job = await pool.enqueue_job(
        PREVIEW_ARQ_FUNCTION_NAME,
        pocket_id,
        content_hash,
        scrub_build_input(generator_input),
        normalize_engine(engine),
        timeout,
        _job_id=job_id,
        _queue_name=SITE_BUILD_QUEUE_NAME,
    )
    if job is None:
        status, reason = await _preview_job_outcome(pool, job_id)
        logger.info(
            "sites.preview: pocket %s render %s already has a job (%s) — not enqueueing",
            pocket_id,
            content_hash[:12],
            status,
        )
        return PreviewBuildEnqueue(job_id=job_id, status=status, reason=reason)

    logger.info(
        "sites.preview: queued build %s for pocket %s (%ds budget)", job_id, pocket_id, timeout
    )
    return PreviewBuildEnqueue(job_id=job_id, status="queued")


__all__ = [
    "ARQ_FUNCTION_NAME",
    "BUILDABLE_ENGINES",
    "OUT_OF_SANDBOX_MARGIN_SECONDS",
    "PREVIEW_ARQ_FUNCTION_NAME",
    "RUNG_ARTIFACT_MISSING",
    "RUNG_ENGINE_NOT_BUILDABLE",
    "RUNG_DEPLOY_FAILED",
    "RUNG_ENQUEUE_FAILED",
    "RUNG_PREVIEW_UNREADABLE",
    "RUNG_SANDBOX_UNAVAILABLE",
    "RUNG_SCAFFOLD_EMPTY",
    "RUNG_SCAFFOLD_FAILED",
    "SKIPPED_TREE_DIRS",
    "BuildSettlement",
    "PreviewBuildEnqueue",
    "enqueue_preview_build",
    "enqueue_site_build",
    "is_buildable_engine",
    "read_generated_tree",
    "resolve_build_settlement",
    "run_site_build",
    "run_site_preview_build",
    "scrub_build_input",
    "site_build_job_timeout_seconds",
]
