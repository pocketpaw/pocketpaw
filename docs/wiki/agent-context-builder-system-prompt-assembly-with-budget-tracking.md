---
{
  "title": "Agent Context Builder — System Prompt Assembly with Budget Tracking",
  "summary": "AgentContextBuilder is the central orchestrator that assembles the full system prompt for each agent turn, weaving together identity, instructions, memory recalls, knowledge-base results, channel hints, health state, and injected files while staying within a configurable character budget. Its priority-based injection model ensures critical blocks (identity, instructions) are never dropped, while optional blocks are trimmed or skipped as budget tightens.",
  "concepts": [
    "AgentContextBuilder",
    "context window budget",
    "_Priority",
    "_INJECTION_CAPS",
    "system prompt assembly",
    "memory injection",
    "kb-go",
    "channel hints",
    "AGENTS.md",
    "file context sanitisation",
    "health state",
    "BootstrapProviderProtocol"
  ],
  "categories": [
    "bootstrap",
    "context-management",
    "system-prompt",
    "memory"
  ],
  "source_docs": [
    "0000000000000002"
  ],
  "backlinks": null,
  "word_count": 503,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## What the Context Builder Does

Every time the agent runtime prepares a new turn, it needs to construct a system prompt that is coherent, up-to-date, and within the model's context window. `AgentContextBuilder` is the single place where that assembly happens. It calls a `BootstrapProviderProtocol` implementation to get the base identity/instructions, then queries memory and the knowledge base, and finally injects a series of optional blocks before handing the assembled prompt to the agent loop.

## Priority Model

Blocks are assigned one of four priority levels via `_Priority`:

- **CRITICAL** — Always included; truncated only as a last resort. Used for `identity` and `instructions`.
- **HIGH** — Included if budget allows, capped to a per-block maximum.
- **MEDIUM** — Included if budget allows; skipped when context is tight.
- **LOW** — First to be dropped entirely when the budget is exceeded.

`_INJECTION_CAPS` defines character caps per block, with `None` meaning "use remaining budget". For example, `memory_context` is capped at 4,000 characters and `kb_context` at 3,000, preventing a single noisy memory recall from consuming the entire window.

The default budget is `_DEFAULT_BUDGET_CHARS = 32_000` characters, a practical ceiling that keeps the assembled prompt well under GPT-4-class context limits while leaving room for conversation history.

## What Gets Injected and When

| Block | Priority | Trigger |
|---|---|---|
| Identity + instructions | CRITICAL | Always |
| Memory context | HIGH | MemoryManager has results for the current query |
| KB context | HIGH | kb-go returns relevant articles |
| Sender block | MEDIUM | Channel provides user metadata |
| Channel hints | MEDIUM | Non-default channel (Discord, Matrix, etc.) |
| AGENTS.md | MEDIUM | Target repo contains project-specific constraints |
| File context | MEDIUM | Files were attached to the message |
| Health state | HIGH | Agent health is degraded or unhealthy |
| Skills list | LOW | Skill registry is populated |

## Path Sanitisation

File context paths are sanitised before injection to prevent path traversal strings (e.g., `../../../etc/passwd`) from appearing in the system prompt. This matters because system prompts are passed verbatim to the model; injecting raw user-supplied paths could mislead the agent into reasoning about sensitive file locations.

## Channel-Aware Format Hints

`CHANNEL_FORMAT_HINTS` maps each `Channel` enum value to a short formatting directive (e.g., "Avoid markdown tables; prefer plain text" for SMS channels). Injecting the hint at the MEDIUM priority level means it is included on most turns but dropped gracefully under extreme context pressure.

## KB Injection (Added 2026-04-08)

The context builder now queries `kb-go` for structured knowledge articles alongside soul memory recalls. This dual-source approach means the agent can answer both "what do I remember about this user?" (episodic memory) and "what do I know about this topic?" (structured knowledge) within the same turn.

## Known Gaps

The `_DEFAULT_BUDGET_CHARS` constant is not yet configurable per-agent. A high-context cloud agent with a 128k token model could benefit from a larger budget, but changing it currently requires editing the module directly.