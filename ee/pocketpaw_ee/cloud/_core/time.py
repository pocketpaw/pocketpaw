"""Datetime serialization helpers for cloud services.

Beanie/Mongo persists ``datetime`` values without timezone info, so reads
return naive ``datetime`` objects. ``datetime.isoformat()`` on a naive value
produces ``"2026-04-18T07:00:00"`` with no offset — JS ``new Date(...)`` then
parses that string as **local time**, shifting timestamps by the user's UTC
offset. ``iso_utc`` re-anchors naive values to UTC before formatting so the
emitted string is always unambiguous (``...+00:00``).
"""

from __future__ import annotations

from datetime import UTC, datetime


def iso_utc(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.isoformat()


__all__ = ["iso_utc"]
