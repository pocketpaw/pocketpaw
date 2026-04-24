---
{
  "title": "In-App Notification Document",
  "summary": "The `Notification` model is the persistence layer for PocketPaw's in-app notification system, storing per-user alerts with typed sources, read state, and optional expiry. It pairs with the `NotificationService` for fan-out and the realtime layer for push delivery.",
  "concepts": [
    "Notification document",
    "Beanie TimestampedDocument",
    "NotificationSource",
    "read state",
    "expiry",
    "compound index",
    "in-app alerts",
    "realtime fan-out",
    "workspace scoping"
  ],
  "categories": [
    "data-models",
    "notifications",
    "enterprise-cloud"
  ],
  "source_docs": [
    "db4729f5f0c23c31"
  ],
  "backlinks": null,
  "word_count": 503,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

The `Notification` document records every in-app alert delivered to a user: mentions, comments, replies, workspace invitations, agent completion events, and shared pocket alerts. It is a write-once record — notifications are created by the service layer and then only mutated to flip the `read` flag.

## Why a Dedicated Document?

Rather than embedding notifications inside other documents (e.g., the mentioned message or the completing agent run), a dedicated collection provides a single queryable surface for "what does this user need to see?" This separation means notification queries never compete with message or agent queries, and the notification backlog can be pruned independently.

## Field Design

**`workspace` and `recipient` (indexed)** — Both fields carry Beanie `Indexed()` wrappers. The workspace index supports workspace-scoped admin queries (e.g., "purge all notifications for a deleted workspace"). The recipient index is the hot path: nearly every notification query starts with `recipient = current_user_id`.

**`type`** — A free string representing the notification category. The module doc lists known values: `mention`, `comment`, `reply`, `invite`, `agent_complete`, `pocket_shared`. Using a string rather than an enum keeps the model open for extension as new notification types are introduced without a schema migration.

**`source: NotificationSource | None`** — An embedded sub-document that records where the notification originated. The `type` field identifies the source entity kind (e.g., `"message"`, `"pocket"`), `id` is the source entity's ID, and `pocket_id` optionally links to the parent pocket for deep-linking from the notification to the correct pocket context. Making the source optional allows system notifications (e.g., quota warnings) that have no associated entity.

**`read: bool = False`** — The read/unread state. The compound index `(recipient, read, created_at DESC)` powers the unread-count badge and the filtered unread list with a single covered query.

**`expires_at: datetime | None`** — Optional TTL marker. Unlike invites (which always expire), most notifications are permanent until explicitly dismissed. The optional expiry supports transient notifications such as "agent is running" progress alerts that should auto-clear once stale, without requiring a cron job to query and delete them.

## Compound Index

The single index `(recipient, read, created_at DESC)` is carefully ordered:

1. `recipient` as the leading key means every query can immediately scope to one user's notifications.
2. `read` as the second key lets the unread filter (`read=False`) use the index without a collection scan.
3. `created_at DESC` means the most recent notifications appear first without a sort-stage penalty.

## Integration Points

The `Notification` document is consumed by `NotificationService` (create, list, mark-read, clear-all) and surfaced through `notifications/router.py`. The `NotificationService.create` method also emits a `NotificationNew` realtime event immediately after insert, so connected WebSocket clients receive push delivery without polling.

## Known Gaps

- `expires_at` is not enforced as a MongoDB TTL index (`expireAfterSeconds`). Documents with a past `expires_at` remain in the collection until explicitly queried and deleted.
- The `type` field is unvalidated — any string is accepted. A future migration to a `Literal` or `Enum` would provide earlier error detection.
- No workspace-level index for admin bulk operations (e.g., purging all notifications when a workspace is deleted).