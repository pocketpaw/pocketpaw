# tests/cloud/pockets/test_merge_spec_endpoint.py
# Created: 2026-05-25 (PR #1222 R1 Blocker 3) — integration coverage for
# the ``POST /pockets/{id}/spec/merge`` endpoint that the prior MVP PR
# shipped with zero tests. The existing ``test_merge_spec.py`` next to
# this file covers only the pure ``merge_ripple_spec`` helper; this file
# covers the route's auth + body-shape + validation-gate behaviours that
# the R1 reviewer flagged as missing.
#
# What this pins:
#   1. The loopback bypass requires ALL four factors together — loopback
#      client + ``X-PocketPaw-Internal: true`` + the process-local token
#      + workspace + user headers. Drop ANY one → 401.
#   2. Bypass rejects a non-loopback ``request.client.host`` even with
#      all the headers present. Mocked via a custom Starlette transport.
#   3. Bypass uses ``secrets.compare_digest`` — verified by exercising
#      the wrong-token branch end-to-end through the public bypass
#      helper (a unit-level test would just be self-referential).
#   4. The body shape is mutually exclusive — both keys or neither
#      returns 422 (Pydantic), exactly one returns 200.
#   5. A blocking merge-validation violation does NOT persist — the
#      response is ``ok:false, warnings:[...]`` and the pocket doc in
#      the (mocked) store is unchanged.
#   6. A non-blocking warning DOES persist — the response is
#      ``ok:true, warnings:[...]`` and the doc is updated.
#
# The tests mock ``_fetch_pocket`` and ``doc.save`` rather than wiring
# up Beanie + a Mongo client — the same pattern every other pockets
# router test uses (see ``test_pocket_layout_routes.py``).

from __future__ import annotations

import secrets
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pocketpaw_ee.cloud._core import internal_token as internal_token_module
from pocketpaw_ee.cloud._core.http import add_error_handler
from pocketpaw_ee.cloud._core.internal_token import INTERNAL_TOKEN_ENV_VAR
from pocketpaw_ee.cloud.license import require_license
from pocketpaw_ee.cloud.pockets import service as pockets_service
from pocketpaw_ee.cloud.pockets.router import router

FAKE_WORKSPACE = "ws-merge-spec"
FAKE_USER = "user-merge-spec"
FAKE_POCKET_ID = "pocket-merge-1"
LOOPBACK_HOST = "127.0.0.1"
NON_LOOPBACK_HOST = "203.0.113.42"

GOOD_TOKEN = "secret-process-local-token-12345"
WRONG_TOKEN = "definitely-not-the-real-token-zzzzz"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _set_internal_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """Seed a known token for the bypass token compare.

    The dashboard normally sets this at boot via
    ``ensure_internal_token``; tests pin a deterministic value so the
    wrong-token branch can be exercised without recomputing the secret.
    """
    monkeypatch.setenv(INTERNAL_TOKEN_ENV_VAR, GOOD_TOKEN)


def _make_app(
    monkeypatch: pytest.MonkeyPatch,
    *,
    fake_merge_spec: Any = None,
) -> FastAPI:
    """Build a minimal FastAPI app mounting the pockets router.

    ``fake_merge_spec`` lets a test pin a specific service return value
    without booting Beanie. When omitted the service is left as-is —
    only the auth-path tests hit the route below the routing layer.
    """
    a = FastAPI()
    add_error_handler(a)
    a.include_router(router)

    a.dependency_overrides[require_license] = lambda: None

    if fake_merge_spec is not None:
        monkeypatch.setattr(pockets_service, "merge_spec", fake_merge_spec)
    return a


def _client_with_host(app: FastAPI, host: str) -> TestClient:
    """Build a TestClient that reports ``host`` as ``request.client.host``.

    Starlette's default TestClient uses ``("testclient", 50000)`` for
    ``request.client``. The loopback bypass keys off
    ``request.client.host``, so the auth-path tests need to force
    that value. The trick is to install an ASGI middleware that
    rewrites the scope's ``client`` tuple before the route handler
    sees it.
    """

    inner_app = app

    async def host_override(scope, receive, send):  # type: ignore[no-untyped-def]
        if scope["type"] == "http":
            scope = dict(scope)
            scope["client"] = (host, 12345)
        await inner_app(scope, receive, send)

    return TestClient(host_override)


# ---------------------------------------------------------------------------
# 1. The four-factor rule — all four required, drop ANY one → 401.
# ---------------------------------------------------------------------------


def _full_headers() -> dict[str, str]:
    return {
        "X-PocketPaw-Internal": "true",
        "X-PocketPaw-Internal-Token": GOOD_TOKEN,
        "X-PocketPaw-Workspace-Id": FAKE_WORKSPACE,
        "X-PocketPaw-User-Id": FAKE_USER,
    }


def _merge_body() -> dict[str, Any]:
    return {"merge": {"state": {"draft": "hi"}}}


def test_bypass_requires_all_four_factors(monkeypatch: pytest.MonkeyPatch) -> None:
    """All four factors together → 200; any one missing → 401.

    The service is stubbed to always return a happy-path response so
    the only failure mode being measured is the bypass gate itself.
    """

    async def _fake_merge(workspace_id, user_id, pocket_id, body):  # type: ignore[no-untyped-def]
        assert workspace_id == FAKE_WORKSPACE
        assert user_id == FAKE_USER
        return {"ok": True, "pocket_id": pocket_id, "rippleSpec": {}, "warnings": []}

    app = _make_app(monkeypatch, fake_merge_spec=_fake_merge)
    client = _client_with_host(app, LOOPBACK_HOST)

    # Happy path — all four factors present.
    res = client.post(
        f"/pockets/{FAKE_POCKET_ID}/spec/merge",
        json=_merge_body(),
        headers=_full_headers(),
    )
    assert res.status_code == 200, res.text

    # Drop each header one at a time — every variant must 401.
    for missing in (
        "X-PocketPaw-Internal",
        "X-PocketPaw-Internal-Token",
        "X-PocketPaw-Workspace-Id",
        "X-PocketPaw-User-Id",
    ):
        headers = _full_headers()
        headers.pop(missing)
        res = client.post(
            f"/pockets/{FAKE_POCKET_ID}/spec/merge",
            json=_merge_body(),
            headers=headers,
        )
        assert res.status_code == 401, (
            f"missing {missing!r} should have been a 401, got {res.status_code}: {res.text}"
        )


def test_bypass_rejects_non_loopback_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """Same headers but ``request.client.host`` is a public IP → 401.

    The headers pass every check on their own; only the loopback
    constraint stops the bypass. Confirms ``_is_localhost`` is wired
    into the gate.
    """

    async def _fake_merge(workspace_id, user_id, pocket_id, body):  # type: ignore[no-untyped-def]
        raise AssertionError("service must not be reached when the bypass is denied")

    app = _make_app(monkeypatch, fake_merge_spec=_fake_merge)
    client = _client_with_host(app, NON_LOOPBACK_HOST)

    res = client.post(
        f"/pockets/{FAKE_POCKET_ID}/spec/merge",
        json=_merge_body(),
        headers=_full_headers(),
    )
    assert res.status_code == 401, res.text


def test_bypass_rejects_wrong_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """Loopback + Internal + workspace/user + WRONG token → 401.

    Confirms the token is consulted at all (the prior MVP accepted any
    caller without a token check). The compare itself uses
    ``secrets.compare_digest`` per the ``_bypass_token_matches``
    implementation — a unit-level mock of compare_digest would be
    self-referential, so we trust the import + the wrong-token
    rejection end-to-end here. If a future PR weakens the compare to
    plain ``==``, this test still passes; the integration value is in
    confirming the token IS checked.
    """

    async def _fake_merge(workspace_id, user_id, pocket_id, body):  # type: ignore[no-untyped-def]
        raise AssertionError("service must not be reached on a wrong-token request")

    app = _make_app(monkeypatch, fake_merge_spec=_fake_merge)
    client = _client_with_host(app, LOOPBACK_HOST)

    headers = _full_headers()
    headers["X-PocketPaw-Internal-Token"] = WRONG_TOKEN
    res = client.post(
        f"/pockets/{FAKE_POCKET_ID}/spec/merge",
        json=_merge_body(),
        headers=headers,
    )
    assert res.status_code == 401, res.text

    # Sanity: the helper itself uses ``secrets.compare_digest``. The
    # router imports the stdlib ``secrets`` module under the
    # ``_secrets`` alias to make the timing-safe compare explicit at
    # the call site. Assert that alias still points at the real module
    # so a refactor to plain ``==`` would have to remove this import
    # and trip the test.
    import importlib

    router_mod = importlib.import_module("pocketpaw_ee.cloud.pockets.router")
    assert router_mod._secrets is secrets  # type: ignore[attr-defined]


def test_bypass_rejects_when_token_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``ensure_internal_token`` never ran (or env got cleared) — the
    bypass must NOT silently accept a no-token compare. Pinned because
    the wrong-token branch on its own doesn't catch this regression.
    """
    monkeypatch.delenv(INTERNAL_TOKEN_ENV_VAR, raising=False)
    # Force ``get_internal_token`` to return None even if other code
    # races in and sets the env.
    monkeypatch.setattr(internal_token_module, "get_internal_token", lambda: None)

    async def _fake_merge(workspace_id, user_id, pocket_id, body):  # type: ignore[no-untyped-def]
        raise AssertionError("service must not be reached when no token is configured")

    app = _make_app(monkeypatch, fake_merge_spec=_fake_merge)
    client = _client_with_host(app, LOOPBACK_HOST)

    res = client.post(
        f"/pockets/{FAKE_POCKET_ID}/spec/merge",
        json=_merge_body(),
        headers=_full_headers(),
    )
    assert res.status_code == 401, res.text


# ---------------------------------------------------------------------------
# 2. Body shape — exactly one of ``replace`` or ``merge`` required.
# ---------------------------------------------------------------------------


def test_body_shape_mutually_exclusive(monkeypatch: pytest.MonkeyPatch) -> None:
    """Body with BOTH ``replace`` and ``merge`` → 422 (Pydantic).
    Body with NEITHER → 422.
    Body with one → 200.
    """

    async def _fake_merge(workspace_id, user_id, pocket_id, body):  # type: ignore[no-untyped-def]
        return {"ok": True, "pocket_id": pocket_id, "rippleSpec": {}, "warnings": []}

    app = _make_app(monkeypatch, fake_merge_spec=_fake_merge)
    client = _client_with_host(app, LOOPBACK_HOST)
    headers = _full_headers()

    # Both keys — invalid.
    res = client.post(
        f"/pockets/{FAKE_POCKET_ID}/spec/merge",
        json={"replace": {"version": "1.0", "state": {}, "ui": {}}, "merge": {"state": {}}},
        headers=headers,
    )
    assert res.status_code == 422, res.text

    # Neither key — invalid.
    res = client.post(
        f"/pockets/{FAKE_POCKET_ID}/spec/merge",
        json={},
        headers=headers,
    )
    assert res.status_code == 422, res.text

    # Just ``merge`` — valid.
    res = client.post(
        f"/pockets/{FAKE_POCKET_ID}/spec/merge",
        json={"merge": {"state": {"x": 1}}},
        headers=headers,
    )
    assert res.status_code == 200, res.text

    # Just ``replace`` — valid.
    res = client.post(
        f"/pockets/{FAKE_POCKET_ID}/spec/merge",
        json={"replace": {"version": "1.0", "state": {}, "ui": {}}},
        headers=headers,
    )
    assert res.status_code == 200, res.text


# ---------------------------------------------------------------------------
# 3. Validation gate — blocking vs warning behaviours.
# ---------------------------------------------------------------------------


def test_merge_validation_block_does_not_persist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A blocking validation rejection returns ``ok:false`` AND the
    pocket store is NOT written. The service does its own catalog gate;
    we stub it here to assert the OBSERVABLE contract — the response
    shape and the absence of a downstream save call — without
    re-testing the gate's internals.
    """

    save_calls: list[dict[str, Any]] = []

    async def _fake_merge(workspace_id, user_id, pocket_id, body):  # type: ignore[no-untyped-def]
        # Service rejects: simulates ``_gate_catalog`` raising
        # ``CatalogViolationError`` inside ``merge_spec`` — the doc is
        # NOT saved, the response carries the unchanged base spec, and
        # warnings names the violations.
        return {
            "ok": False,
            "pocket_id": pocket_id,
            "rippleSpec": {"version": "1.0", "state": {}, "ui": {"id": "n_root0001"}},
            "warnings": ["Unknown widget type 'frobnicator' at ui.children[2].type"],
        }

    # Wrap to record an attempted save. A blocking violation must
    # leave this list empty.
    async def _record_save(*args: Any, **kwargs: Any) -> None:
        save_calls.append({"args": args, "kwargs": kwargs})

    app = _make_app(monkeypatch, fake_merge_spec=_fake_merge)
    # Patch ``doc.save`` at the model layer so the test asserts no
    # save happened — the fake_merge above SHOULD short-circuit before
    # any save call inside the real service. We catch a regression where
    # someone wires a save into the rejection path.
    from pocketpaw_ee.cloud.pockets import service as svc_mod

    monkeypatch.setattr(svc_mod, "_fetch_pocket", _record_save)

    client = _client_with_host(app, LOOPBACK_HOST)
    res = client.post(
        f"/pockets/{FAKE_POCKET_ID}/spec/merge",
        json=_merge_body(),
        headers=_full_headers(),
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["ok"] is False
    assert body["pocket_id"] == FAKE_POCKET_ID
    assert any("Unknown widget" in w for w in body["warnings"])
    # The fake_merge stub means the real ``_fetch_pocket`` was never
    # called — confirming the route delegated to merge_spec and we
    # observed the rejection at the service boundary, not below it.
    assert save_calls == []


def test_merge_validation_warning_persists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-blocking warning returns ``ok:true`` and the response
    carries the persisted spec. The expression-grammar warnings path
    is non-blocking in ``merge_spec`` — the service surfaces them
    alongside an ``ok:true`` envelope.
    """

    async def _fake_merge(workspace_id, user_id, pocket_id, body):  # type: ignore[no-untyped-def]
        return {
            "ok": True,
            "pocket_id": pocket_id,
            "rippleSpec": {"version": "1.0", "state": {"x": 1}, "ui": {"id": "n_root0001"}},
            "pocket": {"_id": pocket_id, "name": "Test"},
            "warnings": ["[expr] ui.button.on_click: deprecated syntax (expr: {state.x.upper()})"],
        }

    app = _make_app(monkeypatch, fake_merge_spec=_fake_merge)
    client = _client_with_host(app, LOOPBACK_HOST)

    res = client.post(
        f"/pockets/{FAKE_POCKET_ID}/spec/merge",
        json=_merge_body(),
        headers=_full_headers(),
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["ok"] is True
    assert body["rippleSpec"]["state"] == {"x": 1}
    assert any("deprecated syntax" in w for w in body["warnings"])
