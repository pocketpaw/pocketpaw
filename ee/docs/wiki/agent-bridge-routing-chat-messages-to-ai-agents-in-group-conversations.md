---
{
  "title": "Agent Bridge — Routing Chat Messages to AI Agents in Group Conversations",
  "summary": "Event-driven bridge that listens for chat messages in groups, evaluates whether each agent should respond based on its respond_mode (silent, auto, mention_only, smart), and streams agent responses back to the group via WebSocket. Also handles ripple spec extraction and auto-pocket creation from agent output.",
  "concepts": [
    "agent bridge",
    "respond mode",
    "smart relevance",
    "streaming response",
    "ripple spec extraction",
    "auto-pocket creation",
    "event-driven",
    "loop prevention",
    "WebSocket broadcast",
    "knowledge injection",
    "soul observation"
  ],
  "categories": [
    "cloud",
    "agents",
    "chat",
    "event handling",
    "AI integration"
  ],
  "source_docs": [
    "00a62e50b3e8a9dc"
  ],
  "backlinks": null,
  "word_count": 625,
  "compiled_at": "2026-04-08T07:25:31Z",
  "compiled_with": "agent",
  "version": 1
}
---

# Agent Bridge — Routing Chat Messages to AI Agents in Group Conversations

## Purpose

The agent bridge connects the cloud chat system to PocketPaw's agent pool. When a user sends a message in a group that has agents, this module decides which agents should respond and orchestrates their responses — including streaming, persistence, knowledge injection, and even automatic pocket creation from agent-generated ripple specs.

## Message Flow

1. `message.sent` event fires on the event bus
2. `on_message_for_agents()` loads the group and checks each agent's respond mode
3. Qualifying agents get a response task spawned via `asyncio.create_task()`
4. Each agent streams its response back through WebSocket broadcasts

### Loop Prevention

The very first check is `if sender_type == "agent": return` — this prevents infinite loops where agents respond to each other's messages. Without this guard, two auto-responding agents in the same group would create an unbounded conversation loop.

## Respond Modes

Each agent in a group has a `respond_mode` that controls when it activates:

| Mode | Behavior |
|------|----------|
| `silent` | Never responds |
| `auto` | Responds to every message |
| `mention_only` | Only responds when explicitly @mentioned |
| `smart` | Uses an LLM call to decide relevance |

### Smart Relevance Check

The `smart` mode makes a lightweight LLM call using Claude Haiku to determine if a message is relevant to the agent's persona. It constructs a simple YES/NO prompt with the agent's persona (truncated to 200 chars) and the message (truncated to 500 chars). The backend is hardcoded to `claude_agent_sdk` with `claude-haiku-4-5-20251001`.

On failure, it defaults to `False` (don't respond) — a safe fallback that prevents spurious responses when the LLM call fails.

## Agent Response Pipeline

`_run_agent_response()` orchestrates the full response lifecycle:

1. **Instance retrieval** — gets or creates an agent instance from the pool
2. **History loading** — fetches the 20 most recent messages from the group (MongoDB), reversed to chronological order
3. **Knowledge injection** — queries the agent's knowledge engine for relevant context
4. **Stream start notification** — broadcasts `agent.stream_start` to group members
5. **Streaming response** — runs the agent and broadcasts `agent.stream_chunk` events with accumulated text
6. **Ripple spec extraction** — scans the response for JSON code blocks containing `lifecycle` or `widgets` keys
7. **Auto-pocket creation** — if a ripple spec is found, normalizes it and creates a Pocket document
8. **Message persistence** — saves the final agent message to MongoDB with any ripple attachments
9. **Stream end notification** — broadcasts `agent.stream_end` with the final content, pocket ID, etc.
10. **Soul observation** — calls `pool.observe()` to record the interaction in the agent's soul

### Ripple Spec Detection

The bridge uses a regex to find JSON code blocks in agent output: `` ```json\s*(\{.*?\})\s*``` ``. If the parsed JSON contains `lifecycle` or `widgets` keys, it's treated as a ripple spec, normalized via `normalize_ripple_spec`, and attached to the message. The JSON block is then stripped from the text content.

This is a best-effort heuristic — agents aren't explicitly instructed to output ripple specs in code blocks, so this relies on convention.

## Registration

`register_agent_bridge()` subscribes `on_message_for_agents` to the `message.sent` event. Called during app startup.

## Known Gaps

- Smart relevance check hardcodes the model (`claude-haiku-4-5-20251001`) and backend (`claude_agent_sdk`) — not configurable
- `asyncio.create_task()` for agent responses means errors are fire-and-forget; failed tasks only log exceptions
- Ripple spec detection regex is greedy with `re.DOTALL` — could match unintended JSON blocks
- History is limited to 20 messages with no pagination or summarization for long conversations
- `group_members[0]` is used as pocket owner — assumes the first member is the right owner, which may not hold for all group types
- No rate limiting on agent responses — a flood of messages could spawn unbounded concurrent agent tasks
