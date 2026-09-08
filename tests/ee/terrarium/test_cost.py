# tests/ee/terrarium/test_cost.py — what an hour of watching costs.
#
# Three things have to hold for that number to mean anything: every citizen's
# decide is actually measured (one call per living citizen per tick), an
# unknown model name degrades to a price instead of killing the tick, and the
# derived figure reaches BOTH wires while the raw meter summary — which names
# the model tier — reaches neither.

from __future__ import annotations

import pytest

pytest.importorskip("pocketpaw_ee")
pytest.importorskip("mongomock_motor")

from pocketpaw_ee.terrarium import llm as citizen_llm  # noqa: E402
from pocketpaw_ee.terrarium import service as svc  # noqa: E402
from pocketpaw_ee.terrarium import world  # noqa: E402
from pocketpaw_ee.terrarium.physics import load_physics, seed_physics_path  # noqa: E402

from .conftest import create_universe  # noqa: E402


async def test_a_metered_tick_counts_one_call_per_living_citizen(client):
    """Population is the multiplier — a tick is one judgment call each."""
    uni = create_universe(client, founders=3)
    client.post(f"/terrarium/universes/{uni['id']}/tick?n=1")
    doc = await svc._get_universe_doc(uni["id"])
    assert doc is not None
    assert doc.cost["calls"] == 3
    assert doc.cost["cost_usd"] > 0
    assert doc.cost["cost_per_call"] > 0

    # Accrual, not replacement: the second tick adds to the first.
    client.post(f"/terrarium/universes/{uni['id']}/tick?n=2")
    doc = await svc._get_universe_doc(uni["id"])
    assert doc is not None
    assert doc.cost["calls"] == 9


def test_an_unknown_model_name_falls_back_instead_of_raising():
    """Upstream's CostMeter raises on a model outside its table. Ours must not:
    a tick that dies over a price list is a worse bug than a rough price."""
    meter = citizen_llm.CostMeter("some-model-nobody-priced")
    meter.record("a" * 400, "b" * 40)
    assert meter.model in citizen_llm.PRICING
    assert meter.cost_usd > 0
    assert meter.summary()["calls"] == 1


async def test_a_metered_llm_is_transparent_and_still_counts():
    """The wrapper must not change what the citizen decides, only measure it."""
    physics = load_physics(seed_physics_path("dust"))
    snap = world.CitizenSnapshot(id="c1", name="Nim", balance=100)
    digest = world.build_digest(
        day=1,
        tick=0,
        pool=0,
        citizen=snap,
        ledger=[],
        nearby_speech=[],
        new_artifacts=[],
        weather=[],
        viewer_messages=[],
        memories=[],
        constitution=[],
    )
    inner = citizen_llm.MockLlm()
    wrapped = citizen_llm.MeteredLlm(inner, "claude-haiku-4-5")
    assert wrapped.meter.model == "claude-haiku-4-5"

    kwargs = {"prompt": "decide", "physics": physics, "citizen": snap, "digest": digest}
    assert await wrapped.decide(**kwargs) == await inner.decide(**kwargs)
    assert wrapped.meter.calls == 1
    assert wrapped.meter.output_tokens > 0


async def test_the_cost_per_watched_hour_is_on_both_wires_and_the_meter_is_on_neither(
    client, monkeypatch
):
    monkeypatch.setenv("TERRARIUM_PUBLIC_ENABLED", "1")
    uni = create_universe(client, public=True, founders=2)
    client.post(f"/terrarium/universes/{uni['id']}/tick?n=1")

    private = client.get(f"/terrarium/universes/{uni['id']}").json()["universe"]
    public = client.get(f"/terrarium/public/universes/{uni['id']}").json()["universe"]
    assert private["cost_per_watched_hour"] > 0
    assert public["cost_per_watched_hour"] == private["cost_per_watched_hour"]

    # The meter summary names the model tier, which is exactly what the public
    # projection strips from ``physics``. It stays off both wires.
    for wire in (private, public):
        assert "cost" not in wire
        assert "claude" not in str(wire)


async def test_the_watched_hour_scales_with_the_worlds_own_clock():
    """A world-day that runs in half the wall clock burns twice the money per
    watched hour. The arithmetic is the point, so it is pinned directly."""

    class _Doc:
        cost = {"cost_per_call": 0.01}
        physics = {"time": {"ticks_per_day": 12, "world_day_seconds": 3600}}

    doc = _Doc()
    assert svc.cost_per_watched_hour(doc, 5) == 0.60  # 5 * 12 * 1 hour * $0.01
    doc.physics = {"time": {"ticks_per_day": 12, "world_day_seconds": 1800}}
    assert svc.cost_per_watched_hour(doc, 5) == 1.20
    assert svc.cost_per_watched_hour(doc, 0) == 0.0
