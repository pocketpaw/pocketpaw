---
{
  "title": "E2E Test Configuration: Playwright Browser Setup and Dashboard Server Lifecycle",
  "summary": "This conftest manages the full lifecycle of PocketPaw's end-to-end test environment: it checks for Playwright browser installations and skips the entire e2e suite if they are missing, finds a free OS port, starts the dashboard server in a subprocess, waits for it to become ready, and exposes the base URL to all e2e tests through a session-scoped fixture.",
  "concepts": [
    "Playwright",
    "browser automation",
    "e2e testing",
    "dashboard server",
    "subprocess",
    "dynamic port allocation",
    "session fixtures",
    "server readiness",
    "multiprocessing",
    "autouse"
  ],
  "categories": [
    "testing",
    "end-to-end",
    "Playwright",
    "dashboard",
    "test"
  ],
  "source_docs": [
    "831b3e97938eaf07"
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

End-to-end tests run a real browser (Chromium via Playwright) against a real server instance. The conftest bootstraps everything that test functions need before the first test runs, and tears it down cleanly after the last one finishes.

## Playwright Browser Guard

```python
@pytest.fixture(scope="session", autouse=True)
def require_playwright_browsers():
    if not _playwright_browsers_installed():
        pytest.skip("Playwright Chromium not found...")
```

Playwright browsers are not bundled with the Python package; they must be installed separately with `uv run playwright install`. This guard checks for the Chromium executable before any test runs. Without it, the first test that requests a `page` fixture would fail with a confusing `fixture 'page' not found` error — the `autouse=True` skip at session scope produces a cleaner `SKIPPED` outcome that CI can distinguish from a real failure.

## Dynamic Port Allocation

```python
def find_free_port() -> int:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(('', 0))
        return s.getsockname()[1]
```

Hard-coding a port number creates two failure modes: the port may already be in use on the developer's machine, or two parallel CI workers may claim the same port. The OS-assigned port approach avoids both. The socket is bound, the port is read, and the socket is released — the server binds that port a moment later. There is a small TOCTOU window, but in practice this race is negligible in CI environments.

## Dashboard Server Subprocess

```python
def run_dashboard(port):
    # starts the ASGI server in a subprocess

@pytest.fixture(scope="session")
def dashboard_server(dashboard_port):
    proc = Process(target=run_dashboard, args=(dashboard_port,))
    proc.start()
    wait_for_server(dashboard_port)
    yield f"http://localhost:{dashboard_port}"
    proc.terminate()
    proc.join()
```

The server runs in a separate `multiprocessing.Process` rather than a thread to avoid event loop conflicts between the server's async runtime and pytest-asyncio's event loop. `scope="session"` means the server starts once and is shared across all e2e tests, which is important for performance — starting a full FastAPI app plus Playwright browser for every test would make the suite prohibitively slow.

## Server Readiness Wait

`wait_for_server` polls a TCP socket on the server's port until it accepts a connection or a timeout expires. This prevents tests from starting before the server is actually ready to serve requests — a race condition that manifests as intermittent `ConnectionRefused` errors in CI.

## Browser Context Configuration

The `browser_context_args` fixture configures Playwright's browser context. Common settings include disabling certificate validation for local HTTPS, setting viewport size for consistent screenshot-based assertions, and configuring locale.

## Known Gaps

The `run_dashboard` function starts the server without a database — it depends on whatever state the developer's local environment has. There is no fixture that seeds a fresh database before the e2e session, so tests that depend on specific data (e.g., pre-existing agents or workspaces) are order-dependent. A future hardening pass should add a session-scoped database seed fixture.