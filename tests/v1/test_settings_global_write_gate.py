"""A cloud session cannot rewrite the process-wide settings.

``PUT /api/v1/settings`` calls ``Settings.load()`` / ``.save()`` — the single
on-disk config for the whole PROCESS. There is no ``workspace_id`` anywhere in
the handler, and the update is a blind ``setattr`` loop over whatever keys the
body carries, bounded only by ``hasattr`` and ``_IMMUTABLE_FIELDS`` (which
covers seven security fields and nothing else — provider keys, model routing and
channel credentials are all writable).

So one caller who passes the gate rewrites config for EVERY tenant on the
deployment, while the product presents this as workspace settings.

THIS IS NOT A FIX FOR A BROKEN GATE

``require_scope`` fails closed, and the EE bridge grants ``full_access`` only to
``is_superuser`` — the 2026-06-10 escalation fix, which is holding. A workspace
owner cannot reach this route today. What this removes is the situation where
one narrow superuser check is the only thing between a self-service tenant and
everyone else's provider keys.

DETECTING CLOUD FROM THE OSS PACKAGE

``pocketpaw`` must never import ``pocketpaw_ee`` (an import-linter contract
enforces it), so ``is_multi_tenant_cloud()`` is unavailable here.
``request.state.workspace_id`` is the seam: the EE auth bridge stamps it from
the caller's own JWT and nothing else sets it, so its presence means a cloud
session reached this route.

404 rather than 403, because a surface a deployment has turned off should not
announce itself.

Mutations that must fail these tests: dropping the call from the handler,
treating any present value of the override as truthy, and raising 403 instead
of 404.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from pocketpaw.api.v1.settings import _refuse_global_write_in_cloud


def _request(**state) -> Request:
    request = Request({"type": "http", "method": "PUT", "path": "/", "headers": []})
    for key, value in state.items():
        setattr(request.state, key, value)
    return request


def test_a_cloud_session_is_refused(monkeypatch):
    """The finding: a cloud caller must not rewrite every tenant's config."""
    monkeypatch.delenv("POCKETPAW_ALLOW_GLOBAL_SETTINGS_WRITE", raising=False)
    with pytest.raises(HTTPException) as exc:
        _refuse_global_write_in_cloud(_request(workspace_id="ws-1"))
    assert exc.value.status_code == 404


def test_a_local_install_is_unaffected(monkeypatch):
    """No cloud session means one tenant, and the dashboard still works."""
    monkeypatch.delenv("POCKETPAW_ALLOW_GLOBAL_SETTINGS_WRITE", raising=False)
    _refuse_global_write_in_cloud(_request())  # must not raise


def test_the_override_re_opens_it(monkeypatch):
    monkeypatch.setenv("POCKETPAW_ALLOW_GLOBAL_SETTINGS_WRITE", "1")
    _refuse_global_write_in_cloud(_request(workspace_id="ws-1"))  # must not raise


@pytest.mark.parametrize("value", ["", "0", "false", "no", "off", "maybe"])
def test_only_an_explicit_override_re_opens_it(monkeypatch, value):
    """``...=0`` must not mean yes."""
    monkeypatch.setenv("POCKETPAW_ALLOW_GLOBAL_SETTINGS_WRITE", value)
    with pytest.raises(HTTPException) as exc:
        _refuse_global_write_in_cloud(_request(workspace_id="ws-1"))
    assert exc.value.status_code == 404


def test_the_handler_actually_calls_the_gate():
    """A helper nobody calls is documentation.

    Asserted against the parsed handler rather than by driving the route,
    because the route needs the whole dashboard middleware stack to reach a
    state where request.state.workspace_id is set.
    """
    import ast
    import inspect

    import pocketpaw.api.v1.settings as module

    tree = ast.parse(inspect.getsource(module))
    handler = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "update_settings"
    )
    called = {
        node.func.id
        for node in ast.walk(handler)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "_refuse_global_write_in_cloud" in called, (
        "update_settings does not call the cloud gate; the process-wide write is open"
    )
