# tests/ee/terrarium/test_kill_switch.py — the public feed runs behind the
# world (delay buffer), and the owner can go dark (pause / resume).

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

pytest.importorskip("pocketpaw_ee")
pytest.importorskip("mongomock_motor")

from pocketpaw_ee.terrarium import service  # noqa: E402
from pocketpaw_ee.terrarium.domain import UniverseDoc  # noqa: E402

from .conftest import WS, create_universe, make_client  # noqa: E402

T0 = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)


async def test_public_events_stop_buffer_short_of_the_edge(client, monkeypatch):
    monkeypatch.setenv("TERRARIUM_PUBLIC_ENABLED", "1")
    monkeypatch.setenv("TERRARIUM_PUBLIC_DELAY_EVENTS", "3")
    uni = create_universe(client, public=True, founders=2)
    client.post(f"/terrarium/universes/{uni['id']}/tick?n=2")

    live = client.get(f"/terrarium/universes/{uni['id']}/events").json()["events"]
    edge = live[-1]["seq"]
    guest = client.get(f"/terrarium/public/universes/{uni['id']}/events").json()["events"]
    assert guest and guest[-1]["seq"] == edge - 3
    assert [e["seq"] for e in guest] == [e["seq"] for e in live if e["seq"] <= edge - 3]

    detail = client.get(f"/terrarium/public/universes/{uni['id']}").json()["universe"]
    assert detail["public_lag"] == 3
    # A world with fewer rows than the buffer lags by what it has, not more.
    monkeypatch.setenv("TERRARIUM_PUBLIC_DELAY_EVENTS", "1000")
    assert client.get(f"/terrarium/public/universes/{uni['id']}/events").json()["events"] == []
    assert (
        client.get(f"/terrarium/public/universes/{uni['id']}").json()["universe"]["public_lag"]
        == edge
    )


def test_the_delay_default_is_twenty_and_garbage_falls_back(monkeypatch):
    monkeypatch.delenv("TERRARIUM_PUBLIC_DELAY_EVENTS", raising=False)
    assert service.public_delay_events() == 20
    monkeypatch.setenv("TERRARIUM_PUBLIC_DELAY_EVENTS", "lots")
    assert service.public_delay_events() == 20
    monkeypatch.setenv("TERRARIUM_PUBLIC_DELAY_EVENTS", "-5")
    assert service.public_delay_events() == 0


async def test_pause_darkens_the_public_route_and_skips_the_sweep(client, monkeypatch):
    monkeypatch.setenv("TERRARIUM_PUBLIC_ENABLED", "1")
    uni = create_universe(client, public=True, founders=1)
    doc = await UniverseDoc.get(uni["id"])
    doc.last_viewed_at = T0
    doc.last_tick_at = T0 - timedelta(days=1)
    await doc.save()
    assert [r["universe_id"] for r in await service.scheduler_sweep(T0)] == [uni["id"]]
    assert client.get(f"/terrarium/public/universes/{uni['id']}").status_code == 200

    res = client.post(f"/terrarium/universes/{uni['id']}/pause")
    assert res.status_code == 200, res.text
    assert res.json()["universe"]["status"] == "paused"
    assert client.get(f"/terrarium/universes/{uni['id']}").json()["universe"]["status"] == "paused"

    assert await service.scheduler_sweep(T0) == []
    assert client.post(f"/terrarium/universes/{uni['id']}/tick").status_code == 400
    for path in ("", "/events", "/moments", "/citizens", "/artifacts"):
        assert client.get(f"/terrarium/public/universes/{uni['id']}{path}").status_code == 404
    assert client.get("/terrarium/public/universes").json()["universes"] == []

    res = client.post(f"/terrarium/universes/{uni['id']}/resume")
    assert res.status_code == 200, res.text
    assert res.json()["universe"]["status"] == "running"
    assert [r["universe_id"] for r in await service.scheduler_sweep(T0)] == [uni["id"]]
    assert client.get(f"/terrarium/public/universes/{uni['id']}").status_code == 200
    assert client.post(f"/terrarium/universes/{uni['id']}/tick").status_code == 200


async def test_a_non_owner_member_gets_403_and_the_owner_member_may_pause(client, monkeypatch):
    uni = create_universe(client)  # created by the admin fixture user
    member = make_client(monkeypatch, workspace_id=WS, user_id="u-member", role="member")
    assert member.post(f"/terrarium/universes/{uni['id']}/pause").status_code == 403
    assert member.post(f"/terrarium/universes/{uni['id']}/resume").status_code == 403
    assert client.get(f"/terrarium/universes/{uni['id']}").json()["universe"]["status"] == "running"

    # The owner is the creator, whatever their role.
    doc = await UniverseDoc.get(uni["id"])
    doc.creator = "u-member"
    await doc.save()
    assert member.post(f"/terrarium/universes/{uni['id']}/pause").status_code == 200
