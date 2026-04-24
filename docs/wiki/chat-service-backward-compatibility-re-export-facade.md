---
{
  "title": "Chat Service - Backward-Compatibility Re-Export Facade",
  "summary": "`service.py` is a thin facade that re-exports `GroupService`, `MessageService`, and their helper functions from their new dedicated modules, preserving import compatibility for all code written before the refactor. It carries no logic of its own.",
  "concepts": [
    "re-export",
    "backward compatibility",
    "refactoring",
    "import facade",
    "GroupService",
    "MessageService",
    "module split",
    "noqa F401"
  ],
  "categories": [
    "chat",
    "cloud EE",
    "architecture",
    "refactoring"
  ],
  "source_docs": [
    "3ee76a6a7e5622a2"
  ],
  "backlinks": null,
  "word_count": 242,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

During a refactor, the original `service.py` monolith was split into two focused modules:

- `group_service.py` - group CRUD, membership, agents, DMs
- `message_service.py` - message CRUD, reactions, threads, search

A hard cut would have broken every import of the form `from ee.cloud.chat.service import GroupService`. Rather than a large-scale find-and-replace across the codebase, `service.py` was converted to a re-export module:

```python
from ee.cloud.chat.group_service import GroupService, _group_response  # noqa: F401
from ee.cloud.chat.message_service import MessageService, _message_response  # noqa: F401
```

## Why `# noqa: F401`?

The `# noqa: F401` comments silence the 'imported but unused' linter warning because the purpose of these imports is re-export, not local use. Without `noqa`, a well-intentioned cleanup might remove the re-exports and silently break callers.

## Migration Strategy

The pattern is a standard Python backward-compatibility shim. Existing callers keep working without change. New code is encouraged to import directly from `group_service` or `message_service` to make dependencies explicit and avoid confusion about where the implementation lives. Once all callers have been updated, `service.py` can be removed.

## Private Helper Re-Exports

`_group_response` and `_message_response` are technically private (underscore prefix) but re-exported because the router and test modules reference them via `service`. This is a pragmatic compromise - ideally these helpers would be promoted to public methods on their respective service classes.

## Known Gaps

- The re-exported private helpers create an implicit public API for internal functions.
- There is no deprecation warning guiding callers to update their imports.