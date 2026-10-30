"""Smoke test that `mount_cloud` registers the extracted handler and the
cloud middleware stack. This is the only Phase 0 test that touches the real
cloud app, so it doubles as a regression guard for the wire-in.
"""

from __future__ import annotations

from fastapi import FastAPI
from pocketpaw_ee.cloud import mount_cloud
from pocketpaw_ee.cloud._core import timing
from pocketpaw_ee.cloud._core.csrf import CSRFMiddleware
from pocketpaw_ee.cloud._core.ee_auth_bridge import EEAuthBridgeMiddleware
from pocketpaw_ee.cloud._core.errors import CloudError
from pocketpaw_ee.cloud._core.http import cloud_error_handler
from pocketpaw_ee.cloud._core.request_log import RequestLogMiddleware


def test_mount_cloud_registers_cloud_error_handler() -> None:
    app = FastAPI()
    mount_cloud(app)
    assert app.exception_handlers.get(CloudError) is cloud_error_handler


def test_mount_cloud_installs_the_cloud_middleware_in_order() -> None:
    app = FastAPI()
    mount_cloud(app)
    # user_middleware is outermost-first. RequestLog is what feeds the
    # /_admin/perf timing buffers now; there is no separate timing layer.
    expected = [CSRFMiddleware, RequestLogMiddleware, EEAuthBridgeMiddleware]
    assert [m.cls for m in app.user_middleware if m.cls in expected] == expected
    assert not hasattr(timing, "TimingMiddleware")
