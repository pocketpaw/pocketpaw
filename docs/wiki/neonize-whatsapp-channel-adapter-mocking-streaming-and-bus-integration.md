---
{
  "title": "Neonize WhatsApp Channel Adapter: Mocking, Streaming, and Bus Integration",
  "summary": "Tests for the NeonizeAdapter, which connects PocketPaw to WhatsApp Personal via the neonize library. Covers auto-installation of the optional dependency, message send/receive lifecycle, stream buffering, QR code exposure, and bus subscription.",
  "concepts": [
    "NeonizeAdapter",
    "WhatsApp",
    "channel adapter",
    "neonize",
    "module mocking",
    "stream buffering",
    "bus subscription",
    "JID",
    "QR code",
    "auto-install",
    "MessageBus",
    "optional dependency"
  ],
  "categories": [
    "channel adapters",
    "WhatsApp",
    "testing",
    "message bus",
    "test"
  ],
  "source_docs": [
    "4f17ecf26fd5fb6e"
  ],
  "backlinks": null,
  "word_count": 476,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`NeonizeAdapter` bridges PocketPaw's internal message bus to WhatsApp Personal accounts using the `neonize` Python library. Because `neonize` is an optional, heavyweight dependency that connects to real WhatsApp infrastructure, the test file performs an elaborate module-level mock before importing the adapter — a pattern required when the import itself would fail without the library installed.

## Module Mocking Strategy

At the top of the file, `sys.modules` is pre-populated with fake `neonize`, `neonize.aioze`, `neonize.aioze.client`, `neonize.aioze.events`, and `neonize.utils` modules built from `types.ModuleType`. This ensures:
1. `import neonize` in the adapter succeeds even without the package installed.
2. Calls to `build_jid`, `NewAClient`, and event types use controlled mocks.
3. CI environments without WhatsApp credentials can still run these tests.

## Configuration Tests

- `test_channel_property`: `NeonizeAdapter.channel` must return `Channel.WHATSAPP`. Returning the wrong channel would cause the bus to misroute outbound messages.
- `test_default_db_path` / `test_custom_db_path`: The SQLite session database defaults to `~/.pocketpaw/neonize.sqlite3`. A custom path must be respected — operators running multiple WhatsApp accounts on one machine need separate databases.

## Auto-Install Behavior

- `test_import_error_auto_installs`: When `neonize` is missing at runtime (not at test time), `_on_start` attempts to pip-install it automatically. If installation fails, the adapter raises with a clear error message rather than silently failing to connect. This design trades security (running pip at runtime) for operator convenience on personal deployments.

## Lifecycle Tests

- `test_start_stop`: Starting the adapter subscribes to the `MessageBus` for outbound messages; stopping it unsubscribes. Without proper teardown, the adapter would continue receiving bus events after being stopped, causing send attempts on a closed connection.
- `test_qr_data_attribute` / `test_connected_attribute`: WhatsApp Personal requires QR code scanning on first connection. The adapter exposes `_qr_data` so the dashboard can render it, and `_connected` to report status.

## Message Sending

- `test_send_normal_message`: Outbound `OutboundMessage` events trigger `_send_text` with a cached JID (WhatsApp address). The JID is cached to avoid repeated `build_jid` calls, which would add latency on every message.
- `test_send_skipped_when_not_connected`: If the adapter is not yet connected (QR not scanned), outbound messages are silently dropped. The alternative — queuing them — would require unbounded buffering, risking memory exhaustion during long offline periods.

## Stream Buffering

WhatsApp messages must be delivered as complete strings, not token-by-token as the LLM streams them:
- `test_stream_chunk_buffering`: Each streaming chunk is appended to an internal buffer rather than sent immediately.
- `test_stream_end_flushes_buffer`: When the stream ends, the full buffer is sent as a single WhatsApp message. This prevents WhatsApp from delivering 50 one-word messages to the user.
- `test_stream_end_empty_buffer_no_send`: If the stream ends with an empty buffer (e.g., the model produced no text), no message is sent. Sending an empty message would confuse users.

## Known Gaps

No TODOs in the test file. The auto-install behavior bypasses the workspace's supply chain security (minimum package age policy) because pip is called directly at runtime. This is a known design tension not yet resolved.
