# ee/pocketpaw_ee/sites/daytona_runner.py — drives one ephemeral Daytona build
# round-trip: create → upload → build → read the sentinel → extract → delete.
#
# Created 2026-08-09 (SG-9i slice 1). The decision procedure lives next door in
# ``daytona_build.py`` as pure functions; this module is the I/O half that feeds it.
# Split that way on purpose: the part that must be right (what happened, and who is
# to blame) is testable without a sandbox, and this part is testable with a fake
# client.
#
# Edited 2026-08-10 (SL-3 — the install-time supply-chain floor): the upload step now
# writes a ``bunfig.toml`` into the sandbox project (see :data:`SANDBOX_BUNFIG`).
#
# WHY IT BELONGS HERE rather than in the generated project or the image: this
# workspace's install protections live in the DEVELOPER'S HOME DIR (``~/.npmrc``,
# ``~/.bunfig.toml``) and are in no repo, so a fresh container inherited none of them.
# The captain's ruling that Daytona is ALWAYS the build host is what turns that from a
# footnote into the whole exposure — the build box was weaker than the runtime image
# beside it. Injecting at this boundary means a template change cannot silently drop it,
# and it keeps a build-host control out of the customer's source tree.
#
# NOT DONE, and deliberately not faked: ``--frozen-lockfile``. The generator emits no
# lockfile — verified, nothing under paw-sites writes one — so the flag would fail every
# build rather than harden it. Enforcing it needs a vetted lockfile for the allowlisted
# dependency set, which is its own design and is recorded as owed rather than pretended.
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
# THE PRIMARY REASON IS LIVE. A svelte project carries ``__CAPTURE_SIGNED_KEY__``
# substituted into ``src/routes/api/submit/+server.ts`` — a real per-site secret, minted at
# ``sites/service.py:1068`` and substituted by ``paw-sites/src/scaffold.ts:71`` (and again
# for the dynamic track at ``dynamic-scaffold.ts:232``). React has no server route and so
# no key. The decision that this exposure is acceptable rests ENTIRELY on the key living
# only in a container that is then destroyed. A snapshot would make it durable.
#
# ── CORRECTION, 2026-08-09. This header previously said the opposite. ────────────────
#
# It read: *"Lead capture was then dropped (captain: do not serve ``/api/submit``), so no
# route and no key enters any sandbox on either engine, and that specific justification is
# gone."* Quoted rather than deleted, because a reader one ``git blame`` away should be able
# to see the correction happened rather than assume the header was always right.
#
# THAT WAS FALSE, and the distinction is the whole lesson. The captain's ruling was about
# SERVING ``/api/submit``, not about GENERATING it, and it was never implemented in the
# generator: the route template still exists and the key is still substituted into every
# svelte build. What WAS true is narrower — no key enters a DAYTONA sandbox today, but only
# because NOTHING does, since this lane has no callers yet. That narrow truth was
# generalised into a claim about "either engine", which is false about the path that
# actually runs.
#
# THE GAP WORTH REMEMBERING: the distance between "the captain decided X" and "X is true of
# the artifact" was ONE GREP, and nobody ran it for several turns — including the author of
# this header, while writing this header. A decision reported as made arrives formatted as a
# fact about the code. It is not one until you have looked.
#
# TWO FURTHER GROUNDS, now ADDITIONAL rather than replacements:
#   1. Build inputs are customer content. A snapshot moves them from an ephemeral
#      container into durable blob storage, which is a different data-residency
#      question than the one this lane was cleared for (SG-0 conditions 4 and 6).
#   2. Lead capture is "we will see what we can do about leads" — not deleted forever.
#      Even once the generator does stop emitting the route, a returning server route
#      brings the secret back with it.
#
# WIRING CONTRACT THIS IMPLIES: the moment this lane gets callers, the key enters the
# sandbox unless the wiring strips it — substitute the tokens after the artifact returns,
# or inject them at deploy via wrangler ``[vars]``. Today's protection is the accident that
# nothing calls ``run_build``.
#
# WHAT WOULD ENTER IS NO LONGER AN INFERENCE — it has a filename. A canary build on
# 2026-08-10 (a real generated svelte project built with a marked
# ``capture_signed_key``, run by SG-7) found the key in
# ``src/routes/api/submit/+server.ts`` — i.e. in the source map this runner UPLOADS — and
# compiled into ``.svelte-kit/output/server/entries/endpoints/api/submit/_server.ts.js``.
# The contract above is therefore checkable rather than merely cautionary: the file is
# known by name before anyone writes the wiring.
#
# THE SAME CANARY FOUND THE KEY NOWHERE IN THE DEPLOYABLE ARTIFACT
# (``.svelte-kit/cloudflare/``), including under ``_app/`` on the widest client-bundle
# setting. DO NOT read that as reassurance about this lane. The key is absent from the
# artifact because the compiled server route is absent from it — and that same missing file
# is what the shipped ``_worker.js`` imports (``./../output/server/index.js``, verified on
# a local adapter-cloudflare build). So the svelte artifact this lane tars CANNOT EXECUTE.
# The security pass and the correctness bug are one fact; see §8 item 14 of the findings
# record cited below.
#
# Recorded as an OBLIGATION, not a note, in the proving-phase findings record — see §9a of
# ``docs/design/drafts/2026-08-09-sites-proving-SG12-findings.md`` in paw-workspace, which
# also carries the three options and why patching the built output is the worst of them.
# Cross-referenced deliberately: whoever finds this header while about to add a caller
# should land on the obligation, not just on the history of a corrected claim.
#
# CACHING ``node_modules`` IN A SNAPSHOT IS THE OBVIOUS OPTIMISATION HERE, and it is the
# thing that would undo this. If you are reading this because you were about to do it: the
# primary reason above is live, so re-make the security decision first — do not reason from
# the retracted claim that the signed key is gone.
#
# Updated 2026-08-11 (SG-7, fault ladder): the downloaded artifact now passes through
# ``daytona_build.verify_artifact`` before the result is assembled, so an empty, truncated,
# unreadable or ``node_modules``-carrying artifact demotes the classification to a
# non-deployable ``infra_lost`` and the bytes are dropped, instead of leaving ``deployable``
# True on a result a caller would deploy. The rejection carries its OWN ``retryable``: a
# truncated transfer is worth another attempt, a build that produced garbage is not.
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

#: SL-3 — the install-time supply-chain floor, uploaded INTO the sandbox project.
#:
#: WHY THIS FILE HAS TO EXIST HERE AT ALL. This workspace's protections live in the
#: DEVELOPER'S HOME DIR (``~/.npmrc``, ``~/.bunfig.toml``) and are not in any repo, so a
#: fresh container inherits NONE of them: no release-age floor, no ``ignore-scripts``. A
#: build box that resolves from the open registry with lifecycle scripts enabled is
#: strictly weaker than the runtime image beside it, and once Daytona is the ONLY build
#: host that inversion is the whole exposure rather than a note.
#:
#: ``minimumReleaseAge`` is the same 7-day floor the dev machines enforce, expressed in
#: SECONDS because that is bun's unit — 604800. It is the control that would have caught
#: a compromised fresh publish of an already-vetted package, which the allowlist cannot:
#: the allowlist pins WHICH packages and a caret pin still floats the VERSION.
#:
#: ``ignore-scripts`` matters more here than on a laptop. A postinstall script in a build
#: container runs with the sandbox's network and its filesystem, next to the artifact we
#: are about to deploy. Nothing in the vetted set needs one.
#:
#: DELIBERATELY UPLOADED, NOT TEMPLATED INTO THE GENERATED PROJECT. Two reasons: it is a
#: property of the BUILD HOST, not of the customer's site, so it has no business in their
#: source tree; and injecting it at this boundary means a template change cannot silently
#: drop it. It lands in the project dir because that is where bun looks.
SANDBOX_BUNFIG_REL = "bunfig.toml"
SANDBOX_BUNFIG = """# Written by pocketpaw's build lane — NOT part of your site's source.
# Install-time supply-chain floor for this sandbox. See daytona_runner.SANDBOX_BUNFIG.
[install]
# 7 days, in seconds. Matches the floor the dev machines enforce via ~/.bunfig.toml.
minimumReleaseAge = 604800
# No lifecycle scripts. Nothing in the vetted dependency set needs one, and a
# postinstall here would run beside the artifact we are about to deploy.
ignoreScripts = true
"""

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
        # SL-3 — the supply-chain floor. OURS WINS, and the conflict is resolved HERE
        # rather than by upload ordering.
        #
        # ``bulk_upload`` hands the whole list to the Daytona SDK in ONE batch call, so
        # which write survives for a duplicate destination is the SDK's business and is
        # not specified by anything we control. Relying on "later overwrites earlier"
        # would be a guess dressed as a guarantee, so the caller's copy is dropped
        # explicitly instead — deterministic, and visible in the log when it happens.
        #
        # Ours wins because this is a FLOOR: a control the built project can override is
        # not one. The trade is that a legitimate project-level bun setting would be
        # discarded, which is why the drop is logged at WARNING rather than passed over in
        # silence. No generated project emits a bunfig.toml today, so this is a guard
        # against a future template or a hostile source map, not a live collision.
        bunfig_dst = f"{SANDBOX_PROJECT_DIR}/{SANDBOX_BUNFIG_REL}"
        displaced = [u for u in uploads if u[1] == bunfig_dst]
        if displaced:
            logger.warning(
                "sites: dropped a project-supplied %s in favour of the lane's "
                "supply-chain floor (sandbox %s)",
                SANDBOX_BUNFIG_REL,
                sandbox_id,
            )
            uploads = [u for u in uploads if u[1] != bunfig_dst]
        uploads.append((SANDBOX_BUNFIG.encode(), bunfig_dst))
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
