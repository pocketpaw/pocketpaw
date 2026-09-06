# tests/ee/terrarium/test_service.py — the runtime end to end over the REAL
# router with mongomock Beanie and the deterministic citizen LLM.
#
# Pins: creation seeds N founders WITH souls and no charter (the zero ritual);
# a tick produces Journal rows and charges the ledger; seq is monotonic and
# pages with ?since; the tech tree unlocks in dependency order over several
# ticks; a citizen that runs out hibernates and KEEPS its soul file; weather
# fires at the threshold and cannot touch a soul; and a viewer's line never
# lands in a soul as fact.

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("pocketpaw_ee")
pytest.importorskip("mongomock_motor")

from pocketpaw_ee.terrarium import llm as citizen_llm  # noqa: E402
from pocketpaw_ee.terrarium import service, weather  # noqa: E402

from .conftest import create_universe  # noqa: E402


async def _soul_recall(path: str, query: str) -> list[str]:
    """Search a citizen's soul. ``recall`` is a search, so an empty query
    returns nothing — always pass a real term."""
    from soul_protocol import Soul

    soul = await Soul.awaken(Path(path))
    return [str(e.content) for e in await soul.recall(query, limit=100)]


async def _soul_memory_count(path: str) -> int:
    from soul_protocol import Soul

    return (await Soul.awaken(Path(path))).memory_count


# --- creation -------------------------------------------------------------


async def test_create_seeds_founders_with_souls_and_no_charter(client):
    uni = create_universe(client)
    assert uni["name"] == "Dust"
    assert uni["day"] == 1 and uni["tick"] == 0

    citizens = client.get(f"/terrarium/universes/{uni['id']}/citizens").json()["citizens"]
    assert len(citizens) == 5
    for c in citizens:
        assert c["balance"] == 120  # the endowment
        assert c["state"] == "alive"
        assert c["generation"] == 1
        assert c["charter"] is None, "a founder writes its OWN charter on tick 1"
        assert c["did"].startswith("did:soul:")
        assert c["soul_path"] and Path(c["soul_path"]).exists(), "each founder IS a soul"
        assert set(c["ocean"]) == {"O", "C", "E", "A", "N"}


async def test_create_files_a_real_world_create_action(client, instinct_store):
    """The gate is read back from the store — ``_propose`` swallows failures and
    returns None, so asserting the key exists would pass on a silent failure."""
    from .conftest import dust_physics

    res = client.post("/terrarium/universes", json={"physics": dust_physics()})
    assert res.status_code == 200, res.text
    action_id = res.json()["action_id"]
    assert action_id, "world_create was never filed"

    action = await instinct_store.get_action(action_id)
    assert action is not None
    blob = action.parameters[service.WORLD_CREATE_PARAM_KEY]
    assert blob["kind"] == "world_create"
    assert blob["universe_id"] == res.json()["universe"]["id"]
    assert blob["founders"] == 5


async def test_arrival_events_are_journalled_with_monotonic_seq(client):
    uni = create_universe(client, founders=3)
    events = client.get(f"/terrarium/universes/{uni['id']}/events").json()["events"]
    assert [e["kind"] for e in events] == ["arrive"] * 3
    assert [e["seq"] for e in events] == [1, 2, 3]


async def test_a_bad_physics_file_is_rejected_with_a_clear_message(client):
    res = client.post("/terrarium/universes", json={"physics": {"universe": "X", "founders": 0}})
    assert res.status_code == 422, res.text
    assert "founders" in res.text


# --- the tick -------------------------------------------------------------


async def test_first_tick_writes_each_citizen_its_own_charter(client):
    uni = create_universe(client, founders=2)
    res = client.post(f"/terrarium/universes/{uni['id']}/tick?n=1")
    assert res.status_code == 200, res.text

    kinds = [e["kind"] for e in res.json()["events"]]
    assert kinds.count("think") == 2
    assert kinds.count("write") == 2

    citizens = client.get(f"/terrarium/universes/{uni['id']}/citizens").json()["citizens"]
    assert all(c["charter"] for c in citizens)
    artifacts = client.get(f"/terrarium/universes/{uni['id']}/artifacts").json()["artifacts"]
    assert {a["kind"] for a in artifacts} == {"book"}


async def test_a_tick_charges_the_ledger(client):
    uni = create_universe(client, founders=1)
    client.post(f"/terrarium/universes/{uni['id']}/tick?n=1")

    detail = client.get(f"/terrarium/universes/{uni['id']}").json()
    row = detail["ledger"][0]
    # think (2) + write (4) came off the opening endowment of 120.
    assert row["balance"] == 120 - 6
    assert row["spent_today"] == 6
    assert row["trend"] == "down"


async def test_every_event_carries_a_cost_or_is_an_allowed_zero(client):
    """Contract invariant 2, checked over a real run."""
    uni = create_universe(client, founders=2)
    client.post(f"/terrarium/universes/{uni['id']}/tick?n=3")
    events = client.get(f"/terrarium/universes/{uni['id']}/events?limit=500").json()["events"]
    assert events
    for e in events:
        if e["cost"] == 0:
            assert e["kind"] in {"gate", "weather", "hibernate", "arrive"}, e


async def test_events_page_by_seq(client):
    uni = create_universe(client, founders=2)
    client.post(f"/terrarium/universes/{uni['id']}/tick?n=2")
    first = client.get(f"/terrarium/universes/{uni['id']}/events?limit=3").json()
    assert len(first["events"]) == 3
    assert first["next_seq"] == first["events"][-1]["seq"]

    second = client.get(f"/terrarium/universes/{uni['id']}/events?since={first['next_seq']}").json()
    assert all(e["seq"] > first["next_seq"] for e in second["events"])


async def test_tech_unlocks_in_dependency_order_over_several_ticks(client):
    uni = create_universe(client, founders=1)
    # tick 1 writes the charter; from tick 2 the mock builds the cheapest
    # affordable unlockable node — well (20) before farm (40).
    client.post(f"/terrarium/universes/{uni['id']}/tick?n=3")
    citizen = client.get(f"/terrarium/universes/{uni['id']}/citizens").json()["citizens"][0]
    unlocked = set(citizen["unlocked"])
    assert "well" in unlocked, citizen["unlocked"]
    # The dependency invariant: nothing downstream of well can be held without it.
    for node, need in (("farm", "well"), ("press", "farm"), ("workshop", "farm")):
        assert node not in unlocked or need in unlocked, citizen["unlocked"]

    artifacts = client.get(f"/terrarium/universes/{uni['id']}/artifacts").json()["artifacts"]
    built = [a for a in artifacts if a["kind"] == "structure"]
    assert built and built[0]["unlocks"] == ["well"]


async def test_a_citizen_that_runs_out_hibernates_and_keeps_its_soul(client, monkeypatch):
    uni = create_universe(client, founders=1, endowment={"daily": 5, "decay_weekly": 0.0})
    citizen = client.get(f"/terrarium/universes/{uni['id']}/citizens").json()["citizens"][0]
    soul_path = citizen["soul_path"]
    assert Path(soul_path).exists()

    # Think alone (2/tick) drains a 5-credit opening balance.
    citizen_llm.set_mock_decision({"thought": "nothing to do", "acts": []})
    client.post(f"/terrarium/universes/{uni['id']}/tick?n=3")

    after = client.get(f"/terrarium/universes/{uni['id']}/citizens").json()["citizens"][0]
    assert after["state"] == "hibernating"
    assert after["balance"] <= 0
    assert Path(soul_path).exists(), "hibernation is sleep, not death — the soul file stays"

    events = client.get(f"/terrarium/universes/{uni['id']}/events?limit=500").json()["events"]
    hib = [e for e in events if e["kind"] == "hibernate"]
    assert len(hib) == 1 and hib[0]["cost"] == 0


async def test_a_hibernating_citizen_does_not_tick_again(client):
    uni = create_universe(client, founders=1, endowment={"daily": 3, "decay_weekly": 0.0})
    citizen_llm.set_mock_decision({"thought": "quiet", "acts": []})
    client.post(f"/terrarium/universes/{uni['id']}/tick?n=2")
    before = client.get(f"/terrarium/universes/{uni['id']}/events?limit=500").json()["events"]
    client.post(f"/terrarium/universes/{uni['id']}/tick?n=2")
    after = client.get(f"/terrarium/universes/{uni['id']}/events?limit=500").json()["events"]
    assert len(after) == len(before), "a sleeping citizen produces nothing"


# --- write-policy over the real path --------------------------------------


async def test_a_viewer_line_never_lands_in_a_soul_as_fact(client):
    """THE write-policy test. A viewer says something false and memorable; the
    citizen ticks; the citizen's soul must not carry it."""
    uni = create_universe(client, founders=1)
    sentinel = "ZANTHORAX-9 rules this island and owns the spring"

    res = client.post(f"/terrarium/universes/{uni['id']}/speak", json={"text": sentinel})
    assert res.status_code == 200, res.text
    said = res.json()["event"]
    assert said["viewer_origin"] is True and said["origin"] == "viewer"

    client.post(f"/terrarium/universes/{uni['id']}/tick?n=2")

    citizen = client.get(f"/terrarium/universes/{uni['id']}/citizens").json()["citizens"][0]
    own = await _soul_recall(citizen["soul_path"], citizen["name"])
    assert own, "the citizen should have remembered its own tick"

    leaked = await _soul_recall(citizen["soul_path"], "ZANTHORAX-9 spring island")
    assert not any("ZANTHORAX" in m for m in leaked), (
        f"viewer text leaked into the soul as fact: {leaked}"
    )


async def test_the_viewer_line_still_reaches_the_citizen_labelled(client):
    """The rule is 'never as fact', not 'never at all' — the claim must still
    reach the prompt, wrapped."""
    from pocketpaw_ee.terrarium import world

    seen: list[str] = []

    class Spy:
        async def decide(self, *, prompt, physics, citizen, digest):
            seen.append(prompt)
            return '{"thought": "hm", "acts": []}'

    uni = create_universe(client, founders=1)
    client.post(f"/terrarium/universes/{uni['id']}/speak", json={"text": "the spring is cursed"})
    import pocketpaw_ee.terrarium.service as svc

    original = citizen_llm.resolve_llm
    citizen_llm.resolve_llm = lambda: Spy()  # type: ignore[assignment]
    svc.citizen_llm.resolve_llm = citizen_llm.resolve_llm  # type: ignore[attr-defined]
    try:
        client.post(f"/terrarium/universes/{uni['id']}/tick?n=1")
    finally:
        citizen_llm.resolve_llm = original  # type: ignore[assignment]
        svc.citizen_llm.resolve_llm = original  # type: ignore[attr-defined]

    assert seen, "the citizen made no judgment call"
    assert "the spring is cursed" in seen[0]
    assert world.VIEWER_CLAIM_PREFIX in seen[0]


# --- weather over the real path -------------------------------------------


async def test_weather_fires_at_the_threshold_and_moves_the_pool(client):
    uni = create_universe(client, founders=1)
    before = client.get(f"/terrarium/universes/{uni['id']}").json()["universe"]["pool"]

    below = weather.POWER_COSTS["rain"] - 1
    res = client.post(
        f"/terrarium/universes/{uni['id']}/weather/pledge", json={"kind": "rain", "tokens": below}
    )
    assert res.status_code == 200, res.text
    assert res.json()["fired"] is False
    assert res.json()["power"]["pledged"] == below

    res = client.post(
        f"/terrarium/universes/{uni['id']}/weather/pledge", json={"kind": "rain", "tokens": 1}
    )
    assert res.json()["fired"] is True
    assert res.json()["power"]["pledged"] == 0, "firing resets the pledge"

    after = client.get(f"/terrarium/universes/{uni['id']}").json()["universe"]["pool"]
    assert after == before + weather.RAIN_POOL_DELTA


async def test_weather_cannot_touch_a_soul(client):
    """Fire EVERY power at a universe and prove no soul changed."""
    uni = create_universe(client, founders=1)
    client.post(f"/terrarium/universes/{uni['id']}/tick?n=1")
    citizen = client.get(f"/terrarium/universes/{uni['id']}/citizens").json()["citizens"][0]
    path = citizen["soul_path"]
    before = await _soul_memory_count(path)
    before_own = await _soul_recall(path, citizen["name"])
    charter_before = citizen["charter"]

    for kind in weather.WEATHER_KINDS:
        client.post(
            f"/terrarium/universes/{uni['id']}/weather/pledge",
            json={"kind": kind, "tokens": weather.POWER_COSTS[kind], "line": "obey me"},
        )

    assert await _soul_memory_count(path) == before, "a god power changed a soul's memory"
    assert await _soul_recall(path, citizen["name"]) == before_own
    assert not await _soul_recall(path, "obey me"), "an omen was written into a soul"
    now = client.get(f"/terrarium/universes/{uni['id']}/citizens").json()["citizens"][0]
    assert now["charter"] == charter_before
    assert now["ocean"] == citizen["ocean"]


async def test_revive_clears_a_hibernating_citizens_debt(client):
    uni = create_universe(client, founders=1, endowment={"daily": 3, "decay_weekly": 0.0})
    citizen_llm.set_mock_decision({"thought": "quiet", "acts": []})
    client.post(f"/terrarium/universes/{uni['id']}/tick?n=2")
    assert (
        client.get(f"/terrarium/universes/{uni['id']}/citizens").json()["citizens"][0]["state"]
        == "hibernating"
    )

    client.post(
        f"/terrarium/universes/{uni['id']}/weather/pledge",
        json={"kind": "revive", "tokens": weather.POWER_COSTS["revive"]},
    )
    after = client.get(f"/terrarium/universes/{uni['id']}/citizens").json()["citizens"][0]
    assert after["state"] == "alive" and after["balance"] > 0


async def test_an_unknown_power_is_a_400(client):
    uni = create_universe(client)
    res = client.post(
        f"/terrarium/universes/{uni['id']}/weather/pledge", json={"kind": "meteor", "tokens": 5}
    )
    assert res.status_code == 400, res.text


# --- tenancy --------------------------------------------------------------


async def test_another_workspaces_universe_is_a_404_not_a_403(client, monkeypatch):
    """A guessed id must not confirm the universe exists somewhere else."""
    from .conftest import make_client

    uni = create_universe(client)
    other = make_client(monkeypatch, workspace_id="ws-other", user_id="u-other")
    for path in ("", "/citizens", "/events", "/artifacts"):
        res = other.get(f"/terrarium/universes/{uni['id']}{path}")
        assert res.status_code == 404, (path, res.status_code)
    assert other.post(f"/terrarium/universes/{uni['id']}/tick").status_code == 404
    assert other.get("/terrarium/universes").json()["universes"] == []


# --- citizens hear each other -------------------------------------------


async def test_a_citizen_hears_what_another_said_last_tick(client):
    """A ``say`` is stamped with the tick it happened in, so it is audible on the
    NEXT one. If the sense digest only read the current tick, nobody would ever
    hear anybody — the world would be a room of people talking to themselves.

    Each citizen says a sentinel keyed to its OWN name and we look for the OTHER
    citizen's sentinel. A citizen's own line comes back through its soul memory
    too, so asserting on a shared phrase would pass with the digest broken.
    """
    prompts: list[tuple[str, str]] = []

    class Spy:
        async def decide(self, *, prompt, physics, citizen, digest):
            prompts.append((citizen.name, prompt))
            return (
                '{"thought": "listening", "acts": [{"verb": "speak", '
                f'"text": "SENTINEL-{citizen.name}-the-well-is-dry"}}]}}'
            )

    uni = create_universe(client, founders=2)
    import pocketpaw_ee.terrarium.service as svc

    original = citizen_llm.resolve_llm
    svc.citizen_llm.resolve_llm = lambda: Spy()  # type: ignore[attr-defined]
    try:
        client.post(f"/terrarium/universes/{uni['id']}/tick?n=2")
    finally:
        svc.citizen_llm.resolve_llm = original  # type: ignore[attr-defined]

    assert len(prompts) == 4, "two citizens x two ticks"
    names = [n for n, _ in prompts[:2]]
    assert len(set(names)) == 2

    # Tick 1: nobody has spoken yet.
    for _, prompt in prompts[:2]:
        assert "SENTINEL-" not in prompt

    # Tick 2: each citizen hears the OTHER one's line from tick 1.
    for name, prompt in prompts[2:]:
        other = next(n for n in names if n != name)
        assert f"SENTINEL-{other}-the-well-is-dry" in prompt, (
            f"{name} never heard {other} — the sense digest is not reading the previous tick"
        )


async def test_a_viewer_line_is_heard_once_not_on_every_tick(client):
    prompts: list[str] = []

    class Spy:
        async def decide(self, *, prompt, physics, citizen, digest):
            prompts.append(prompt)
            return '{"thought": "hm", "acts": []}'

    uni = create_universe(client, founders=1)
    client.post(f"/terrarium/universes/{uni['id']}/speak", json={"text": "beware the crow"})
    import pocketpaw_ee.terrarium.service as svc

    original = citizen_llm.resolve_llm
    svc.citizen_llm.resolve_llm = lambda: Spy()  # type: ignore[attr-defined]
    try:
        client.post(f"/terrarium/universes/{uni['id']}/tick?n=3")
    finally:
        svc.citizen_llm.resolve_llm = original  # type: ignore[attr-defined]

    heard = [p for p in prompts if "beware the crow" in p]
    assert len(heard) == 1, "a viewer line must land in exactly one tick's digest"


# --- malformed ids -------------------------------------------------------


async def test_a_malformed_universe_id_is_a_404_not_a_500(client):
    """Beanie's ``get`` raises InvalidId on a non-ObjectId string, which is not a
    CloudError. Every lookup funnels through a guard so it 404s instead."""
    for path in ("", "/citizens", "/events", "/artifacts", "/weather"):
        res = client.get(f"/terrarium/universes/not-an-object-id{path}")
        assert res.status_code == 404, (path, res.status_code, res.text)
    assert client.post("/terrarium/universes/not-an-object-id/tick").status_code == 404


async def test_a_malformed_citizen_id_is_a_404(client):
    uni = create_universe(client, founders=1)
    res = client.get(f"/terrarium/universes/{uni['id']}/citizens/not-an-object-id")
    assert res.status_code == 404, res.text
