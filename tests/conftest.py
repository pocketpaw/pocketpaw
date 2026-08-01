"""Pytest configuration.

Updated: 2026-06-12 (connector-store-unification CS-1) — added
_isolate_connector_state so the registry's write-through state store never
persists test config to the real ~/.pocketpaw/connectors/state.
Updated: 2026-06-12 (CS-2) — the same fixture also redirects the registry's
home-dir definition scan (~/.pocketpaw/connectors/*.yaml) to a temp dir so
YAMLs on a dev machine can't leak into test registries.
"""

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
def _isolate_connector_state(tmp_path, monkeypatch):
    """Prevent tests from persisting connector config to the real ~/.pocketpaw.

    ConnectorRegistry.connect() is write-through to a state store that
    defaults to ~/.pocketpaw/connectors/state (CS-1). Point the default at a
    per-test temp dir so suites that build a registry with the default store
    stay hermetic. Tests that exercise the store directly pass an explicit
    ``base_dir`` instead.
    """
    monkeypatch.setattr(
        "pocketpaw.connectors.state_store._default_state_dir",
        lambda: tmp_path / "connector-state",
    )
    monkeypatch.setattr(
        "pocketpaw.connectors.registry._default_home_connectors_dir",
        lambda: tmp_path / "home-connectors",
    )


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


# ---------------------------------------------------------------------------
# Gated-proposal test seam (feat/growth-g4, security review F2)
# ---------------------------------------------------------------------------


def seed_gated_action(client, payload: dict):
    """Seed a PENDING Action carrying a gated blob, the way a real proposer does.

    ``POST /instinct/actions`` is the GENERIC propose route, open to any MEMBER.
    It now REFUSES reserved gated-blob parameter keys (``_ship_action``,
    ``_growth_send``, ``_admin_action``, …) with
    ``422 instinct.reserved_parameter_key`` — a member could otherwise file an
    innocuous Tray card whose blob dispatches a privileged executor the moment
    someone clicks Approve. Only the in-process helper that owns each kind
    (``ee.cloud.ship.propose``, ``ee.cloud.growth.propose``, …) may mint one;
    those call ``store.propose`` directly and never cross this route.

    Gate tests still need such an Action in the store. This helper reproduces
    the state the real helper leaves behind: POST the payload with the plain
    parameters, then write the gated blob onto the stored row. The write is a
    plain synchronous sqlite UPDATE (not ``store.update_parameters``) so the
    helper works identically inside and outside a running event loop.

    Returns the propose response, so a call site keeps reading
    ``resp.json()["id"]`` / ``resp.status_code`` exactly as before.
    """
    import json as _json
    import sqlite3

    from pocketpaw_ee.instinct import router as _instinct_router
    from pocketpaw_ee.instinct.router import RESERVED_GATED_PARAM_KEYS

    parameters = dict(payload.get("parameters") or {})
    gated = {k: v for k, v in parameters.items() if k in RESERVED_GATED_PARAM_KEYS}
    plain = {k: v for k, v in parameters.items() if k not in gated}

    resp = client.post("/instinct/actions", json={**payload, "parameters": plain})
    if not gated or resp.status_code != 201:
        return resp

    store = _instinct_router._store(payload.get("workspace_id") or "")
    with sqlite3.connect(store._db_path) as db:
        db.execute(
            "UPDATE instinct_actions SET parameters = ? WHERE id = ?",
            (_json.dumps(parameters), resp.json()["id"]),
        )
    return resp


async def aseed_gated_action(client, payload: dict):
    """``seed_gated_action`` for an httpx ``AsyncClient``. Same contract."""
    import json as _json
    import sqlite3

    from pocketpaw_ee.instinct import router as _instinct_router
    from pocketpaw_ee.instinct.router import RESERVED_GATED_PARAM_KEYS

    parameters = dict(payload.get("parameters") or {})
    gated = {k: v for k, v in parameters.items() if k in RESERVED_GATED_PARAM_KEYS}
    plain = {k: v for k, v in parameters.items() if k not in gated}

    resp = await client.post("/instinct/actions", json={**payload, "parameters": plain})
    if not gated or resp.status_code != 201:
        return resp

    store = _instinct_router._store(payload.get("workspace_id") or "")
    with sqlite3.connect(store._db_path) as db:
        db.execute(
            "UPDATE instinct_actions SET parameters = ? WHERE id = ?",
            (_json.dumps(parameters), resp.json()["id"]),
        )
    return resp
