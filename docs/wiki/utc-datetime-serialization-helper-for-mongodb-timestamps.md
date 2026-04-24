---
{
  "title": "UTC Datetime Serialization Helper for MongoDB Timestamps",
  "summary": "This module provides `iso_utc`, a one-function utility that fixes a silent timestamp corruption bug caused by MongoDB and Beanie returning naive datetime objects that JavaScript then misinterprets as local time. By re-anchoring naive datetimes to UTC before formatting, it ensures all serialized timestamps carry an unambiguous UTC offset.",
  "concepts": [
    "datetime serialization",
    "UTC",
    "naive datetime",
    "ISO 8601",
    "MongoDB timestamp",
    "Beanie ODM",
    "timezone offset",
    "JavaScript Date parsing",
    "tzinfo",
    "isoformat"
  ],
  "categories": [
    "utilities",
    "datetime",
    "cloud EE"
  ],
  "source_docs": [
    "8cdc38b25a8b4225"
  ],
  "backlinks": null,
  "word_count": 366,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`ee/cloud/shared/time.py` exists to solve a single, subtle correctness problem: MongoDB stores datetime values without timezone information, and the Python `datetime.isoformat()` method emits strings without a UTC offset for naive datetimes. JavaScript's `new Date("2026-04-18T07:00:00")` then treats that string as local time, silently shifting every timestamp displayed in the frontend by the user's UTC offset.

## The Root Cause

Beanie and PyMongo return datetime objects as Python `datetime` instances with `tzinfo=None`. This is a known behavior: MongoDB stores UTC timestamps internally, but the driver strips the timezone info on read. The result is a naive datetime that Python treats as having no timezone.

When this naive datetime is serialized with `isoformat()`, the output is `"2026-04-18T07:00:00"` — no `+00:00` suffix. According to the ISO 8601 specification and ECMAScript's date parsing rules, a datetime string without a timezone designator is parsed as local time. A user in UTC-8 would see the timestamp shifted 8 hours earlier than intended.

## The Fix

```python
def iso_utc(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.isoformat()
```

`replace(tzinfo=UTC)` does not convert the time value — it stamps the existing naive value as UTC. This is semantically correct because MongoDB always stores UTC; the naive datetime is genuinely UTC, just missing the annotation. After stamping, `isoformat()` produces `"2026-04-18T07:00:00+00:00"`, which JavaScript parses unambiguously as UTC.

## Usage Pattern

Every API response that includes a timestamp field should call `iso_utc` instead of `.isoformat()` directly. The uploads router and other cloud endpoints use it to format `createdAt`, `updatedAt`, and `deleted_at` fields:

```python
from ee.cloud.shared.time import iso_utc

response["createdAt"] = iso_utc(record.created_at)
```

The `None` passthrough means the function is safe to call on optional timestamp fields without a separate null check at the call site.

## Known Gaps

- `iso_utc` handles only `datetime` objects, not `date` objects. A `date` field without time would still serialize without timezone context, but dates are typically less sensitive to timezone shifts.
- There is no counterpart `from_iso_utc` parser for inbound request timestamps. Parsing timezone-aware strings from client requests relies on Pydantic's default datetime parsing, which is generally correct but not explicitly documented as the companion to this serializer.