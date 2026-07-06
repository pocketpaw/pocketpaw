# tests/cloud/test_security_proxy.py — the /api/v1/security/* shield proxy (SEC-5).
#
# Created: 2026-07-01 (feat/sec-5-security-proxy) — pins the cloud-side proxy to
# shield, the same-box Go daemon on a UNIX socket. Coverage:
#   * FORWARDING — each route forwards to shield with the right method / path /
#     JSON body / Bearer token; shield's status + JSON pass through; a shield 400
#     (bad PATCH) is forwarded through as a 400.
#   * RBAC — an OWNER passes; a non-OWNER (MEMBER / ADMIN) gets 403 on EVERY route.
#   * NO-SHIELD — socket unset (client None) or unreachable (transport raises) →
#     the typed available:false (GET) / 409 (write), NEVER a 500.
#   * ACTOR INJECTION — resolve injects the OWNER user id as the ``actor`` field
#     and overwrites any client-supplied actor.
#
# The shield hop is faked with an httpx.MockTransport-backed AsyncClient injected
# via ``shield_client_dep`` — a real client so the Bearer header / JSON body are
# exercised end-to-end, but no actual socket is dialed. RBAC runs the REAL guard
# (the user's workspace role drives ``require_action_any_workspace``), mirroring
# tests/cloud/test_belt_console.py::_build_app.

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

pytest.importorskip("pocketpaw_ee")

import httpx  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

# ---------------------------------------------------------------------------
# Fake shield — an httpx.MockTransport that records the last request it saw and
# returns a scripted response.
# ---------------------------------------------------------------------------


class _FakeShield:
    """A recording fake for shield's control API.

    Builds a real ``httpx.AsyncClient`` over a ``MockTransport`` so the router's
    actual request path (headers, JSON body, query params) is exercised. The
    last request is captured for assertions; the response is scripted per-test.
    """

    def __init__(self, *, status: int = 200, json_body: Any = None) -> None:
        self.status = status
        self.json_body = json_body if json_body is not None else {"ok": True}
        self.requests: list[httpx.Request] = []

    def _handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(self.status, json=self.json_body)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.MockTransport(self._handler),
            base_url="http://shield",
            headers={"Authorization": "Bearer sekret-token"},
            timeout=5.0,
        )

    @property
    def last(self) -> httpx.Request:
        assert self.requests, "shield was never called"
        return self.requests[-1]


class _UnreachableShield:
    """A client whose transport raises ConnectError — shield-is-down simulation."""

    def _handler(self, request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.MockTransport(self._handler),
            base_url="http://shield",
            headers={"Authorization": "Bearer sekret-token"},
            timeout=5.0,
        )


# ---------------------------------------------------------------------------
# App builder — real RBAC guard, fake shield client injected per test.
# ---------------------------------------------------------------------------


def _build_app(
    *,
    role: str = "owner",
    workspace_id: str = "w1",
    user_id: str = "u1",
    shield: _FakeShield | _UnreachableShield | None = None,
    no_shield: bool = False,
) -> FastAPI:
    """A TestClient app over the security router with the REAL RBAC guard.

    ``role`` drives ``require_action_any_workspace("security.manage")`` (OWNER):
    an owner passes, a member/admin is 403. License is bypassed. The shield hop
    is faked: ``shield`` injects a recording/unreachable client; ``no_shield``
    forces the socket-unset branch (client is ``None``).
    """
    from pocketpaw_ee.cloud._core.http import add_error_handler
    from pocketpaw_ee.cloud.auth import current_active_user
    from pocketpaw_ee.cloud.license import require_license
    from pocketpaw_ee.cloud.security.router import router, shield_client_dep

    app = FastAPI()
    add_error_handler(app)
    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[require_license] = lambda: None

    user = SimpleNamespace(
        id=user_id,
        is_active=True,
        active_workspace=workspace_id,
        workspaces=[SimpleNamespace(workspace=workspace_id, role=role)],
    )

    async def _fake_user_dep():
        return user

    app.dependency_overrides[current_active_user] = _fake_user_dep

    if no_shield:
        app.dependency_overrides[shield_client_dep] = lambda: None
    elif shield is not None:
        app.dependency_overrides[shield_client_dep] = lambda: shield.client()

    return app


# ---------------------------------------------------------------------------
# Forwarding — reads
# ---------------------------------------------------------------------------


def test_get_decisions_forwards_with_bearer_and_query_params():
    shield = _FakeShield(json_body={"decisions": [{"id": "d1"}]})
    with TestClient(_build_app(shield=shield)) as client:
        res = client.get("/api/v1/security/decisions?status=pending&limit=25")
    assert res.status_code == 200, res.text
    # available:true envelope wraps shield's JSON.
    body = res.json()
    assert body["available"] is True
    assert body["decisions"] == [{"id": "d1"}]
    # Forwarded to shield's /v1/decisions with the query params + Bearer token.
    # shield versions its control API under /v1 (paw-shield internal/api); a
    # live-smoke found the proxy was forwarding to the bare paths, which 404'd
    # against a real shield — so these assert the /v1-prefixed contract.
    req = shield.last
    assert req.method == "GET"
    assert req.url.path == "/v1/decisions"
    assert dict(req.url.params) == {"status": "pending", "limit": "25"}
    assert req.headers["authorization"] == "Bearer sekret-token"


def test_get_stats_forwards_and_wraps_available_true():
    shield = _FakeShield(json_body={"egress_blocked": 3, "decisions_pending": 1})
    with TestClient(_build_app(shield=shield)) as client:
        res = client.get("/api/v1/security/stats")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["available"] is True
    assert body["egress_blocked"] == 3
    assert shield.last.url.path == "/v1/stats"
    assert shield.last.method == "GET"


def test_get_config_forwards():
    shield = _FakeShield(json_body={"mode": "deny", "allow": ["api.github.com"]})
    with TestClient(_build_app(shield=shield)) as client:
        res = client.get("/api/v1/security/config")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["available"] is True
    assert body["mode"] == "deny"
    assert shield.last.url.path == "/v1/config"


# ---------------------------------------------------------------------------
# Forwarding — writes
# ---------------------------------------------------------------------------


def test_patch_config_forwards_json_body():
    shield = _FakeShield(json_body={"mode": "allow"})
    with TestClient(_build_app(shield=shield)) as client:
        res = client.patch("/api/v1/security/config", json={"mode": "allow"})
    assert res.status_code == 200, res.text
    # Write bodies pass through verbatim (NOT wrapped in available:true).
    assert res.json() == {"mode": "allow"}
    req = shield.last
    assert req.method == "PATCH"
    assert req.url.path == "/v1/config"
    import json as _json

    assert _json.loads(req.content) == {"mode": "allow"}
    assert req.headers["authorization"] == "Bearer sekret-token"


def test_patch_config_shield_400_passes_through_as_400():
    """A shield validation failure (400) reaches the caller as a 400, verbatim."""
    shield = _FakeShield(status=400, json_body={"detail": "unknown mode 'bogus'"})
    with TestClient(_build_app(shield=shield)) as client:
        res = client.patch("/api/v1/security/config", json={"mode": "bogus"})
    assert res.status_code == 400, res.text
    assert res.json() == {"detail": "unknown mode 'bogus'"}


def test_resolve_forwards_and_injects_owner_as_actor():
    shield = _FakeShield(json_body={"id": "d9", "status": "resolved"})
    with TestClient(_build_app(user_id="owner-42", shield=shield)) as client:
        res = client.post(
            "/api/v1/security/decisions/d9/resolve",
            json={"action": "ban"},
        )
    assert res.status_code == 200, res.text
    assert res.json() == {"id": "d9", "status": "resolved"}
    req = shield.last
    assert req.method == "POST"
    assert req.url.path == "/v1/decisions/d9/resolve"
    import json as _json

    sent = _json.loads(req.content)
    # The original field is preserved AND the OWNER is stamped as actor.
    assert sent["action"] == "ban"
    assert sent["actor"] == "owner-42"


def test_resolve_overwrites_client_supplied_actor():
    """A client cannot spoof the actor — the authenticated OWNER always wins."""
    shield = _FakeShield(json_body={"ok": True})
    with TestClient(_build_app(user_id="real-owner", shield=shield)) as client:
        res = client.post(
            "/api/v1/security/decisions/d1/resolve",
            json={"action": "allow", "actor": "someone-else"},
        )
    assert res.status_code == 200, res.text
    import json as _json

    sent = _json.loads(shield.last.content)
    assert sent["actor"] == "real-owner"


def test_resolve_with_empty_body_still_injects_actor():
    """No JSON body → the proxy still forwards {actor: <owner>}."""
    shield = _FakeShield(json_body={"ok": True})
    with TestClient(_build_app(user_id="u-solo", shield=shield)) as client:
        res = client.post("/api/v1/security/decisions/d1/resolve")
    assert res.status_code == 200, res.text
    import json as _json

    sent = _json.loads(shield.last.content)
    assert sent == {"actor": "u-solo"}


# ---------------------------------------------------------------------------
# RBAC — OWNER passes, non-OWNER is 403 on every route
# ---------------------------------------------------------------------------

_ALL_ROUTES = [
    ("GET", "/api/v1/security/decisions", None),
    ("GET", "/api/v1/security/stats", None),
    ("GET", "/api/v1/security/config", None),
    ("PATCH", "/api/v1/security/config", {"mode": "deny"}),
    ("POST", "/api/v1/security/decisions/d1/resolve", {"action": "ban"}),
]


@pytest.mark.parametrize("method,path,body", _ALL_ROUTES)
@pytest.mark.parametrize("role", ["member", "admin"])
def test_non_owner_is_forbidden_on_every_route(role, method, path, body):
    shield = _FakeShield()
    with TestClient(_build_app(role=role, shield=shield)) as client:
        res = client.request(method, path, json=body)
    assert res.status_code == 403, f"{role} {method} {path}: {res.text}"
    # The guard rejected BEFORE any shield call — the proxy never reached out.
    assert shield.requests == []


@pytest.mark.parametrize("method,path,body", _ALL_ROUTES)
def test_owner_passes_on_every_route(method, path, body):
    shield = _FakeShield()
    with TestClient(_build_app(role="owner", shield=shield)) as client:
        res = client.request(method, path, json=body)
    assert res.status_code in (200, 409), f"owner {method} {path}: {res.text}"
    assert res.status_code != 403


# ---------------------------------------------------------------------------
# No-shield degrade — socket unset (client None)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path", ["/api/v1/security/decisions", "/api/v1/security/stats", "/api/v1/security/config"]
)
def test_reads_degrade_to_available_false_when_socket_unset(path):
    with TestClient(_build_app(no_shield=True)) as client:
        res = client.get(path)
    assert res.status_code == 200, res.text
    assert res.json() == {"available": False, "reason": "shield_not_deployed"}


def test_patch_config_degrades_to_409_when_socket_unset():
    with TestClient(_build_app(no_shield=True)) as client:
        res = client.patch("/api/v1/security/config", json={"mode": "deny"})
    assert res.status_code == 409, res.text
    assert res.json() == {"detail": "shield_not_deployed"}


def test_resolve_degrades_to_409_when_socket_unset():
    with TestClient(_build_app(no_shield=True)) as client:
        res = client.post("/api/v1/security/decisions/d1/resolve", json={"action": "ban"})
    assert res.status_code == 409, res.text
    assert res.json() == {"detail": "shield_not_deployed"}


# ---------------------------------------------------------------------------
# No-shield degrade — socket present but unreachable (transport raises)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path", ["/api/v1/security/decisions", "/api/v1/security/stats", "/api/v1/security/config"]
)
def test_reads_degrade_to_unreachable_when_shield_down(path):
    with TestClient(_build_app(shield=_UnreachableShield())) as client:
        res = client.get(path)
    assert res.status_code == 200, res.text
    assert res.json() == {"available": False, "reason": "unreachable"}


def test_writes_degrade_to_409_when_shield_down():
    with TestClient(_build_app(shield=_UnreachableShield())) as client:
        res = client.patch("/api/v1/security/config", json={"mode": "deny"})
    assert res.status_code == 409, res.text
    assert res.json() == {"detail": "shield_not_deployed"}
    with TestClient(_build_app(shield=_UnreachableShield())) as client:
        res = client.post("/api/v1/security/decisions/d1/resolve", json={"action": "ban"})
    assert res.status_code == 409, res.text
    assert res.json() == {"detail": "shield_not_deployed"}
