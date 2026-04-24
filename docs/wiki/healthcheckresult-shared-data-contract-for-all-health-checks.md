---
{
  "title": "HealthCheckResult — Shared Data Contract for All Health Checks",
  "summary": "`HealthCheckResult` is the dataclass that all PocketPaw health check functions return, providing a consistent schema with check identity, status severity, human-readable message, and an actionable fix hint. It auto-stamps a UTC ISO-8601 timestamp on creation and includes a `to_dict()` serializer for API and storage use.",
  "concepts": [
    "HealthCheckResult",
    "dataclass",
    "status levels",
    "ok warning critical",
    "check_id",
    "fix_hint",
    "UTC timestamp",
    "to_dict",
    "health check contract",
    "serialization"
  ],
  "categories": [
    "health monitoring",
    "data models"
  ],
  "source_docs": [
    "e67ada89ebe0bf12"
  ],
  "backlinks": null,
  "word_count": 461,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`result.py` defines the single shared data contract used by every health check in PocketPaw. Without a common return type, the health engine could not aggregate, store, or display check results uniformly. `HealthCheckResult` is that contract.

## Fields

```python
@dataclass
class HealthCheckResult:
    check_id: str       # e.g. "api_key_primary"
    name: str           # e.g. "Primary API Key"
    category: str       # "config" | "connectivity" | "storage" | "updates" | "integrations"
    status: str         # "ok" | "warning" | "critical"
    message: str        # e.g. "Anthropic API key is configured"
    fix_hint: str       # e.g. "Set your API key in Settings > API Keys"
    timestamp: str = "" # Auto-set to UTC ISO-8601 on creation
    details: list[str] | None = None
```

The `check_id` is a stable machine identifier (snake_case), used as a dictionary key in the playbooks system and for deduplication in the error store. The `name` is a human-readable label for the UI.

## Auto-Timestamping via `__post_init__`

```python
def __post_init__(self):
    if not self.timestamp:
        self.timestamp = datetime.now(tz=UTC).isoformat()
```

The timestamp is set in `__post_init__` rather than as a default factory because the dataclass needs to allow callers to supply a timestamp explicitly (e.g., when reconstructing from stored JSON). A `field(default_factory=...)` would not allow this override pattern cleanly. The `if not self.timestamp` guard means callers can pass an empty string to trigger auto-stamping, or a real timestamp to preserve it.

All timestamps use UTC explicitly (`tz=UTC`) to avoid timezone ambiguity in logs and APIs.

## Status Levels

The three-tier status model reflects operational severity:

- **ok** — check passed, no action needed.
- **warning** — degraded or uncertain state; agent may still work but something is suboptimal (missing optional tool, pending update, borderline disk usage).
- **critical** — agent is likely broken or will fail for users (missing API key, unreachable LLM, unwritable audit log).

This maps cleanly to UI badge colours and alert thresholds without needing a numeric severity scale.

## Serialization via `to_dict()`

```python
def to_dict(self) -> dict:
    return {
        "check_id": ..., "name": ..., "category": ...,
        "status": ..., "message": ..., "fix_hint": ...,
        "timestamp": ..., "details": ...,
    }
```

`to_dict()` exists alongside the dataclass because FastAPI response serialization and the error store both need plain dicts. Using `dataclasses.asdict()` would also work, but an explicit method gives control over field ordering and excludes internal state if any is added later.

## Known Gaps

- `status` is typed as `str` rather than a `Literal["ok", "warning", "critical"]` or enum. This means typos in check functions would only surface at runtime, not at type-check time.
- `details` is `list[str] | None` but is never populated by any of the current check implementations — it appears reserved for future multi-line diagnostic output.
- `category` is also an unconstrained string; a future refactor could use an enum or `Literal` to enforce valid categories.
