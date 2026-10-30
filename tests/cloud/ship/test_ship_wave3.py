# tests/cloud/ship/test_ship_wave3.py — Wave 3 cloud surface (SHIP-18):
# resource limits, persistent volumes, and lifecycle bounces (restart / rebuild).
# All box-free — the engine is faked through the SHIP-1 transcript replay
# (conftest SHIP3_REPLIES, extended with the new commands).
#
# The invariants under test:
#   * set_resources persists cpu_limit + memory_limit_mb on the app and rejects
#     an all-zero call (422).
#   * create_volume mounts a volume and records it on the app's `volumes` list —
#     the backing path is not a secret, but the mount round-trips.
#   * restart / rebuild answer a LifecycleOut confirming the action; they do NOT
#     mutate persisted app config.
#   * every Wave-3 route is workspace-scoped — a cross-tenant call 404s.

from __future__ import annotations

import pytest

from tests.cloud.ship.conftest import (
    _app_on_box,
    _ready_box,
    install_fake_engine,
)

# ---------------------------------------------------------------------------
# Resource limits
# ---------------------------------------------------------------------------


async def test_set_resources_persists_cpu_and_memory(w1, monkeypatch):
    install_fake_engine(monkeypatch)
    app_id = await _app_on_box(w1, await _ready_box(w1))

    resp = await w1.put(f"/ship/apps/{app_id}/resources", json={"cpu": 1000, "memory_mb": 512})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["cpu_limit"] == 1000
    assert body["memory_limit_mb"] == 512
    app = (await w1.get("/ship/apps")).json()[0]
    assert app["cpu_limit"] == 1000
    assert app["memory_limit_mb"] == 512


async def test_set_resources_rejects_an_all_zero_call(w1, monkeypatch):
    install_fake_engine(monkeypatch)
    app_id = await _app_on_box(w1, await _ready_box(w1))

    resp = await w1.put(f"/ship/apps/{app_id}/resources", json={"cpu": 0, "memory_mb": 0})

    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Persistent volumes
# ---------------------------------------------------------------------------


async def test_create_volume_mounts_and_records_it(w1, monkeypatch):
    install_fake_engine(monkeypatch)
    app_id = await _app_on_box(w1, await _ready_box(w1))

    resp = await w1.post(f"/ship/apps/{app_id}/volumes", json={"mount_path": "/data"})

    assert resp.status_code == 200, resp.text
    volumes = resp.json()["volumes"]
    # The name defaults to <app-name>-data; the mount + backing path round-trip.
    assert {
        "name": "demo-data",
        "mount_path": "/data",
        "host_path": "/var/lib/dokku/data/storage/demo-data",
    } in volumes
    # It sticks on a re-read.
    assert (await w1.get("/ship/apps")).json()[0]["volumes"] == volumes


async def test_create_volume_rejects_a_relative_mount_path(w1, monkeypatch):
    install_fake_engine(monkeypatch)
    app_id = await _app_on_box(w1, await _ready_box(w1))

    resp = await w1.post(f"/ship/apps/{app_id}/volumes", json={"mount_path": "data"})

    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Lifecycle — restart / rebuild
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("action", ["restart", "rebuild"])
async def test_lifecycle_confirms_the_action(w1, monkeypatch, action):
    install_fake_engine(monkeypatch)
    app_id = await _app_on_box(w1, await _ready_box(w1))

    resp = await w1.post(f"/ship/apps/{app_id}/{action}")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body == {"app_id": app_id, "action": action}


# ---------------------------------------------------------------------------
# Tenancy — every Wave-3 route 404s across a workspace boundary
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "method,suffix,payload",
    [
        ("put", "/resources", {"cpu": 1000}),
        ("post", "/volumes", {"mount_path": "/data"}),
        ("post", "/restart", None),
        ("post", "/rebuild", None),
    ],
)
async def test_wave3_routes_404_cross_tenant(w1, w2, monkeypatch, method, suffix, payload):
    install_fake_engine(monkeypatch)
    app_id = await _app_on_box(w1, await _ready_box(w1))

    kwargs = {"json": payload} if payload is not None else {}
    resp = await getattr(w2, method)(f"/ship/apps/{app_id}{suffix}", **kwargs)

    assert resp.status_code == 404, f"{method.upper()} {suffix} leaked: {resp.status_code}"
