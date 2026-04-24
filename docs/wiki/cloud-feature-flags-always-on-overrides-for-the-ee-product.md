---
{
  "title": "Cloud Feature Flags - Always-On Overrides for the EE Product",
  "summary": "The `ee/cloud/features.py` module force-enables features that are optional in the OSS build but required for the cloud EE product to function correctly. Currently it overrides `chat_titles_enabled` to always return `True`, ensuring sidebar titles and realtime UI always work in cloud mode.",
  "concepts": [
    "feature flags",
    "cloud overrides",
    "chat titles",
    "OSS vs EE",
    "feature gating",
    "deployment modes",
    "shadow module"
  ],
  "categories": [
    "cloud EE",
    "feature flags",
    "configuration",
    "architecture"
  ],
  "source_docs": [
    "ca0c56c54675d266"
  ],
  "backlinks": null,
  "word_count": 345,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

PocketPaw has two deployment modes: an open-source (OSS) build where some features are opt-in or disabled by default, and an enterprise cloud build where those same features must always be active because the product depends on them.

`features.py` is the cloud EE layer's mechanism for hardcoding these overrides. Instead of checking environment variables or database flags, functions in this file simply return constants:

```python
def chat_titles_enabled() -> bool:
    return True
```

## Why Not Environment Variables?

Feature flags toggled by environment variables can be accidentally disabled by an ops change, leading to a degraded product. For features that are *structurally required* by the cloud product - not 'nice to have' but 'the sidebar does not work without this' - hardcoding `True` in the EE module removes that failure mode entirely.

The OSS build has its own implementation of `chat_titles_enabled` that may return `False` depending on configuration. When the `ee.cloud` package is installed, it shadows the OSS implementation, ensuring cloud deployments always get the correct behaviour without a per-deployment environment variable.

## Module Structure

The module imports only `__future__` for `annotations`, making it a zero-dependency leaf. Feature flag resolution should be fast and side-effect-free. Introducing database calls or network round-trips into a feature flag check would degrade every code path that calls the flag function.

## Current Overrides

| Function | OSS Default | Cloud Override | Reason |
|----------|-------------|----------------|--------|
| `chat_titles_enabled()` | configurable | `True` | Chat titles drive the sidebar group list; without them the UI is broken |

The module comment notes that outbound webhooks are another cloud-required feature, though that override may live elsewhere.

## Extension Pattern

Adding a new cloud-required feature follows the same pattern: define a function returning `True`. The OSS code calls the function through its abstraction layer; the cloud EE module provides the always-on override.

## Known Gaps

- The module contains only one override. As more OSS-optional features become cloud-required, this file should grow rather than spreading overrides across multiple modules.
- There is no test asserting that `chat_titles_enabled()` returns `True` in the cloud context.