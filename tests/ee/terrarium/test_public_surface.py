# tests/ee/terrarium/test_public_surface.py — the anonymous read surface is the
# one security boundary in terrarium, so it gets its own file.
#
# The rule under test is DOUBLE-GATED and FAIL-CLOSED: a route answers only when
# BOTH ``TERRARIUM_PUBLIC_ENABLED`` is on AND the universe itself is flagged
# public. Either gate closed is a flat 404 — never a 403, which would confirm
# the universe exists. And there is no write route: speaking and pledging move
# credits and enter citizens' context, so they always require an account.

from __future__ import annotations

import pytest

pytest.importorskip("pocketpaw_ee")
pytest.importorskip("mongomock_motor")

from pocketpaw_ee.terrarium.router import public_router  # noqa: E402

from .conftest import create_universe  # noqa: E402

PUBLIC_PATHS = (
    "/terrarium/public/universes",
    "/terrarium/public/universes/{id}",
    "/terrarium/public/universes/{id}/events",
    "/terrarium/public/universes/{id}/citizens",
    "/terrarium/public/universes/{id}/artifacts",
)


def test_the_public_router_carries_no_write_route():
    """Structural: a POST/PUT/PATCH/DELETE on the public router is a bug."""
    for route in public_router.routes:
        assert set(route.methods) <= {"GET", "HEAD"}, f"{route.path} exposes {route.methods}"


def test_the_public_router_carries_no_auth_dependency():
    """It must be its own router with NO ambient dependency — that is the whole
    reason for the split (an added guard would silently change its posture)."""
    assert public_router.dependencies == []


async def test_flag_off_every_public_route_is_dark(client):
    """DEFAULT OFF. Even a universe that opted in is invisible."""
    uni = create_universe(client, public=True)
    for path in PUBLIC_PATHS:
        res = client.get(path.format(id=uni["id"]))
        assert res.status_code == 404, (path, res.status_code)
    assert client.get(f"/terrarium/public/universes/{uni['id']}/citizens/x").status_code == 404


@pytest.mark.parametrize("value", ["", "0", "false", "no", "off", "maybe", "TRUE-ish"])
async def test_a_non_truthy_flag_value_keeps_the_surface_dark(client, monkeypatch, value):
    """Fail-closed on garbage: only an explicit truthy value opens it."""
    monkeypatch.setenv("TERRARIUM_PUBLIC_ENABLED", value)
    uni = create_universe(client, public=True)
    assert client.get("/terrarium/public/universes").status_code == 404
    assert client.get(f"/terrarium/public/universes/{uni['id']}").status_code == 404


async def test_flag_on_but_universe_private_is_still_a_404(client, monkeypatch):
    """The second gate. A workspace universe that never opted in stays hidden."""
    monkeypatch.setenv("TERRARIUM_PUBLIC_ENABLED", "1")
    uni = create_universe(client, public=False)
    for path in PUBLIC_PATHS[1:]:
        res = client.get(path.format(id=uni["id"]))
        assert res.status_code == 404, (path, res.status_code)
    assert client.get("/terrarium/public/universes").json()["universes"] == []


async def test_flag_on_and_universe_public_reads(client, monkeypatch):
    monkeypatch.setenv("TERRARIUM_PUBLIC_ENABLED", "1")
    uni = create_universe(client, public=True, founders=2)
    client.post(f"/terrarium/universes/{uni['id']}/tick?n=1")

    listing = client.get("/terrarium/public/universes").json()["universes"]
    assert [u["id"] for u in listing] == [uni["id"]]

    detail = client.get(f"/terrarium/public/universes/{uni['id']}").json()
    assert detail["universe"]["name"] == "Dust"
    assert len(detail["citizens"]) == 2
    assert detail["ledger"]

    assert client.get(f"/terrarium/public/universes/{uni['id']}/events").json()["events"]
    assert client.get(f"/terrarium/public/universes/{uni['id']}/artifacts").json()["artifacts"]
    cid = detail["citizens"][0]["id"]
    assert client.get(f"/terrarium/public/universes/{uni['id']}/citizens/{cid}").status_code == 200


async def test_the_public_projection_strips_server_side_fields(client, monkeypatch):
    """soul_path is a SERVER FILESYSTEM PATH. It must never cross the boundary,
    and neither should the creator's user id or the model tiers."""
    monkeypatch.setenv("TERRARIUM_PUBLIC_ENABLED", "1")
    uni = create_universe(client, public=True, founders=1)

    detail = client.get(f"/terrarium/public/universes/{uni['id']}").json()
    blob = str(detail)
    assert "soul_path" not in blob
    assert "creator" not in detail["universe"]
    assert "models" not in detail["universe"]["physics"]
    assert "did" not in detail["citizens"][0]
    # The private surface still carries them for the workspace.
    private = client.get(f"/terrarium/universes/{uni['id']}").json()
    assert private["citizens"][0]["soul_path"]

    listed = client.get(f"/terrarium/public/universes/{uni['id']}/citizens").json()["citizens"]
    assert "soul_path" not in str(listed)
    profile = client.get(
        f"/terrarium/public/universes/{uni['id']}/citizens/{listed[0]['id']}"
    ).json()
    assert profile["memories"] == [], "souls are not readable anonymously"


async def test_a_public_citizen_from_another_universe_is_a_404(client, monkeypatch):
    monkeypatch.setenv("TERRARIUM_PUBLIC_ENABLED", "1")
    a = create_universe(client, public=True, founders=1)
    b = create_universe(client, public=True, founders=1)
    b_cid = client.get(f"/terrarium/public/universes/{b['id']}/citizens").json()["citizens"][0][
        "id"
    ]
    assert client.get(f"/terrarium/public/universes/{a['id']}/citizens/{b_cid}").status_code == 404


async def test_an_unknown_universe_id_is_a_404_with_the_flag_on(client, monkeypatch):
    monkeypatch.setenv("TERRARIUM_PUBLIC_ENABLED", "1")
    assert client.get("/terrarium/public/universes/000000000000000000000000").status_code == 404


async def test_a_malformed_id_404s_rather_than_500s_on_the_public_surface(client, monkeypatch):
    """Beanie's ``get`` raises InvalidId on a non-ObjectId string, which is not a
    CloudError. A 500 here is both a bad response and a fingerprint, so every
    lookup funnels through a guard that turns a malformed id into a 404."""
    monkeypatch.setenv("TERRARIUM_PUBLIC_ENABLED", "1")
    uni = create_universe(client, public=True, founders=1)
    for path in PUBLIC_PATHS[1:]:
        res = client.get(path.format(id="../../etc/passwd"))
        assert res.status_code == 404, (path, res.status_code)
        res = client.get(path.format(id="not-an-object-id"))
        assert res.status_code == 404, (path, res.status_code)
    res = client.get(f"/terrarium/public/universes/{uni['id']}/citizens/not-an-object-id")
    assert res.status_code == 404, res.text
