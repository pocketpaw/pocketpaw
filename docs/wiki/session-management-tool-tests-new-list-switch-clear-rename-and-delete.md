---
{
  "title": "Session Management Tool Tests — New, List, Switch, Clear, Rename, and Delete",
  "summary": "This test file validates the six session management tools in `pocketpaw.tools.builtin.sessions`: `NewSessionTool`, `ListSessionsTool`, `SwitchSessionTool`, `ClearSessionTool`, `RenameSessionTool`, and `DeleteSessionTool`. Tests cover tool naming, happy-path behavior, error handling, and policy group membership.",
  "concepts": [
    "NewSessionTool",
    "ListSessionsTool",
    "SwitchSessionTool",
    "ClearSessionTool",
    "RenameSessionTool",
    "DeleteSessionTool",
    "session alias",
    "MemoryManager",
    "policy group",
    "minimal profile",
    "tool contract",
    "error handling"
  ],
  "categories": [
    "testing",
    "tools",
    "session management",
    "memory",
    "test"
  ],
  "source_docs": [
    "27cd06fd730ff62d"
  ],
  "backlinks": null,
  "word_count": 483,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`tests/test_session_tools.py` tests PocketPaw's built-in session management tools. These tools expose memory manager session operations to agents and users via natural language commands. Each tool wraps async `MemoryManager` methods and formats results as human-readable strings.

## NewSessionTool

`TestNewSessionTool` — name is `"new_session"`. The key behavior test (`test_creates_alias`) verifies that calling `execute(session_key="discord:123")` calls `mm.set_session_alias` with the original key as the first argument and a new key of the form `"discord:123:<suffix>"` as the second. This alias pattern allows multiple named sessions within a single channel/user combination.

`test_error_handling` patches `get_memory_manager` to raise `RuntimeError("boom")` and asserts the result contains `"Error"` rather than propagating the exception. This is consistent with PocketPaw's tool contract: tools never raise; they return strings.

## ListSessionsTool

`TestListSessionsTool` — name is `"list_sessions"`. Tests:

- **Empty** — `list_sessions_for_chat` returns `[]`; result contains `"no sessions"`.
- **With sessions** — two sessions are listed with `title`, `message_count`, and `is_active` state. The active session is distinguishable from inactive ones.

## SwitchSessionTool

`TestSwitchSessionTool` — name is `"switch_session"`. This tool is the most complex: it must accept both numeric indices and text queries, and handle multiple match ambiguity.

- **Switch by number** — `"1"` selects the first session in the list.
- **Switch invalid number** — non-numeric index returns a useful error.
- **Switch by text — single match** — text query matching exactly one session switches to it.
- **Switch by text — no match** — returns a not-found message.
- **Switch by text — multiple matches** — returns an ambiguity message asking the user to be more specific.
- **No sessions** — handled gracefully.

```python
async def test_switch_by_text_multiple(mock_get_mm):
    # Multiple matching sessions → ambiguity message, no switch
```

## ClearSessionTool

`TestClearSessionTool` — name is `"clear_session"`. Clears the current session's messages:

- **With messages** — messages are cleared; result confirms.
- **Empty session** — clearing an already-empty session is a no-op; handled without error.

## RenameSessionTool

`TestRenameSessionTool` — name is `"rename_session"`. Tests:

- **Rename success** — `mm.update_session_title` is called; result confirms the new name.
- **Rename not found** — unknown session key returns a not-found message.

## DeleteSessionTool

`TestDeleteSessionTool` — name is `"delete_session"`. Tests:

- **Delete success** — `mm.delete_session` is called; result confirms deletion.
- **Delete nothing** — deleting a nonexistent session returns a message rather than an error.

## Policy Group Membership

`TestSessionToolPolicy` verifies:

- A `"sessions"` policy group exists in the tool policy registry.
- The `"minimal"` tool profile includes the `"sessions"` group — meaning session management is available even in the most restricted tool configuration. This is a deliberate product decision: users should always be able to manage their conversation history regardless of what other tools are enabled.

## Known Gaps

No `TODO` or `FIXME` markers. Tests use `mock_get_mm` patching at the module level rather than injecting the memory manager as a dependency, which means the tests are tightly coupled to the import path. If the import path changes, all tests in this file break.
