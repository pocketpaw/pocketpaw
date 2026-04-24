---
{
  "title": "WhatsApp Personal Mode Adapter via Neonize",
  "summary": "NeonizeAdapter connects PocketPaw to WhatsApp using the neonize Python library, which wraps the open-source whatsmeow Go client. It requires only a QR-code scan to pair — no Meta Developer account or public webhook URL is needed.",
  "concepts": [
    "neonize",
    "whatsmeow",
    "WhatsApp Web multi-device",
    "QR code pairing",
    "dual event loop",
    "asyncio.run_coroutine_threadsafe",
    "JID cache",
    "pre-flight TCP probe",
    "stream buffering",
    "BaseChannelAdapter",
    "SQLite session persistence",
    "daemon thread"
  ],
  "categories": [
    "channel-adapters",
    "messaging",
    "whatsapp",
    "async-runtime"
  ],
  "source_docs": [
    "d96ee12be0eebc3b"
  ],
  "backlinks": null,
  "word_count": 615,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

NeonizeAdapter is the personal-mode WhatsApp channel adapter in PocketPaw. Unlike `WhatsAppAdapter` (which targets the official Business Cloud API), this adapter uses [neonize](https://github.com/krypton-byte/neonize) — a Python binding around whatsmeow, a Go implementation of the WhatsApp Web multi-device protocol. The practical implication: any personal WhatsApp number can be paired by scanning a QR code, with no Meta Developer account, approved business number, or public tunnel required.

## The Dual Event Loop Problem

The most architecturally significant challenge the adapter solves is bridging two incompatible async runtimes. Neonize's Go runtime dispatches callbacks (QR codes, incoming messages, connection events) via `asyncio.run_coroutine_threadsafe()` onto its own `event_global_loop` — a dedicated Python event loop that must be running in a background thread. FastAPI's ASGI server runs on a separate main event loop.

The module-level `_ensure_neonize_loop_running()` function handles this by starting `event_global_loop.run_forever()` in a daemon thread, protected by a double-checked locking pattern (`_neonize_loop_lock`) so it starts exactly once regardless of how many adapters exist. Without this, neonize callbacks would silently never fire.

For outbound sends, `asyncio.run_coroutine_threadsafe()` is used again to dispatch `client.send_message()` onto neonize's loop. If that loop is not running, the call falls back to `await` directly — a defensive path for edge cases during shutdown.

## Pre-flight Connectivity Check

`_preflight_connectivity_check()` performs a TCP probe to `web.whatsapp.com:443` before letting neonize's Go runtime attempt the WebSocket connection. This exists because a failed WebSocket dial in the Go runtime causes a panic that kills the entire Python process — there is no catchable Python exception. The pre-flight check converts that fatal scenario into a normal `ConnectionError`, allowing PocketPaw to surface a helpful message and keep running.

The probe uses `loop.run_in_executor()` to run the blocking `socket.connect()` call without blocking the event loop, wrapped in `asyncio.wait_for()` with a 5-second timeout.

## Session Persistence and JID Caching

Neonize stores WhatsApp session credentials in a SQLite database at `~/.pocketpaw/neonize.sqlite3` by default. This path can be overridden via `db_path` at construction time. On subsequent starts with a valid session file, neonize reconnects without re-scanning the QR code.

For outbound messages, WhatsApp addresses contacts via JID (Jabber Identifier) protobufs, not plain phone number strings. The adapter caches JID objects keyed by their string representation (`_jid_cache`) so that replies reuse the exact protobuf object received in the incoming message. If no cached JID is available (e.g., the bot is initiating contact), `build_jid()` reconstructs it from the `user@server` string.

## Stream Buffering

WhatsApp does not support live-edited messages or streaming delivery. When PocketPaw's agent generates a streamed response, `send()` accumulates `is_stream_chunk` payloads per `chat_id` in `_buffers` and only transmits the complete text when the `is_stream_end` flag arrives. Partial chunks are silently dropped with no visible side effects.

## Message Handling and Media

Incoming messages are handled in the `on_message` closure registered on the neonize client. The handler skips self-sent messages via `source.IsFromMe`. For media messages (image, document, audio, video, sticker), the handler calls `client.download_any()` to fetch bytes, then delegates to `MediaDownloader` for saving to disk. A `[Attached: filename]` hint is appended to the text content so the LLM sees both the text and a reference to any attached file.

## Graceful Shutdown

`_on_stop()` dispatches `client.disconnect()` onto neonize's event loop (not FastAPI's) and waits up to 5 seconds using `future.result(timeout=5)`. Any remaining asyncio tasks are cancelled and awaited to prevent "Task destroyed but pending" warnings at process exit.

## Known Gaps

- The `_qr_data` attribute is set when a QR code arrives but there is no built-in mechanism to surface it to a REST endpoint or UI — this is left to the consuming application.
- Auto-install of the `neonize` package on first run may require a process restart; the adapter raises a `RuntimeError` with instructions rather than handling this transparently.