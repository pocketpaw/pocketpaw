---
{
  "title": "Smoke Test: DM and Group Chat Persistence for Human Users",
  "summary": "This end-to-end test verifies that DM creation between two workspace members, message exchange, and group chat all persist correctly in MongoDB — without any agent involvement. It validates the full social messaging layer: workspace invites, DM groups, populated member objects, message context types, and cross-member read access.",
  "concepts": [
    "DM",
    "group chat",
    "workspace invite",
    "member population",
    "context_type",
    "senderType",
    "cross-member visibility",
    "chat groups API",
    "MongoDB messages collection",
    "enterprise chat",
    "smoke test"
  ],
  "categories": [
    "testing",
    "messaging",
    "workspace",
    "authorization"
  ],
  "source_docs": [
    "f8e480826d5f24fd"
  ],
  "backlinks": null,
  "word_count": 465,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Why Human-Only Messaging Needs Its Own Test

Most PocketPaw messaging tests involve agents. This smoke test deliberately excludes agents — the agent pool is mocked out — to isolate and validate the human DM and group chat flows that the enterprise dashboard depends on. Business users sending messages to each other via PocketPaw's chat layer must work independently of whether any AI agent is configured.

## Setup: Two Users in One Workspace

The test registers Alice as workspace owner, then invites Bob via a token-based invite flow:

1. Alice creates the workspace and posts an invite for Bob's email.
2. Bob registers, logs in, and accepts the invite token via `POST /api/v1/workspaces/invites/{token}/accept`.
3. Both set their active workspace so subsequent API calls are scoped correctly.

This invite-accept sequence mirrors exactly what the enterprise onboarding UI does, making the smoke test a functional regression check for the entire user-onboarding path.

## DM Flow

Alice opens a DM with Bob via `POST /api/v1/chat/dm/{bob_user_id}`. The test then checks:

- The returned group has `type="dm"`
- The `members` list contains both Alice and Bob
- `GET /api/v1/chat/groups/{dm_id}` returns populated member objects with `name` fields, not just IDs

The populated-members check matters because the client uses this response to display the DM title ("Bob" instead of a UUID). A regression here would show up as unnamed DM conversations in the UI.

## Message Exchange and Cross-Visibility

Alice sends `"hey bob"`, and the test confirms Bob can read it via `GET /api/v1/chat/groups/{dm_id}/messages`. Bob replies `"hey alice"`, and Alice's read confirms both messages appear. This bidirectional check catches authorization bugs where a member can write but not read, or where messages are only visible to the sender.

## Mongo Storage Assertion for DMs

```python
for m in rows:
    assert m.context_type == "group"
    assert not m.session_key, "DM messages must not have session_key"
```

DM messages are stored with `context_type="group"` and must not carry a `session_key`. This is intentional: DMs and group chats are group-context entities, not pocket/session-context entities. Mixing them up would cause DM messages to appear in AI session histories, which would be a serious data pollution bug.

## Group Chat Flow

Alice creates a private group called `"engineering"` and adds Bob as an `edit`-role member. Both post messages and read each other's output. The final assertion verifies that all messages have `senderType="user"` — no agent rows should appear in a human-only group thread:

```python
for m in r.json()["items"]:
    assert m["senderType"] == "user"
```

This guards against a scenario where an auto-triggered agent accidentally injects a response into a human group chat.

## Known Gaps

The test does not exercise:
- Group membership removal or role changes
- Message pagination (all messages fit in one page)
- DM between more than two users (group DM variant)
- Notification or webhook delivery