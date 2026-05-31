"""Tests for trace API endpoints."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from pocketpaw.api.v1.traces import router


def _build_client() -> TestClient:
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    return TestClient(app)


def test_traces_router_registered_in_v1() -> None:
    from pocketpaw.api.v1 import _V1_ROUTERS

    modules = [item[0] for item in _V1_ROUTERS]
    assert "pocketpaw.api.v1.traces" in modules


def test_list_traces_endpoint() -> None:
    client = _build_client()

    fake_store = MagicMock()
    fake_store.list_traces = AsyncMock(
        return_value=[
            {
                "trace_id": "trace_1",
                "session_key": "cli:chat1",
                "total_cost_usd": 0.1,
            }
        ]
    )

    with patch("pocketpaw.api.v1.traces.get_trace_store", return_value=fake_store):
        resp = client.get("/api/v1/traces?limit=20&session_id=chat1&min_cost=0.01")

    assert resp.status_code == 200
    assert resp.json()[0]["trace_id"] == "trace_1"


def test_get_trace_endpoint_not_found() -> None:
    client = _build_client()

    fake_store = MagicMock()
    fake_store.get_trace = AsyncMock(return_value=None)

    with patch("pocketpaw.api.v1.traces.get_trace_store", return_value=fake_store):
        resp = client.get("/api/v1/traces/missing")

    assert resp.status_code == 404


def test_get_trace_endpoint_success() -> None:
    client = _build_client()

    fake_store = MagicMock()
    fake_store.get_trace = AsyncMock(
        return_value={
            "trace_id": "trace_2",
            "session_key": "websocket:abc",
            "total": {"total_cost_usd": 0.2},
        }
    )

    with patch("pocketpaw.api.v1.traces.get_trace_store", return_value=fake_store):
        resp = client.get("/api/v1/traces/trace_2")

    assert resp.status_code == 200
    assert resp.json()["trace_id"] == "trace_2"
