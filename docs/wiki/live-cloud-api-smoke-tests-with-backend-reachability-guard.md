---
{
  "title": "Live Cloud API Smoke Tests with Backend Reachability Guard",
  "summary": "Smoke tests that run real HTTP calls against a locally running `paw-cloud` backend. The entire module skips automatically when the backend is unreachable, making these safe to include in CI without a live server requirement. Tests cover login, profile retrieval, agent listing, room lifecycle, DMs, and unauthenticated rejection.",
  "concepts": [
    "smoke tests",
    "real HTTP",
    "reachability guard",
    "socket check",
    "module-scoped fixture",
    "httpx.Client",
    "OCEAN rooms",
    "auth cookie",
    "unauthenticated rejection",
    "paw-cloud"
  ],
  "categories": [
    "testing",
    "api",
    "auth",
    "e2e",
    "test"
  ],
  "source_docs": [
    "a95dca0985c9e59b"
  ],
  "backlinks": null,
  "word_count": 490,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Purpose and Design Philosophy

This is the only test file in the suite that makes real, unpatched HTTP calls. Its purpose is to catch integration regressions between the test-time assumptions (mock responses) and the actual backend behavior — a class of bug that mocks cannot detect.

The file is designed to be zero-friction: run the backend locally, run pytest, get results. When the backend is not running, all tests are skipped without failure.

## Reachability Guard

```python
def _is_backend_up(host: str = "localhost", port: int = 3000) -> bool:
    try:
        with socket.create_connection((host, port), timeout=2):
            return True
    except OSError:
        return False

BACKEND_UP = _is_backend_up()
skip_if_no_backend = pytest.mark.skipif(not BACKEND_UP, reason="paw-cloud not running at localhost:3000")
```

The `socket.create_connection` check with a 2-second timeout runs at import time. If the backend is not listening, `BACKEND_UP` is `False` and every test is decorated with `@skip_if_no_backend`. This prevents CI failures in environments without a live backend while still running the tests in developer environments that have one.

## Shared Session Fixture

The `auth_client` fixture has `scope="module"`, meaning login happens once per module run rather than once per test. This matters because:

1. The backend may rate-limit repeated logins from the same IP.
2. Module-level scope reuses the cookie session across all tests, simulating a real browser session.
3. The fixture uses the synchronous `httpx.Client` (not async) to work with `TestClient`-style test patterns.

## Test Coverage

**`test_login_returns_ok`** — re-invokes login to verify the response body structure. The `auth_client` fixture already logged in; this test re-logs in to verify the 200/201 response and confirms the body is a dict with user data.

**`test_get_me_returns_user_profile`** — `GET /auth/me` with the session cookie returns profile data. This verifies that the session cookie set during login is correctly sent on subsequent requests.

**`test_agents_list_populated`** — `GET /agents` must return a non-empty list. This catches silent registration failures where the agent definitions exist in code but failed to seed into the backend database.

**`test_ocean_room_lifecycle`** — creates a room, adds a message, verifies persistence, then deletes the room. This is the minimal OCEAN room contract: create → write → read → delete.

**`test_get_dms_list`** — `GET /rooms/dms` returns a list (may be empty for a fresh account). This verifies the DM endpoint exists and responds without error, even with no DMs created.

**`test_unauthorized_access_rejected`** — makes a request *without* the auth cookie and verifies 401 or 403. This is the minimum security gate test: unauthenticated requests must never succeed.

## Configuration

`BASE_URL`, `SUPERUSER_EMAIL`, and `SUPERUSER_PASSWORD` are module-level constants. In a real CI environment these would be injected via environment variables; the current hardcoded values assume a seeded local dev database.

## Known Gaps

Credentials are hardcoded (`SUPERUSER_EMAIL = "daw@aahnik.dev"`). This is a development convenience but would fail in any environment without that specific seed user. A production-ready version would read from environment variables.

The `test_login_returns_ok` test re-logs in with the already-authenticated `auth_client`, which means it makes two login calls per test run. This is redundant but harmless.