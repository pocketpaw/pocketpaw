---
{
  "title": "Extras Install and Channel Dependency Endpoint Tests",
  "summary": "This test module covers the `GET /api/extras/check` and `POST /api/extras/install` dashboard endpoints that manage optional channel adapter dependencies. It validates the allowlist-based security model that prevents arbitrary package installation and tests the `missing_dep` fallback path when a channel adapter fails to start due to an ImportError.",
  "concepts": [
    "extras",
    "optional dependencies",
    "channel adapters",
    "dashboard API",
    "auto_install",
    "dependency check",
    "WhatsApp personal",
    "neonize",
    "allowlist",
    "restart_required",
    "missing_dep",
    "auth bypass",
    "FastAPI TestClient"
  ],
  "categories": [
    "testing",
    "channel management",
    "security",
    "dashboard",
    "test"
  ],
  "source_docs": [
    "e41764a99ffd8748"
  ],
  "backlinks": null,
  "word_count": 538,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

PocketPaw supports optional channel adapters (Discord, WhatsApp, Telegram, etc.) that require separate Python packages. Rather than bundling everything, the dashboard exposes two endpoints: `GET /api/extras/check` to query whether a dependency is present, and `POST /api/extras/install` to install it on demand. This test module exercises every code path for both endpoints.

## Why This Test File Exists

Optional deps introduce three risk categories. First, a check for an unknown channel should not error — it should report `installed: True` with an empty package name, because channels without tracked deps (like Signal) always have what they need. Second, the install endpoint must reject unknown extra names with a 400 to prevent an attacker from passing arbitrary package specs to the underlying pip invocation. Third, some adapters (notably WhatsApp via `neonize`) require a native extension that cannot be hot-loaded — after install the server must restart, and the test verifies that the `restart_required` flag propagates to callers.

## Test Structure

### Fixtures and Helpers

All tests share a `test_client` fixture that creates a `starlette.TestClient` with `raise_server_exceptions=False`, ensuring error responses become HTTP codes rather than exceptions in the test process. Three context managers handle isolation:

- `_auth_bypass()` patches `_is_genuine_localhost` to `True`, bypassing the middleware that restricts the dashboard to localhost callers.
- `_dep_installed()` / `_dep_missing()` patch `_is_module_importable` to control whether a dependency appears installed.

```python
def _auth_bypass():
    return patch("pocketpaw.dashboard_auth._is_genuine_localhost", return_value=True)

def _dep_installed():
    return patch("pocketpaw.dashboard_channels._is_module_importable", return_value=True)
```

### TestExtrasCheck

Five tests cover the check endpoint:

1. **Installed dep** — asserts `installed: True` and that `extra`, `package`, and `pip_spec` fields are populated.
2. **Missing dep** — asserts `installed: False`.
3. **Unknown channel** — channels not in `_CHANNEL_DEPS` always return `installed: True, package: ""` so callers never see a 404 for a valid channel.
4. **All known channels** — iterates the entire `_CHANNEL_DEPS` dict and verifies metadata is consistent, preventing silent drift when new channels are added.
5. **WhatsApp special case** — WhatsApp uses `neonize` and `pocketpaw[whatsapp-personal]` rather than a standard channel name, guarding against the adapter's atypical dep tree.

### TestExtrasInstall

Six tests cover the install endpoint:

- **Unknown extra returns 400** — closes the arbitrary-package injection vector.
- **Already installed returns ok immediately** — idempotency guard prevents re-running pip unnecessarily.
- **Successful install** — verifies `auto_install` is called with the correct `(extra, module)` pair, e.g. `("discord", "discli")`.
- **WhatsApp maps to personal extra** — the UI-visible name `whatsapp` must be translated to `whatsapp-personal` before calling `auto_install`.
- **Install failure** — `RuntimeError` from pip is surfaced as `{"error": "..."}` in the 200 response body (not a 500).
- **Restart required** — when `auto_install` returns `{"status": "restart_required"}`, the endpoint re-wraps it as `{"status": "ok", "restart_required": true}`.

### TestToggleMissingDep

The channel toggle endpoint (`POST /api/channels/toggle`) can also trigger the missing-dep path if `_start_channel_adapter` raises `ImportError`. These two tests verify:

- An `ImportError` converts to `{"missing_dep": true, "package": ..., "pip_spec": ...}`.
- A non-`ImportError` exception converts to `{"error": "..."}` without the `missing_dep` key.

This distinction matters because the UI uses `missing_dep` to decide whether to show an install prompt or a generic error message.

## Known Gaps

No `TODO` or `FIXME` markers are present. Test coverage for the `restart_required` path in the toggle endpoint is not present — only the install endpoint covers that flow.