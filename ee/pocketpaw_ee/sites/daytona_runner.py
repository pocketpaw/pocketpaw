# ee/pocketpaw_ee/sites/daytona_runner.py — drives one ephemeral Daytona build
# round-trip: create → upload → build → read the sentinel → extract → delete.
#
# Created 2026-08-09 (SG-9i slice 1). The decision procedure lives next door in
# ``daytona_build.py`` as pure functions; this module is the I/O half that feeds it.
# Split that way on purpose: the part that must be right (what happened, and who is
# to blame) is testable without a sandbox, and this part is testable with a fake
# client.
#
# THE ORDER OF STEPS IS THE CONTRACT, not an implementation detail:
#
#   1. Our clock starts BEFORE ``create_sandbox``. It is the only timing signal that
#      survives the sandbox's death, and it is what separates a timeout from an
#      eviction when no sentinel comes back.
#   2. The sentinel is read BEFORE any teardown. Once the sandbox is gone the
#      evidence is gone with it, and "not found" cannot distinguish "deleted
#      normally" from "died and was reaped".
#   3. Teardown is EXPLICIT and in a ``finally``, with Daytona's own auto-delete as a
#      backstop rather than the primary mechanism (see ``_lifecycle_minutes``).
#
# WHY NOT LET THE SANDBOX SELF-DELETE UNCONDITIONALLY, which is the literal ruling:
# an unconditional immediate self-delete also destroys the build log, so a genuine
# build failure would reach the user as "your site failed to build" with no reason to
# act on. Deleting after the read costs exactly the same and keeps the stderr. The
# auto-delete backstop still guarantees no sandbox outlives us if THIS process dies.
#
# ┌───────────────────────────────────────────────────────────────────────────────────┐
# │ INVARIANT — THIS LANE NEVER SNAPSHOTS THE SANDBOX.                                │
# └───────────────────────────────────────────────────────────────────────────────────┘
#
# HISTORY, because the reason CHANGED and the rule did not. This invariant was
# originally load-bearing for a specific secret: a svelte project carried
# ``__CAPTURE_SIGNED_KEY__`` substituted into ``src/routes/api/submit/+server.ts``
# (verified 2026-08-09 — it was the only file in a generated svelte project containing
# it), so a per-site secret was uploaded INTO the sandbox on that track. React never had
# it, having no server route at all. The decision that the exposure was acceptable
# rested entirely on the key living only in a container that is then destroyed.
#
# Lead capture was then dropped (2026-08-09, captain: do not serve ``/api/submit``), so
# no route and no key enters any sandbox on either engine, and that specific
# justification is gone.
#
# THE RULE STAYS ANYWAY, on two grounds that do not depend on it:
#   1. Build inputs are customer content. A snapshot moves them from an ephemeral
#      container into durable blob storage, which is a different data-residency
#      question than the one this lane was cleared for (SG-0 conditions 4 and 6).
#   2. Lead capture is "we will see what we can do about leads" — not deleted forever.
#      If a server route returns, the secret returns with it, and an invariant quietly
#      dropped in the meantime would not come back on its own.
#
# CACHING ``node_modules`` IN A SNAPSHOT IS THE OBVIOUS OPTIMISATION HERE, and it is the
# thing that would undo this. If you are reading this because you were about to do it:
# re-make the decision on the two grounds above rather than noting that the signed key
# is gone.
#
# Updated 2026-08-10 (SG-7, fault ladder): the downloaded artifact now passes through
# ``daytona_build.verify_artifact`` before the result is assembled, so an empty, truncated,
# unreadable, or ``node_modules``-carrying artifact demotes the classification to a
# non-deployable ``infra_lost`` instead of leaving ``deployable`` True. The rejection
# carries its OWN ``retryable``: a truncated transfer is worth another attempt, a build
# that produced garbage is not.
from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pocketpaw_ee.sites.daytona_build import (
    BUILD_RESULT_FILENAME,
    BuildClassification,
    artifact_tar_command,
    build_wrapper_script,
    classify_build,
    promised_artifact_bytes,
    verify_artifact,
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

#: Seconds added to the in-sandbox timeout for our own ``execute_command`` budget.
#: The inner ``timeout(1)`` should always fire first, because that path still runs the
#: trap and produces a sentinel; ours is only the backstop for a sandbox that has
#: stopped responding altogether. If they were equal, a race would decide whether we
#: get evidence.
EXEC_TIMEOUT_SLACK_SECONDS = 120

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
) -> BuildRunResult:
    """Build ``files`` in a fresh Daytona sandbox and return the verdict + artifact.

    ``files`` is a ``{relative_path: contents}`` map — the same shape a source-engine
    pocket stores, so the generator's output can be passed straight through with no
    intermediate temp directory.

    ``timeout_seconds`` is a parameter and deliberately has no default: the value comes
    from ``daytona_build.build_timeout_seconds`` over measured p95s, and baking a guess
    in here would quietly become the real policy.

    Never raises for a build outcome — a failed build, a timeout and a lost sandbox are
    all *results*, and the caller needs the classification to decide between reporting
    and retrying. It may still raise if the sandbox cannot be created at all, which is
    a distinct condition the caller must handle as retryable (nothing has run yet).
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
    )
    # Rendered here rather than inside the wrapper so an engine whose output is the
    # project root fails BEFORE a sandbox is created and billed.
    artifact_tar_command(engine, SANDBOX_PROJECT_DIR, SANDBOX_ARTIFACT_PATH)

    name = sandbox_name or f"paw-build-{int(time.time() * 1000)}"
    idle_minutes = _lifecycle_minutes(timeout_seconds)

    # ── The clock starts here, before anything exists. ──────────────────────
    t_start = time.monotonic()
    sandbox_id: str | None = None
    deleted = False
    t_created = t_uploaded = t_exec_done = t_extracted = t_start

    try:
        info = await client.create_sandbox(
            name=name,
            cpu=cpu,
            memory=memory_gb,
            disk=disk_gb,
            # Idle auto-stop EXCEEDS the build budget (see _lifecycle_minutes).
            auto_stop_interval=idle_minutes,
            # 0 = delete immediately on stop. This is the BACKSTOP for our own process
            # dying, not the primary teardown — the explicit delete below is.
            auto_delete_interval=0,
        )
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
                # NEVER ``blames_user``: a leaked include-list and a failed transfer are
                # both ours, and telling the user their build is broken would be a lie.
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
                    artifact = None
        t_extracted = time.monotonic()
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
