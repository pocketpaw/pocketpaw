---
{
  "title": "Slack Channel Adapter with Socket Mode and Live Streaming",
  "summary": "SlackAdapter connects PocketPaw to Slack using Socket Mode, which requires no public webhook URL — the bot maintains a persistent WebSocket connection to Slack's servers. It supports slash commands, DM messages, app mentions, threaded replies, file uploads, and live-edited streaming responses.",
  "concepts": [
    "Slack Socket Mode",
    "AsyncSocketModeHandler",
    "slack_bolt",
    "App-Level Token",
    "Bot User OAuth Token",
    "live-edit streaming",
    "thread_ts",
    "slash commands",
    "chat.update",
    "files_upload_v2",
    "channel allow-list",
    "mention stripping"
  ],
  "categories": [
    "channel-adapters",
    "messaging",
    "slack",
    "streaming"
  ],
  "source_docs": [
    "35b85367f039fa76"
  ],
  "backlinks": null,
  "word_count": 546,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

SlackAdapter uses `slack_bolt`'s `AsyncSocketModeHandler` to connect to Slack without a publicly reachable server. Instead of Slack pushing HTTP requests to a URL, the adapter opens an outbound WebSocket connection to Slack's Socket Mode infrastructure using an App-Level Token (`xapp-...`). This makes the adapter work behind firewalls and NAT with no tunnel configuration.

## Token Validation at Startup

Before starting Socket Mode, `_on_start()` performs eager token validation using `auth.test()` for the Bot User OAuth Token and `AiohttpSocketModeClient.issue_new_wss_url()` for the App-Level Token. Both validations raise a descriptive `RuntimeError` on failure rather than allowing neonize-style silent retry loops. This surfaces misconfiguration immediately rather than hiding it behind cryptic connection failures.

## Event Routing

Three event types are handled:

1. **`app_mention`** — any message where the bot is @-mentioned in a channel
2. **`message`** (DMs only) — messages in direct message channels (`channel_type == "im"`), with bot messages and subtypes filtered out to prevent the bot replying to itself
3. **Slash commands** — a fixed set of PocketPaw commands (`/new`, `/sessions`, `/resume`, `/clear`, `/rename`, `/status`, `/delete`, `/backend`, `/backends`, `/model`, `/tools`, `/help`, `/kill`) registered at bot startup

The slash command registration uses a closure over `_cmd_name` to capture the command string for each loop iteration — without the `_cmd=_cmd_name` default argument, Python's late binding would make all handlers reference the last value of `_cmd_name`.

Note: each slash command must also be declared in the Slack App manifest at `api.slack.com → Slash Commands` to prevent Slack from returning "command not found" errors.

## Thread Awareness

Incoming events that carry `thread_ts` (the timestamp of the parent message) are forwarded in the message metadata. Outbound messages check `metadata.get("thread_ts")` and include it as the `thread_ts` parameter in `chat_postMessage` and `chat_update` calls. This ensures the bot always replies within the correct thread rather than creating a new top-level message.

## Live-Edit Streaming

Slack supports message editing via `chat.update`, enabling a streaming UX: as the agent produces tokens, the bot sends an initial `"..."` placeholder, then updates the same message every 1.5 seconds with accumulated content. The 1.5-second throttle (`last_update` timestamp in `_buffers`) prevents hitting Slack's rate limit (approximately 1 update per second per channel).

The buffer per `chat_id` stores the Slack message timestamp (`ts`) needed to target `chat.update`. On `stream_end`, `_flush_stream_buffer()` issues a final update with the complete formatted text.

## File Uploads

Attachments from incoming messages are downloaded using `download_url_with_auth()` with the bot token as a Bearer header (Slack's `url_private_download` requires authentication). Outbound media files are uploaded using `files_upload_v2`.

## Channel Allow-list

`allowed_channel_ids` filters inbound events to a specific set of Slack channels. If the list is empty, all channels are accepted. This is useful for scoping a PocketPaw bot to a single workspace channel without server-side Slack configuration.

## Mention Stripping

App-mention events include the bot's user mention token (e.g., `<@U12345>`) at the start of the message text. The handler strips all `<@USERID>` tokens with a regex before publishing to the bus, so the agent receives only the user's intent.

## Known Gaps

- The `allowed_channel_ids` filter does not distinguish between DMs and public channels — a DM has a channel ID starting with `D`, which can be explicitly excluded or included.
- There is no deduplication guard for duplicate Slack events, which can occasionally be re-delivered by Socket Mode on reconnect.