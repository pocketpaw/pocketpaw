"""Tests for ee.cloud._core.time.iso_utc."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

from pocketpaw_ee.cloud._core.time import iso_utc


def test_none_returns_none() -> None:
    assert iso_utc(None) is None


def test_naive_datetime_anchored_to_utc() -> None:
    naive = datetime(2026, 4, 27, 12, 0, 0)
    assert iso_utc(naive) == "2026-04-27T12:00:00+00:00"


def test_aware_utc_passthrough() -> None:
    aware = datetime(2026, 4, 27, 12, 0, 0, tzinfo=UTC)
    assert iso_utc(aware) == "2026-04-27T12:00:00+00:00"


def test_aware_non_utc_preserved() -> None:
    """Non-UTC tz is preserved (we only re-anchor naive values)."""
    pst = timezone(timedelta(hours=-8))
    aware = datetime(2026, 4, 27, 4, 0, 0, tzinfo=pst)
    assert iso_utc(aware) == "2026-04-27T04:00:00-08:00"
