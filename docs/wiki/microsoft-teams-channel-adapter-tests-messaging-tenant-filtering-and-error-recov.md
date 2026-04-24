---
{
  "title": "Microsoft Teams Channel Adapter Tests: Messaging, Tenant Filtering, and Error Recovery",
  "summary": "Tests for PocketPaw's Microsoft Teams channel adapter, covering initialisation, message processing, stream buffering, tenant-based filtering, bus integration, lifecycle management, and error recovery. The `botbuilder-core` dependency is mocked since it is optional.",
  "concepts": [
    "TeamsAdapter",
    "BaseChannelAdapter",
    "botbuilder-core",
    "tenant filtering",
    "stream buffering",
    "error recovery",
    "MessageBus",
    "Bot Framework",
    "webhook handler"
  ],
  "categories": [
    "testing",
    "channel adapters",
    "Microsoft Teams",
    "test"
  ],
  "source_docs": [
    "33c20e56d573efff"
  ],
  "backlinks": null,
  "word_count": 458,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

The Teams adapter connects PocketPaw to Microsoft Teams via the Bot Framework. Because `botbuilder-core` is an optional dependency (not all PocketPaw deployments need Teams), the entire SDK is mocked at the `sys.modules` level before the adapter is imported. This lets the test suite run in any environment.

## Initialisation

`TestTeamsAdapterInit` confirms that default configuration values (timeout, buffer sizes, retry counts) match documented defaults, and that custom configuration overrides work correctly. These tests prevent silent regressions when default values are changed during refactoring.

## Message Processing

`TestTeamsAdapterProcessActivity` covers four inbound message scenarios:

- `test_process_message`: A normal Teams `message` activity creates an inbound event and publishes it to the bus.
- `test_skip_non_message_activity`: Non-message activities (reactions, member joins) are silently dropped. Processing these as chat messages would produce nonsensical agent responses.
- `test_empty_text_skipped`: Empty message text is dropped to avoid triggering agent responses to blank messages.
- `test_tenant_filter`: Activities from tenants not in the allowed list are dropped. Enterprise Teams deployments share bots across tenants; filtering prevents cross-tenant data leakage.

## Stream Buffering

Three stream tests mirror the Slack adapter's buffering strategy:

- `test_send_stream_accumulates`: Token events accumulate in a buffer.
- `test_send_stream_end_flushes`: The end signal flushes the buffer as a single Teams message update.
- `test_send_empty_skipped`: Empty stream payloads are not sent to the Teams API.

`test_send_without_adapter`: When no Bot Framework adapter is configured (startup failed), send operations fail gracefully without crashing the agent loop.

## Error Recovery

`TestTeamsAdapterErrorRecovery` is notable for its defensive coverage:

- `test_send_exception_caught`: Network errors during send are caught and logged, not propagated. An unhandled send exception would crash the current agent session.
- `test_process_activity_exception_caught`: Exceptions during activity processing are isolated to that activity; the webhook handler continues processing subsequent activities.
- `test_webhook_handler_invalid_json`: Malformed JSON in the webhook body returns an HTTP error without crashing the handler.

## Bus Integration

`TestTeamsAdapterBusIntegration` verifies the adapter's pub/sub wiring: outbound messages subscribed from the bus, inbound messages published to the bus, and subscription cleanup on stop.

## Lifecycle

`TestTeamsAdapterLifecycle` covers start/stop semantics including a crucial idempotency test: `test_double_stop_is_safe` confirms that calling `stop()` twice does not raise. This prevents errors in cleanup code that may call stop without checking current state.

## Tenant Filtering Details

`TestTeamsAdapterTenantFilter` adds three tenant filter scenarios: explicit tenant ID match, tenant passed as an object (not just a string), and no-filter mode where all tenants are allowed. The object-vs-string distinction is a Bot Framework quirk where `tenant.id` must be extracted from a tenant object.

## Known Gaps

No tests for the Teams OAuth 2.0 token refresh flow or rate limiting, which are common failure points in production Teams bots.

```python
# Tenant filter pattern
# Drops activities from tenants not in the allowed list
if self.allowed_tenants and activity.channel_data.get("tenant", {}).get("id") not in self.allowed_tenants:
    return
```
