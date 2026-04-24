---
{
  "title": "Backends API Tests: Listing, Availability Checks, and Safe Install Subprocess Handling",
  "summary": "This test file covers PocketPaw's `/api/v1/backends` router, verifying that the backend registry is exposed correctly, that the `_check_available` helper correctly detects missing imports, and that the async install endpoint handles subprocess timeouts and stderr secret redaction without leaking sensitive data.",
  "concepts": [
    "backend registry",
    "LLM backend",
    "_check_available",
    "verify_import",
    "subprocess install",
    "timeout handling",
    "process kill",
    "stderr redaction",
    "secret sanitisation",
    "FastAPI router",
    "pip install"
  ],
  "categories": [
    "agent backends",
    "testing",
    "security",
    "API",
    "test"
  ],
  "source_docs": [
    "b1d25cbf6cfaf1ff"
  ],
  "backlinks": null,
  "word_count": 557,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

PocketPaw supports multiple AI agent backends (e.g. different LLM providers or local models). The `/backends` API lets the dashboard discover which backends are configured, whether they are available (their dependencies installed), and trigger installation of missing ones. This test file locks down the contract for all three concerns.

## Test App Isolation

The file uses a pair of module-level helper functions rather than fixtures:

```python
def _test_app():
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    return app

def _client():
    return TestClient(_test_app())
```

This pattern avoids `pytest` fixture overhead for simple synchronous tests but means each test creates a fresh `TestClient`. The trade-off is slightly more object creation per test vs. simpler code for tests that do not share state.

## Listing Backends (`GET /backends`)

`TestListBackends` patches `_check_available` at the module level to control which backends appear available:

- **Returns an array**: The response must be a non-empty list, confirming the registry has at least one entry and the route serialises it as JSON array (not a dict or null).
- **Required fields**: Every backend entry must contain `name`, `displayName`, `available`, and `capabilities` (which must itself be a list). These fields are the contract the frontend depends on for rendering the backends panel.
- **Unavailable backend**: When `_check_available` returns `False`, at least one backend must appear with `available: false`. This path exercises the UI badge that tells users they need to install a dependency.

## Availability Check Helper (`_check_available`)

`TestCheckAvailable` exercises the helper directly without going through the HTTP layer:

- **No install hint**: A backend with an empty `install_hint` dict is assumed to be always available (i.e., it has no optional dependency). This prevents backends that ship with PocketPaw core from being incorrectly marked as missing.
- **Missing import**: A backend whose `install_hint` specifies `verify_import: "nonexistent_module_xyz_123"` returns `False`. The helper tries to import the module at runtime to detect whether it is installed, which is more reliable than checking package metadata (a package can be installed but its import name can differ from the package name).

## Install Endpoint (`POST /backends/install`)

`TestInstallBackend` covers the two critical failure modes of the async subprocess install:

### Timeout with process kill

```python
proc.communicate = AsyncMock(side_effect=[TimeoutError(), (b"", b"")])
```

The install endpoint spawns `pip install` as an async subprocess. If `communicate()` times out, the test asserts that:
1. `proc.kill()` is called exactly once — the hung process must be reaped to avoid zombie processes.
2. `communicate()` is awaited a second time after the kill to drain the pipe buffers and prevent the child from blocking on a full stdout/stderr pipe.
3. The response body contains a user-readable error message naming the package, not a raw Python traceback.

### Stderr secret redaction

If pip exits with a non-zero return code, its stderr may contain API keys or tokens that were passed as environment variables or in the pip spec. The test injects a known secret string (`pp_abcdefghijklmnopqrstuvwx`) into the fake stderr and asserts:

- `"[REDACTED]"` appears in the error response.
- The raw secret does not appear anywhere in the response.

This prevents credentials from leaking into the dashboard UI or browser developer tools.

## Known Gaps

No `TODO` or `FIXME` markers are present. The tests do not cover: successful install (non-zero exit but no timeout), multiple simultaneous install requests for the same backend, or install of a backend that is already installed.