# Calendar module — RRULE recurrence expansion.
# Created: 2026-05-19 (feat/calendar-module).
#
# Pure-function module. No DB, no bus, no I/O. Takes a master Event with a
# Recurrence and expands to concrete instances within a window. Exceptions
# are honoured. UNTIL / COUNT terminators are honoured. RRULE strings may
# omit DTSTART — we synthesize one from master.starts_at if missing.

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from dateutil.rrule import rrulestr  # type: ignore[import-untyped]

from ee.calendar.domain import Event

logger = logging.getLogger(__name__)


# Hard cap on expansion to avoid runaway memory / pathological RRULEs.
# 5000 instances covers ~13 years of daily recurrence — well beyond any
# legitimate use case.
_MAX_INSTANCES = 5000


def parse_rrule(rrule_str: str, dtstart: datetime | None = None) -> Any:
    """Parse an RFC 5545 RRULE string into a dateutil.rrule.rrule.

    If `dtstart` is supplied and the RRULE doesn't carry its own DTSTART
    line, it's injected. Raises ``ValueError`` on malformed input.
    """
    try:
        return rrulestr(rrule_str, dtstart=dtstart)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"Invalid RRULE: {rrule_str!r} — {exc}") from exc


def expand_recurrence(
    master: Event,
    range_start: datetime,
    range_end: datetime,
) -> list[Event]:
    """Expand a recurring master event into concrete instances overlapping
    [range_start, range_end).

    Rules:
    * If master has no `recurrence`, return [master] when its window overlaps
      the range, else [].
    * `range_start` inclusive, `range_end` exclusive.
    * `recurrence.exceptions` removes matching occurrences (compared to the
      generated start datetime, not the human-visible local date).
    * `recurrence.until` and `recurrence.count` are honoured if the underlying
      RRULE didn't already encode them.
    * Each expanded instance keeps the master's id (callers can disambiguate
      via starts_at).
    """
    if range_end <= range_start:
        return []

    rec = master.recurrence
    if rec is None:
        if master.ends_at > range_start and master.starts_at < range_end:
            return [master]
        return []

    duration = master.ends_at - master.starts_at
    if duration <= timedelta(0):
        # Defensive: zero-length master shouldn't exist (dto enforces it) but
        # guard the expansion just in case bad data lands via sync.
        duration = timedelta(minutes=1)

    rule = parse_rrule(rec.rrule, dtstart=master.starts_at)

    # Exception list — compare on UTC datetimes by normalising both sides.
    exceptions = {_normalize(e) for e in rec.exceptions}

    instances: list[Event] = []
    count_remaining = rec.count if rec.count is not None else None

    # `between` returns occurrences in [range_start - duration, range_end).
    # We shift the lower bound back by `duration` so that events starting
    # before `range_start` but ending inside the window are still surfaced.
    # `inc=True` includes equal endpoints which matches our "starts_at <
    # range_end" semantics when paired with the duration shift.
    window_start = range_start - duration
    occurrences = rule.between(window_start, range_end, inc=True)

    for occ in occurrences:
        if rec.until is not None and occ > rec.until:
            break
        if count_remaining is not None:
            if count_remaining <= 0:
                break
            count_remaining -= 1
        if _normalize(occ) in exceptions:
            continue
        if len(instances) >= _MAX_INSTANCES:
            logger.warning(
                "expand_recurrence hit max instances cap of %d for event %s",
                _MAX_INSTANCES,
                master.id,
            )
            break
        instance_end = occ + duration
        # Filter out occurrences entirely before the window — `between` with
        # the duration shift includes them.
        if instance_end <= range_start:
            continue
        instances.append(
            master.model_copy(
                update={
                    "starts_at": occ,
                    "ends_at": instance_end,
                }
            )
        )

    return instances


def _normalize(dt: datetime) -> datetime:
    """Strip tzinfo for set-membership comparison. dateutil produces tz-aware
    datetimes when DTSTART is tz-aware; exceptions arriving from external
    sync may be naive. We compare on naive UTC equivalents."""
    if dt.tzinfo is None:
        return dt
    # Convert to UTC and drop tzinfo.
    from datetime import UTC

    return dt.astimezone(UTC).replace(tzinfo=None)
