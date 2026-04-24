---
{
  "title": "Paw CLI: Click-Based Command Interface for Project-Level AI",
  "summary": "paw/cli.py implements the full paw CLI using Click with eight commands — init, ask, chat, serve, status, doctor, os, channels — and an async bridge that handles both normal invocation and pytest-asyncio environments. The serve command is a documented placeholder; all other commands are functional.",
  "concepts": [
    "paw CLI",
    "Click",
    "async bridge",
    "ThreadPoolExecutor",
    "pytest-asyncio",
    "rich",
    "paw init",
    "paw ask",
    "paw chat",
    "MCP server",
    "paw doctor",
    "AgentRouter"
  ],
  "categories": [
    "paw",
    "cli",
    "soul-protocol"
  ],
  "source_docs": [
    "45ae948e9532b340"
  ],
  "backlinks": null,
  "word_count": 426,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`cli.py` is the user-facing entry point for the `paw` subpackage. It exposes the `paw` command group via Click, with subcommands covering the full lifecycle: initialization, one-shot queries, interactive chat, health checks, and channel adapter management.

## Async Bridge

Click is synchronous. PocketPaw's internals are async. The bridge function handles this transparently:

```python
def _run_async(coro):
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=1) as ex:
        return ex.submit(asyncio.run, coro).result()
```

The `try/except RuntimeError` detects whether there is already a running event loop. In production, there is not — `asyncio.run()` creates a fresh one. In pytest-asyncio tests, there is one. Calling `asyncio.run()` from inside a running loop raises `RuntimeError: This event loop is already running`. The workaround runs the coroutine in a `ThreadPoolExecutor` thread with its own fresh event loop. Without this, `paw` commands would crash in any test suite using `pytest-asyncio`.

## Optional Rich Dependency

`_get_console()` and `_print()` try to import `rich.Console` and fall back to `click.echo`. This keeps `rich` optional — the CLI functions without it but outputs plain text.

## Command Overview

| Command | Description |
|---------|-------------|
| `paw init` | Creates `.paw/`, births or awakens the soul, writes `paw.yaml`, runs heuristic scan |
| `paw ask` | One-shot question: recalls memories, builds system prompt, routes through AgentRouter |
| `paw chat` | REPL loop: recalls context per turn, streams responses, saves soul on exit |
| `paw serve` | Placeholder — prints a notice that MCP server is coming |
| `paw status` | Shows soul name, provider, mood, energy, active domains |
| `paw doctor` | Health checks: soul-protocol, rich, .paw dir, paw.yaml, soul file, PocketPaw health engine |
| `paw os` | Launches PocketPaw full dashboard via `run_dashboard()` |
| `paw channels` | Starts headless channel adapters (Telegram, Slack, Discord) |

## Memory-Aware Ask and Chat

Both `ask` and `chat` call `agent.bridge.recall()` before routing to AgentRouter, injecting relevant memories into the system prompt. After a successful response, `agent.bridge.observe()` records the interaction so the soul can learn from it. The soul file is saved after each `chat` session exit.

## Known Gaps

- **`paw serve` is a stub**: The command body prints a placeholder message. Full MCP server integration is documented as "coming in a future release."
- **`paw ask` with `provider=none`** only shows recalled memories without calling any LLM — useful for offline testing but silent about why no answer is generated.
- **Exit commands are a frozenset**: `_EXIT_COMMANDS = frozenset({"exit", "quit", "bye"})`. "q" is not included, which may surprise users expecting the common terminal abbreviation.