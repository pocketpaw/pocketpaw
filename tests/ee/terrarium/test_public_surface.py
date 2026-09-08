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
    "/terrarium/public/universes/{id}/moments",
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
    monkeypatch.setenv("TERRARIUM_PUBLIC_DELAY_EVENTS", "0")  # the buffer has its own test
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


# ---------------------------------------------------------------------------
# Moments — the story feed a stranger reads
# ---------------------------------------------------------------------------


async def test_the_moments_alias_returns_only_moments(client, monkeypatch):
    monkeypatch.setenv("TERRARIUM_PUBLIC_ENABLED", "1")
    monkeypatch.setenv("TERRARIUM_PUBLIC_DELAY_EVENTS", "0")
    uni = create_universe(client, public=True, founders=3)
    client.post(f"/terrarium/universes/{uni['id']}/tick?n=2")

    everything = client.get(f"/terrarium/public/universes/{uni['id']}/events").json()["events"]
    moments = client.get(f"/terrarium/public/universes/{uni['id']}/moments").json()["events"]
    assert moments, "three founders acting together should have made a moment"
    assert {e["kind"] for e in moments} == {"moment"}
    assert len(moments) < len(everything)
    # The alias and the filter are the same read.
    filtered = client.get(f"/terrarium/public/universes/{uni['id']}/events?kind=moment").json()[
        "events"
    ]
    assert [e["seq"] for e in filtered] == [e["seq"] for e in moments]
    assert moments[0]["data"]["actors"] and moments[0]["body"]


async def test_the_moment_payload_names_citizens_and_never_a_soul_path(client, monkeypatch):
    """The moment carries a structured payload, which is a NEW field on the
    public wire: it must name citizens the way the map does and nothing else."""
    monkeypatch.setenv("TERRARIUM_PUBLIC_ENABLED", "1")
    uni = create_universe(client, public=True, founders=3)
    client.post(f"/terrarium/universes/{uni['id']}/tick?n=2")

    blob = str(client.get(f"/terrarium/public/universes/{uni['id']}/moments").json())
    assert "soul_path" not in blob
    assert ".soul" not in blob
    assert "did" not in blob
    assert "/" not in blob, "no server path may reach the anonymous surface"
    assert uni["creator"] not in blob if uni.get("creator") else True


async def test_an_unknown_kind_is_rejected_cleanly(client, monkeypatch):
    """A bad filter is a 400 with a stable code, not a 500 and not silence."""
    monkeypatch.setenv("TERRARIUM_PUBLIC_ENABLED", "1")
    uni = create_universe(client, public=True, founders=1)
    res = client.get(f"/terrarium/public/universes/{uni['id']}/events?kind=../../etc")
    assert res.status_code == 400, res.text
    assert res.json()["error"]["code"] == "terrarium.bad_event_kind"
    # A KNOWN kind with nothing to show is an empty page, not an error.
    ok = client.get(f"/terrarium/public/universes/{uni['id']}/events?kind=raid")
    assert ok.status_code == 200 and ok.json()["events"] == []


async def test_a_bad_kind_on_a_hidden_universe_is_still_a_flat_404(client, monkeypatch):
    """Gate first, validate second. Otherwise the 400 confirms the universe."""
    uni = create_universe(client, public=False)
    assert (
        client.get(f"/terrarium/public/universes/{uni['id']}/events?kind=nonsense").status_code
        == 404
    )
    monkeypatch.setenv("TERRARIUM_PUBLIC_ENABLED", "1")
    assert (
        client.get(f"/terrarium/public/universes/{uni['id']}/events?kind=nonsense").status_code
        == 404
    )
