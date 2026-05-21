# tests/ee/calendar/test_recurrence.py — RRULE expansion tests.
# Created: 2026-05-19 (feat/calendar-module).
#
# Pure-function tests. No DB, no bus. Validate that the recurrence
# expander honours COUNT, UNTIL, exceptions, and the no-recurrence
# pass-through behaviour.

from __future__ import annotations

from datetime import datetime

import pytest

from ee.calendar.domain import Recurrence
from ee.calendar.recurrence import expand_recurrence, parse_rrule

# ---------------------------------------------------------------------------
# Daily
# ---------------------------------------------------------------------------


def test_daily_recurrence_5_days(event_factory):
    master = event_factory(
        starts_at=datetime(2026, 5, 19, 9, 0),
        ends_at=datetime(2026, 5, 19, 9, 30),
        recurrence=Recurrence(rrule="FREQ=DAILY;COUNT=5"),
    )
    instances = expand_recurrence(
        master,
        datetime(2026, 5, 19),
        datetime(2026, 5, 26),
    )
    assert len(instances) == 5
    assert instances[0].starts_at == datetime(2026, 5, 19, 9, 0)
    assert instances[-1].starts_at == datetime(2026, 5, 23, 9, 0)
    # Each instance preserves the master's duration.
    for inst in instances:
        assert (inst.ends_at - inst.starts_at).total_seconds() == 30 * 60


# ---------------------------------------------------------------------------
# Weekly with UNTIL
# ---------------------------------------------------------------------------


def test_weekly_recurrence_with_until(event_factory):
    master = event_factory(
        starts_at=datetime(2026, 1, 5, 14, 0),
        ends_at=datetime(2026, 1, 5, 15, 0),
        recurrence=Recurrence(rrule="FREQ=WEEKLY;UNTIL=20260202T000000"),
    )
    instances = expand_recurrence(
        master,
        datetime(2026, 1, 1),
        datetime(2026, 3, 1),
    )
    # Jan 5, 12, 19, 26 are within the window before UNTIL=2026-02-02.
    assert len(instances) == 4
    assert instances[0].starts_at == datetime(2026, 1, 5, 14, 0)
    assert instances[-1].starts_at == datetime(2026, 1, 26, 14, 0)


# ---------------------------------------------------------------------------
# Monthly with COUNT
# ---------------------------------------------------------------------------


def test_monthly_recurrence_with_count(event_factory):
    master = event_factory(
        starts_at=datetime(2026, 1, 15, 10, 0),
        ends_at=datetime(2026, 1, 15, 11, 0),
        recurrence=Recurrence(rrule="FREQ=MONTHLY;COUNT=3"),
    )
    instances = expand_recurrence(
        master,
        datetime(2026, 1, 1),
        datetime(2026, 12, 31),
    )
    assert len(instances) == 3
    assert [i.starts_at.month for i in instances] == [1, 2, 3]


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


def test_recurrence_with_exceptions(event_factory):
    """An instance whose start matches an exception is skipped."""
    master = event_factory(
        starts_at=datetime(2026, 5, 19, 9, 0),
        ends_at=datetime(2026, 5, 19, 9, 30),
        recurrence=Recurrence(
            rrule="FREQ=DAILY;COUNT=5",
            exceptions=[datetime(2026, 5, 21, 9, 0)],
        ),
    )
    instances = expand_recurrence(
        master,
        datetime(2026, 5, 19),
        datetime(2026, 5, 26),
    )
    starts = [i.starts_at for i in instances]
    assert datetime(2026, 5, 21, 9, 0) not in starts
    # 5 originally - 1 exception = 4 expanded.
    assert len(instances) == 4


# ---------------------------------------------------------------------------
# Terminator handling — Recurrence.until + Recurrence.count combine with RRULE
# ---------------------------------------------------------------------------


def test_recurrence_terminator_handling_until(event_factory):
    """Even with no UNTIL in the RRULE, Recurrence.until caps expansion."""
    master = event_factory(
        starts_at=datetime(2026, 5, 19, 9, 0),
        ends_at=datetime(2026, 5, 19, 9, 30),
        recurrence=Recurrence(
            rrule="FREQ=DAILY",  # unbounded
            until=datetime(2026, 5, 22, 23, 59),
        ),
    )
    instances = expand_recurrence(
        master,
        datetime(2026, 5, 19),
        datetime(2026, 6, 1),
    )
    # 5/19, 5/20, 5/21, 5/22 — Recurrence.until breaks the loop on 5/23.
    assert len(instances) == 4


def test_recurrence_terminator_handling_count(event_factory):
    """Recurrence.count overrides an unbounded RRULE."""
    master = event_factory(
        starts_at=datetime(2026, 5, 19, 9, 0),
        ends_at=datetime(2026, 5, 19, 9, 30),
        recurrence=Recurrence(
            rrule="FREQ=DAILY",  # unbounded
            count=3,
        ),
    )
    instances = expand_recurrence(
        master,
        datetime(2026, 5, 19),
        datetime(2026, 6, 1),
    )
    assert len(instances) == 3


# ---------------------------------------------------------------------------
# Pass-through (no recurrence)
# ---------------------------------------------------------------------------


def test_no_recurrence_returns_single_when_in_window(event_factory):
    master = event_factory(
        starts_at=datetime(2026, 5, 19, 9, 0),
        ends_at=datetime(2026, 5, 19, 10, 0),
    )
    instances = expand_recurrence(
        master,
        datetime(2026, 5, 19),
        datetime(2026, 5, 26),
    )
    assert len(instances) == 1
    assert instances[0] is master  # frozen — identity preserved


def test_no_recurrence_returns_empty_when_outside_window(event_factory):
    master = event_factory(
        starts_at=datetime(2026, 1, 1, 9, 0),
        ends_at=datetime(2026, 1, 1, 10, 0),
    )
    instances = expand_recurrence(
        master,
        datetime(2026, 5, 19),
        datetime(2026, 5, 26),
    )
    assert instances == []


# ---------------------------------------------------------------------------
# parse_rrule
# ---------------------------------------------------------------------------


def test_parse_rrule_valid():
    rule = parse_rrule("FREQ=DAILY;COUNT=3", dtstart=datetime(2026, 5, 19, 9, 0))
    occurrences = list(rule)
    assert len(occurrences) == 3


def test_parse_rrule_invalid_raises():
    with pytest.raises(ValueError):
        parse_rrule("NOT_A_RULE", dtstart=datetime(2026, 5, 19))
