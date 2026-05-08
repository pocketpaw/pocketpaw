# tests/scenarios/conftest.py — Phase 4 scenario-test fixtures.
# Created: 2026-05-08
#
# Scenarios talk to a LIVE pocketpaw runtime. Set POCKETPAW_API_URL
# (default http://localhost:8888) and have the runtime up before
# running these tests:
#
#   uv run pocketpaw serve   # in another terminal
#   uv run pytest tests/scenarios/ -v
#
# Each scenario file is independent and self-cleans the resources
# it creates. The conftest provides:
#
#   - api_url: the configured POCKETPAW_API_URL
#   - skip_if_offline: auto-skip when the runtime is not reachable
#   - admin_credentials: the seeded admin email + password
#
# Admin login + bearer JWT helpers live in each test file rather than
# here so the seed-credential coupling stays explicit per scenario.

from __future__ import annotations

import os

import httpx
import pytest


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


POCKETPAW_API_URL = os.environ.get("POCKETPAW_API_URL", "http://localhost:8888")
SCENARIO_ADMIN_EMAIL = os.environ.get("SCENARIO_ADMIN_EMAIL", "admin@pocketpaw.ai")
SCENARIO_ADMIN_PASSWORD = os.environ.get("SCENARIO_ADMIN_PASSWORD", "admin123")


# ---------------------------------------------------------------------------
# Reachability gate
# ---------------------------------------------------------------------------


def _api_reachable() -> bool:
    """Return True if the configured pocketpaw API responds to /health."""
    try:
        resp = httpx.get(f"{POCKETPAW_API_URL}/api/v1/health", timeout=2.0)
        # Any 2xx/3xx counts as reachable. Health may return "degraded" in
        # local dev (no encrypted secrets, version warning) — that's still
        # a live API.
        return resp.status_code < 500
    except (httpx.ConnectError, httpx.TimeoutException, httpx.RequestError):
        return False


@pytest.fixture(scope="session", autouse=True)
def _skip_if_offline() -> None:
    """Auto-skip every scenario test when the runtime is not running."""
    if not _api_reachable():
        pytest.skip(
            f"pocketpaw API not reachable at {POCKETPAW_API_URL}. "
            "Boot it with `uv run pocketpaw serve` and retry.",
            allow_module_level=True,
        )


@pytest.fixture(scope="session")
def api_url() -> str:
    """Base URL of the live pocketpaw runtime."""
    return POCKETPAW_API_URL


@pytest.fixture(scope="session")
def admin_credentials() -> tuple[str, str]:
    """Seeded admin credentials. Override via env if your seed differs."""
    return SCENARIO_ADMIN_EMAIL, SCENARIO_ADMIN_PASSWORD
