# ee/pocketpaw_ee/sites/build_state.py — the ephemeral-build lane's lifecycle state
# machine and bounded single-flight guard. Pure functions; no Beanie, no I/O.
#
# Created 2026-08-09 (SG-9i): a cold-per-build lane cannot sit in a request-synchronous
# publish path, so a publish has to enqueue and return. That needs somewhere to record
# where a build got to, and a guard that stops two publishes of the same site racing
# into two sandboxes.
#
# MODELLED ON DP0-4 (``sites/service.py:_provisioning_is_stale``) because that pattern
# already solved the hard half correctly: status alone is a ONE-WAY DOOR. A job that no
# worker ever consumed, or that died before writing a terminal status, pins the row in
# ``building`` forever and turns every later publish into a silent no-op — an
# unpublishable site with no error to see. Stamping the entry is what lets the service
# re-enqueue once the window lapses.
#
# TWO THINGS DP0-4 GOT WRONG THAT THIS DOES DIFFERENTLY, both deliberate:
#
#   1. ITS WINDOW IS A CONSTANT (30 minutes). Here the window is derived from the
#      build's OWN timeout, because that timeout already bounds how long a live build
#      can legitimately run. A constant is wrong in both directions: too short and it
#      re-enqueues on top of a healthy long build (two sandboxes, two bills, a racing
#      artifact), too long and a stuck row blocks publishes for half an hour.
#
#   2. ITS JOB ID IS A TRANSIENT ``PrivateAttr``. Ours is persisted. A queued build is
#      precisely when a user reloads, and a transient id is gone on reload — so the
#      client loses its polling handle at the moment the wait is longest.
#
# WHY ``queued`` IS A SEPARATE STATE FROM ``building``: without it, a publish waiting
# behind the concurrency cap looks identical to one that is stuck, and the cap converts
# a crash into a support ticket. This is the state that makes a cap safe to turn on.
#
# ┌───────────────────────────────────────────────────────────────────────────────────┐
# │ ADDING A NEW IN-FLIGHT STATE HAS A DEPLOY-ORDERING CONSTRAINT.                     │
# └───────────────────────────────────────────────────────────────────────────────────┘
#
# The two halves of this feature resolve an UNKNOWN status in opposite directions, and
# both are right on their own axis:
#
#   * the wire (``SiteResponse``) tells clients to treat an unrecognised status as
#     IN-PROGRESS, so growing the vocabulary never shows a user a spurious error;
#   * ``should_enqueue`` here treats an unrecognised status as TERMINAL and enqueues,
#     because a redundant build costs one sandbox while a stuck guard costs the site
#     every future publish.
#
# The consequence: a new in-flight state must be present in ``IN_FLIGHT_STATUSES`` on
# EVERY READER before any writer is allowed to emit it. Get that backwards during a
# rolling deploy and an old reader sees a new writer's in-flight row as terminal and
# starts a second sandbox on top of a live build — two bills and two artifacts racing to
# deploy, which is precisely the case this guard exists to prevent. Deploy readers first,
# writers second.
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Literal, get_args

#: none     — never built.
#: queued   — enqueued, no sandbox yet. Waiting on the concurrency cap.
#: building — a sandbox exists and the build is running.
#: built    — a verified artifact was produced.
#: failed   — terminal. Either the user's build broke or we gave up retrying.
BuildStatus = Literal["none", "queued", "building", "built", "failed"]

#: The states a build can be in while still in flight. Anything else is terminal, and a
#: terminal row never blocks a new publish. This is the SOLE authority — ``should_enqueue``
#: gates on it directly.
IN_FLIGHT_STATUSES: frozenset[str] = frozenset({"queued", "building"})

#: DERIVED, never hand-listed. Written out by hand it was decorative and nothing read it,
#: which made it a latent lie: add a state to ``BuildStatus`` and forget one of the two
#: sets and the exported constant starts misdescribing the machine. Deriving makes them
#: impossible to desync, and the subtraction fails loudly if a state is ever in
#: IN_FLIGHT but missing from the Literal.
TERMINAL_STATUSES: frozenset[str] = frozenset(get_args(BuildStatus)) - IN_FLIGHT_STATUSES

#: Added to the build timeout to get the staleness window. Covers the parts of a build
#: attempt that sit OUTSIDE the in-sandbox timeout — sandbox create, upload, artifact
#: download, teardown — plus queue wait. Measured live at ~5s of overhead per build
#: (react 8.7s total against a 2.9s build), so 10 minutes is generous on purpose: the
#: window exists to unstick a dead row, not to police a slow one.
STALE_MARGIN = timedelta(minutes=10)


def stale_after(timeout_seconds: int) -> timedelta:
    """The staleness window for a build whose own budget is ``timeout_seconds``.

    Derived rather than constant — see the module header for why a fixed window is
    wrong in both directions. A non-positive timeout falls back to the margin alone
    rather than producing a zero or negative window, which would make every in-flight
    build read as stale and defeat the guard.
    """
    return timedelta(seconds=max(0, timeout_seconds)) + STALE_MARGIN


def _read_stamp(value: Any) -> datetime | None:
    """Coerce a stamp to an aware datetime, or ``None`` when it is unusable.

    A naive datetime is assumed UTC — the writers all stamp UTC, and treating a naive
    stamp as unreadable would make every row written by an older writer look stale.
    """
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def build_is_stale(doc: Any, timeout_seconds: int, *, now: datetime | None = None) -> bool:
    """True when an in-flight build's stamp is older than its window.

    A MISSING OR UNREADABLE STAMP READS AS STALE. That is the same asymmetric-failure
    call DP0-4 made and it is the safe direction: a redundant enqueue costs one
    idempotent build, while a stuck guard costs the site every future publish. Getting
    this backwards produces a site that silently cannot be republished.
    """
    stamp = _read_stamp(getattr(doc, "build_started_at", None))
    if stamp is None:
        return True
    reference = now or datetime.now(UTC)
    if reference.tzinfo is None:  # pragma: no cover - defensive
        reference = reference.replace(tzinfo=UTC)
    return reference - stamp > stale_after(timeout_seconds)


def should_enqueue(doc: Any, timeout_seconds: int, *, now: datetime | None = None) -> bool:
    """Should a publish enqueue a build for this site?

    Yes unless a build is genuinely in flight — i.e. the status is ``queued`` or
    ``building`` AND its stamp is inside the window. A terminal status never blocks:
    ``built`` and ``failed`` are both legitimate starting points for a rebuild, and
    treating ``failed`` as blocking would mean one bad build wedges the site until
    someone edits the database.
    """
    status = getattr(doc, "build_status", None)
    if status not in IN_FLIGHT_STATUSES:
        return True
    return build_is_stale(doc, timeout_seconds, now=now)


def is_in_flight(doc: Any) -> bool:
    """True when the row CLAIMS a build is running, ignoring staleness.

    Distinct from ``not should_enqueue(...)`` on purpose: this is what a UI should read
    to decide whether to show progress, while ``should_enqueue`` is what the service
    reads to decide whether to spend money. A stale row should still render as
    in-flight to a viewer — it just must not block the next publish.
    """
    return getattr(doc, "build_status", None) in IN_FLIGHT_STATUSES


__all__ = [
    "IN_FLIGHT_STATUSES",
    "STALE_MARGIN",
    "TERMINAL_STATUSES",
    "BuildStatus",
    "build_is_stale",
    "is_in_flight",
    "should_enqueue",
    "stale_after",
]
