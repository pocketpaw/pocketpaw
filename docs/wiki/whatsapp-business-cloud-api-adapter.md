---
{
  "title": "WhatsApp Business Cloud API Adapter",
  "summary": "WhatsAppAdapter connects PocketPaw to the official Meta WhatsApp Business Cloud API, receiving messages via webhook callbacks verified with HMAC and sending replies via the Graph API. It handles the two-step media download flow and sends automatic read receipts.",
  "concepts": [
    "WhatsApp Business Cloud API",
    "Meta Graph API",
    "HMAC webhook verification",
    "two-step media download",
    "read receipts",
    "media upload",
    "stream buffering",
    "httpx",
    "phone number allow-list",
    "BaseChannelAdapter",
    "v21.0"
  ],
  "categories": [
    "channel-adapters",
    "messaging",
    "whatsapp",
    "business-api"
  ],
  "source_docs": [
    "d37d21b8ca7d5340"
  ],
  "backlinks": null,
  "word_count": 510,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

WhatsAppAdapter targets Meta's official [WhatsApp Business Cloud API](https://developers.facebook.com/docs/whatsapp/cloud-api) (as opposed to `NeonizeAdapter`, which uses the unofficial Web protocol). This path requires a Meta Developer account, an approved business phone number, and a public webhook URL for receiving events — but it is the production-grade approach for commercial deployments.

## Webhook Verification

`handle_webhook_verify()` handles Meta's subscription challenge: when the webhook URL is first registered, Meta sends a GET request with `hub.mode=subscribe`, `hub.verify_token`, and `hub.challenge`. The adapter uses `hmac.compare_digest()` to compare the incoming token against the configured `verify_token` in constant time, preventing timing attacks. On success, it returns the challenge string for Meta to confirm ownership.

## Inbound Webhook Processing

`handle_webhook_message()` traverses Meta's nested payload structure: `entry[] → changes[] → value → messages[]`. For each message, it checks the phone number allow-list and calls `_extract_content_and_media()` to get text and any attached files. After publishing to the bus, it sends a read receipt via `_mark_as_read()` so the sender sees the double-blue-tick confirmation.

## Two-Step Media Download

WhatsApp Business Cloud API uses a two-step process for media retrieval:

1. `GET https://graph.facebook.com/{version}/{media_id}` — returns a JSON body containing a short-lived download URL
2. `GET {download_url}` — fetches the actual file bytes, with the same Authorization header

This is implemented in `_download_whatsapp_media()`. The short-lived URL is not cached — each download call fetches a fresh URL. The second download reuses the existing `httpx.AsyncClient` (with auth headers baked in at construction) via `MediaDownloader.download_url_with_auth()`.

## Supported Media Types

The adapter maps WhatsApp message type strings (`image`, `document`, `audio`, `video`, `sticker`) to their JSON block keys via `media_key_map`. Each block may contain a `caption`, a `media_id`, a `mime_type`, and a `filename`. For unsupported message types (e.g., `reaction`, `location`, `interactive`), the adapter produces a human-readable placeholder like `"[reaction message received]"` rather than failing silently with an empty message.

## Outbound Media Upload

`_send_media_file()` implements the two-step upload pattern: first `POST {phone_number_id}/media` with a multipart form upload to get a `media_id`, then `POST {phone_number_id}/messages` with the media ID and type. MIME types are resolved from a local extension map rather than relying on Python's `mimetypes` module to avoid platform-specific discrepancies.

## Stream Buffering

Like all other WhatsApp adapters, outbound streaming responses are buffered per `chat_id` in `_buffers`. Chunks are accumulated until `stream_end` arrives, then the complete text is sent as a single API call. This prevents the message feed from being cluttered with dozens of partial fragments.

## HTTP Client and Auth

A single `httpx.AsyncClient` is created in `_on_start()` with the Authorization header set globally (`Bearer {access_token}`). This saves header construction overhead on every request. The client is closed in `_on_stop()`. All send and download methods guard against `self._http is None` to handle the case where the adapter was never fully started.

## Known Gaps

- API version is hardcoded as `v21.0` in the module-level constant `WHATSAPP_API_VERSION`. Upgrading to a newer Graph API version requires a code change.
- The `handle_webhook_verify()` method is sync (not async) because FastAPI calls it inline during GET request handling. This is intentional but worth noting as an inconsistency with the async `handle_webhook_message()` path.