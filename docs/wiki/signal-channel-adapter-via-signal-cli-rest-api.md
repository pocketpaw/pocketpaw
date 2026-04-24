---
{
  "title": "Signal Channel Adapter via signal-cli REST API",
  "summary": "SignalAdapter integrates PocketPaw with Signal by polling a self-hosted signal-cli REST API instance every 2 seconds for inbound messages and sending replies via its HTTP endpoint. No streaming is supported — responses are buffered and sent as a single message when the agent finishes.",
  "concepts": [
    "signal-cli",
    "signal-cli-rest-api",
    "short polling",
    "phone number allow-list",
    "stream buffering",
    "attachment download",
    "httpx",
    "E.164 format",
    "BaseChannelAdapter"
  ],
  "categories": [
    "channel-adapters",
    "messaging",
    "signal",
    "polling"
  ],
  "source_docs": [
    "f8e861bda586f1d9"
  ],
  "backlinks": null,
  "word_count": 511,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

SignalAdapter bridges PocketPaw to the [Signal](https://signal.org) encrypted messaging network via [signal-cli-rest-api](https://github.com/bbernhard/signal-cli-rest-api) — a containerizable HTTP wrapper around the signal-cli Java application. This approach avoids direct dependency on the Signal protocol library and requires only an HTTP client, making the adapter portable and testable.

## Polling Architecture

Signal provides no native push webhook mechanism for self-hosted bots, so the adapter uses short polling: `_poll_loop()` calls `GET /v1/receive/{phone_number}` every 2 seconds while `_running` is true. The poll task is created in `_on_start()` via `asyncio.create_task()` and cancelled cleanly in `_on_stop()`.

Poll errors are logged at `DEBUG` level for `httpx.HTTPError` (transient network issues expected in normal operation) and `ERROR` for unexpected exceptions. This distinction prevents log noise from brief connectivity interruptions.

## Message Parsing

Each item returned by the receive endpoint follows signal-cli's envelope format: `{"envelope": {"source": ..., "dataMessage": {"message": ..., "attachments": [...]}}}`. The handler extracts `source` or `sourceNumber` (both field names appear in different signal-cli versions) to identify the sender. If neither is present, the message is dropped to avoid routing errors.

## Phone Number Allow-list

`allowed_phone_numbers` is an optional list of E.164-formatted phone numbers. If set, messages from any other number are silently discarded. This is the primary authorization mechanism for the Signal adapter, since Signal has no server-side bot authentication system.

## Attachment Handling

Attachments in signal-cli are referenced by a numeric ID. The adapter fetches each attachment by calling `GET /v1/attachments/{id}` on the same REST API host, delegating actual download and storage to `MediaDownloader`. The attachment filename and MIME type from the envelope metadata are forwarded to produce a correctly named local file. A `[Attached: name]` hint is appended to the message content so the LLM receives context about the attached files.

## Stream Buffering

Signal's REST API does not support message editing or incremental delivery. Outbound messages from streaming agent responses are accumulated per `chat_id` in `_buffers`. The full text is sent only when `is_stream_end` is received. This mirrors the same pattern used by WhatsApp adapters and prevents partial text from being delivered in an incomplete state.

## Sending

Outbound text is sent via `POST /v2/send` with a JSON body containing `message`, `number` (the sender's registered phone number), and `recipients`. Markdown is pre-processed by `convert_markdown()` with `Channel.SIGNAL`, which strips all formatting marks because Signal renders plain text only.

## HTTP Client Lifecycle

An `httpx.AsyncClient` is created in `_on_start()` with a 30-second timeout and closed in `_on_stop()`. Reusing a single client across all poll cycles and sends avoids connection-per-request overhead. The client is guarded with `if self._http` checks in every method that uses it to handle the case where `_on_start()` was never called (e.g., missing phone number).

## Known Gaps

- Polling introduces up to 2 seconds of latency on inbound messages. A webhook-based alternative would require changes to signal-cli-rest-api's deployment configuration.
- There is no retry logic for failed sends — a non-2xx response from `/v2/send` is logged as an error but not retried.
- Group message support is not implemented; the adapter treats the group's ID as the `chat_id` but has not been tested with group threads.