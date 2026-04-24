---
{
  "title": "GroupService Realtime Emit Tests",
  "summary": "This test module verifies that every public mutation in GroupService fires the correct realtime event via the bus emit() call. It also confirms that membership-changing operations correctly invalidate the AudienceResolver group cache so downstream subscribers see fresh data.",
  "concepts": [
    "GroupService",
    "realtime events",
    "emit",
    "AudienceResolver",
    "cache invalidation",
    "GroupCreated",
    "GroupMemberAdded",
    "GroupJoined",
    "idempotency",
    "pytest-asyncio",
    "unittest.mock",
    "patch",
    "ConnectionManager"
  ],
  "categories": [
    "testing",
    "realtime",
    "chat",
    "group management",
    "test"
  ],
  "source_docs": [
    "tests/cloud/chat/test_group_emits.py"
  ],
  "backlinks": null,
  "word_count": 563,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`test_group_emits.py` is the authoritative contract test for `GroupService`'s realtime side effects. The rule it enforces is simple but critical: every mutation that changes group state must emit a corresponding typed event through `emit()`, and every mutation that changes who is in a group must also call `resolver.invalidate_group()` to bust the `AudienceResolver` cache.

Without these tests, it is easy for a developer to add a new operation — say, bulk-adding agents — and forget to wire up the emit. The event would be silently dropped and clients would never update their UI.

## Test Infrastructure

The module defines two small helpers:

```python
def _capture_emits():
    recorded: list = []
    async def fake_emit(ev):
        recorded.append(ev)
    return recorded, fake_emit
```

`_capture_emits` returns a list that collects every event passed to `emit`. Tests then filter `recorded` by type (e.g. `isinstance(e, GroupCreated)`) to assert the right events fired with the right payload. This pattern avoids mocking the event *class* itself and instead inspects real instances.

`_fake_group` builds a `SimpleNamespace` that satisfies all the fields `GroupService` reads from a `Group` document, including a pre-wired `save = AsyncMock()`. Tests patch `_get_group_or_404` to return one of these stand-ins, keeping the test focused purely on emit behavior rather than DB queries.

## Covered Scenarios

### CRUD operations
- **create_group** → `GroupCreated` with the full member ID set (owner + invited members)
- **update_group** → `GroupUpdated` carrying the updated fields
- **archive_group** → `GroupUpdated` with `{"archived": True}`

### Membership mutations
Each of join, leave, add_members, and remove_member asserts both the membership event *and* a cache invalidation:

- **join_group** (new member) → `GroupMemberAdded` + `GroupJoined` (scoped to the joiner so their sidebar auto-hydrates) + `invalidate_group`
- **join_group** (already a member) → no events, no cache invalidation — idempotency guard
- **leave_group** → `GroupMemberRemoved` + `invalidate_group`
- **add_members** (new users only) → one `GroupMemberAdded` *per* newly added user + one `GroupJoined` listing all of them + `invalidate_group`
- **add_members** (all already members) → no events, no cache invalidation
- **remove_member** → `GroupMemberRemoved` + `invalidate_group`
- **set_member_role** → `GroupMemberRole` only — role changes do not alter who can see the group, so the audience cache is intentionally *not* busted

### Agent operations
- **add_agent** → `GroupAgentAdded`
- **update_agent** → `GroupAgentUpdated`
- **remove_agent** → `GroupAgentRemoved`

### DM creation
- **get_or_create_dm** (new) → emits on creation
- **get_or_create_dm** (existing) → no emit (idempotency)
- **get_or_create_agent_dm** (new/existing) — same pattern

### Access control edge cases
- `join_group` allows `channel` type groups (self-joinable)
- `join_group` rejects `private` groups (invite-only invariant)

## Why Cache Invalidation Is Coupled to Membership

The `AudienceResolver` maintains a group-member index used to route realtime messages. If this cache is stale after a membership change, events may be sent to users who left or missed for users who just joined. Invalidation is therefore a correctness requirement, not a performance optimization.

## Why `GroupJoined` Exists Alongside `GroupMemberAdded`

`GroupMemberAdded` is broadcast to the whole group, letting existing members know someone joined. `GroupJoined` is scoped to the new member(s) so their client sidebar can hydrate the new room without requiring a full page refresh. The two events serve different audiences and different UI flows.

## Known Gaps

No TODOs or FIXMEs were found in this file. The test for `get_or_create_agent_dm` (no emit on existing) is listed in the AST but not visible in the truncated source — coverage of that edge case should be verified if the file is modified.
