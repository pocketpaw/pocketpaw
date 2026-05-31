"""Pytest configuration."""

import asyncio
import os
import sys
from unittest.mock import patch

import pytest

from pocketpaw.security.audit import AuditLogger

# Tests run with loopback / RFC1918 URLs in many places (`http://localhost:*`
# ollama defaults, mock HTTP servers, etc). In production that's the exact
# SSRF shape blocked by security.url_validators.validate_external_url — here
# we relax the check so Settings() instantiates cleanly. Tests that need the
# strict behaviour monkeypatch POCKETPAW_ALLOW_INTERNAL_URLS=false themselves.
os.environ.setdefault("POCKETPAW_ALLOW_INTERNAL_URLS", "true")


@pytest.fixture(scope="session", autouse=True)
def _setup_asyncio_child_watcher():
    """Attach a child watcher so subprocess-based tests don't crash.

    On Python < 3.12 the default child watcher requires attachment to
    the running event loop.  On 3.12+ child watchers were removed, so
    this is a no-op.
    """
    if sys.version_info < (3, 12) and hasattr(asyncio, "ThreadedChildWatcher"):
        watcher = asyncio.ThreadedChildWatcher()
        asyncio.set_child_watcher(watcher)
    yield


@pytest.fixture(autouse=True)
def _enable_test_full_access(request, monkeypatch):
    """Flip the require_scope testing-bypass on for all tests by default.

    Router-only tests (which mount FastAPI routers without the dashboard
    middleware) can't set request.state.full_access on their own — this
    fixture lets them exercise route logic without every fixture having
    to install middleware. Tests that explicitly verify fail-closed
    scope behaviour use the ``enforce_scope`` marker to opt out.
    """
    if "enforce_scope" in request.keywords:
        return
    monkeypatch.setattr("pocketpaw.api.deps._TESTING_FULL_ACCESS", True)


@pytest.fixture(autouse=True)
def _isolate_audit_log(tmp_path):
    """Prevent tests from writing to the real ~/.pocketpaw/audit.jsonl.

    Creates a temp audit logger per test and patches the singleton so
    ToolRegistry.execute() and any other callers write to a throwaway file.
    """
    temp_logger = AuditLogger(log_path=tmp_path / "audit.jsonl")
    with (
        patch("pocketpaw.security.audit._audit_logger", temp_logger),
        patch("pocketpaw.security.audit.get_audit_logger", return_value=temp_logger),
        patch("pocketpaw.tools.registry.get_audit_logger", return_value=temp_logger),
    ):
        yield temp_logger
