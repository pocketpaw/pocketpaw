# tests/ee/terrarium/conftest.py — shared fixtures for the terrarium suite.
#
# Composes on ``tests/ee/conftest.py::beanie_test_db`` (mongomock-motor) for the
# document tests, and adds three things this module needs: the deterministic
# citizen LLM (POCKETPAW_TERRARIUM_LLM=mock, which is also the production
# DEFAULT), a soul root pointed at tmp_path so no test writes a .soul into the
# repo, and a TestClient wiring BOTH terrarium routers with the real RBAC guard.
#
# The public router is mounted here too, so the "flag off = dark" and "flag on +
# universe public = readable" cases exercise the REAL route wiring rather than
# calling the service directly.

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

pytest.importorskip("pocketpaw_ee")
pytest.importorskip("mongomock_motor")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from pocketpaw_ee.cloud._core.deps import current_workspace_id  # noqa: E402
from pocketpaw_ee.cloud._core.http import add_error_handler  # noqa: E402
from pocketpaw_ee.cloud.auth import current_active_user  # noqa: E402
from pocketpaw_ee.cloud.license import require_license  # noqa: E402
from pocketpaw_ee.terrarium import llm as citizen_llm  # noqa: E402
from pocketpaw_ee.terrarium.physics import load_physics, seed_physics_path  # noqa: E402
from pocketpaw_ee.terrarium.router import (  # noqa: E402
    public_router as terrarium_public_router,
)
from pocketpaw_ee.terrarium.router import router as terrarium_router  # noqa: E402

WS = "ws-terra"
USER = "u-terra"


@pytest.fixture
def instinct_store(tmp_path, monkeypatch):
    """An isolated InstinctStore wired everywhere terrarium resolves the gate.

    Without this the ``world_create`` / ``world_spawn`` filing either fails
    silently (``service._propose`` swallows and returns None) or writes to the
    developer's real store — either way the gate would be untested. Copied from
    ``tests/cloud/test_belt_mandates.py``.
    """
    from pocketpaw.instinct.store import InstinctStore

    store = InstinctStore(tmp_path / "instinct_terrarium.db")
    monkeypatch.setattr("pocketpaw.stores.get_instinct_store", lambda *a, **k: store)
    monkeypatch.setattr("pocketpaw_ee.instinct.router._store", lambda *a, **k: store)
    return store


@pytest.fixture(autouse=True)
def mock_citizen_llm(monkeypatch, tmp_path):
    """Deterministic citizens + a throwaway soul root for every test here."""
    monkeypatch.setenv("POCKETPAW_TERRARIUM_LLM", "mock")
    monkeypatch.setenv("POCKETPAW_TERRARIUM_SOUL_ROOT", str(tmp_path / "souls"))
    citizen_llm.set_mock_decision(None)
    yield
    citizen_llm.set_mock_decision(None)


@pytest.fixture(autouse=True)
def public_off(monkeypatch):
    """The public surface is OFF unless a test turns it on. Mirrors the default."""
    monkeypatch.delenv("TERRARIUM_PUBLIC_ENABLED", raising=False)


def make_client(monkeypatch, *, workspace_id: str = WS, user_id: str = USER, role: str = "admin"):
    """One app holding both terrarium routers with the real RBAC guard."""
    from unittest.mock import AsyncMock

    import pocketpaw_ee.cloud.workspace.service as ws_svc

    monkeypatch.setattr(ws_svc, "get_workspace_plan", AsyncMock(return_value="enterprise"))

    app = FastAPI()
    add_error_handler(app)
    app.include_router(terrarium_router)
    app.include_router(terrarium_public_router)
    app.dependency_overrides[require_license] = lambda: None

    user = SimpleNamespace(
        id=user_id,
        active_workspace=workspace_id,
        workspaces=[SimpleNamespace(workspace=workspace_id, role=role)],
    )

    async def _fake_user_dep():
        return user

    app.dependency_overrides[current_active_user] = _fake_user_dep
    app.dependency_overrides[current_workspace_id] = lambda: workspace_id
    return TestClient(app)


@pytest.fixture
def client(monkeypatch, beanie_test_db):
    return make_client(monkeypatch)


def dust_physics(**overrides: Any) -> dict:
    """The bundled Dust seed as a dict, with per-test overrides applied."""
    raw = load_physics(seed_physics_path("dust")).model_dump()
    raw.update(overrides)
    return raw


def create_universe(client: TestClient, *, public: bool = False, **overrides: Any) -> dict:
    res = client.post(
        "/terrarium/universes",
        json={"physics": dust_physics(**overrides), "public": public},
    )
    assert res.status_code == 200, res.text
    return res.json()["universe"]
