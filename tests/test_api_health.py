from fastapi import FastAPI
from fastapi.testclient import TestClient

from pocketpaw.api.v1.health import router


def test_health_includes_backend_info():
    app = FastAPI()
    app.include_router(router)

    client = TestClient(app)

    res = client.get("/health")
    data = res.json()

    assert res.status_code == 200
    assert "active_backend" in data
    assert "fallback_backends" in data
    assert isinstance(data["fallback_backends"], list)
