---
{
  "title": "Feature Flag Resolution: OSS Settings with Cloud Override Layer",
  "summary": "`features.py` provides a thin feature flag resolution layer that reads from OSS `Settings` but allows the `ee.cloud.features` module to force-override specific capabilities when running in cloud mode. The `ee.cloud` import is lazy, ensuring that OSS builds without the enterprise package continue to work without modification.",
  "concepts": [
    "feature flags",
    "cloud override",
    "lazy import",
    "OSS vs enterprise",
    "dual distribution",
    "Settings",
    "chat_titles_enabled",
    "ee.cloud",
    "ImportError handling"
  ],
  "categories": [
    "configuration",
    "enterprise edition",
    "feature flags",
    "deployment"
  ],
  "source_docs": [
    "d2a9b1a316c5a193"
  ],
  "backlinks": null,
  "word_count": 423,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`features.py` (`src/pocketpaw/features.py`) solves a specific dual-distribution problem: PocketPaw ships both as an open-source package and as a hosted cloud product. Some features are disabled in OSS but need to be force-enabled in cloud deployments without modifying the OSS code path.

## The _cloud_override Pattern

```python
def _cloud_override(name: str) -> bool | None:
    try:
        from ee.cloud import features as cloud_features
    except ImportError:
        return None
    getter = getattr(cloud_features, name, None)
    if getter is None:
        return None
    try:
        return bool(getter())
    except Exception:
        return None
```

This function attempts to import `ee.cloud.features` on every call. If the package is not installed (OSS environment), the `ImportError` is caught and `None` is returned — meaning "no override, use OSS default." If the package is installed but doesn't define the named getter (e.g., a new feature not yet in the cloud package), `getattr` returns `None` and falls through to the OSS default.

The inner `try/except Exception` around `getter()` ensures that a buggy cloud feature function (raising at runtime) doesn't crash the process — it also returns `None` and falls back to OSS behavior.

Lazy importing inside the function body rather than at module level is deliberate. A module-level import would fail at startup in OSS environments, preventing the `features` module from loading at all. The lazy pattern defers the import to call time, where the failure can be caught.

## chat_titles_enabled

```python
def chat_titles_enabled(settings: Settings) -> bool:
    override = _cloud_override("chat_titles_enabled")
    if override is not None:
        return override
    return settings.chat_title_generation_enabled
```

This is the only exposed feature flag currently. The resolution order is:
1. Cloud override (if present and callable) — highest priority
2. OSS `Settings.chat_title_generation_enabled` — user-configured fallback

The function accepts `Settings` explicitly rather than calling `get_settings()` internally. This makes the function testable without monkeypatching the settings loader — tests can pass a `Settings(chat_title_generation_enabled=True)` directly.

## Why Not Environment Variables?

The cloud override approach is preferred over environment variables for feature flags because it allows the cloud deployment layer to implement arbitrarily complex logic (e.g., per-account feature rollout, A/B testing) without PocketPaw's OSS code needing to know the details. The `getter()` callsite is opaque from the OSS perspective.

## Known Gaps

- The `_cloud_override` function imports `ee.cloud.features` on every feature flag call. For features checked frequently (e.g., per-request), this is a repeated module import. Python's module cache makes this cheap, but it is not zero-cost.
- Only one feature (`chat_titles_enabled`) is currently handled. As more features need cloud overrides, each needs a dedicated wrapper function — there is no generic `feature_enabled(name, settings_attr)` helper yet, which would reduce boilerplate.