---
{
  "title": "Slack Bot Gateway: Bootstrapping the PocketPaw Slack Integration",
  "summary": "The `slack_gateway.py` module is the top-level entry point for running PocketPaw as a Slack bot, wiring together the message bus, Slack adapter, and agent loop into a single coroutine. It handles clean startup and shutdown so the bot can run reliably as a long-lived async process.",
  "concepts": [
    "SlackAdapter",
    "AgentLoop",
    "message bus",
    "Socket Mode",
    "async startup",
    "channel allowlist",
    "Settings",
    "CancelledError",
    "shutdown handling",
    "command handler"
  ],
  "categories": [
    "channel-adapters",
    "slack-integration",
    "async-runtime"
  ],
  "source_docs": [
    "8e674174b37f20f5"
  ],
  "backlinks": null,
  "word_count": 501,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`run_slack_bot` is the single exported coroutine that bootstraps the entire Slack-facing side of PocketPaw. Rather than scattering initialization across multiple places, all three major subsystems — the message bus, the Slack adapter, and the agent loop — are assembled here and started in a coordinated sequence. This tight coupling at the gateway layer is intentional: it provides one clear place to trace how messages flow from Slack into the agent runtime.

## Startup Sequence

The function follows a strict ordering:

1. **Message bus acquisition** — `get_message_bus()` returns the singleton bus, which must exist before any adapter or loop can publish or subscribe to events.
2. **Adapter construction** — `SlackAdapter` is built with three credentials from `Settings`: `slack_bot_token` (used for API calls), `slack_app_token` (used for Socket Mode connection), and `slack_allowed_channel_ids` (an allowlist that gates which channels the bot will respond in). The channel allowlist is a security boundary — without it, the bot would respond to any channel it is invited to.
3. **Agent loop wiring** — `AgentLoop` is instantiated and registered with the command handler via `get_command_handler().set_agent_loop(agent_loop)`. This deferred wiring pattern avoids a circular import: the command handler module does not import `AgentLoop` at module load time; it only holds a reference once the gateway sets it.
4. **Concurrent start** — `adapter.start(bus)` is awaited first (connects to Slack's Socket Mode and begins routing inbound events onto the bus), then `agent_loop.start()` is wrapped in an `asyncio.create_task` so the loop processes events concurrently.

## Shutdown Handling

The gateway uses a `try/finally` block around `await loop_task`. When an `asyncio.CancelledError` propagates (typically from a process signal via `asyncio.run` cancellation), the finally block ensures:

- `agent_loop.stop()` is called — drains in-flight requests and closes the LLM backend session.
- `adapter.stop()` is called — cleanly disconnects from Slack's Socket Mode API.

Without this ordering, abrupt process termination could leave Slack events in an unacknowledged state (causing Slack to retry delivery) or leave the LLM backend with open HTTP connections.

## Configuration via Settings

All secrets and configuration are pulled from the `Settings` object, which is populated from environment variables. This means the gateway itself contains no hardcoded values and can be pointed at different Slack workspaces purely by changing environment variables — useful for dev/staging/production parity.

## Design Rationale

The gateway is deliberately thin. It does not contain retry logic, health checks, or reconnection handling — those responsibilities belong to `SlackAdapter` and `AgentLoop` respectively. Keeping the gateway file small makes it easy to audit the startup/shutdown contract at a glance.

```python
async def run_slack_bot(settings: Settings) -> None:
    bus = get_message_bus()
    adapter = SlackAdapter(
        bot_token=settings.slack_bot_token,
        app_token=settings.slack_app_token,
        allowed_channel_ids=settings.slack_allowed_channel_ids,
    )
    agent_loop = AgentLoop()
    get_command_handler().set_agent_loop(agent_loop)
    await adapter.start(bus)
    loop_task = asyncio.create_task(agent_loop.start())
    try:
        await loop_task
    except asyncio.CancelledError:
        pass
    finally:
        await agent_loop.stop()
        await adapter.stop()
```

## Known Gaps

No explicit reconnection logic is present in this file — if the Socket Mode connection drops and `SlackAdapter` does not handle reconnects internally, the entire bot process would need to be restarted externally (e.g., by a process supervisor like systemd or Docker restart policy).