# tests/cloud/test_csrf_middleware.py
# Created: 2026-05-17 (security #1117 P1) — Behavior tests for the CSRF
#   middleware that gates cookie-authenticated state-changing requests.
#   Covers the token-mint endpoint, the happy path, the missing/mismatch
#   rejection paths, the Bearer-skip path, and the exempt-route paths.
#   The middleware lives at ``ee/cloud/_core/csrf.py``; this file is the
#   canonical spec for its behavior.

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ee.cloud._core.csrf import (
    AUTH_COOKIE_NAME,
    CSRF_COOKIE_NAME,
    CSRF_HEADER_NAME,
    CSRFMiddleware,
    csrf_router,
)


def _build_app() -> FastAPI:
    """Minimal app: CSRF middleware + token endpoint + a couple of
    arbitrary state-changing routes so we can assert behavior without
    pulling the full ``mount_cloud`` stack."""

    app = FastAPI()
    app.add_middleware(CSRFMiddleware)
    app.include_router(csrf_router, prefix="/api/v1")

    @app.post("/api/v1/widgets")
    def create_widget() -> dict:
        return {"ok": True}

    @app.post("/api/v1/auth/login")
    def fake_login() -> dict:
        return {"ok": True}

    @app.get("/api/v1/widgets")
    def list_widgets() -> dict:
        return {"items": []}

    return app


@pytest.fixture
def client() -> TestClient:
    return TestClient(_build_app())


# ---------------------------------------------------------------------------
# /auth/csrf token mint
# ---------------------------------------------------------------------------


def test_get_csrf_returns_token_and_sets_paw_csrf_cookie(client: TestClient) -> None:
    res = client.get("/api/v1/auth/csrf")
    assert res.status_code == 200
    body = res.json()
    assert "csrf_token" in body
    token = body["csrf_token"]
    assert token, "token should be a non-empty string"

    # Cookie is set on the response and the body value matches the cookie.
    assert client.cookies.get(CSRF_COOKIE_NAME) == token

    # Header reflects HttpOnly=False so JS can read it back. TestClient
    # strips the attribute list, so we re-check by parsing the raw header.
    set_cookie = res.headers.get("set-cookie", "")
    assert CSRF_COOKIE_NAME in set_cookie
    assert "HttpOnly" not in set_cookie  # JS needs to read this one


def test_get_csrf_is_idempotent_when_cookie_already_present(client: TestClient) -> None:
    """Calling /auth/csrf twice in the same browser keeps the same token —
    rotating on every fetch would invalidate any in-flight request that
    grabbed the previous token milliseconds earlier."""

    first = client.get("/api/v1/auth/csrf").json()["csrf_token"]
    second = client.get("/api/v1/auth/csrf").json()["csrf_token"]
    assert first == second


# ---------------------------------------------------------------------------
# Happy path: cookie-auth POST with matching header
# ---------------------------------------------------------------------------


def test_post_with_valid_csrf_header_succeeds(client: TestClient) -> None:
    token = client.get("/api/v1/auth/csrf").json()["csrf_token"]

    res = client.post(
        "/api/v1/widgets",
        headers={CSRF_HEADER_NAME: token},
        cookies={AUTH_COOKIE_NAME: "fake-jwt-for-test"},
    )
    assert res.status_code == 200
    assert res.json() == {"ok": True}


# ---------------------------------------------------------------------------
# Reject path: cookie-auth POST without / with mismatched header
# ---------------------------------------------------------------------------


def test_post_without_csrf_header_returns_403_when_cookie_auth(
    client: TestClient,
) -> None:
    # Mint the CSRF cookie so we know the rejection is about the header,
    # not the cookie missing.
    client.get("/api/v1/auth/csrf")

    res = client.post(
        "/api/v1/widgets",
        cookies={AUTH_COOKIE_NAME: "fake-jwt-for-test"},
    )
    assert res.status_code == 403
    assert res.json() == {"detail": "csrf_invalid"}


def test_post_with_mismatched_csrf_header_returns_403(client: TestClient) -> None:
    client.get("/api/v1/auth/csrf")  # mint cookie

    res = client.post(
        "/api/v1/widgets",
        headers={CSRF_HEADER_NAME: "definitely-not-the-real-token"},
        cookies={AUTH_COOKIE_NAME: "fake-jwt-for-test"},
    )
    assert res.status_code == 403
    assert res.json() == {"detail": "csrf_invalid"}


# ---------------------------------------------------------------------------
# Bypass paths: Bearer auth, no auth, safe methods, exempt routes
# ---------------------------------------------------------------------------


def test_post_with_bearer_skips_csrf_check(client: TestClient) -> None:
    """Browsers never auto-send Authorization headers, so a Bearer caller
    isn't reachable via CSRF and we skip the check entirely."""

    res = client.post(
        "/api/v1/widgets",
        headers={"Authorization": "Bearer fake-token"},
        # Intentionally NO X-CSRF-Token header and no paw_csrf cookie.
    )
    assert res.status_code == 200


def test_post_without_any_auth_passes_csrf_layer(client: TestClient) -> None:
    """No cookie auth, no Bearer — the CSRF middleware lets the request
    through so the route's own auth dep can return 401. Double-protecting
    would mask the real error with an opaque 403."""

    res = client.post("/api/v1/widgets")
    # Our test route doesn't enforce auth, so we just confirm CSRF didn't
    # intercept (status != 403).
    assert res.status_code != 403


def test_get_requests_skip_csrf(client: TestClient) -> None:
    """Safe verbs (GET/HEAD/OPTIONS) are never checked — they should be
    side-effect free."""

    res = client.get("/api/v1/widgets", cookies={AUTH_COOKIE_NAME: "fake-jwt"})
    assert res.status_code == 200


def test_login_endpoint_exempt_from_csrf(client: TestClient) -> None:
    """The bootstrap endpoints (login, logout, register, csrf, health) are
    exempt — requiring a CSRF token to call login would be a chicken-and-
    egg since login is what mints the auth cookie."""

    # No cookie, no header — would normally be rejected on a POST with
    # cookie auth, but login is exempt by path.
    res = client.post(
        "/api/v1/auth/login",
        cookies={AUTH_COOKIE_NAME: "fake-jwt-for-test"},
    )
    assert res.status_code == 200


def test_logout_clears_paw_csrf_cookie() -> None:
    """A successful logout must expire paw_csrf alongside paw_auth.

    Without this hook, paw_csrf lived its full 7-day max_age after the
    auth cookie was cleared. JS can read paw_csrf (it's intentionally
    NOT HttpOnly) and could submit it on the next login — narrow but
    real CSRF replay surface flagged in the #1119 review.
    """

    app = FastAPI()
    app.add_middleware(CSRFMiddleware)

    @app.post("/api/v1/auth/logout")
    def fake_logout() -> dict:
        return {"ok": True}

    client = TestClient(app)
    res = client.post(
        "/api/v1/auth/logout",
        cookies={CSRF_COOKIE_NAME: "stale-token"},
    )
    assert res.status_code == 200
    # The response Set-Cookie should expire paw_csrf (Max-Age=0).
    set_cookie = res.headers.get("set-cookie", "")
    assert CSRF_COOKIE_NAME in set_cookie, set_cookie
    assert "Max-Age=0" in set_cookie or 'expires=Thu, 01 Jan 1970' in set_cookie.lower()


def test_logout_failure_does_not_clear_paw_csrf() -> None:
    """If the logout route returns non-2xx (e.g. session was already
    invalid), we leave paw_csrf alone — only successful logouts pair the
    two cookies' lifecycles."""

    app = FastAPI()
    app.add_middleware(CSRFMiddleware)

    @app.post("/api/v1/auth/logout")
    def failing_logout():
        from fastapi import HTTPException

        raise HTTPException(status_code=401)

    client = TestClient(app)
    res = client.post(
        "/api/v1/auth/logout",
        cookies={CSRF_COOKIE_NAME: "stale-token"},
    )
    assert res.status_code == 401
    set_cookie = res.headers.get("set-cookie", "")
    # No Set-Cookie for paw_csrf at all when logout failed.
    assert CSRF_COOKIE_NAME not in set_cookie
