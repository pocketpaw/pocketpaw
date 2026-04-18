---
{
  "title": "TimestampedDocument — Base Model with Automatic Timestamps",
  "summary": "Base Beanie document class that automatically manages `createdAt` and `updatedAt` timestamps using Beanie's event hook system. All cloud domain documents inherit from this class.",
  "concepts": [
    "TimestampedDocument",
    "Beanie Document",
    "before_event",
    "createdAt",
    "updatedAt",
    "state management",
    "event hooks",
    "UTC timestamps"
  ],
  "categories": [
    "models",
    "base classes",
    "database",
    "infrastructure"
  ],
  "source_docs": [
    "cfa3cc57b97f98e2"
  ],
  "backlinks": null,
  "word_count": 278,
  "compiled_at": "2026-04-08T07:26:37Z",
  "compiled_with": "agent",
  "version": 1
}
---

# TimestampedDocument — Base Model with Automatic Timestamps

`cloud/models/base.py`

## Purpose

This module provides the foundational document class for all cloud models. `TimestampedDocument` extends Beanie's `Document` with automatic timestamp management, eliminating the need for every model to manually set creation and update times.

## How It Works

### Fields
- `createdAt: datetime` — Set once when the document is first inserted. Default factory generates the current UTC time.
- `updatedAt: datetime` — Updated on every save/replace/update operation.

### Event Hooks

Beanie's `@before_event` decorator registers methods that run before specific database operations:

- `@before_event(Insert)` on `_set_created` — Before the first insert, sets both `createdAt` and `updatedAt` to the current time. Setting both ensures they match on creation.
- `@before_event(Replace, Save, Update)` on `_set_updated` — Before any modification, refreshes `updatedAt`. This covers all Beanie save patterns: `.save()` (Save), `.replace()` (Replace), and `.update()` (Update).

### State Management

`use_state_management = True` in Settings enables Beanie's state tracking. This means Beanie tracks which fields changed since the last load, enabling partial updates (only changed fields are sent to MongoDB) and optimistic concurrency control.

## Why This Exists

Without this base class, every document would need to:
1. Define timestamp fields
2. Remember to set them on create and update
3. Handle the UTC timezone correctly

Centralizing this in a base class prevents timestamp bugs (wrong timezone, forgotten update, inconsistent field names) across all models.

## Design Decisions

- **camelCase field names** (`createdAt` not `created_at`): Matches the frontend/MongoDB convention, avoiding the need for field aliases.
- **UTC explicitly**: Uses `datetime.now(UTC)` rather than `datetime.utcnow()` (which is deprecated and timezone-naive).
- **Default factory**: Fields have default factories so documents can be created without explicitly passing timestamps.
