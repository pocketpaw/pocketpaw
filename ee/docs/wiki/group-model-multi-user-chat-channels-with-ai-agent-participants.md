---
{
  "title": "Group Model: Multi-User Chat Channels with AI Agent Participants",
  "summary": "Beanie document model for chat groups/channels that support both human members and AI agent participants. Groups are workspace-scoped and support Slack-like features including public/private/DM types, pinned messages, and agent response modes.",
  "concepts": [
    "Group",
    "GroupAgent",
    "chat channel",
    "workspace",
    "respond_mode",
    "Beanie",
    "compound index",
    "denormalized counter"
  ],
  "categories": [
    "Models",
    "Messaging",
    "Data Layer",
    "AI Agents"
  ],
  "source_docs": [
    "8cf89e3fd04f9985"
  ],
  "backlinks": null,
  "word_count": 368,
  "compiled_at": "2026-04-08T07:26:05Z",
  "compiled_with": "agent",
  "version": 1
}
---

# Group Model: Multi-User Chat Channels with AI Agent Participants

## Purpose

The `Group` model represents a chat channel within a workspace — similar to Slack channels but with first-class AI agent support. Groups are the primary container for real-time messaging and agent interactions in PocketPaw Enterprise.

## Design Decisions

### Agent Response Modes (GroupAgent)
The `GroupAgent` embedded model captures how an AI agent behaves within a group. The `respond_mode` field supports four modes:
- `mention_only` — agent only responds when @mentioned (default, least noisy)
- `auto` — agent responds to every message (useful for dedicated assistant channels)
- `silent` — agent observes but never responds (for logging/monitoring agents)
- `smart` — agent decides when to respond based on context

This design means the same agent can behave differently in different channels without requiring separate agent configurations.

### Workspace-Scoped with Compound Index
The compound index `[("workspace", 1), ("slug", 1)]` ensures slug uniqueness within a workspace. Two different workspaces can have a `#general` channel, but the same workspace cannot have duplicates.

### Denormalized Counters
`message_count` and `last_message_at` are denormalized onto the group document rather than computed from the messages collection. This avoids expensive `count()` queries when rendering the channel list sidebar, where every group needs its message count displayed.

## Fields

| Field | Type | Purpose |
|-------|------|---------|
| `workspace` | `Indexed(str)` | Parent workspace ID |
| `name` | `str` | Display name |
| `slug` | `str` | URL-safe identifier |
| `type` | `str` | `public`, `private`, or `dm` |
| `members` | `list[str]` | User IDs |
| `agents` | `list[GroupAgent]` | Agents with their response modes |
| `owner` | `str` | Creator user ID |
| `archived` | `bool` | Soft archive flag |
| `last_message_at` | `datetime | None` | Denormalized latest message timestamp |
| `message_count` | `int` | Denormalized total messages |

## Known Gaps

- No `max_members` limit — large groups could cause performance issues with the members list embedded in the document.
- The `slug` field defaults to empty string, meaning it is not auto-generated from the name. Callers must set it explicitly or risk empty slugs.
- No read-receipt or unread-count tracking at the group level.
