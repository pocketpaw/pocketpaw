"""Tests for ee.cloud._core.http — extracted CloudError exception handler."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pocketpaw_ee.cloud._core.errors import (
    CloudError,
    Forbidden,
    Internal,
    NotFound,
    RateLimited,
)
from pocketpaw_ee.cloud._core.http import add_error_handler, cloud_error_handler


def _build_app() -> FastAPI:
    app = FastAPI()
    add_error_handler(app)

    @app.get("/notfound")
    def _nf() -> dict:
        raise NotFound("workspace", "abc")

    @app.get("/forbidden")
    def _fb() -> dict:
        raise Forbidden("auth.denied", "You shall not pass")

    @app.get("/rate")
    def _rl() -> dict:
        raise RateLimited("api.rate_limited")

    @app.get("/internal")
    def _ie() -> dict:
        raise Internal()

    @app.get("/raw_cloud")
    def _raw() -> dict:
        raise CloudError(418, "teapot", "I am a teapot")

    @app.get("/ok")
    def _ok() -> dict:
        return {"ok": True}

    return app


def test_not_found_returns_404_envelope() -> None:
    client = TestClient(_build_app())
    resp = client.get("/notfound")
    assert resp.status_code == 404
    assert resp.json() == {
        "error": {"code": "workspace.not_found", "message": "workspace 'abc' not found"}
    }


def test_forbidden_returns_403_envelope() -> None:
    client = TestClient(_build_app())
    resp = client.get("/forbidden")
    assert resp.status_code == 403
    assert resp.json() == {"error": {"code": "auth.denied", "message": "You shall not pass"}}


def test_rate_limited_returns_429() -> None:
    client = TestClient(_build_app())
    resp = client.get("/rate")
    assert resp.status_code == 429
    assert resp.json()["error"]["code"] == "api.rate_limited"


def test_internal_returns_500_with_generic_envelope() -> None:
    client = TestClient(_build_app())
    resp = client.get("/internal")
    assert resp.status_code == 500
    assert resp.json() == {"error": {"code": "internal", "message": "Internal server error"}}


def test_arbitrary_cloud_error_uses_provided_status() -> None:
    client = TestClient(_build_app())
    resp = client.get("/raw_cloud")
    assert resp.status_code == 418
    assert resp.json() == {"error": {"code": "teapot", "message": "I am a teapot"}}


def test_non_cloud_routes_unaffected() -> None:
    client = TestClient(_build_app())
    assert client.get("/ok").json() == {"ok": True}


def test_handler_function_is_idempotent_when_added_twice() -> None:
    """add_error_handler may be called more than once during app reload."""
    app = FastAPI()
    add_error_handler(app)
    add_error_handler(app)

    @app.get("/x")
    def _x() -> dict:
        raise NotFound("thing")

    resp = TestClient(app).get("/x")
    assert resp.status_code == 404


async def test_cloud_error_handler_returns_envelope_directly() -> None:
    """Direct unit test of the handler function — no app, no client."""
    from starlette.requests import Request

    err = NotFound("workspace", "abc")
    scope = {"type": "http", "method": "GET", "path": "/", "headers": []}
    response = await cloud_error_handler(Request(scope), err)
    assert response.status_code == 404
    body = bytes(response.body).decode("utf-8")
    assert '"workspace.not_found"' in body
