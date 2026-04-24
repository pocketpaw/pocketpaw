---
{
  "title": "Group Document — Multi-User Channels with AI Agent Participants",
  "summary": "The Beanie ODM document for chat groups and channels — workspace-scoped rooms that mix human members with AI agents. Models Slack-style channel semantics with a role/permission layer, agent respond modes, and pinned message support.",
  "concepts": [
    "Group document",
    "Beanie ODM",
    "GroupAgent",
    "respond_mode",
    "member_roles",
    "channel type",
    "private group",
    "DM",
    "agent participation",
    "workspace scoping",
    "last_message_at",
    "MemberRole"
  ],
  "categories": [
    "data modeling",
    "MongoDB",
    "collaboration",
    "agents"
  ],
  "source_docs": [
    "aa3ecae2538fe6f1"
  ],
  "backlinks": null,
  "word_count": 547,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`group.py` defines the `Group` document and the `GroupAgent` embedded model. Groups are the multi-user, multi-agent communication primitive in PocketPaw — analogous to Slack channels or Discord servers but with first-class AI agent participation.

## Group Types

```python
type: str = Field(default="private", pattern="^(public|private|dm|channel)$")
```

Four types control visibility and access:

- `private` — only explicit members can read/post. Default for new groups.
- `channel` — workspace-wide readable, like a Slack public channel.
- `public` — visible and accessible across the workspace without explicit membership.
- `dm` — direct message between exactly two users (enforced at the API layer, not by schema constraints).

The default of `private` is a security-first choice — new groups are invisible to non-members until the creator explicitly broadens access.

## Member Role Model

```python
members: list[str] = []          # User IDs — presence means access
member_roles: dict[str, MemberRole] = {}  # Overrides: absent = "edit"
owner: str
```

The role system is opt-in. A user in `members` with no entry in `member_roles` has the default `"edit"` role (can post and react). Role overrides are stored in a dict keyed by user ID:

- `"view"` — read-only
- `"edit"` — post/react (the default when absent)
- `"admin"` — manage group settings, members, and agents

The `owner` field is the implicit top tier and is not stored in `member_roles` — the owner always has full control. This three-level + owner model covers the most common collaboration patterns without the complexity of a full RBAC system.

## Agent Participation via GroupAgent

```python
class GroupAgent(BaseModel):
    agent: str            # Agent ID
    role: str = "assistant"    # assistant | listener | moderator
    respond_mode: str = "mention_only"  # mention_only | auto | silent | smart
```

`respond_mode` controls when the agent engages:

- `mention_only` — only responds when directly mentioned
- `auto` — responds to every message in the group
- `silent` — receives all messages but never responds (useful for logging/monitoring agents)
- `smart` — uses the agent's own judgment about when to respond (model-driven)

The `role` field (`assistant`, `listener`, `moderator`) is a semantic label used for display and for future permission differentiation — a `moderator` agent could have elevated permissions to pin/delete messages.

## Metadata Fields

`last_message_at` and `message_count` are denormalized onto the `Group` document for fast sidebar rendering — the client does not need to aggregate over the `messages` collection to show unread counts and last activity. These fields are updated by the message write path.

`archived` soft-deletes the group from active views without destroying its history.

## Index Design

```python
indexes = [
    [("workspace", 1), ("slug", 1)],
]
```

The compound index on `(workspace, slug)` enables fast slug lookups within a workspace, which is the primary access pattern for channel navigation by URL slug.

## Known Gaps

- No unique index on `(workspace, slug)` — duplicate slugs within a workspace are only prevented at the application layer.
- `member_roles` is an unbounded dict. For very large groups, this field could grow significantly. A separate `GroupMember` collection would scale better.
- The `dm` type has no schema-level constraint on member count — two-person enforcement is left entirely to the API layer.
- `agents` is a list without a uniqueness constraint — the same agent ID could be added twice with different `respond_mode` values, causing ambiguous behavior.