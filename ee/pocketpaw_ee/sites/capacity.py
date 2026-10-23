# ee/pocketpaw_ee/sites/capacity.py — build admission when the Daytona org is full.
#
# Daytona caps an organization's TOTAL cpu / memory / disk across running sandboxes
# (e.g. "Total memory limit exceeded. Maximum allowed: 10GiB."). A site build asks for
# 4 GiB, so a few idle agent boxes are enough to refuse every build. That refusal is
# not an outage: a slot frees as soon as another sandbox stops or is deleted.
#
# This module owns what the build jobs share:
#   * ``is_capacity_error`` — the NARROW classifier. Only a ``DaytonaValidationError``
#     whose message names an org resource limit counts; every other create failure
#     (Daytona unconfigured, auth, network, a bad image) stays ``no_sandbox``.
#   * ``next_retry_delay`` — the jittered backoff the jobs feed to arq's ``Retry``.
#     The schedule is bounded (about five minutes in total); ``None`` means the budget
#     is spent and the job settles as ``sandbox_unavailable:capacity``.
#   * ``reason_message`` — the one sentence users and agent tools see for the reasons
#     no build log explains: capacity waits, a sandbox we never got, a job arq stopped at
#     its own timeout (``timed_out:job_timeout``) and a job that crashed on our side
#     (``job_crashed:unhandled_exception``), so no caller improvises "the build server is
#     down" out of a machine reason.
#   * ``describe_refusal`` — which limit a refusal names (cpu / memory / disk / gpu /
#     sandbox_count) plus Daytona's message with secrets redacted, for the WARNING every
#     failed create logs (``admission.create_admitted``). ``redact`` is that scrubber on
#     its own, for any exception text headed for a log line.
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

_RESOURCE_PATTERN = re.compile(r"total\s+(cpu|memory|disk|gpu)\s+limit", re.IGNORECASE)
# Things that must never reach a log line: auth headers, key=value secrets, and long
# opaque tokens (API keys, signed URLs' signatures).
_SECRET_PATTERNS = (
    re.compile(r"(?i)(bearer|basic)\s+[A-Za-z0-9._~+/=-]+"),
    re.compile(
        r"(?i)((?:api[_-]?key|token|secret|password|authorization|signature|sig)"
        r"[\"']?\s*[:=]\s*[\"']?)[^\s\"'&,;]+"
    ),
    # No dashes, so UUIDs and our sandbox names (short dash-separated parts) survive.
    re.compile(r"[A-Za-z0-9_]{32,}"),
)
_MESSAGE_CAP = 2000


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


def redact(text: str, *, cap: int = _MESSAGE_CAP) -> str:
    """``text`` with auth headers, ``key=value`` secrets and long opaque tokens replaced
    by ``[redacted]``, cut to ``cap`` characters. For exception text headed for a log."""
    text = _SECRET_PATTERNS[0].sub(r"\1 [redacted]", text)
    text = _SECRET_PATTERNS[1].sub(r"\1[redacted]", text)
    text = _SECRET_PATTERNS[2].sub("[redacted]", text)
    return text[:cap]


def describe_refusal(exc: BaseException) -> tuple[str | None, str]:
    """``(limit, message)`` for a failed create. ``limit`` is ``cpu`` / ``memory`` /
    ``disk`` / ``gpu`` / ``sandbox_count`` when ``exc`` is a capacity refusal (see
    :func:`is_capacity_error`), else ``None``. ``message`` is the full text, secrets
    redacted, capped at a couple of thousand characters."""
    message = redact(str(exc))
    if not is_capacity_error(exc):
        return None, message
    found = _RESOURCE_PATTERN.search(message)
    return (found.group(1).lower() if found else "sandbox_count"), message


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

#: arq stopped the job at its own timeout. Reuses the classifier's ``timed_out`` rung;
#: the cause tells it apart from an in-sandbox timeout, which has a build log to show.
JOB_TIMEOUT_REASON = "timed_out:job_timeout"
#: The job raised something we did not plan for: a bug on our side, not the site's and
#: not a sandbox we never got. ``job_crashed`` is the rung (``build_job.RUNG_JOB_CRASHED``).
JOB_CRASHED_REASON = "job_crashed:unhandled_exception"

JOB_TIMEOUT_MESSAGE = (
    "The build ran past its time limit and was stopped before it finished. Try again in "
    "a few minutes; if it keeps happening, the build itself may be too slow."
)
JOB_CRASHED_MESSAGE = (
    "The build stopped because of an error on our side, not in the site. Nothing is "
    "wrong with the site; try again in a few minutes."
)


def reason_message(reason: str | None) -> str | None:
    """The user-facing sentence for a capacity / sandbox reason, else ``None``.

    Accepts a bare reason or ``"<rung>:<cause>"``. Only these reasons get a sentence
    here; every other reason (a settled in-sandbox ``timed_out:*`` included, which has a
    build log) keeps whatever its caller already says."""
    if not isinstance(reason, str) or not reason:
        return None
    rung, _, cause = reason.partition(":")
    if WAITING_REASON in (rung, cause):
        return CAPACITY_WAITING_MESSAGE
    if reason == JOB_TIMEOUT_REASON:
        return JOB_TIMEOUT_MESSAGE
    if rung == JOB_CRASHED_REASON.partition(":")[0]:
        return JOB_CRASHED_MESSAGE
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
    "JOB_CRASHED_MESSAGE",
    "JOB_CRASHED_REASON",
    "JOB_TIMEOUT_MESSAGE",
    "JOB_TIMEOUT_REASON",
    "NO_SANDBOX_CAUSE",
    "NO_SANDBOX_MESSAGE",
    "RETRY_DELAYS_SECONDS",
    "WAITING_REASON",
    "describe_refusal",
    "is_capacity_error",
    "next_retry_delay",
    "reason_message",
    "redact",
]
