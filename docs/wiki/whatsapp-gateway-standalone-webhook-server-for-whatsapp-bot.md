---
{
  "title": "WhatsApp Gateway: Standalone Webhook Server for WhatsApp Bot",
  "summary": "The WhatsApp gateway module creates a minimal FastAPI application with Meta webhook verification and message-delivery endpoints, then runs it under uvicorn as a standalone service. It delegates all WhatsApp message handling to `WhatsAppAdapter`, keeping the gateway thin and the adapter reusable across deployment modes.",
  "concepts": [
    "WhatsApp gateway",
    "WhatsAppAdapter",
    "webhook verification",
    "Meta Cloud API",
    "hub.challenge",
    "FastAPI",
    "uvicorn",
    "POST /webhook/whatsapp",
    "standalone mode",
    "module-level adapter"
  ],
  "categories": [
    "whatsapp",
    "channel adapter",
    "web server"
  ],
  "source_docs": [
    "c7a2234e7d34e266"
  ],
  "backlinks": null,
  "word_count": 447,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

PocketPaw supports WhatsApp as a messaging channel through Meta's Cloud API webhook model. Unlike Telegram (which uses polling or webhooks interchangeably), WhatsApp requires an HTTPS webhook endpoint that Meta verifies before delivering messages. `whatsapp_gateway.py` owns the server side of this contract: it handles the verification handshake and routes incoming message payloads to the adapter.

## Webhook Verification Handshake

Meta's webhook verification protocol sends a `GET /webhook/whatsapp` request with three query parameters: `hub.mode`, `hub.verify_token`, and `hub.challenge`. The server must confirm the verify token matches and echo back `hub.challenge` as plain text. The gateway delegates this to `WhatsAppAdapter.handle_webhook_verify`, which knows the configured verify token. If the adapter is not yet initialised, the route returns HTTP 503 to prevent a verification-before-startup race condition.

## Message Delivery Route

`POST /webhook/whatsapp` receives message payloads from Meta after verification. The raw JSON body is parsed and passed to the adapter's handler. The route always returns HTTP 200 immediately—Meta's delivery guarantees require a fast acknowledgement; processing happens asynchronously after the response is sent. Returning a non-200 status causes Meta to retry delivery, which would produce duplicate messages.

## Module-Level Adapter Reference

`_whatsapp_adapter` is stored as a module-level variable rather than injected into the FastAPI app's state. This allows `create_whatsapp_app` to build the FastAPI app before the adapter is fully configured (e.g., while settings are being resolved), and the routes close over the module-level reference. The adapter is set during `run_whatsapp_bot` startup.

## `run_whatsapp_bot` Startup Sequence

The function is the single entry point for standalone WhatsApp deployment:

1. Instantiate and configure `WhatsAppAdapter` with the loaded `Settings`.
2. Set the module-level `_whatsapp_adapter` reference so route closures can see it.
3. Start uvicorn with the FastAPI app.

It is called by the CLI (`pocketpaw run --channel whatsapp`) and is not shared with the multi-channel bus mode, where the adapter is registered differently.

## Separation of Concerns

The gateway intentionally contains no message-processing logic. Parsing, entity extraction, tool dispatch, and reply generation all live in `WhatsAppAdapter`. This means the gateway can be replaced (e.g., with a different ASGI server, or deployed behind a cloud function proxy) without touching the adapter logic.

## Known Gaps

- The module only supports a single `WhatsAppAdapter` instance. Multi-tenant deployments where different WhatsApp business accounts need separate webhooks would require either multiple processes or a routing layer keyed on the webhook path.
- There is no request signature verification on the `POST /webhook/whatsapp` route. Meta signs webhook payloads with HMAC-SHA256 using the app secret; verifying that signature prevents spoofed payloads from being processed—a meaningful security gap for production deployments.
- Uvicorn is started with default settings (no TLS, no worker count tuning). Production deployments must run behind a TLS-terminating reverse proxy.
