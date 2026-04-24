---
{
  "title": "Daily Notes Isolation Tests: Per-User Scoping and Backward-Compatible Legacy Note Visibility",
  "summary": "This test file, added during security sprint cluster D (issue #887), validates that daily notes in the `MemoryManager` are scoped to the `sender_id` that created them, preventing one user's private notes from leaking into another user's agent context. It also tests the backward-compatibility guarantee that legacy notes written before the fix remain visible as system-wide entries.",
  "concepts": [
    "daily notes",
    "sender_id",
    "user isolation",
    "MemoryManager",
    "FileMemoryStore",
    "_resolve_user_id",
    "DAILY memory type",
    "multi-user scoping",
    "legacy note backward compat",
    "metadata user_id",
    "security sprint"
  ],
  "categories": [
    "security",
    "testing",
    "memory",
    "multi-user isolation",
    "test"
  ],
  "source_docs": [
    "b4b05b56a7adfa9b"
  ],
  "backlinks": null,
  "word_count": 517,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

PocketPaw supports multi-user deployments where multiple humans share a single agent instance. Daily notes — short-term reminders and context entries created via `manager.note()` — were originally stored without user attribution. In a multi-user deployment, this meant every user's notes were visible to every other user's agent context. Issue #887 added `sender_id`-based scoping to fix this, and these tests define the exact isolation contract.

## Fixture Design

The `manager` fixture creates a `MemoryManager` backed by a `FileMemoryStore` in a temp directory. It also monkeypatches `settings.owner_id` to `"owner"` — a non-default value. This is important: if `owner_id` were left at its default, `_resolve_user_id()` might return `"default"` for all users, making the isolation test a false positive (both Alice and Bob would get the same scoped ID, appearing isolated when they're actually not).

## Test 1: Notes Carry User ID Metadata

`test_note_records_sender_id` calls `manager.note("alice's groceries list", sender_id="alice")` and then reads all `DAILY` type entries from the store. It asserts that the returned entry has `metadata["user_id"]` equal to `manager._resolve_user_id("alice")`. This confirms the fix was applied at the write path — the user ID is stamped onto the entry when it is created, not derived at read time.

## Test 2: Context Excludes Cross-User Notes

`test_context_excludes_other_users_daily_notes` is the core isolation test. It writes one note for `"alice"` and one for `"bob"`, then calls `get_context_for_agent(sender_id="alice")` and `get_context_for_agent(sender_id="bob")`. Four assertions are checked:
- Alice's note is in Alice's context.
- Bob's note is **not** in Alice's context (the cross-user leak that was the original bug).
- Bob's note is in Bob's context.
- Alice's note is **not** in Bob's context.

The error message on the cross-user assertion explicitly names the failure mode: `"Cross-user daily-note leak: alice saw bob's note"`, making CI failures immediately actionable.

## Test 3: Legacy Notes Without User ID Remain Visible

`test_legacy_notes_without_sender_id_are_visible` directly injects a `MemoryEntry` with no `user_id` in its metadata — simulating notes written by operators or by older versions of PocketPaw before the fix. It then asserts this legacy note appears in Alice's context.

This backward-compatibility guarantee is intentional: operators who upgraded PocketPaw after writing system-wide notes (e.g., deployment instructions, shared context) should not silently lose access to that context after the upgrade. The docstring explains the reasoning: treat missing `user_id` as system-wide rather than dropping the entry.

## Why This Pattern Matters

The failure mode this tests against — a shared memory store that doesn't scope reads by user — is a common class of multi-tenancy bug. It's invisible in single-user testing, requires no malicious action to trigger, and can expose sensitive personal information (medical appointments, financial notes, personal goals) to unrelated users sharing the same PocketPaw instance.

The fixture forces a non-default `owner_id` precisely because the bug only manifests when `_resolve_user_id` produces distinct values for different senders. Without that monkeypatch, the test would pass even without the fix.

## Known Gaps

The legacy-note backward-compatibility behavior is currently unconditional — all notes without `user_id` are visible to all users. A future enhancement might allow operators to opt into strict mode (legacy notes visible only to admins), but no such option is currently implemented or tested.