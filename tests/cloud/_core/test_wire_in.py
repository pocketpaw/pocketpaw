"""Smoke test that `mount_cloud` registers the extracted handler and
the timing middleware. This is the only Phase 0 test that touches the
real cloud app, so it doubles as a regression guard for the wire-in.
"""

from __future__ import annotations

from fastapi import FastAPI
from pocketpaw_ee.cloud import mount_cloud
from pocketpaw_ee.cloud._core.errors import CloudError
from pocketpaw_ee.cloud._core.http import cloud_error_handler
from pocketpaw_ee.cloud._core.timing import TimingMiddleware


def test_mount_cloud_registers_cloud_error_handler() -> None:
    app = FastAPI()
    mount_cloud(app)
    assert app.exception_handlers.get(CloudError) is cloud_error_handler


def test_mount_cloud_installs_timing_middleware() -> None:
    app = FastAPI()
    mount_cloud(app)
    # Starlette wraps the user_middleware list as `Middleware` records;
    # asserting any of them is `TimingMiddleware` covers the install.
    assert any(m.cls is TimingMiddleware for m in app.user_middleware)
