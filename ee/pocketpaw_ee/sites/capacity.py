# ee/pocketpaw_ee/sites/capacity.py — build admission when the Daytona org is full.
#
# Daytona caps an organization's TOTAL cpu / memory / disk across running sandboxes
# (e.g. "Total memory limit exceeded. Maximum allowed: 10GiB."). A site build asks for
# 4 GiB, so a few idle agent boxes are enough to refuse every build. That refusal is
# not an outage: a slot frees as soon as another sandbox stops or is deleted.
#
# This module owns three things the build jobs share:
#   * ``is_capacity_error`` — the NARROW classifier. Only a ``DaytonaValidationError``
#     whose message names an org resource limit counts; every other create failure
#     (Daytona unconfigured, auth, network, a bad image) stays ``no_sandbox``.
#   * ``next_retry_delay`` — the jittered backoff the jobs feed to arq's ``Retry``.
#     The schedule is bounded (about five minutes in total); ``None`` means the budget
#     is spent and the job settles as ``sandbox_unavailable:capacity``.
#   * ``reason_message`` — the one sentence agent tools show for these rungs, so no
#     caller has to improvise "the build server is down" out of a machine reason.
#
# The arq functions that retry register ``max_tries=CAPACITY_MAX_TRIES``; the schedule
# and that cap are one constant so they cannot drift.
from __future__ import annotations

import random
import re
from typing import Any

#: The cause half of ``sandbox_unavailable:<cause>`` once the capacity budget is spent.
CAPACITY_CAUSE = "capacity"
#: The cause for a sandbox that could not be created for any other reason.
NO_SANDBOX_CAUSE = "no_sandbox"
#: The reason a build carries while it waits (status stays ``queued``).
WAITING_REASON = "waiting_for_capacity"

#: Seconds to wait before each capacity retry. Nominal total 270s; with jitter and the
#: create round-trips it lands near five minutes. Long enough for an idle box to hit
#: its auto-stop or a running build to finish, short enough that a user who is
#: watching is told the truth before they give up.
RETRY_DELAYS_SECONDS: tuple[int, ...] = (15, 30, 45, 60, 60, 60)
#: ``max_tries`` for the arq functions that retry on capacity: the first try plus one
#: per delay.
CAPACITY_MAX_TRIES = 1 + len(RETRY_DELAYS_SECONDS)
#: +/- fraction applied to each delay so a burst of refused builds does not retry in
#: lockstep and refill the org at the same instant.
_JITTER = 0.25

# Daytona's org-limit messages: "Total memory limit exceeded. Maximum allowed: 10GiB.",
# "Total CPU limit exceeded…", "Total disk limit exceeded…", plus the concurrent-sandbox
# quota. Anchored on "limit/quota exceeded" so an unrelated validation error (a bad
# image name, an invalid resource request) never reads as capacity.
_CAPACITY_PATTERN = re.compile(
    r"total\s+(?:cpu|memory|disk|gpu)\s+limit\s+exceeded"
    r"|(?:concurrent|running|active)\s+sandbox(?:es)?\s+(?:limit|quota)\s+(?:exceeded|reached)"
    r"|sandbox\s+(?:limit|quota)\s+(?:exceeded|reached)",
    re.IGNORECASE,
)

_VALIDATION_ERROR_NAME = "DaytonaValidationError"


def _is_validation_error(exc: BaseException) -> bool:
    try:
        from daytona import DaytonaValidationError
    except Exception:  # noqa: BLE001 — SDK absent: fall back to the class name
        return any(cls.__name__ == _VALIDATION_ERROR_NAME for cls in type(exc).__mro__)
    return isinstance(exc, DaytonaValidationError)


def is_capacity_error(exc: BaseException | None) -> bool:
    """True when ``exc`` (or an exception it was raised from) is Daytona refusing a
    create because the organization's resource limit is full."""
    seen: set[int] = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        if _is_validation_error(exc) and _CAPACITY_PATTERN.search(str(exc)):
            return True
        exc = exc.__cause__ or exc.__context__
    return False


def next_retry_delay(ctx: Any, *, rng: random.Random | None = None) -> float | None:
    """Seconds to defer the next try, or ``None`` when the capacity budget is spent.

    Keyed on arq's ``job_try`` (1 on the first run). A ``ctx`` without one is a direct
    call (a test, a script) and gets no retry, so nothing outside arq ever loops."""
    job_try = ctx.get("job_try") if isinstance(ctx, dict) else None
    if not isinstance(job_try, int) or job_try < 1 or job_try > len(RETRY_DELAYS_SECONDS):
        return None
    base = RETRY_DELAYS_SECONDS[job_try - 1]
    roll = (rng or random).uniform(-_JITTER, _JITTER)
    return round(base * (1 + roll), 2)


CAPACITY_WAITING_MESSAGE = (
    "Build capacity is full right now; the build is queued and will start when a slot "
    "frees up. Nothing is wrong with the site."
)
CAPACITY_EXHAUSTED_MESSAGE = (
    "Build capacity is full right now; try again in a few minutes. Nothing is wrong with the site."
)
NO_SANDBOX_MESSAGE = (
    "The build service could not be reached: it is not configured or not responding "
    "on our side. Nothing is wrong with the site."
)


def reason_message(reason: str | None) -> str | None:
    """The user-facing sentence for a capacity / sandbox reason, else ``None``.

    Accepts a bare reason or ``"<rung>:<cause>"``. Only these rungs get a sentence
    here; every other reason keeps whatever its caller already says."""
    if not isinstance(reason, str) or not reason:
        return None
    rung, _, cause = reason.partition(":")
    if WAITING_REASON in (rung, cause):
        return CAPACITY_WAITING_MESSAGE
    if rung != "sandbox_unavailable":
        return None
    if cause == CAPACITY_CAUSE:
        return CAPACITY_EXHAUSTED_MESSAGE
    return NO_SANDBOX_MESSAGE


__all__ = [
    "CAPACITY_CAUSE",
    "CAPACITY_EXHAUSTED_MESSAGE",
    "CAPACITY_MAX_TRIES",
    "CAPACITY_WAITING_MESSAGE",
    "NO_SANDBOX_CAUSE",
    "NO_SANDBOX_MESSAGE",
    "RETRY_DELAYS_SECONDS",
    "WAITING_REASON",
    "is_capacity_error",
    "next_retry_delay",
    "reason_message",
]
