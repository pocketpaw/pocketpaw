---
{
  "title": "Paw Soul Tools: BaseTool Wrappers for Soul-Protocol Operations",
  "summary": "tools.py defines nine BaseTool subclasses that expose soul-protocol's memory and identity operations as agent-callable tools, including remember, recall, edit core memory, status introspection, rubric-based self-evaluation, hot reload from disk, selective forgetting, and topic-aware context retrieval.",
  "concepts": [
    "BaseTool",
    "SoulRememberTool",
    "SoulRecallTool",
    "SoulEditCoreTool",
    "SoulStatusTool",
    "SoulEvaluateTool",
    "SoulForgetTool",
    "SoulCoreMemoryTool",
    "SoulContextTool",
    "soul-protocol",
    "hasattr guard",
    "importance",
    "GDPR"
  ],
  "categories": [
    "paw",
    "tools",
    "soul-protocol"
  ],
  "source_docs": [
    "0b92d53be87720b4"
  ],
  "backlinks": null,
  "word_count": 419,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`tools.py` bridges soul-protocol's Python API into PocketPaw's tool protocol so AI agents can directly manage their own memory and identity during execution. Each class wraps one soul-protocol capability in a `BaseTool` subclass exposing a name, JSON schema (`parameters`), and an async `execute()` method.

## Tool Inventory

| Tool | Name | Core Operation | Min Version |
|------|------|---------------|-------------|
| SoulRememberTool | `soul_remember` | `soul.remember(content, importance)` | All |
| SoulRecallTool | `soul_recall` | `soul.recall(query, limit)` | All |
| SoulEditCoreTool | `soul_edit_core` | `soul.edit_core_memory(persona, human)` | All |
| SoulStatusTool | `soul_status` | `soul.state` introspection | All |
| SoulEvaluateTool | `soul_evaluate` | `manager.evaluate(user_input, agent_output)` | >= 0.2.4 |
| SoulReloadTool | `soul_reload` | `manager.reload()` | >= 0.2.4 |
| SoulForgetTool | `soul_forget` | `soul.forget / forget_entity / forget_before` | >= 0.2.8 |
| SoulCoreMemoryTool | `soul_core_memory` | `soul.get_core_memory()` | >= 0.2.8 |
| SoulContextTool | `soul_context` | `soul.context_for(prompt, max_memories)` | >= 0.2.8 |

## Version Guard Pattern

Tools that require newer soul-protocol versions use `hasattr` checks:

```python
async def execute(self, **kw) -> str:
    if not hasattr(self._soul, "get_core_memory"):
        return self._error("Requires soul-protocol >= 0.2.8.")
    cm = self._soul.get_core_memory()
    ...
```

This avoids hard version pinning in `pyproject.toml` and lets older soul-protocol installs still use the core tools while gracefully failing on newer ones.

## SoulRememberTool: Importance Metadata

The `importance` parameter (1-10, default 5) is surfaced in the tool schema so the agent can make deliberate choices about what is worth remembering. High-importance memories survive eviction longer in soul-protocol's retention logic.

## SoulForgetTool: Three Deletion Modes

`SoulForgetTool` supports three mutually exclusive modes:
1. **Entity deletion** (`entity`): forgets all memories about a named entity
2. **Time-based deletion** (`before_date`): forgets memories older than an ISO 8601 date
3. **Query-based deletion** (`query`): forgets memories matching a content query

The tool description explicitly mentions GDPR compliance — this is not just for tidying memory, it is a legal hygiene operation.

## SoulEvaluateTool: Self-Improvement Loop

`SoulEvaluateTool` calls `manager.evaluate(user_input, agent_output)` to score a response against quality rubrics (completeness, relevance, helpfulness, specificity, empathy, clarity, originality). The scores feed into soul-protocol's skill XP system, creating a self-improvement loop.

## Known Gaps

- **SoulEvaluateTool requires SoulManager**: It takes both `soul` and `manager` as constructor arguments and is not registered in `get_paw_agent()` — only in the full PocketPaw daemon — because `SoulManager` is not available in the lightweight paw context.
- **No tool for memory listing**: There is no `SoulListMemoriesTool` that returns all stored memories without a query, making it hard for an agent to audit what it has remembered.