---
{
  "title": "SkillExecutor: Streaming Skill Execution Through the Agent Backend",
  "summary": "The `SkillExecutor` bridges skill definitions (Markdown files) and the agent backend (Open Interpreter or Claude Code) by building a prompt from skill content and user arguments, then streaming the agent's response back as an async iterator of result dicts. It is the runtime engine that transforms a loaded skill into an executed task.",
  "concepts": [
    "SkillExecutor",
    "skill execution",
    "AgentRouter",
    "streaming results",
    "AsyncIterator",
    "prompt building",
    "agent backend",
    "Open Interpreter",
    "Claude Code",
    "singleton pattern",
    "reset agent"
  ],
  "categories": [
    "skills system",
    "agent runtime",
    "api"
  ],
  "source_docs": [
    "d98e9e6dd718c6f1"
  ],
  "backlinks": null,
  "word_count": 473,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## What SkillExecutor Does

A skill is a Markdown file containing instructions for an AI agent. On its own, it is inert. `SkillExecutor` activates it by: (1) building a complete prompt that combines the skill's instructions with the user's runtime arguments, (2) routing that prompt through the `AgentRouter` to the configured backend, and (3) streaming results back to the caller as they arrive.

This design decouples skill authors from backend specifics — a skill written for Open Interpreter works unchanged when the user switches to Claude Code, because `SkillExecutor` handles the translation.

## AgentRouter Integration

`_get_agent_router()` returns the singleton `AgentRouter`, which abstracts over multiple agent backends. The lazy retrieval pattern (not stored at `__init__` time) allows the router to be configured after the executor is constructed — important during application startup where initialization order matters.

## The `execute()` Method

```python
async def execute(skill_name: str, args: str) -> AsyncIterator[dict]:
```

`execute()` looks up the skill by name from the `SkillLoader`, delegates to `execute_skill()`, and streams results. If the skill is not found, it yields an error event dict rather than raising — this keeps the streaming interface uniform: callers always get an async iterator regardless of whether the skill exists.

## The `execute_skill()` Method

`execute_skill()` handles the full execution lifecycle:
1. Calls `skill.build_prompt(args)` to produce the final prompt string
2. Passes the prompt to `AgentRouter.run()` with streaming enabled
3. Annotates each yielded chunk with a `timestamp` field (UTC ISO8601) for audit and debugging
4. Yields a final `done` event on completion

The timestamp annotation is added at the executor level, not by the agent backend — this ensures consistent timestamp format regardless of what the backend produces.

## `reset_agent()`

This method resets the underlying agent router state, clearing conversation history and tool state. It exists to support the use case where a user starts a new skill invocation that should not carry context from a prior session. Without a reset mechanism, state from a prior skill execution could influence the next one.

## `list_skills()` 

Returns a list of skill metadata dicts from the `SkillLoader`. This powers the dashboard's skill browser, allowing users to discover available skills without directly accessing the loader.

## Singleton Access

`get_skill_executor()` returns a module-level singleton, initialized with current `Settings` and the default `SkillLoader`. The singleton ensures the agent router is shared and not re-initialized per request.

## Known Gaps

- **No execution timeout**: Long-running skills can hold the agent backend indefinitely. There is no configurable timeout to cancel execution after N seconds.
- **No concurrent execution guard**: If two requests invoke the same skill simultaneously, they share the same agent router instance, which may not be designed for concurrent use.
- **Error events are not structured**: Error yielding uses ad-hoc dict shapes rather than a defined error event schema, making it harder for callers to reliably detect and handle errors.