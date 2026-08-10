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
from __future__ import annotations

import json
import logging
import math
import os
import shlex
from dataclasses import dataclass
from typing import Any, Literal

from pocketpaw_ee.sites.engines import normalize_engine, static_output_rel

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
        deploy", which overpromised in a way that matters. It is derived from the sentinel
        alone, so it stays True when the download afterwards returns zero bytes or a
        truncated payload — nothing here has seen the artifact.

        A caller must therefore gate a deploy on ``BuildRunResult.ok`` (which also requires
        bytes to have arrived), never on this flag by itself. Whoever wires this lane owns
        the download verification; see the SG-7 wiring contracts.
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
    """
    rel = static_output_rel(engine)
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


def build_wrapper_script(
    engine: str | None,
    project_dir: str,
    *,
    timeout_seconds: int,
    artifact_path: str,
    install_command: str = "bun install",
    build_command: str = "bun run build",
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
    """
    tar_cmd = artifact_tar_command(engine, project_dir, artifact_path)
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
ARTIFACT_REL={shlex.quote(static_output_rel(engine))}
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
    "BuildClassification",
    "BuildOutcome",
    "artifact_tar_command",
    "build_timeout_seconds",
    "build_wrapper_script",
    "classify_build",
    "resolve_build_timeout_seconds",
]
