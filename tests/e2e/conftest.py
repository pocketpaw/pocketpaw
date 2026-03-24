# E2E Test Configuration for Playwright
# Created: 2026-02-05
#
# Provides fixtures for E2E tests:
# - Dashboard server startup/shutdown
# - Browser configuration
# - Test isolation
#
# IMPORTANT: Before running E2E tests, install Playwright browsers (one-time setup):
#   Linux/Mac: uv run playwright install
#   Windows:   .venv\Scripts\python -m playwright install
#
# Run with: pytest tests/e2e/ -v
# Run headed (see browser): pytest tests/e2e/ -v --headed
#
# If you see "fixture 'page' not found", it means Playwright browsers are not installed.

import os
import socket
import time
from contextlib import closing
from multiprocessing import Process

import pytest


def find_free_port() -> int:
    """Find a free port on localhost."""
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("", 0))
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        return s.getsockname()[1]


def run_dashboard(port: int):
    """Run the dashboard server in a subprocess."""
    import uvicorn

    from pocketpaw.dashboard import app

    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


def wait_for_server(port: int, timeout: float = 10.0) -> bool:
    """Wait for the server to be ready."""
    start = time.time()
    while time.time() - start < timeout:
        try:
            with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
                s.settimeout(1)
                s.connect(("127.0.0.1", port))
                return True
        except (OSError, ConnectionRefusedError):
            time.sleep(0.1)
    return False


@pytest.fixture(scope="session")
def dashboard_port() -> int:
    """Get a free port for the dashboard."""
    return find_free_port()


@pytest.fixture(scope="session")
def dashboard_server(dashboard_port: int):
    """Start the dashboard server for the test session.

    Yields the base URL for the dashboard.
    """
    # Set test environment
    os.environ["POCKETPAW_TEST_MODE"] = "1"

    # Start server in subprocess
    process = Process(target=run_dashboard, args=(dashboard_port,))
    process.start()

    # Wait for server to be ready
    if not wait_for_server(dashboard_port):
        process.terminate()
        process.join(timeout=5)
        pytest.fail(f"Dashboard server failed to start on port {dashboard_port}")

    yield f"http://127.0.0.1:{dashboard_port}"

    # Cleanup
    process.terminate()
    process.join(timeout=5)
    if process.is_alive():
        process.kill()


@pytest.fixture(scope="session")
def browser_context_args(browser_context_args):
    """Configure browser context for tests."""
    return {
        **browser_context_args,
        "viewport": {"width": 1280, "height": 720},
        "ignore_https_errors": True,
    }


@pytest.fixture
def dashboard_url(dashboard_server: str) -> str:
    """Alias for dashboard_server URL."""
    return dashboard_server
