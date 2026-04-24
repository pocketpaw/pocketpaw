---
{
  "title": "Telegram Bot Gateway — Agent Runtime Entry Point",
  "summary": "bot_gateway.py wires the three core runtime components — MessageBus, TelegramAdapter, and AgentLoop — into a running process for Telegram-based deployments. It handles clean shutdown on cancellation and is the primary entry point when running PocketPaw as a Telegram bot.",
  "concepts": [
    "bot_gateway",
    "run_bot",
    "TelegramAdapter",
    "AgentLoop",
    "MessageBus",
    "asyncio.Task",
    "allowed_user_id",
    "clean shutdown",
    "CancelledError",
    "command handler",
    "startup sequence"
  ],
  "categories": [
    "gateway",
    "telegram",
    "agent-runtime",
    "concurrency"
  ],
  "source_docs": [
    "0000000000000005"
  ],
  "backlinks": null,
  "word_count": 408,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Role in the Architecture

`run_bot()` is the top-level async function that starts a PocketPaw instance wired to Telegram. It is invoked from the CLI or a process manager and runs until the process receives a cancellation signal. Understanding its structure helps when debugging startup failures or extending PocketPaw to support new entry points.

## Startup Sequence

The function follows a strict three-step initialisation order:

### 1. Message Bus
```python
bus = get_message_bus()
```
The bus is initialised first because both the adapter and the agent loop depend on it. `get_message_bus()` returns a singleton `MessageBus` instance. Initialising it before the adapter prevents a race where the adapter tries to publish an event before the bus is ready.

### 2. Telegram Adapter
```python
adapter = TelegramAdapter(token=..., allowed_user_id=...)
```
The adapter is created with credentials from `Settings` and started against the bus. The `allowed_user_id` restriction prevents the bot from responding to arbitrary Telegram users — only the configured owner can interact with it. This is a security boundary, not just a feature flag.

### 3. Agent Loop
```python
agent_loop = AgentLoop()
get_command_handler().set_agent_loop(agent_loop)
```
The `AgentLoop` is created and registered with the command handler before being started as a background task. The command handler registration is done here (rather than inside `AgentLoop.__init__`) so that the command subsystem can reference the loop without creating a circular import at module load time.

## Task-Based Concurrency

The agent loop is launched as an `asyncio.Task` via `create_task()`. This allows the gateway to `await loop_task` in the main coroutine, which means the process stays alive as long as the agent loop is running. If the loop exits unexpectedly, the gateway exits too — which is the correct behaviour: a Telegram bot with no running agent loop is useless, so a clean crash is preferable to a zombie process.

## Shutdown Handling

The `try/except asyncio.CancelledError` block ensures that when the process receives a cancellation (e.g., SIGTERM from a process manager or Ctrl-C from a developer), the gateway:

1. Logs a stopping message.
2. Calls `agent_loop.stop()` to flush any in-flight work.
3. Calls `adapter.stop()` to disconnect from the Telegram API cleanly.

The `finally` block guarantees cleanup runs even if the loop task raises an unexpected exception, preventing orphaned Telegram polling sessions that would block the next startup.

## Known Gaps

The gateway is Telegram-specific. There is no generic `run_gateway(adapter, settings)` abstraction that would allow the same startup pattern to be reused for Discord or Matrix without code duplication.