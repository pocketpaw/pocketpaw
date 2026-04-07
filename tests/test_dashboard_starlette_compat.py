"""Regression test: dashboard index must return 200 on Starlette 1.0.0."""

import importlib.metadata

from fastapi.testclient import TestClient

# Logged on failure to make version-related issues easier to diagnose
STARLETTE_VERSION = importlib.metadata.version("starlette")


def test_dashboard_index_returns_200():
    """GET / must return 200 OK — regression for Starlette 1.0.0 compat."""
    from pocketpaw.dashboard import app

    client = TestClient(app)
    response = client.get("/")
    assert response.status_code == 200, (
        f"Dashboard returned {response.status_code} "
        f"(starlette=={STARLETTE_VERSION}). "
        "Check TemplateResponse signature in dashboard.py."
    )


def test_dashboard_index_returns_html():
    """GET / must return text/html content."""
    from pocketpaw.dashboard import app

    client = TestClient(app)
    response = client.get("/")
    assert "text/html" in response.headers.get("content-type", "")
