# ee/pocketpaw_ee/sites/daytona_runner.py -- drives one ephemeral Daytona build
# round-trip: create -> upload -> build -> read the sentinel -> extract -> delete.
#
# The decision procedure (what happened, who is to blame) lives in ``daytona_build.py``
# as pure functions; this module is the I/O half that feeds it, testable with a fake
# client. The downloaded artifact passes ``daytona_build.verify_artifact`` before the
# result is assembled: an empty, truncated, unreadable or ``node_modules``-carrying
# artifact demotes the build to a non-deployable ``infra_lost`` (with the rejection's
# own ``retryable``) and the bytes are dropped.
#
# Optional arguments, all inert when omitted: ``image`` (the preview lane's
# Playwright image), ``on_artifact`` (stores the draft as soon as a clean artifact is
# verified), ``after_build`` (the browser harness, run in the same sandbox before
# teardown; a raise is logged and never costs the build its verdict), ``labels``.
# The sandbox ``bunfig.toml`` is OPEN (no release-age floor, scripts allowed) because
# the sandbox is the isolation boundary; an author-supplied one wins.
# ``--frozen-lockfile`` is NOT used: the generator emits no lockfile, so the flag would
# fail every build. Enforcing it needs a vetted lockfile, owed as its own design.
#
# THE ORDER OF STEPS IS THE CONTRACT:
#   1. The clock starts BEFORE ``create_sandbox``; it is what separates a timeout from
#      an eviction when no sentinel comes back.
#   2. The sentinel is read BEFORE teardown; once the box is gone the evidence is too.
#   3. Teardown is explicit. A ``finally`` deletes by id once create returned one. A
#      create that raised or was cancelled before returning an id may still have
#      registered the box (the SDK POSTs, then waits for ``started``), so it is found by
#      its unique name and deleted, shielded from a second cancel
#      (``sandbox_reaper.teardown_unreturned``). Every box carries ``paw_lane=site-build``
#      plus the caller's labels (``paw_pocket``, ``paw_job``); with a ``paw_job``, a box
#      an earlier attempt of the same job left is deleted before the create. Backstops:
#      Daytona's auto-delete on stop (``auto_delete_interval=0``) and the reaper cron.
#      Not an unconditional immediate self-delete: that would also destroy the log.
#
# INVARIANT -- THIS LANE NEVER SNAPSHOTS THE SANDBOX. A svelte project carries
# ``__CAPTURE_SIGNED_KEY__`` substituted into ``src/routes/api/submit/+server.ts``, a
# real per-site secret, and a canary build confirmed it lands in the uploaded source
# and the compiled server route. Accepting that rests entirely on the key living only
# in a container that is then destroyed; a snapshot would make it durable. Build inputs
# are also customer content, and a snapshot moves them into durable storage (a
# different data-residency question). Caching ``node_modules`` in a snapshot is the
# obvious optimisation and the thing that would undo this: re-make the security
# decision first. The obligation (strip or inject the key at deploy) and the svelte
# artifact's missing server output are recorded in section 9a and section 8 item 14 of
# ``docs/design/drafts/2026-08-09-sites-proving-SG12-findings.md`` in paw-workspace.
from __future__ import annotations

import logging
import math
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pocketpaw_ee.sites.bun_supply_chain import BUILD_BUNFIG, BUILD_BUNFIG_REL
from pocketpaw_ee.sites.daytona_build import (
    BUILD_RESULT_FILENAME,
    BuildClassification,
    artifact_tar_command,
    build_wrapper_script,
    classify_build,
    promised_artifact_bytes,
    verify_artifact,
)
from pocketpaw_ee.sites.sandbox_reaper import (
    delete_job_leftovers,
    lane_labels,
    teardown_unreturned,
    unique_sandbox_name,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from pocketpaw_ee.cloud.daytona.client import DaytonaClient

logger = logging.getLogger(__name__)

#: Where the project is materialized inside the sandbox. Under the image's workdir
#: (``/home/daytona``) so it is writable by the sandbox user without a chown.
SANDBOX_PROJECT_DIR = "/home/daytona/paw-build"

#: Where the wrapper and the packed artifact live. ``/tmp`` on purpose: neither is
#: part of the project, and keeping them out of it means the include-list tar cannot
#: accidentally pick them up.
SANDBOX_WRAPPER_PATH = "/tmp/paw-build.sh"
SANDBOX_ARTIFACT_PATH = "/tmp/paw-artifact.tgz"

#: The sandbox bunfig, uploaded INTO the sandbox project when it ships none. Lives in
#: ``bun_supply_chain`` (next to the host floor it deliberately differs from) and is
#: re-exported here under the original names.
#:
#: Deliberately NOT naming that other runner here: this module is the Daytona lane,
#: and ``test_fault_ladder_build`` text-scans it to keep a local build fallback from
#: being reintroduced. Sharing a supply-chain constant is not a build fallback, but
#: the guard reads source text, and a guard that has to reason about intent is worth
#: less than one that does not.
SANDBOX_BUNFIG_REL = BUILD_BUNFIG_REL
SANDBOX_BUNFIG = BUILD_BUNFIG

#: Seconds added to the in-sandbox timeout for our own ``execute_command`` budget.
#: The inner ``timeout(1)`` should always fire first, because that path still runs the
#: trap and produces a sentinel; ours is only the backstop for a sandbox that has
#: stopped responding altogether. If they were equal, a race would decide whether we
#: get evidence.
EXEC_TIMEOUT_SLACK_SECONDS = 120

#: The ``paw_lane`` label value on every sandbox this module creates.
BUILD_LANE = "site-build"

#: Minutes of headroom between the build's own timeout and Daytona's idle auto-stop.
_LIFECYCLE_MARGIN_MINUTES = 10


def _lifecycle_minutes(timeout_seconds: int) -> int:
    """Idle auto-stop window, in MINUTES, for a build sandbox.

    Derived from the build timeout rather than a constant, because a fixed value is
    wrong in the dangerous direction: an auto-stop shorter than the build would kill
    healthy long builds, and Daytona counts *inactivity* — which a long ``bun install``
    with no API traffic may well look like. So the window always exceeds the build's
    own budget with margin.

    Note the SDK counts this in minutes while every timeout in this lane is in
    seconds; conflating them is how the shipped default became 60 HOURS (see
    ``DaytonaClient.create_sandbox``'s docstring).
    """
    return math.ceil(timeout_seconds / 60) + _LIFECYCLE_MARGIN_MINUTES


@dataclass(frozen=True)
class BuildTimings:
    """Measured wall-clock per phase, in seconds. These are the S / U / (I+B) / D
    terms the cost model and the timeout formula both consume."""

    create_seconds: float
    upload_seconds: float
    exec_seconds: float
    extract_seconds: float
    total_seconds: float

    def as_dict(self) -> dict[str, float]:
        return {
            "S_create": round(self.create_seconds, 2),
            "U_upload": round(self.upload_seconds, 2),
            "IB_exec": round(self.exec_seconds, 2),
            "D_extract": round(self.extract_seconds, 2),
            "total": round(self.total_seconds, 2),
        }


@dataclass(frozen=True)
class BuildRunResult:
    classification: BuildClassification
    timings: BuildTimings
    artifact: bytes | None
    artifact_bytes: int
    sandbox_id: str | None
    sandbox_deleted: bool
    #: PP-2: what the ``after_build`` hook returned, or ``None`` when there was no hook,
    #: the build was not clean, or the hook raised.
    post_build: Any = None

    @property
    def ok(self) -> bool:
        return self.classification.deployable and self.artifact_bytes > 0


async def _read_sentinel(client: DaytonaClient, sandbox_id: str) -> bytes | None:
    """Read the result sentinel, or ``None`` when it cannot be read.

    Every failure here — file absent, sandbox already gone, transport error — maps to
    ``None``, which the classifier reads as "the build did not prove it completed".
    That is the correct reading in all of those cases, so there is nothing to
    distinguish and no reason to raise.
    """
    remote = f"{SANDBOX_PROJECT_DIR}/{BUILD_RESULT_FILENAME}"
    try:
        return await client.download_file(sandbox_id, remote)
    except Exception as exc:  # noqa: BLE001 — any failure means "no evidence"
        logger.info("daytona_runner: no sentinel readable from %s (%s)", sandbox_id, exc)
        return None


async def run_build(
    files: dict[str, str | bytes],
    *,
    engine: str,
    timeout_seconds: int,
    client: DaytonaClient | None = None,
    sandbox_name: str | None = None,
    cpu: int = 2,
    memory_gb: int = 4,
    disk_gb: int = 10,
    install_command: str = "bun install",
    build_command: str = "bun run build",
    artifact_rel: str | None = None,
    image: str | None = None,
    after_build: Callable[[Any, str, str], Awaitable[Any]] | None = None,
    on_artifact: Callable[[bytes], Awaitable[Any]] | None = None,
    extra_uploads: list[tuple[bytes, str]] | None = None,
    ssr_markers: tuple[str, ...] | None = None,
    log_tail_bytes: int | None = None,
    labels: dict[str, str] | None = None,
) -> BuildRunResult:
    """Build ``files`` in a fresh Daytona sandbox and return the verdict + artifact.

    ``files`` is a ``{relative_path: contents}`` map — the same shape a source-engine
    pocket stores, so the generator's output can be passed straight through with no
    intermediate temp directory.

    ``timeout_seconds`` is a parameter and deliberately has no default: the value comes
    from ``daytona_build.build_timeout_seconds`` over measured p95s, and baking a guess
    in here would quietly become the real policy.

    ``artifact_rel`` (SL-4) names which output dir this build will write, for the one
    engine whose builds have two shapes. It is threaded straight through to the wrapper's
    include-list and sentinel; ``None`` keeps the nominal per-engine value. The caller
    predicts it — this module does not, because the prediction reads the pocket's bindings
    and this module is deliberately ignorant of everything but files and a sandbox.

    Never raises for a build outcome — a failed build, a timeout and a lost sandbox are
    all *results*, and the caller needs the classification to decide between reporting
    and retrying. It may still raise if the sandbox cannot be created at all, which is
    a distinct condition the caller must handle as retryable (nothing has run yet).

    ``image`` / ``after_build`` / ``on_artifact`` — see the module header.

    ``extra_uploads`` are ``(bytes, absolute sandbox path)`` pairs written beside the
    project (the project lane uploads the vendored generator and its build script);
    ``ssr_markers`` / ``log_tail_bytes`` reach :func:`build_wrapper_script`. All three
    are inert when omitted, so existing callers render byte-identical sandboxes.
    ``after_build`` receives ``(client, sandbox_id, static_dir)`` where ``static_dir`` is
    the absolute in-sandbox path of the output dir this build wrote; ``on_artifact``
    receives the verified artifact bytes before it.

    ``labels`` are merged over ``{"paw_lane": "site-build"}`` and stamped on the
    sandbox; the build jobs pass ``paw_pocket`` and ``paw_job``. With a ``paw_job``, a
    box an earlier attempt of the same job left behind is deleted before the create.
    """
    if client is None:
        from pocketpaw_ee.cloud.daytona.client import get_daytona_client

        client = get_daytona_client()
        if client is None:
            raise RuntimeError(
                "Daytona is not configured (DAYTONA_API_URL / DAYTONA_API_KEY unset)"
            )

    wrapper = build_wrapper_script(
        engine,
        SANDBOX_PROJECT_DIR,
        timeout_seconds=timeout_seconds,
        artifact_path=SANDBOX_ARTIFACT_PATH,
        install_command=install_command,
        build_command=build_command,
        artifact_rel=artifact_rel,
        **({"ssr_markers": ssr_markers} if ssr_markers is not None else {}),
        **({"log_tail_bytes": log_tail_bytes} if log_tail_bytes is not None else {}),
    )
    # Rendered here rather than inside the wrapper so an engine whose output is the
    # project root fails BEFORE a sandbox is created and billed.
    #
    # ``artifact_rel`` is passed (SL-4) rather than left to default. Validating the NOMINAL
    # rel while the wrapper packs an OVERRIDDEN one would make this pre-flight check the
    # one place in the lane that disagrees with what actually runs — it would clear an
    # override the tar then refuses, which is the opposite of what a pre-flight is for.
    artifact_tar_command(
        engine, SANDBOX_PROJECT_DIR, SANDBOX_ARTIFACT_PATH, output_rel=artifact_rel
    )

    name = sandbox_name or unique_sandbox_name("paw-build-")
    idle_minutes = _lifecycle_minutes(timeout_seconds)

    # ── The clock starts here, before anything exists. ──────────────────────
    t_start = time.monotonic()
    sandbox_id: str | None = None
    deleted = False
    t_created = t_uploaded = t_exec_done = t_extracted = t_start

    post_build: Any = None
    create_kwargs: dict[str, Any] = {
        "name": name,
        "cpu": cpu,
        "memory": memory_gb,
        "disk": disk_gb,
        # Idle auto-stop EXCEEDS the build budget (see _lifecycle_minutes).
        "auto_stop_interval": idle_minutes,
        # 0 = delete immediately on stop. This is the BACKSTOP for our own process
        # dying, not the primary teardown — the explicit delete below is.
        "auto_delete_interval": 0,
    }
    if image:
        # PP-2: only when set, so a caller that passes nothing creates exactly as before.
        create_kwargs["image"] = image
    create_kwargs["labels"] = lane_labels(BUILD_LANE, labels)
    await delete_job_leftovers(client, create_kwargs["labels"])

    try:
        try:
            info = await client.create_sandbox(**create_kwargs)
        except BaseException:
            # The SDK registers the box BEFORE it waits for ``started``, so a create that
            # timed out or was cancelled can leave a box we hold no id for. Find it by
            # its unique name and delete it (shielded), then re-raise the original.
            await teardown_unreturned(client, name, create_kwargs["labels"])
            raise
        sandbox_id = info.id
        await client.wait_for_sandbox(sandbox_id, target_state="started")
        t_created = time.monotonic()

        uploads: list[tuple[str | bytes, str]] = [
            (
                contents.encode() if isinstance(contents, str) else contents,
                f"{SANDBOX_PROJECT_DIR}/{rel}",
            )
            for rel, contents in files.items()
        ]
        # The sandbox bunfig is OPEN (no release-age floor, scripts allowed): the
        # sandbox is the isolation boundary for author packages. An author may ship
        # their own bunfig.toml, and theirs wins — ours is uploaded only when the
        # project has none, so there is exactly one bunfig destination either way
        # (``bulk_upload`` gives no ordering guarantee for duplicates).
        bunfig_dst = f"{SANDBOX_PROJECT_DIR}/{SANDBOX_BUNFIG_REL}"
        if not any(u[1] == bunfig_dst for u in uploads):
            uploads.append((SANDBOX_BUNFIG.encode(), bunfig_dst))
        uploads.extend(extra_uploads or [])
        uploads.append((wrapper.encode(), SANDBOX_WRAPPER_PATH))
        await client.bulk_upload(sandbox_id, uploads)
        t_uploaded = time.monotonic()

        # Our budget is deliberately LOOSER than the in-sandbox one, so the inner
        # timeout wins the race and we get a sentinel instead of a bare exec failure.
        try:
            await client.execute_command(
                sandbox_id,
                f"bash {SANDBOX_WRAPPER_PATH}",
                timeout=timeout_seconds + EXEC_TIMEOUT_SLACK_SECONDS,
            )
        except Exception as exc:  # noqa: BLE001
            # NOT fatal, and this is the crux: an exec that raises is exactly what
            # both a real build failure and a dead sandbox look like. We do not guess
            # here — we go read the sentinel and let the evidence decide.
            logger.info("daytona_runner: exec did not return cleanly (%s)", exc)
        t_exec_done = time.monotonic()

        # ── Read the evidence BEFORE teardown. ─────────────────────────────
        sentinel = await _read_sentinel(client, sandbox_id)
        elapsed = time.monotonic() - t_start
        classification = classify_build(
            sentinel, elapsed_seconds=elapsed, timeout_seconds=timeout_seconds
        )

        artifact: bytes | None = None
        if classification.deployable:
            try:
                artifact = await client.download_file(sandbox_id, SANDBOX_ARTIFACT_PATH)
            except Exception as exc:  # noqa: BLE001
                # The sentinel said the artifact existed and was non-empty, so failing
                # to fetch it is transport loss, not a build problem.
                logger.warning("daytona_runner: artifact download failed (%s)", exc)
                classification = BuildClassification(
                    outcome="infra_lost",
                    reason="artifact_download_failed",
                    retryable=True,
                    blames_user=False,
                    stderr_tail=classification.stderr_tail,
                )
            else:
                # SG-7: verify the BYTES, not the sentinel's claims about them. Runs here
                # because it is the first moment the artifact exists locally, and before
                # the return because ``deployable`` is the flag a caller deploys on —
                # leaving it True for an empty or node_modules-carrying artifact is how a
                # blank or 500 MB site ships.
                #
                # NEVER ``blames_user``: a leaked exclusion and a failed transfer are both
                # ours, and telling the user their build is broken would be a lie.
                # ``retryable`` comes from the rejection rather than being fixed here,
                # because only the rejection knows whether the transfer or the build is at
                # fault.
                rejection = verify_artifact(
                    artifact, expected_bytes=promised_artifact_bytes(sentinel)
                )
                if rejection is not None:
                    logger.warning(
                        "daytona_runner: artifact rejected (%s, retryable=%s)",
                        rejection.reason,
                        rejection.retryable,
                    )
                    classification = BuildClassification(
                        outcome="infra_lost",
                        reason=rejection.reason,
                        retryable=rejection.retryable,
                        blames_user=False,
                        stderr_tail=classification.stderr_tail,
                    )
                    # Dropped rather than returned alongside the rejection: a caller that
                    # read the bytes off a result it did not gate on would deploy exactly
                    # what this check refused.
                    artifact = None
        t_extracted = time.monotonic()

        # The artifact hook first (the preview lane stores the draft here), so the
        # draft is visible before the browser check below spends its time. Same rule
        # as after_build: only a clean, verified artifact, and a raise is swallowed.
        if on_artifact is not None and artifact is not None and classification.deployable:
            try:
                await on_artifact(artifact)
            except Exception as exc:  # noqa: BLE001 — see after_build below
                logger.warning("daytona_runner: on_artifact hook raised (%s)", exc)

        # PP-2: the post-build hook (the browser harness), only on a CLEAN build and
        # still inside the sandbox's lifetime. A raise is logged and swallowed: the
        # build verdict and its artifact above are already decided and must survive.
        if after_build is not None and artifact is not None and classification.deployable:
            from pocketpaw_ee.sites.engines import static_output_rel

            rel = artifact_rel or static_output_rel(engine)
            static_dir = f"{SANDBOX_PROJECT_DIR}/{rel}".rstrip("/.") or SANDBOX_PROJECT_DIR
            try:
                post_build = await after_build(client, sandbox_id, static_dir)
            except Exception as exc:  # noqa: BLE001 — see above
                logger.warning("daytona_runner: after_build hook raised (%s)", exc)
                post_build = None
    finally:
        # Explicit teardown. Swallows its own errors: a delete that fails must not mask
        # the build result, and the auto-delete backstop still reaps the sandbox.
        #
        # Deliberately NOT recorded in module state. An earlier draft stashed the
        # delete outcome in a module-level dict so it could be folded into the return
        # value after the ``finally`` ran — which would have been a real bug in exactly
        # this lane, since the whole point of the concurrency ceiling is that several
        # builds run at once and would have raced on that dict. Assembling the result
        # AFTER the try/finally gets the same information with no shared state.
        if sandbox_id is not None:
            try:
                await client.delete_sandbox(sandbox_id)
                deleted = True
                logger.info("daytona_runner: deleted sandbox %s", sandbox_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "daytona_runner: explicit delete of %s failed (%s); "
                    "auto_delete_interval=0 remains the backstop",
                    sandbox_id,
                    exc,
                )

    # Reached only when the try-block completed: an exception during create/upload
    # propagates through the ``finally`` and never gets here, which is correct — there
    # is no build result to report for a sandbox that never ran anything.
    return BuildRunResult(
        classification=classification,
        timings=BuildTimings(
            create_seconds=t_created - t_start,
            upload_seconds=t_uploaded - t_created,
            exec_seconds=t_exec_done - t_uploaded,
            extract_seconds=t_extracted - t_exec_done,
            total_seconds=t_extracted - t_start,
        ),
        artifact=artifact,
        artifact_bytes=len(artifact) if artifact else 0,
        sandbox_id=sandbox_id,
        sandbox_deleted=deleted,
        post_build=post_build,
    )


__all__ = [
    "EXEC_TIMEOUT_SLACK_SECONDS",
    "SANDBOX_ARTIFACT_PATH",
    "SANDBOX_PROJECT_DIR",
    "SANDBOX_WRAPPER_PATH",
    "BuildRunResult",
    "BuildTimings",
    "run_build",
]
