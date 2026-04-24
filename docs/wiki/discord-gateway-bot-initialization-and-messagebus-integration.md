---
{
  "title": "Discord Gateway — Bot Initialization and MessageBus Integration",
  "summary": "discord_gateway.py provides the `run_discord_bot` entry point that wires the Discord adapter to the MessageBus and starts the AgentLoop for the Discord channel. It translates all Discord-specific configuration from Settings into the adapter, then hands off to the agent infrastructure.",
  "concepts": [
    "discord_gateway",
    "run_discord_bot",
    "DiscordAdapter",
    "MessageBus",
    "AgentLoop",
    "Discord bot",
    "allowed_guild_ids",
    "conversation channels",
    "bot token",
    "channel allow-lists"
  ],
  "categories": [
    "channel-adapters",
    "discord",
    "messaging",
    "bot-infrastructure"
  ],
  "source_docs": [
    "f298e0d5de12afee"
  ],
  "backlinks": null,
  "word_count": 466,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`discord_gateway.py` is a thin initialization module — its entire job is to read Discord settings, construct the Discord adapter with those settings, register it on the MessageBus, and start the agent loop. No Discord-specific logic lives here; that belongs in the adapter itself. This separation keeps the gateway file stable while the adapter evolves independently.

## Initialization Flow

```python
async def run_discord_bot(settings: Settings) -> None:
    bus = get_message_bus()
    adapter = DiscordAdapter(
        token=settings.discord_bot_token,
        allowed_guild_ids=settings.discord_allowed_guild_ids,
        # ... all Discord config fields
    )
    bus.register_adapter(adapter)
    loop = AgentLoop(settings=settings)
    await loop.run()
```

The `get_message_bus()` call retrieves the global singleton bus. The adapter is registered on the bus so outbound messages from agents are delivered to Discord, and inbound Discord messages are forwarded to the agent loop.

## Settings Passthrough

The gateway passes all Discord-specific settings from the `Settings` object to the adapter:

- **`discord_bot_token`** — the bot's authentication token
- **`discord_allowed_guild_ids`** — restricts which Discord servers the bot responds in
- **`discord_allowed_user_ids`** — restricts which users can interact with the bot
- **`discord_allowed_channel_ids`** — restricts which channels trigger the bot
- **`discord_conversation_channel_ids`** — channels where the bot maintains conversation context
- **`discord_conversation_all_channels`** — if true, conversation mode applies globally
- **`discord_conversation_exclude_channel_ids`** — per-channel opt-out from conversation mode
- **`discord_bot_name`** — display name override

The allow-list settings exist to prevent the bot from responding to unintended servers, users, or channels. A Discord bot added to a large shared server without restrictions would respond to all traffic, which could create privacy issues, unexpected inference costs, or confusing user experiences for people who didn't know a bot was in the server.

## Why a Separate Gateway Module

The gateway pattern (a thin module that wires components together for one channel) mirrors the approach taken for other channels like Telegram and Slack. Each channel gets its own `*_gateway.py` entry point. This makes it straightforward to add or remove channels: the main entrypoint (`__main__.py`) imports and calls the relevant gateway, and the rest of the infrastructure is shared.

## Naming Quirk

The import aliases `DiscliAdapter` as `DiscordAdapter`:

```python
from pocketpaw.bus.adapters.discord_adapter import DiscliAdapter as DiscordAdapter
```

This is a typo in the adapter's original class name (`DiscliAdapter`). The alias keeps the gateway code readable while the underlying name is fixed in a separate PR.

## Known Gaps

- The gateway function does not handle bot disconnection or reconnection. If the Discord WebSocket drops, the entire coroutine exits and the bot goes offline until the process is restarted externally.
- There is no startup validation that `discord_bot_token` is set — an empty token causes the adapter to fail at WebSocket connection time rather than failing fast at initialization with a clear configuration error.
- The `DiscliAdapter` → `DiscordAdapter` alias is a workaround for a typo in the adapter class name; the adapter itself should be renamed.
