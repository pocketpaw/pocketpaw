---
{
  "title": "WorkspaceService Realtime Event Emission and Cache Invalidation Tests",
  "summary": "This suite verifies that every mutating `WorkspaceService` method fires the correct typed event through `emit()`, and that membership or admin list changes also invalidate the `AudienceResolver` workspace cache. All database and permission primitives are patched at their seams, isolating the emit behavior from persistence concerns.",
  "concepts": [
    "WorkspaceService",
    "emit",
    "AudienceResolver",
    "cache invalidation",
    "WorkspaceMemberAdded",
    "WorkspaceInviteCreated",
    "invite token security",
    "workspace deletion cascade",
    "realtime events",
    "patch seams"
  ],
  "categories": [
    "testing",
    "realtime events",
    "workspace management",
    "enterprise edition",
    "test"
  ],
  "source_docs": [
    "4bc338ee44f97a8e"
  ],
  "backlinks": null,
  "word_count": 536,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

PocketPaw's enterprise workspace service manages the full lifecycle of workspaces: creation, updates, deletion, member role changes, member removal, and invite flows (create, accept, revoke). Every mutation must broadcast a typed event so connected clients receive real-time updates. This file pins that contract.

## Why Cache Invalidation Is Tested Alongside Emit

The `AudienceResolver` caches which users belong to which workspace to avoid repeated database queries when routing WebSocket messages. When membership changes (member added, role changed, member removed, workspace deleted), the cached audience list becomes stale. If the cache is not invalidated, the wrong set of users receives subsequent events — a confidentiality bug. The test assertions therefore always pair emit verification with `resolver_mock.invalidate_workspace.assert_called_once_with(...)` checks.

## Patch Strategy

Every test uses Python's `unittest.mock.patch` context manager to substitute three seams:

1. **`emit`** — replaced with `fake_emit` (an async function that appends to a list).
2. **`get_resolver`** — replaced with a lambda returning a `MagicMock` so `invalidate_workspace` calls can be asserted.
3. **Database models** (`Workspace.get`, `User.get`, `Invite.find_one`, etc.) — replaced with `AsyncMock` returning hand-crafted `SimpleNamespace` objects.

This layered substitution keeps each test to a single concern without spinning up a real database.

## Key Test Cases

### Create
`test_create_emits_member_added_and_invalidates_cache` verifies that creating a workspace emits a `WorkspaceMemberAdded` event with role `"owner"` and immediately invalidates the resolver cache. The owner is treated as the first member, not a separate administrative entity.

### Update (Partial Patch)
`test_update_only_sends_patched_fields` is subtle: it sends an empty `UpdateWorkspaceRequest()` and asserts that the emitted `WorkspaceUpdated` payload contains only `{"workspace_id": "w1"}` — no spurious null fields. This prevents clients from treating omitted fields as "cleared".

### Delete Cascade
`test_delete_cascades_membership_cleanup` is the most complex test. It creates two member users, both with the deleted workspace as their active workspace, and verifies:

```python
# Member A had another workspace — active should shift.
assert [m.workspace for m in member_a.workspaces] == ["w2"]
assert member_a.active_workspace == "w2"

# Member B had only the deleted workspace — active resets to None.
assert member_b.workspaces == []
assert member_b.active_workspace is None
```

Resetting `active_workspace` to `None` for users who had no other workspace triggers the first-run modal on their next login rather than leaving them stuck in a broken state.

### Invite Token Security
`test_create_invite_emits_invite_created_without_token` asserts that the invite token is never included in the emitted event payload:

```python
assert "token" not in data
```

Invite tokens are single-use secrets. If they leaked into the event bus, any subscriber (including future third-party integrations) could use them to accept invitations without the intended recipient.

### Known-User Invite Routing
`test_create_invite_includes_user_id_when_invitee_is_known_user` verifies that when the invitee already has an account, the emitted event includes their `user_id`. This is needed so the `AudienceResolver` can route the `invite.created` notification directly to the invitee's WebSocket connection, rather than only to workspace admins.

### Accept Invite — Two Events
Accepting an invite must fire two events in sequence: `WorkspaceInviteAccepted` (so the inviter sees confirmation) and `WorkspaceMemberAdded` (so the workspace subscriber list updates). The cache is also invalidated because the member roster changed.

## Known Gaps

There is no test for the concurrent-invite race condition: two users accepting the same invite simultaneously. The seat-count guard (`_count_members` vs. `seats`) is mocked away in these tests, so overflow is not covered here.