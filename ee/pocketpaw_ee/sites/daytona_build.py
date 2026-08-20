# ee/pocketpaw_ee/sites/daytona_build.py — the ephemeral Daytona build lane's
# DECISION CORE. Pure functions only: no Daytona client, no network, no filesystem.
#
# Created 2026-08-09 (SG-9i): the captain's ruling is that a site build runs in a
# Daytona sandbox with a strict timeout which then deletes itself. That ruling makes
# the previously-specified failure discriminator impossible, and this module is the
# replacement.
#
# WHY THIS EXISTS — the problem the ruling creates. SR-2 established that mid-build
# sandbox death is INDISTINGUISHABLE from a build failure: both surface as a failed
# ``execute_command``. Its fix was to re-query the sandbox afterwards
# (``get_sandbox_by_id``) and branch on whether it was still alive. A SELF-DELETING
# sandbox cannot be re-queried — and worse, "not found" then means BOTH "deleted
# normally" and "died and was reaped", so the query is not merely unavailable, it is
# actively misleading. A strict timeout also makes death-by-timeout more frequent.
#
# THE FIX — invert the inference. Do not try to detect infrastructure failure.
# Require the build to PROVE it completed, and treat the ABSENCE of that proof as
# infrastructure failure. A build that ran to completion can write evidence; a killed
# container cannot. Absence is unambiguous in a way a failed exec never is.
#
# So the in-sandbox wrapper writes ``.paw-build-result.json`` from a shell ``trap ...
# EXIT``, which fires even when the build itself exits non-zero. The caller reads that
# sentinel BEFORE any teardown. ``classify_build`` then maps (sentinel, our elapsed
# clock, the configured timeout) onto exactly one outcome.
#
# WHY THE CLOCK IS OURS. The elapsed time is measured by the enqueuing process,
# started before ``create_sandbox``. It therefore survives the sandbox's death, costs
# no API call, and cannot be lost with the container — unlike anything Daytona could
# tell us after the fact.
#
# WHAT THIS MODULE DELIBERATELY DOES NOT DO: talk to Daytona. The whole point is that
# the decision procedure is testable without a sandbox, because it is the part that
# must be right. Wiring lives in the caller.
#
# Updated 2026-08-11 (SG-7, fault ladder): added :func:`verify_artifact` and
# :func:`promised_artifact_bytes`, which read the DOWNLOADED bytes rather than the
# sentinel's claims about them. See the block above that function for why it earns its
# place on top of the tar command's own exclusion, and for why its rejections split on
# ``retryable`` — a truncated transfer is transient, an unreadable full-size artifact is
# not. Still no Daytona in this module: it is handed bytes, it does not fetch them.
from __future__ import annotations

import io
import json
import logging
import math
import os
import shlex
import tarfile
from dataclasses import dataclass
from typing import Any, Literal

from pocketpaw_ee.sites.engines import (
    candidate_static_output_rels,
    normalize_engine,
    static_output_rel,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# The sentinel
# ---------------------------------------------------------------------------

#: Filename the in-sandbox wrapper writes its result to, relative to the project dir.
#: Read by the caller BEFORE teardown; it is the only evidence the caller trusts.
BUILD_RESULT_FILENAME = ".paw-build-result.json"

#: Sentinel schema version. Bump when a field's MEANING changes (adding an optional
#: field does not need a bump). ``classify_build`` refuses a version it does not know
#: rather than guessing at the fields — a sentinel we cannot read is exactly as
#: uninformative as no sentinel, and must classify the same way.
SENTINEL_SCHEMA = 1

#: How much of the build's stderr the wrapper carries back. Enough to show a stack or
#: a compiler error without dwarfing the response; mirrors the bounded-output posture
#: of ``websandbox/scaffold.py``.
STDERR_TAIL_BYTES = 8192

# ---------------------------------------------------------------------------
# Exit codes that mean "the environment killed us", not "the code is wrong"
# ---------------------------------------------------------------------------
#
# These are the residual gap in the sentinel design, and missing them would undo it.
# A process killed by a signal STILL RUNS THE TRAP, so it produces a sentinel with a
# non-zero ``build_exit`` and would naively classify as a genuine build failure — the
# exact mis-report the whole mechanism exists to prevent. The shell reports a
# signalled death as 128+signum.

#: ``timeout(1)``'s exit code when it kills the supervised command. Evidence of a
#: timeout WITH a sentinel, which is strictly better than inferring one from the
#: clock: we know it was the in-sandbox limit, and we still have the stderr tail.
_EXIT_TIMEOUT = 124

#: 128+9 (SIGKILL). In a memory-capped container this is overwhelmingly the OOM
#: killer, i.e. capacity loss, not a broken build. Retry it; never blame the user.
_EXIT_SIGKILL = 137

#: 128+15 (SIGTERM). The container is being stopped underneath us.
_EXIT_SIGTERM = 143

_INFRA_EXIT_CODES = frozenset({_EXIT_SIGKILL, _EXIT_SIGTERM})

# ---------------------------------------------------------------------------
# Timeout sizing
# ---------------------------------------------------------------------------

#: Floor for any build timeout, in seconds. NOT arbitrary: ``websandbox/scaffold.py``
#: budgets 600s for a cold install of this very toolchain, and a cold-per-build lane
#: pays that install EVERY time. A floor below it guarantees timeouts on healthy
#: builds — the failure mode a "strict" timeout is most likely to introduce.
TIMEOUT_FLOOR_SECONDS = 600

#: Multiplier over measured p95. A cold sandbox has no page cache and may hit a
#: busier registry than the machine the measurement was taken on.
TIMEOUT_SAFETY_FACTOR = 1.5


#: Shared env knob for the build timeout. Deliberately the SAME name SG-P2 is about
#: (``PAW_SITES_BUILD_TIMEOUT_SEC``, read by ``generator_client._build_timeout_sec``):
#: one constant serves the local builder and this lane, so an operator tuning a slow
#: deploy does not have to discover two names. Set nowhere in deploy config today.
_TIMEOUT_ENV = "PAW_SITES_BUILD_TIMEOUT_SEC"

#: Per-engine override, checked before the shared knob:
#: ``PAW_SITES_BUILD_TIMEOUT_SEC_REACT`` / ``..._SVELTE``. Engines differ by more than
#: a constant factor — react installs 4 direct deps, svelte pulls the whole
#: SvelteKit + adapter-cloudflare toolchain — so a single number is either wasteful for
#: one or fatal for the other.
_TIMEOUT_ENV_PER_ENGINE = "PAW_SITES_BUILD_TIMEOUT_SEC_{engine}"


def resolve_build_timeout_seconds(engine: str | None) -> int:
    """The timeout to use for ``engine`` right now, with no measured inputs.

    Resolution order: per-engine env → shared env → :data:`TIMEOUT_FLOOR_SECONDS`.

    Both engines currently land on the 600s floor, because measurement was descoped and
    :func:`build_timeout_seconds` has nothing to compute from. That is deliberately
    LOOSE rather than tight — a timeout is a wedge detector, and the cost of one that is
    too generous is a slow failure, while the cost of one that is too tight is a healthy
    build reported to the user as broken. When real p95s exist, feed them to
    :func:`build_timeout_seconds` and set these env vars from its output; the shape is
    already here so nothing needs restructuring.

    A malformed value falls back rather than raising: the timeout is a safety net and
    must never itself be the thing that breaks a build.
    """
    names = [
        _TIMEOUT_ENV_PER_ENGINE.format(engine=normalize_engine(engine).upper()),
        _TIMEOUT_ENV,
    ]
    for name in names:
        raw = os.environ.get(name, "").strip()
        if not raw:
            continue
        try:
            value = int(raw)
        except ValueError:
            logger.warning("sites: ignoring non-int %s=%r", name, raw)
            continue
        if value > 0:
            return value
        logger.warning("sites: ignoring non-positive %s=%r", name, raw)
    return TIMEOUT_FLOOR_SECONDS


def build_timeout_seconds(
    install_p95_seconds: float,
    build_p95_seconds: float,
    *,
    floor_seconds: int = TIMEOUT_FLOOR_SECONDS,
    safety_factor: float = TIMEOUT_SAFETY_FACTOR,
) -> int:
    """Return the strict per-build timeout, in whole seconds.

    Sized over ``install + build``, NOT build alone. That distinction is the whole
    point: this lane is cold-per-build, so the dependency install is paid on every
    single build and is usually the larger of the two terms. A timeout sized on the
    build alone would kill every healthy build in the lane, which is the specific way
    a "strict timeout" instruction goes wrong.

    Takes **p95**, not mean — the timeout's job is to catch a wedged build, not to
    trim a slow tail. Negative inputs are clamped to zero so a malformed measurement
    degrades to the floor rather than producing a nonsense (or negative) budget.
    """
    install = max(0.0, install_p95_seconds)
    build = max(0.0, build_p95_seconds)
    scaled = math.ceil((install + build) * safety_factor)
    return max(floor_seconds, scaled)


# ---------------------------------------------------------------------------
# Outcomes
# ---------------------------------------------------------------------------

#: The four mutually-exclusive outcomes of a build attempt.
#:
#: ``build_failed`` is the ONLY one that may reach the user as "your site did not
#: build", and reaching it requires positive proof of completion. The other three are
#: ours to absorb — which is what stops capacity loss being reported as the user's
#: fault.
BuildOutcome = Literal["completed_ok", "build_failed", "timed_out", "infra_lost"]


@dataclass(frozen=True)
class BuildClassification:
    """The verdict on one build attempt.

    ``retryable`` and ``blames_user`` are deliberately separate flags rather than one
    enum: a timeout is retryable AND honestly reportable to the user ("your build
    exceeded N seconds" is actionable — they may have an enormous site), whereas
    ``infra_lost`` is retryable and must NEVER be shown as a build problem. Collapsing
    them would force every call site to re-derive the distinction.
    """

    outcome: BuildOutcome
    #: Short machine-readable reason, for logs and metrics. Never user-facing prose.
    reason: str
    #: May the lane attempt this build again? Corrected 2026-08-10 (SG-7): this used to
    #: read "and then fall back to the local builder", which describes a fallback the
    #: captain overrode — the ruling is Daytona-ONLY, so a lane that runs out of retries
    #: fails loudly rather than building somewhere else. Nothing retries yet either; no
    #: caller consumes this flag, so it is a contract for the wiring phase, not a
    #: description of current behaviour.
    retryable: bool
    #: Is this the user's build being wrong? Only ever True for ``build_failed``.
    blames_user: bool
    #: Build stderr tail when we have one, else ``""``. Empty is meaningful: it means
    #: no sentinel survived, which is precisely why the outcome is not ``build_failed``.
    stderr_tail: str = ""

    @property
    def deployable(self) -> bool:
        """True when the CLASSIFICATION cleared the build — not when its bytes were checked.

        Corrected 2026-08-10 (SG-7): this used to claim "there is a verified artifact to
        deploy", which overpromised in a way that matters. Read on a classification that
        came straight out of :func:`classify_build`, it is derived from the sentinel alone,
        so it stays True when the download afterwards returns zero bytes or a truncated
        payload — nothing here has seen the artifact.

        Updated 2026-08-11: ``daytona_runner.run_build`` now runs :func:`verify_artifact`
        over the downloaded bytes and REPLACES this classification with a non-deployable
        one when they are rejected, so the flag does follow the bytes on the result that
        runner hands back. That is a property of the runner, not of this property, and the
        distinction is load-bearing: anything that classifies without downloading (the
        classifier's own tests, a future caller that only wants the verdict) still gets the
        sentinel-only reading.

        A caller must therefore still gate a deploy on ``BuildRunResult.ok``, which also
        requires bytes to have arrived, rather than on this flag by itself.
        """
        return self.outcome == "completed_ok"


def _parse_sentinel(raw: str | bytes | dict[str, Any] | None) -> dict[str, Any] | None:
    """Coerce a sentinel to a dict, or ``None`` when it is unusable.

    A TRUNCATED or malformed sentinel returns ``None`` on purpose. A partial write
    means the container died mid-write, which is infrastructure loss — the same
    conclusion as no sentinel at all. Trusting a half-written sentinel would be the
    one way to turn this mechanism back into a guess.
    """
    if raw is None:
        return None
    if isinstance(raw, dict):
        parsed: Any = raw
    else:
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError, UnicodeDecodeError):
            return None
    if not isinstance(parsed, dict):
        return None
    if parsed.get("schema") != SENTINEL_SCHEMA:
        # An unknown schema is unreadable, and unreadable is indistinguishable from
        # absent. Refuse rather than guess at field meanings.
        return None
    return parsed


def _coerce_exit(value: Any) -> int | None:
    """Read an exit code, or ``None`` when it is missing/unusable.

    ``bool`` is rejected explicitly: ``True``/``False`` are ``int`` subclasses that
    would otherwise read as exit 1 / exit 0 and silently invert a verdict.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def classify_build(
    sentinel: str | bytes | dict[str, Any] | None,
    *,
    elapsed_seconds: float,
    timeout_seconds: int,
) -> BuildClassification:
    """Classify one build attempt into exactly one :data:`BuildOutcome`.

    ``sentinel`` is the raw content of :data:`BUILD_RESULT_FILENAME` read from the
    sandbox BEFORE teardown, or ``None`` when it could not be read (which includes
    "the sandbox was already gone").

    The order of the checks is load-bearing and is documented inline: signalled
    deaths must be recognised BEFORE a non-zero exit is read as a build failure, or a
    container OOM would be reported as the user's bug.
    """
    parsed = _parse_sentinel(sentinel)

    # ── No usable sentinel: the build did not prove it completed. ───────────
    # This is the inversion the whole module rests on. We do not ask "did the
    # sandbox die"; we observe that nothing proved it lived, which is the same
    # evidence and is available after the container is gone.
    if parsed is None:
        if elapsed_seconds >= timeout_seconds:
            return BuildClassification(
                outcome="timed_out",
                reason="no_sentinel_at_timeout",
                retryable=True,
                # Honest and actionable: the build really did exceed the budget.
                blames_user=False,
            )
        return BuildClassification(
            outcome="infra_lost",
            reason="no_sentinel_before_timeout",
            retryable=True,
            blames_user=False,
        )

    stderr_tail = str(parsed.get("stderr_tail") or "")
    install_exit = _coerce_exit(parsed.get("install_exit"))
    build_exit = _coerce_exit(parsed.get("build_exit"))

    # ── Signalled deaths FIRST. ─────────────────────────────────────────────
    # A signalled process still runs the trap, so these arrive WITH a sentinel and
    # would otherwise be read as a genuine failure. Checking them after the
    # non-zero-exit branch would reintroduce the exact mis-report this module exists
    # to prevent, which is why they are checked here and not below.
    for label, code in (("install", install_exit), ("build", build_exit)):
        if code in _INFRA_EXIT_CODES:
            return BuildClassification(
                outcome="infra_lost",
                reason=f"{label}_killed_by_signal_{code}",
                retryable=True,
                blames_user=False,
                stderr_tail=stderr_tail,
            )
        if code == _EXIT_TIMEOUT:
            # ``timeout(1)`` fired inside the sandbox. Better than the clock-inferred
            # case above: we know which step overran AND we kept its stderr.
            return BuildClassification(
                outcome="timed_out",
                reason=f"{label}_exceeded_in_sandbox_timeout",
                retryable=True,
                blames_user=False,
                stderr_tail=stderr_tail,
            )

    # ── A missing exit code is not a success. ───────────────────────────────
    # Fail closed: a sentinel that parsed but carries no build_exit tells us nothing
    # about the build, so it must not be allowed to deploy.
    if install_exit is None or build_exit is None:
        return BuildClassification(
            outcome="infra_lost",
            reason="sentinel_missing_exit_code",
            retryable=True,
            blames_user=False,
            stderr_tail=stderr_tail,
        )

    if install_exit != 0:
        # A failed dependency install is genuinely reportable — a bad manifest is the
        # user's to fix — but it is also what a registry outage looks like. It is
        # retryable AND user-visible; the caller decides which to lead with, and the
        # retry is cheap because nothing was built.
        return BuildClassification(
            outcome="build_failed",
            reason="install_failed",
            retryable=True,
            blames_user=True,
            stderr_tail=stderr_tail,
        )

    if build_exit != 0:
        return BuildClassification(
            outcome="build_failed",
            reason="build_failed",
            retryable=False,
            blames_user=True,
            stderr_tail=stderr_tail,
        )

    # ── Exit 0 is not sufficient. ───────────────────────────────────────────
    # A build that "succeeds" and emits nothing is the empty-deploy failure: every
    # step reports success and a blank site goes live. The artifact size is the
    # positive check that catches it, and it is checked here rather than at the
    # deploy call site so no caller can forget it.
    artifact_bytes = parsed.get("artifact_bytes")
    if not isinstance(artifact_bytes, int) or isinstance(artifact_bytes, bool):
        return BuildClassification(
            outcome="build_failed",
            reason="artifact_size_unknown",
            retryable=False,
            blames_user=True,
            stderr_tail=stderr_tail,
        )
    if artifact_bytes <= 0:
        return BuildClassification(
            outcome="build_failed",
            reason="artifact_empty",
            retryable=False,
            blames_user=True,
            stderr_tail=stderr_tail,
        )

    return BuildClassification(
        outcome="completed_ok",
        reason="ok",
        retryable=False,
        blames_user=False,
        stderr_tail=stderr_tail,
    )


# ---------------------------------------------------------------------------
# Artifact extraction — an INCLUDE-list of exactly one directory
# ---------------------------------------------------------------------------


#: Excluded from the artifact even though the include-list already scopes the tar to the
#: output dir. NOT redundant — SG-7 measured the gap: ``-C <project>/dist .`` cannot reach a
#: ``node_modules`` that sits BESIDE the output dir (the shape ``bun install`` produces), but
#: it packs one NESTED INSIDE it without complaint. Neither engine emits that shape today,
#: so this closes a latent 500 MB-artifact path rather than a live bug.
#:
#: DELIBERATELY UNANCHORED — no ``./`` prefix, and that is the whole point.
#:
#: This was ``"./node_modules"``, written to match the member form ``-C <dir> .`` produces,
#: and verified against bsdtar, which matches an exclude pattern unanchored either way. The
#: Daytona image is ``debian_slim``, i.e. GNU tar, which ANCHORS a pattern containing a
#: slash — so ``./node_modules`` matched only the top-level directory there and a
#: ``dist/sub/node_modules`` shipped. CI caught exactly that, and it is not hypothetical:
#: measured on GNU tar 1.35, ``--exclude=./node_modules`` leaves ``./sub/node_modules/``
#: in the archive while ``--exclude=node_modules`` removes every one at any depth.
#:
#: A bare name is matched unanchored by BOTH tars, so this form closes the hole on the
#: image and keeps the dev box agreeing with CI. It still prunes only a path COMPONENT
#: named exactly ``node_modules``, so an innocently-named ``node_modules_report.html``
#: survives.
_EXCLUDED_MEMBER = "node_modules"


def artifact_tar_command(
    engine: str | None,
    project_dir: str,
    dest_path: str,
    *,
    output_rel: str | None = None,
) -> str:
    """The in-sandbox command that packs ONLY the deployable output.

    Deliberately an **include-list of one directory** rather than an exclude-list of
    junk, and deliberately NOT ``_SNAPSHOT_EXCLUDED_SEGMENTS`` (whose frozenset the
    dev-workspace prune depends on not seeing those paths — widening it would break
    an unrelated subsystem).

    Two properties follow, and both are the reason for the choice:

    * ``node_modules`` is excluded for every engine whose output is a subdirectory
      (``dist``, ``.svelte-kit/cloudflare``) — by the ``-C`` scope for a SIBLING copy, and
      by :data:`_EXCLUDED_MEMBER` for one nested inside the output dir. The second half is
      not redundant: SG-7 measured that ``-C dist .`` packs ``dist/node_modules/``, so the
      scope alone did not make the 500 MB-on-the-wire failure impossible.
    * The failure mode inverts usefully. A wrong exclude-list ships junk SILENTLY; a
      wrong include-list ships NOTHING, LOUDLY — caught by ``artifact_bytes <= 0`` in
      :func:`classify_build` before anything is deployed.

    Raises ``ValueError`` for an engine whose static output is the project root
    (``html``): tarring ``.`` would sweep in ``node_modules`` and defeat the whole
    design. html needs no build and so never reaches this lane; a caller that gets
    here with it has a routing bug and should hear about it loudly.

    ``output_rel`` NAMES WHICH OUTPUT DIR THIS BUILD WILL WRITE, for the one engine that
    has more than one (SL-4). svelte builds on adapter-static (``build``) or
    adapter-cloudflare (``.svelte-kit/cloudflare``) depending on the site's bindings, and
    the include-list is rendered into the wrapper BEFORE the build runs, so it cannot read
    the answer off disk the way ``engines.resolve_static_output_rel`` does. The caller
    predicts it with ``generator_client.expected_static_output_rel`` — the same helper the
    unpack uses — and passes it here. ``None`` keeps the nominal per-engine value, so every
    pre-SL-4 call renders a byte-identical command.

    An ``output_rel`` the engine cannot emit is REFUSED rather than honoured. An
    include-list aimed at a directory that will never exist packs nothing, and an empty
    artifact is indistinguishable from a build that produced nothing — so the caller's
    mistake would arrive disguised as the user's. ``engines.candidate_static_output_rels``
    owns the per-engine set, so this validates against the same list the resolver probes.

    NOTE THIS IS STILL ONE ``shlex``-SPLITTABLE COMMAND, with no shell conditional in it.
    An earlier draft probed for the directory in bash so the tar could self-correct; that
    would have broken ``tests/ee/sites/faults.py::pack_with_real_tar``, which runs the
    command through ``shlex.split`` WITHOUT a shell precisely so the test does not depend
    on which bash a Windows Python resolves. Predicting the directory keeps the command a
    plain argv, and a wrong prediction still fails the designed way: loudly and empty.
    """
    rel = static_output_rel(engine) if output_rel is None else output_rel
    if output_rel is not None and output_rel not in candidate_static_output_rels(engine):
        raise ValueError(
            f"engine {normalize_engine(engine)!r} cannot emit its static output at "
            f"{output_rel!r}; expected one of {candidate_static_output_rels(engine)}"
        )
    if rel == ".":
        raise ValueError(
            f"engine {normalize_engine(engine)!r} emits its static output at the "
            "project root, so an include-list cannot exclude node_modules; this "
            "lane is for engines whose build writes a subdirectory"
        )
    output_dir = f"{project_dir.rstrip('/')}/{rel}"
    return (
        f"tar -czf {shlex.quote(dest_path)} -C {shlex.quote(output_dir)} "
        f"--exclude={shlex.quote(_EXCLUDED_MEMBER)} ."
    )


# ---------------------------------------------------------------------------
# Verifying the artifact BYTES, not the sentinel's claims about them
# ---------------------------------------------------------------------------
#
# WHY THIS EXISTS ALONGSIDE THE TAR COMMAND, which already scopes the archive to the
# output dir AND excludes :data:`_EXCLUDED_MEMBER`. Two reasons, and neither is "in case
# the command is wrong in a way we could have reasoned about":
#
#   1. THE EXCLUSION IS A FLAG ON A BINARY THIS PROCESS NEVER RUNS. Its behaviour is the
#      tar implementation's, not ours, and the two implementations in play already
#      disagreed once: GNU tar (what ``debian_slim`` runs) anchors a pattern containing a
#      slash, bsdtar does not, so ``--exclude=./node_modules`` passed locally and shipped
#      a nested ``node_modules`` in CI. That was found by a test, not by a reader, and the
#      class of bug — "the command still looks correct" — is exactly what a check on the
#      resulting bytes catches and a check on the command cannot.
#   2. NOTHING ELSE IN THE LANE LOOKS AT THE TRANSFER. An empty, truncated or corrupt
#      download is invisible to the sentinel by construction: the sentinel is written
#      inside the sandbox, before any bytes cross the wire.
#
# The include-list plus the exclusion remain the PRIMARY defence and are tested directly,
# by running the real tar over a real node_modules tree
# (tests/ee/sites/test_fault_ladder_build.py). This is the backstop, and the only gate in
# the lane that has read the artifact.

#: A path segment that must never appear in a deployable artifact.
_FORBIDDEN_SEGMENT = "node_modules"


@dataclass(frozen=True)
class ArtifactRejection:
    """Why an artifact may not be deployed, and whether trying again could help.

    ``retryable`` is decided HERE rather than by the caller because the answer differs per
    reason and is not obvious from outside: a truncated download is a transient property
    of the TRANSFER, while an unreadable full-size artifact is a durable property of what
    the BUILD produced. Collapsing the two either burns a publish on a network blip or
    spends a second sandbox re-proving a deterministic failure.
    """

    reason: str
    retryable: bool


def _readable_size(value: Any) -> int | None:
    """A positive artifact size, or ``None`` when the value is unusable.

    ``bool`` is rejected for the same reason :func:`_coerce_exit` rejects it: ``True``
    would otherwise read as a promised size of one byte.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value > 0 else None


def promised_artifact_bytes(sentinel: str | bytes | dict[str, Any] | None) -> int | None:
    """The artifact size the sentinel promised, or ``None`` when it cannot be read.

    Exists so the runner can compare the promise against what actually arrived without
    re-implementing sentinel parsing or widening :class:`BuildClassification`.
    """
    parsed = _parse_sentinel(sentinel)
    if parsed is None:
        return None
    return _readable_size(parsed.get("artifact_bytes"))


def verify_artifact(
    payload: bytes | None, *, expected_bytes: int | None = None
) -> ArtifactRejection | None:
    """Check the DOWNLOADED artifact. Returns a rejection, or ``None`` when it is fit.

    The last gate before a deploy, and the only one that reads the artifact itself rather
    than the sentinel's claims about it. Every rejection must stop the deploy rather than
    shrink it: a partial deploy and a blank site are worse outcomes than a failed publish,
    because they overwrite something that was working.

    ``expected_bytes`` is the size the sentinel promised (see
    :func:`promised_artifact_bytes`). It is what separates a transfer failure from a bad
    build, and that separation is the whole basis of the ``retryable`` split:

    * ``artifact_empty`` — nothing arrived. RETRYABLE: the build reported a size, so the
      bytes existed in the sandbox and it is the download that failed.
    * ``artifact_truncated`` — fewer bytes than promised. RETRYABLE for the same reason,
      and kept DISTINCT from empty because "nothing arrived" and "half arrived" send an
      operator to different places.
    * ``artifact_unreadable`` — the promised size arrived and will not open as a gzip tar.
      NOT retryable: the transfer did its job, so the content is what is wrong and a
      second attempt reproduces it.
    * ``artifact_contains_node_modules`` — the tar command's exclusion leaked. NOT
      retryable, and never the user's fault.

    When no size was promised, a failure defaults to RETRYABLE. That asymmetry is
    deliberate and matches ``build_state.build_is_stale``: a redundant build costs one
    sandbox, whereas calling a transient failure permanent costs the user their publish.

    Reads only the tar INDEX, so cost is bounded by member count, not by artifact size.
    """
    promised = _readable_size(expected_bytes)
    got = len(payload) if payload else 0

    if got == 0:
        return ArtifactRejection("artifact_empty", retryable=True)
    if promised is not None and got < promised:
        return ArtifactRejection("artifact_truncated", retryable=True)

    try:
        with tarfile.open(fileobj=io.BytesIO(payload or b""), mode="r:gz") as tar:
            names = tar.getnames()
    except (tarfile.TarError, OSError, EOFError, ValueError):
        # A full-size payload that will not open is garbage the build produced. With no
        # promised size we cannot tell that from a truncated transfer, so take the safe
        # direction and allow a retry.
        return ArtifactRejection("artifact_unreadable", retryable=promised is None)

    for name in names:
        # Normalise the leading "./" the tar command produces before splitting, so a
        # member recorded as "./node_modules/x" is caught the same as "node_modules/x".
        if _FORBIDDEN_SEGMENT in name.replace("\\", "/").split("/"):
            return ArtifactRejection("artifact_contains_node_modules", retryable=False)
    return None


# ---------------------------------------------------------------------------
# The in-sandbox wrapper
# ---------------------------------------------------------------------------

#: Serializing the sentinel with python3 rather than hand-rolled shell string
#: concatenation is a correctness decision, not a style one: stderr routinely contains
#: quotes, newlines and control characters, and a shell-escaped JSON writer would
#: produce an unparseable sentinel exactly when there is an error worth reading — i.e.
#: it would fail in the case it exists to serve. python3.12 is in the Daytona image by
#: construction (``daytona/image.py`` builds from ``Image.debian_slim("3.12")``).
_SENTINEL_WRITER = r"""
python3 - "$RESULT" "$STDERR_LOG" "$STARTED" "$INSTALL_EXIT" "$BUILD_EXIT" \
         "$ARTIFACT_BYTES" "$ENGINE" "$ARTIFACT_REL" <<'PYEOF'
import datetime, json, os, sys
result, log, started, inst, bld, abytes, engine, rel = sys.argv[1:9]
tail = ""
try:
    with open(log, "rb") as fh:
        try:
            fh.seek(-TAIL_BYTES, os.SEEK_END)
        except OSError:
            fh.seek(0)
        tail = fh.read().decode("utf-8", "replace")
except OSError:
    pass


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


payload = {
    "schema": 1,
    "engine": engine,
    "install_exit": _int(inst),
    "build_exit": _int(bld),
    "started_at": started,
    "finished_at": datetime.datetime.now(datetime.timezone.utc)
    .strftime("%Y-%m-%dT%H:%M:%SZ"),
    "artifact_rel": rel,
    "artifact_bytes": _int(abytes) or 0,
    "stderr_tail": tail,
}
with open(result, "w", encoding="utf-8") as fh:
    json.dump(payload, fh)
PYEOF
"""


#: Build-log substrings that mean the render FAILED even though the build may have exited
#: zero. Mirrors ``paw-sites/src/smoke.ts::KNOWN_WORKERD_FAILURES`` and the copy
#: ``generator_client._WORKERD_SSR_MARKERS`` already keeps for the inline path — a third
#: copy, and worth it, because this one has to reach the INSIDE of a sandbox that has no
#: paw-sites in it.
#:
#: WHY THE LANE NEEDS ITS OWN. An inline publish runs ``runWorkerdSmokeRender``, which
#: fails the verdict on any of these markers. The lane runs bare ``bun install`` +
#: ``bun run build``, so without this a site that prerendered a marker onto a zero exit
#: would deploy. That is not theoretical for the svelte track: ``window is not defined``
#: from a top-level import of a browser-only library is the classic Paw Site failure.
#:
#: The wrapper greps the WHOLE log rather than the sentinel's tail, which makes this
#: STRICTER than reading ``stderr_tail`` Python-side would be: a marker printed early in a
#: long ``bun install`` scrolls out of a tail and would be missed.
WORKERD_SSR_MARKERS: tuple[str, ...] = (
    "window is not defined",
    "document is not defined",
    "No such module",
)


def _render_marker_scan(markers: tuple[str, ...]) -> str:
    """The bash that fails a zero-exit build whose log carries an SSR failure marker.

    ``grep -F`` — FIXED strings, never patterns. A marker is prose ("window is not
    defined"), and letting it be read as a regex would make a future marker containing a
    ``.`` or a ``*`` match things it does not mean.

    Renders to a no-op when ``markers`` is empty, so a caller can switch the scan off
    without the script growing an empty ``if`` that always fires.
    """
    if not markers:
        return "# no SSR markers configured for this engine\n"
    tests = " || ".join(
        f'grep -qF -- {shlex.quote(marker)} "$STDERR_LOG"' for marker in markers
    )
    return (
        f"if {tests}; then\n"
        '  echo "paw: build log carries a known SSR failure marker" >>"$STDERR_LOG"\n'
        "  BUILD_EXIT=1\n"
        '  exit "$BUILD_EXIT"\n'
        "fi\n"
    )


def build_wrapper_script(
    engine: str | None,
    project_dir: str,
    *,
    timeout_seconds: int,
    artifact_path: str,
    install_command: str = "bun install",
    build_command: str = "bun run build",
    artifact_rel: str | None = None,
    ssr_markers: tuple[str, ...] = WORKERD_SSR_MARKERS,
) -> str:
    """Render the bash wrapper the sandbox runs.

    Contract, in order:

    1. ``trap … EXIT`` is installed **before anything can fail**, so the sentinel is
       written on every path — including a non-zero build, which is the case the
       classifier most needs evidence for.
    2. Install, then build, each under ``timeout(1)`` so an overrun surfaces as exit
       124 *with* a sentinel rather than as a silent hang the caller can only infer
       from its own clock.
    3. Tar exactly the engine's output dir (:func:`artifact_tar_command`) and record
       its byte size, so ``artifact_bytes`` can catch an empty build before deploy.

    ``set -u`` but deliberately **not** ``set -e``: the whole design depends on
    reaching the sentinel write with the real exit codes in hand, and ``-e`` would
    abort the script at the first failing step, which is precisely when the evidence
    matters. Every command's status is captured explicitly instead.

    ``artifact_rel`` (SL-4) names which of the engine's output dirs this build will write,
    for the one engine that has two. It reaches BOTH the tar's include-list and the
    sentinel's ``artifact_rel`` field, which is the point: the sentinel is evidence, and
    evidence claiming ``.svelte-kit/cloudflare`` for a build that wrote ``build`` sends
    whoever reads it to look in the wrong place. ``None`` keeps the nominal per-engine
    value and renders a byte-identical script.
    """
    tar_cmd = artifact_tar_command(engine, project_dir, artifact_path, output_rel=artifact_rel)
    marker_scan = _render_marker_scan(ssr_markers)
    result_path = f"{project_dir.rstrip('/')}/{BUILD_RESULT_FILENAME}"
    writer = _SENTINEL_WRITER.replace("TAIL_BYTES", str(STDERR_TAIL_BYTES))

    return f"""#!/usr/bin/env bash
# Generated by pocketpaw_ee.sites.daytona_build — do not edit in place.
set -u

PROJECT={shlex.quote(project_dir)}
RESULT={shlex.quote(result_path)}
STDERR_LOG=/tmp/paw-build-stderr.log
ARTIFACT={shlex.quote(artifact_path)}
ENGINE={shlex.quote(normalize_engine(engine))}
ARTIFACT_REL={shlex.quote(artifact_rel or static_output_rel(engine))}
STARTED="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

# -1 distinguishes "never ran" from "ran and returned 0". A step that never ran
# leaves -1, which classify_build reads as a missing exit code and fails closed.
INSTALL_EXIT=-1
BUILD_EXIT=-1
ARTIFACT_BYTES=0

: > "$STDERR_LOG"

# Installed BEFORE the first command that can fail, so no path skips the sentinel.
write_result() {{{writer}}}
trap write_result EXIT

cd "$PROJECT" || exit 1

timeout {timeout_seconds}s {install_command} >>"$STDERR_LOG" 2>&1
INSTALL_EXIT=$?
if [ "$INSTALL_EXIT" -ne 0 ]; then
  exit "$INSTALL_EXIT"
fi

timeout {timeout_seconds}s {build_command} >>"$STDERR_LOG" 2>&1
BUILD_EXIT=$?
if [ "$BUILD_EXIT" -ne 0 ]; then
  exit "$BUILD_EXIT"
fi

# A CLEAN EXIT IS NOT A CLEAN RENDER. SvelteKit's prerender pass reports an SSR throw in
# the log and can still exit zero, so the marker scan runs on a build that "succeeded".
# Rewriting BUILD_EXIT rather than inventing a new sentinel field is deliberate: the
# outcome IS a build failure, it belongs to the user, and classify_build already routes a
# non-zero build exit to ``build_failed`` with the stderr tail attached — which is exactly
# where the marker the user has to act on already is.
{marker_scan}
{tar_cmd} >>"$STDERR_LOG" 2>&1
if [ -f "$ARTIFACT" ]; then
  ARTIFACT_BYTES=$(wc -c < "$ARTIFACT" | tr -d '[:space:]')
fi

exit 0
"""


__all__ = [
    "BUILD_RESULT_FILENAME",
    "SENTINEL_SCHEMA",
    "STDERR_TAIL_BYTES",
    "TIMEOUT_FLOOR_SECONDS",
    "TIMEOUT_SAFETY_FACTOR",
    "ArtifactRejection",
    "BuildClassification",
    "BuildOutcome",
    "artifact_tar_command",
    "build_timeout_seconds",
    "WORKERD_SSR_MARKERS",
    "build_wrapper_script",
    "classify_build",
    "promised_artifact_bytes",
    "resolve_build_timeout_seconds",
    "verify_artifact",
]
