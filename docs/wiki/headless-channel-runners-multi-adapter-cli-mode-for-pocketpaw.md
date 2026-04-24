---
{
  "title": "Headless Channel Runners: Multi-Adapter CLI Mode for PocketPaw",
  "summary": "`headless.py` provides the CLI runtime functions that launch PocketPaw in headless server mode — either as a single Telegram bot or as multiple channel adapters (Discord, Slack, WhatsApp, Signal, Matrix, Teams, Google Chat) sharing one `AgentLoop` and message bus. It was extracted from `__main__.py` to keep the entry point thin while keeping channel-setup complexity isolated.",
  "concepts": [
    "headless mode",
    "channel adapters",
    "AgentLoop",
    "message bus",
    "Telegram pairing",
    "Discord adapter",
    "Slack adapter",
    "WhatsApp webhook",
    "multi-channel runtime",
    "asyncio task management"
  ],
  "categories": [
    "channel adapters",
    "CLI",
    "runtime",
    "deployment"
  ],
  "source_docs": [
    "16870db3676e7cba"
  ],
  "backlinks": null,
  "word_count": 513,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`headless.py` (`src/pocketpaw/headless.py`) manages the lifecycle of headless channel adapters. It handles first-time setup flows, configuration validation, adapter initialization, and graceful shutdown. All adapter types share a single `AgentLoop` and message bus, which is the architectural key to multi-channel operation.

## Telegram Mode and First-Time Pairing

```python
async def run_telegram_mode(settings: Settings) -> None:
    if not settings.telegram_bot_token or not settings.allowed_user_id:
        port = find_available_port(settings.web_port)
        webbrowser.open(f"http://localhost:{port}")
        await run_pairing_server(settings)
        settings = get_settings(force_reload=True)
    await run_bot(settings)
```

When running for the first time without credentials, the function starts a local pairing server instead of failing. This prevents a frustrating cold-start experience where users see a cryptic "missing token" error. `find_available_port` is called before displaying instructions so that the printed URL is always correct — showing the port before knowing it's available would be misleading.

After `run_pairing_server` completes (user has pasted their token and scanned the QR code), settings are force-reloaded to pick up the newly saved credentials before starting the actual bot.

## Multi-Channel Mode

`run_multi_channel_mode` processes CLI flags (`args.discord`, `args.slack`, etc.) and instantiates the corresponding adapter for each enabled channel:

```python
bus = get_message_bus()
adapters = []

if args.discord:
    if not settings.discord_bot_token:
        logger.error("Discord bot token not configured...")
    else:
        adapters.append(DiscordAdapter(...))
```

Missing credentials log an error but don't abort — the function continues building adapters for channels that are properly configured. This allows a deployment to recover partially (e.g., Slack works, Discord is misconfigured) rather than failing entirely.

All adapters share one `AgentLoop`:

```python
agent_loop = AgentLoop()
for adapter in adapters:
    await adapter.start(bus)
loop_task = asyncio.create_task(agent_loop.start())
```

Sharing one loop means a message from Discord and a message from Slack queue into the same agent, which ensures consistent conversational context and avoids spawning duplicate agent instances.

## WhatsApp Webhook Server

WhatsApp's message delivery model is webhook-based rather than WebSocket-based, so when WhatsApp is active, a minimal uvicorn server is spun up alongside the adapters:

```python
if args.whatsapp:
    wa_app = create_whatsapp_app(settings)
    whatsapp_server = uvicorn.Server(config)
    asyncio.create_task(whatsapp_server.serve())
```

The gateway module's `_whatsapp_adapter` module-level variable is pointed at the running adapter instance so that incoming webhook calls can route messages to the correct adapter without passing references through the HTTP layer.

## Display Detection

```python
def _is_headless() -> bool:
    if sys.platform in ("darwin", "win32"):
        return False
    return not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY")
```

This checks for the absence of both X11 and Wayland display variables on Linux, which is the reliable signal for a headless server. macOS and Windows always have a display subsystem so are excluded from the check.

## Dependency Validation

`_check_extras_installed` exits early with a helpful install command if required optional packages are missing for the selected mode. This prevents confusing `ImportError` tracebacks deep inside the adapter — the error surfaces at startup with clear remediation instructions.

## Known Gaps

- Signal and Matrix adapters are checked with `getattr(args, "signal", False)` rather than `args.signal`, suggesting these CLI flags may not be consistently defined on the `argparse.Namespace` in all code paths.
- The WhatsApp server's `asyncio.create_task` is fire-and-forget with no reference stored. If the server task fails, the exception is silently lost unless the event loop's exception handler catches it.