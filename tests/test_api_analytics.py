"""Tests for analytics API endpoints."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from pocketpaw.api.v1.analytics import router


def _build_client() -> TestClient:
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    return TestClient(app)


def test_analytics_router_registered_in_v1() -> None:
    from pocketpaw.api.v1 import _V1_ROUTERS

    modules = [item[0] for item in _V1_ROUTERS]
    assert "pocketpaw.api.v1.analytics" in modules


def test_analytics_cost_endpoint() -> None:
    client = _build_client()

    with patch(
        "pocketpaw.api.v1.analytics.get_cost_analytics",
        new=AsyncMock(return_value={"period": "day", "totals": {"cost_usd": 1.23}}),
    ):
        resp = client.get("/api/v1/analytics/cost?period=day")

    assert resp.status_code == 200
    assert resp.json()["totals"]["cost_usd"] == 1.23


def test_analytics_performance_endpoint() -> None:
    client = _build_client()

    with patch(
        "pocketpaw.api.v1.analytics.get_performance_analytics",
        new=AsyncMock(return_value={"period": "week", "response_latency_ms": {"avg": 123.4}}),
    ):
        resp = client.get("/api/v1/analytics/performance?period=week")

    assert resp.status_code == 200
    assert resp.json()["period"] == "week"


def test_analytics_usage_endpoint() -> None:
    client = _build_client()

    with patch(
        "pocketpaw.api.v1.analytics.get_usage_analytics",
        new=AsyncMock(return_value={"period": "month", "totals": {"messages": 10}}),
    ):
        resp = client.get("/api/v1/analytics/usage?period=month")

    assert resp.status_code == 200
    assert resp.json()["totals"]["messages"] == 10


def test_analytics_health_endpoint() -> None:
    client = _build_client()

    with patch(
        "pocketpaw.api.v1.analytics.get_health_analytics",
        new=AsyncMock(return_value={"status": "healthy", "error_rate_24h": 0.0}),
    ):
        resp = client.get("/api/v1/analytics/health")

    assert resp.status_code == 200
    assert resp.json()["status"] == "healthy"
