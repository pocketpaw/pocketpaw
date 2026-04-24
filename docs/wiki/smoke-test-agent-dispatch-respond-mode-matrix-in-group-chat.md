---
{
  "title": "Smoke Test: Agent Dispatch Respond-Mode Matrix in Group Chat",
  "summary": "This script verifies that `_should_agent_respond` correctly routes messages in group chat based on each agent's respond mode (`auto`, `mention_only`, `silent`) and the set of @mentions present. It was written to lock down a real bug where two `auto` agents both fired when only one was mentioned.",
  "concepts": [
    "agent dispatch",
    "_should_agent_respond",
    "respond_mode",
    "group chat",
    "mention routing",
    "auto mode",
    "mention_only mode",
    "silent mode",
    "agent_bridge",
    "FakeGroupAgent",
    "broadcast",
    "smoke test"
  ],
  "categories": [
    "testing",
    "agent runtime",
    "group chat",
    "messaging"
  ],
  "source_docs": [
    "8436d0cb25c90830"
  ],
  "backlinks": null,
  "word_count": 527,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

This smoke test exists because group chat with multiple agents introduces a non-obvious routing problem: when a user @mentions one agent in a room containing two `auto` agents, only the mentioned agent should reply. Without explicit logic, both agents would respond — creating a conversation spam problem that was discovered in production.

The test verifies the `_should_agent_respond` function from `ee.cloud.shared.agent_bridge` against a 4x3 decision matrix:

| mode | mention this agent | mention other | no mentions |
|---|---|---|---|
| `silent` | False | False | False |
| `auto` (this) | True | False | True |
| `auto` (other) | False | True | True |
| `mention_only` | True | True | False |

## Why It Exists

The root bug: two `auto` agents in the same group. User sends `@agent-x hello`. Both `agent-x` and `agent-y` have `respond_mode="auto"`. Before the fix, `_should_agent_respond` returned `True` for both because the broadcast path (no mentions) and the mention path (mentions present) were conflated. The fix: if any agent-type mentions exist in the message, only the explicitly named agents should respond — broadcast (`True` for all `auto`) only applies when the message contains no agent mentions at all.

## FakeGroupAgent and Lightweight Isolation

The test deliberately avoids spinning up the full agent pool or calling an LLM. It uses a minimal `FakeGroupAgent` dataclass:

```python
@dataclass
class FakeGroupAgent:
    agent: str
    respond_mode: str
```

This keeps the test fast and deterministic. `_should_agent_respond` only needs the agent's `agent` ID and its `respond_mode` — it doesn't need a live agent process. The smoke script passes these fake objects directly, confirming the function signature contract.

## Mention Payload Shape

The `_mention` helper constructs the dict shape the real message bus produces:

```python
def _mention(agent_id: str) -> dict:
    return {"type": "agent", "id": agent_id, "display_name": f"@{agent_id}"}
```

The `type` field is important — the last test case (`auto-only-user-mentions`) confirms that a list of mentions containing only `{"type": "user"}` entries does NOT suppress `auto` agent responses. Only `type="agent"` mentions gate which agents fire. This prevents a subtle bug: a message that mentions a user (not an agent) should still trigger `auto` agents as if it were a broadcast.

## Test Case Breakdown

- **Two-auto with mention**: Sends `@agent-x` in a room with both `agent-x` and `agent-y` (both `auto`). Only `agent-x` should respond.
- **Two-auto no mention**: Broadcast — both `auto` agents respond.
- **mention_only**: Responds only when directly mentioned; stays silent on broadcasts and when another agent is mentioned.
- **silent**: Never responds, even when directly mentioned. This is a hard opt-out mode for observer agents.
- **User-only mentions**: `auto` agent treats user mentions as a broadcast (not gated).

## Execution Pattern

The script collects all 12 cases into a list, runs them in sequence, and accumulates failures:

```python
if failures:
    print(f"\n{len(failures)} FAIL(s): {failures}")
    return 1
print("\nSMOKE OK")
return 0
```

Return code `1` means at least one case failed, making it safe to run in CI with `sys.exit`.

## Known Gaps

No known gaps flagged in the source. The test covers the original bug matrix completely. Future extensions might add cases for `input_required` mode or agents that are not members of the group.