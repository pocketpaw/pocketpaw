---
{
  "title": "Channel Adapter Schemas — Status, Configuration, and Toggle",
  "summary": "The channel schemas define the state and control contracts for PocketPaw's messaging platform adapters (Discord, Slack, WhatsApp, Telegram, Signal, Matrix, Teams, Google Chat). They separate read (status queries) from write (save config, start/stop) operations.",
  "concepts": [
    "channel adapters",
    "ChannelInfo",
    "ChannelStatusResponse",
    "ChannelSaveRequest",
    "ChannelToggleRequest",
    "Discord",
    "Slack",
    "Telegram",
    "autostart",
    "pattern validation",
    "messaging platforms"
  ],
  "categories": [
    "channels",
    "schemas",
    "integrations"
  ],
  "source_docs": [
    "6f076ef809eaeef3"
  ],
  "backlinks": null,
  "word_count": 460,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

PocketPaw can run as a bot on multiple messaging platforms simultaneously. Each platform integration is a "channel adapter" with its own credentials and lifecycle. These schemas are the API contract for the channels dashboard: querying which channels are running, saving configuration, and toggling adapters on or off.

## `ChannelInfo`

```python
class ChannelInfo(BaseModel):
    configured: bool = False
    running: bool = False
    autostart: bool = False
```

A three-field summary of a single channel adapter's state. The distinction between `configured` and `running` is important: a channel can be configured (credentials saved) but not running (adapter not started), or running (active) and configured (necessarily true). The `autostart` flag indicates whether the adapter should restart automatically when PocketPaw starts.

Default values of `False` for all fields mean that an unconfigured channel is represented by an empty `ChannelInfo()` object rather than `None`, keeping the response shape consistent regardless of configuration state.

## `ChannelStatusResponse`

```python
class ChannelStatusResponse(BaseModel):
    discord: ChannelInfo = ChannelInfo()
    slack: ChannelInfo = ChannelInfo()
    # ... one field per supported platform
```

Each supported platform is a named field with a default of `ChannelInfo()`. This means the status response always includes all 8 platforms, even if none are configured. Clients can iterate a fixed list of known fields rather than handling a variable-length list of platform names.

The hardcoded field list also serves as the authoritative registry of supported channels — adding a new platform requires adding a field here, making the addition visible in code review.

## `ChannelSaveRequest`

```python
class ChannelSaveRequest(BaseModel):
    channel: str
    config: dict
```

An untyped `config: dict` is used rather than per-platform typed models because each channel has completely different configuration fields (Discord needs a bot token; Telegram needs a bot token and optionally a webhook URL; WhatsApp needs a business account ID). A union of typed models would require the client to know which type to use; an untyped dict keeps the endpoint generic at the cost of server-side validation.

## `ChannelToggleRequest`

```python
class ChannelToggleRequest(BaseModel):
    channel: str
    action: str = Field(..., pattern="^(start|stop)$")
```

The `pattern` constraint is a server-side guard that rejects any action value other than `"start"` or `"stop"`. Without this, a caller could send `action: "restart"` or `action: "delete"` and the handler would need to validate manually. The regex constraint shifts that validation to Pydantic's parse step, returning a 422 automatically for invalid values.

## Known Gaps

- `ChannelSaveRequest.config` is untyped. Validation of channel-specific fields (e.g., that a Discord bot token matches the expected format) must happen in the handler or the adapter itself, not at schema parse time.
- There is no per-channel `GET` endpoint schema — the only read shape is `ChannelStatusResponse`, which returns all channels. Fetching configuration for a single channel (e.g., to pre-populate an edit form) requires parsing the full status response.
