# tests/cloud/ship/test_ship_wave2.py — Wave 2 cloud surface (SHIP-17):
# databases beyond mongo (postgres / redis), zero-downtime deploy checks, and
# process scaling. All box-free — the engine is faked through the SHIP-1
# transcript replay (conftest SHIP3_REPLIES, extended with the new commands).
#
# The invariants under test:
#   * db_create works for each db_type and records the injected env-var NAME on
#     the app's `databases` list — NEVER the connection string (a secret).
#   * scale + checks settings persist on the app and come back on AppOut.
#   * every Wave-2 route is workspace-scoped — a cross-tenant call 404s.
#   * the cloud-init template installs the postgres + redis DB plugins.

from __future__ import annotations

import pytest

from tests.cloud.ship.conftest import (
    SERVICE,
    _app_on_box,
    _ready_box,
    install_fake_engine,
)

# ---------------------------------------------------------------------------
# Databases — postgres / redis / mongo through the one generalized route
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "db_type,env_var",
    [("postgres", "DATABASE_URL"), ("redis", "REDIS_URL"), ("mongo", "MONGO_URL")],
)
async def test_create_db_links_each_type_and_records_the_env_var_name(
    w1, monkeypatch, db_type, env_var
):
    install_fake_engine(monkeypatch)
    app_id = await _app_on_box(w1, await _ready_box(w1))

    resp = await w1.post(f"/ship/apps/{app_id}/db", json={"db_type": db_type})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    # The injected VARIABLE NAME is reported; the connection string never is.
    assert body["env_var"] == env_var
    assert "s3cr3tpass" not in resp.text
    # The app now lists the linked database (name + type + env var, no secret).
    app = (await w1.get("/ship/apps")).json()[0]
    assert {"name": SERVICE, "db_type": db_type, "env_var": env_var} in app["databases"]
    assert "s3cr3tpass" not in (await w1.get("/ship/apps")).text


async def test_create_db_defaults_to_mongo_preserving_ship3(w1, monkeypatch):
    """Omitting db_type keeps the SHIP-3 behavior (mongo)."""
    install_fake_engine(monkeypatch)
    app_id = await _app_on_box(w1, await _ready_box(w1))

    resp = await w1.post(f"/ship/apps/{app_id}/db", json={})

    assert resp.status_code == 200, resp.text
    assert resp.json()["env_var"] == "MONGO_URL"


# ---------------------------------------------------------------------------
# Process scaling
# ---------------------------------------------------------------------------


async def test_set_scale_persists_on_the_app(w1, monkeypatch):
    install_fake_engine(monkeypatch)
    app_id = await _app_on_box(w1, await _ready_box(w1))

    resp = await w1.put(f"/ship/apps/{app_id}/scale", json={"scale": {"web": 2, "worker": 1}})

    assert resp.status_code == 200, resp.text
    assert resp.json()["scale"] == {"web": 2, "worker": 1}
    assert (await w1.get("/ship/apps")).json()[0]["scale"] == {"web": 2, "worker": 1}


async def test_set_scale_rejects_a_bad_process_name(w1, monkeypatch):
    install_fake_engine(monkeypatch)
    app_id = await _app_on_box(w1, await _ready_box(w1))

    resp = await w1.put(f"/ship/apps/{app_id}/scale", json={"scale": {"Web Server": 2}})

    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Zero-downtime checks
# ---------------------------------------------------------------------------


async def test_set_checks_persists_zero_downtime_and_path(w1, monkeypatch):
    install_fake_engine(monkeypatch)
    app_id = await _app_on_box(w1, await _ready_box(w1))

    resp = await w1.put(
        f"/ship/apps/{app_id}/checks",
        json={"zero_downtime": True, "healthcheck_path": "/healthz"},
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["zero_downtime"] is True
    assert body["healthcheck_path"] == "/healthz"

    # Disabling round-trips too.
    off = await w1.put(f"/ship/apps/{app_id}/checks", json={"zero_downtime": False})
    assert off.status_code == 200
    assert off.json()["zero_downtime"] is False


# ---------------------------------------------------------------------------
# Tenancy — every Wave-2 route 404s across a workspace boundary
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "method,suffix,payload",
    [
        ("post", "/db", {"db_type": "postgres"}),
        ("put", "/scale", {"scale": {"web": 1}}),
        ("put", "/checks", {"zero_downtime": False}),
    ],
)
async def test_wave2_routes_404_cross_tenant(w1, w2, monkeypatch, method, suffix, payload):
    install_fake_engine(monkeypatch)
    app_id = await _app_on_box(w1, await _ready_box(w1))

    resp = await getattr(w2, method)(f"/ship/apps/{app_id}{suffix}", json=payload)

    assert resp.status_code == 404, f"{method.upper()} {suffix} leaked: {resp.status_code}"


# ---------------------------------------------------------------------------
# Provisioning — the box installs the DB plugins Wave 2 needs
# ---------------------------------------------------------------------------


def test_cloudinit_installs_the_database_plugins():
    from pocketpaw_ee.ship_engine.cloudinit import render_user_data

    out = render_user_data(ssh_public_key="ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAITESTKEY paw-ship")
    assert "dokku-postgres.git postgres" in out
    assert "dokku-redis.git redis" in out
