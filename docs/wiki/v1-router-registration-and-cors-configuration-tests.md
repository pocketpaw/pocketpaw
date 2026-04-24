---
{
  "title": "V1 Router Registration and CORS Configuration Tests",
  "summary": "Validates that all expected domain routers are registered in PocketPaw's v1 API surface and that CORS origins include the Tauri desktop app origin. These tests act as a registry contract — any new router added to the system must be explicitly listed, preventing silent omissions that would cause 404s from newly added features.",
  "concepts": [
    "CORS configuration",
    "router registration",
    "_V1_ROUTERS",
    "mount_v1_routers",
    "Tauri origin",
    "FastAPI router",
    "API surface",
    "CORS origins",
    "route mounting",
    "API gateway"
  ],
  "categories": [
    "testing",
    "API configuration",
    "security",
    "desktop integration",
    "test"
  ],
  "source_docs": [
    "544e43ccf65e9dd6"
  ],
  "backlinks": null,
  "word_count": 418,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`tests/test_api_cors.py` serves as a structural integrity check for PocketPaw's v1 API layer. Despite its name suggesting a focus on CORS, the file splits its concern equally between router registration completeness and CORS origin configuration.

## V1 Router Registration

`TestV1RouterRegistration` imports the `_V1_ROUTERS` list and `mount_v1_routers` factory from `pocketpaw.api.v1`. This list is the authoritative registry of all domain-specific routers that compose the full API surface.

### test_v1_routers_list_complete

This test explicitly asserts the presence of each expected router module by name:

- `pocketpaw.api.v1.auth`
- `pocketpaw.api.v1.sessions`
- `pocketpaw.api.v1.health`
- `pocketpaw.api.v1.identity`
- `pocketpaw.api.v1.settings`
- `pocketpaw.api.v1.channels`
- `pocketpaw.api.v1.memory`
- `pocketpaw.api.v1.mcp`
- `pocketpaw.api.v1.skills`
- `pocketpaw.api.v1.webhooks`
- `pocketpaw.api.v1.backends`

**Why it matters:** In FastAPI, forgetting to include a router in the app factory is a silent error — the routes simply don't exist. If a developer adds a new module but forgets to add it to `_V1_ROUTERS`, all endpoints in that module return 404 with no warning. This test catches that class of omission immediately.

### test_v1_routers_count

Asserts `len(_V1_ROUTERS) >= 26`. This acts as a floor guard — if a refactor accidentally drops routers (e.g., a merge conflict on the registry list), the count test fails even if the module-name assertions don't cover every entry.

### test_mount_v1_routers_succeeds

Calls `mount_v1_routers(app)` on a blank FastAPI app and then inspects the resulting route paths to confirm the auth, sessions, and health routes are reachable. This is an integration smoke test — it catches import errors or misconfigured prefix settings that would cause the mount to fail silently or partially.

## CORS Configuration

`TestCORSConfig` validates two aspects of CORS configuration:

### test_cors_origins_include_tauri

PocketPaw runs as a Tauri desktop application. Tauri uses a custom `tauri://` or `https://tauri.localhost` origin for its WebView, which browsers treat as a distinct origin from `http://localhost`. Without explicitly allowing the Tauri origin, the browser's CORS preflight fails and the dashboard cannot reach the local API server.

This test asserts that the allowed origins list includes the Tauri origin string, preventing a regression where a developer strips "localhost-only" origins thinking they're all equivalent.

### test_api_cors_allowed_origins_in_settings

Verifies the CORS allowed origins are surfaced through PocketPaw's settings system rather than hardcoded. This supports deployments that need to add custom origins (e.g., enterprise dashboards on different subdomains) through configuration rather than code changes.

## Known Gaps

No TODO or FIXME comments are noted. The test file was last updated 2026-02-20. The router count assertion (`>= 26`) is a lower-bound and would not catch additions that add duplicates rather than new routers. A future improvement could assert unique module names.