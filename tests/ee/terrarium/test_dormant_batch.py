# tests/ee/terrarium/test_dormant_batch.py — a world nobody is watching thinks
# in ONE Message Batch at half price, and lands the same world the synchronous
# path lands.
#
# Pins: two sweeps per dormant tick (file, then apply) with nothing written in
# between; the batched Journal matching the synchronous Journal row for row for
# the same seed, charters included; results keyed by custom_id and not by
# position; an errored entry degrading without being billed; an expired entry
# refused on its STATUS even when it carries text; an abandoned batch saying so
# and falling straight back to the live tick; a watched world never batching;
# the flag off meaning no batch exists; and the half rate landing only on the
# batched call.
#
# No network: ``_FakeBatch`` answers every entry with the deterministic MockLlm.

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

pytest.importorskip("pocketpaw_ee")
pytest.importorskip("mongomock_motor")

from pocketpaw_ee.terrarium import llm as citizen_llm  # noqa: E402
from pocketpaw_ee.terrarium import scheduler, service  # noqa: E402
from pocketpaw_ee.terrarium.domain import EventDoc, UniverseDoc  # noqa: E402

from .conftest import USER, WS, create_universe  # noqa: E402

# What a dust founder's model costs per batched entry, from the usage the fake
# reports: premium tier -> claude-opus-5 at $5/$25 per 1M, then halved.
_USAGE = {"input_tokens": 400, "output_tokens": 60}
_FULL_PER_CALL = (400 * 5.00 + 60 * 25.00) / 1_000_000
_BATCHED_PER_CALL = _FULL_PER_CALL / 2


class _FakeBatch:
    """The Batches endpoint with no network.

    ``results`` answers each entry with the same deterministic ``MockLlm`` the
    synchronous path runs, which is what makes the two Journals comparable. It
    hands the mapping back REVERSED on purpose: real results arrive in any order
    the provider likes, and a fake that answered in submission order would let a
    match-by-position bug pass green.
    """

    def __init__(self) -> None:
        self.submitted: list[list[citizen_llm.BatchEntry]] = []
        self.is_ended = True
        self.overrides: dict[str, citizen_llm.BatchResult] = {}

    async def submit(self, entries) -> str:
        self.submitted.append(list(entries))
        return f"b-{len(self.submitted)}"

    async def ended(self, batch_id: str) -> bool:
        return self.is_ended

    async def results(self, batch_id: str) -> dict[str, citizen_llm.BatchResult]:
        mock = citizen_llm.MockLlm()
        out: dict[str, citizen_llm.BatchResult] = {}
        for entry in self.submitted[-1]:
            override = self.overrides.get(entry.citizen.name)
            if override is not None:
                out[entry.custom_id] = override
                continue
            text = await mock.decide(
                prompt=entry.prefix + entry.suffix,
                physics=entry.physics,
                citizen=entry.citizen,
                digest=entry.digest,
            )
            out[entry.custom_id] = citizen_llm.BatchResult("succeeded", text, dict(_USAGE))
        return dict(reversed(list(out.items())))


@pytest.fixture
def fake_batch(monkeypatch):
    """A stubbed batch transport with the flag ON."""
    fake = _FakeBatch()
    monkeypatch.setattr(citizen_llm, "resolve_batch_llm", lambda **_kw: fake, raising=True)
    monkeypatch.setenv("TERRARIUM_BATCH_DORMANT", "1")
    return fake


def says(text: str) -> citizen_llm.BatchResult:
    """A decision body whose one act is a speak — distinctive, cheap, legal."""
    return json.dumps({"thought": "hm", "acts": [{"verb": "speak", "text": text}]})


async def _journal(universe_id: str, since: int = 0) -> list[tuple[str, str, int]]:
    """The Journal read STRAIGHT off the docs. Never over /events — that route
    stamps ``last_viewed_at`` and would wake the world mid-test."""
    docs = (
        await EventDoc.find(EventDoc.universe_id == universe_id, EventDoc.seq > since)
        .sort("+seq")
        .to_list()
    )
    return [(d.kind, d.actor, d.cost) for d in docs]


async def _sleep(universe_id: str, *, now: datetime) -> UniverseDoc:
    """Nobody has watched this world for two days and it is due a dormant tick."""
    doc = await UniverseDoc.get(universe_id)
    doc.last_viewed_at = now - timedelta(days=2)
    doc.last_tick_at = now - timedelta(seconds=7200)
    await doc.save()
    return doc


async def test_the_first_sweep_only_files_the_batch(client, fake_batch):
    """Phase one asks and stops. Mutation: submitting and applying in one sweep
    would write the tick's rows here."""
    uni = create_universe(client, founders=2)
    before = (await UniverseDoc.get(uni["id"])).seq
    now = datetime.now(UTC)
    await _sleep(uni["id"], now=now)

    assert await scheduler.run_scheduler_tick(now=lambda: now) == [], "nothing has ticked yet"
    assert len(fake_batch.submitted) == 1
    assert len(fake_batch.submitted[0]) == 2, "one entry per living citizen"

    doc = await UniverseDoc.get(uni["id"])
    assert doc.batch_id == "b-1"
    assert doc.batch_tick == 0 and doc.batch_at is not None
    assert doc.tick == 0, "the world has not moved"
    assert await _journal(uni["id"], since=before) == [], "and nothing is in the Journal"


async def test_the_batch_path_lands_the_journal_the_sync_path_lands(client, fake_batch):
    """The whole point: two dormant ticks through the batch must be row-for-row
    what two watched ticks wrote for the same seed.

    This pins KINDS, ACTORS and COSTS, so it catches a dropped or degraded batch
    but NOT a misaligned one — a reversed result set writes the same tuples under
    the same actors. The charter assert below is the alignment check here, and
    ``test_results_in_any_order_land_on_the_right_citizens`` is the test that
    names the match-by-position mutation.
    """
    watched = create_universe(client, founders=3)
    first = (await UniverseDoc.get(watched["id"])).seq
    client.post(f"/terrarium/universes/{watched['id']}/tick?n=2")
    sync_rows = await _journal(watched["id"], since=first)
    # Archived so the sweeps below leave it alone.
    doc = await UniverseDoc.get(watched["id"])
    doc.status = "archived"
    await doc.save()

    sleeping = create_universe(client, founders=3)
    since = (await UniverseDoc.get(sleeping["id"])).seq
    n1 = datetime.now(UTC)
    await _sleep(sleeping["id"], now=n1)
    await scheduler.run_scheduler_tick(now=lambda: n1)  # file
    assert await scheduler.run_scheduler_tick(now=lambda: n1) == [sleeping["id"]]  # apply
    # The dormant cadence is one tick per world-day, so the second tick is due
    # two wall-clock hours on. ``_apply_batch`` stamps last_tick_at from the real
    # clock, which is why this is now + 2h and not a frozen instant.
    n2 = n1 + timedelta(hours=2)
    await scheduler.run_scheduler_tick(now=lambda: n2)  # file
    await scheduler.run_scheduler_tick(now=lambda: n2)  # apply

    batch_rows = await _journal(sleeping["id"], since=since)
    assert batch_rows == sync_rows
    assert len(fake_batch.submitted) == 2, "one batch per tick, never one per citizen"

    events = await EventDoc.find(EventDoc.universe_id == sleeping["id"]).sort("+seq").to_list()
    charters = [e for e in events if e.kind == "write"]
    assert charters and all(e.actor in e.body for e in charters), "a charter names its own author"


async def test_results_in_any_order_land_on_the_right_citizens(client, fake_batch):
    """Mutation: ``ordered = list(landed.values())`` sends Sabe's line to Vela."""
    uni = create_universe(client, founders=3)
    since = (await UniverseDoc.get(uni["id"])).seq
    fake_batch.overrides = {
        name: citizen_llm.BatchResult("succeeded", says(f"this is {name}"), dict(_USAGE))
        for name in ("Vela", "Orin", "Sabe")
    }
    now = datetime.now(UTC)
    await _sleep(uni["id"], now=now)
    await scheduler.run_scheduler_tick(now=lambda: now)
    await scheduler.run_scheduler_tick(now=lambda: now)

    said = [
        (e.actor, e.body)
        for e in await EventDoc.find(
            EventDoc.universe_id == uni["id"], EventDoc.seq > since
        ).to_list()
        if e.kind == "say"
    ]
    assert len(said) == 3
    assert all(body == f"this is {actor}" for actor, body in said)


async def test_an_errored_entry_thinks_and_is_not_billed(client, fake_batch):
    """The synchronous degrade, exactly: the citizen pays the in-world think and
    does nothing. The MODEL bill does not move — no call was answered."""
    uni = create_universe(client, founders=2)
    since = (await UniverseDoc.get(uni["id"])).seq
    fake_batch.overrides = {"Vela": citizen_llm.BatchResult("errored")}
    now = datetime.now(UTC)
    await _sleep(uni["id"], now=now)
    await scheduler.run_scheduler_tick(now=lambda: now)
    await scheduler.run_scheduler_tick(now=lambda: now)

    rows = await _journal(uni["id"], since=since)
    assert [r for r in rows if r[1] == "Vela"] == [("think", "Vela", -2)]
    assert ("write", "Orin", -4) in rows, "the neighbour is untouched"

    doc = await UniverseDoc.get(uni["id"])
    assert doc.cost["calls"] == 1 and doc.cost["batch_calls"] == 1, "one entry, one charge"
    citizens = client.get(f"/terrarium/universes/{uni['id']}/citizens").json()["citizens"]
    vela = next(c for c in citizens if c["name"] == "Vela")
    assert vela["balance"] == 120 - 2, "the in-world think is still paid"


async def test_an_expired_entry_is_refused_on_its_status_not_its_text(client, fake_batch):
    """Mutation: treating ``expired`` as success. The status decides, never the
    presence of a body — an expired entry carrying a decision is not a decision.
    """
    uni = create_universe(client, founders=2)
    since = (await UniverseDoc.get(uni["id"])).seq
    fake_batch.overrides = {
        "Vela": citizen_llm.BatchResult("expired", says("the expired entry spoke"), dict(_USAGE))
    }
    now = datetime.now(UTC)
    await _sleep(uni["id"], now=now)
    await scheduler.run_scheduler_tick(now=lambda: now)
    await scheduler.run_scheduler_tick(now=lambda: now)

    events = await EventDoc.find(EventDoc.universe_id == uni["id"], EventDoc.seq > since).to_list()
    assert not any("the expired entry spoke" in e.body for e in events)
    assert [(e.kind, e.cost) for e in events if e.actor == "Vela"] == [("think", -2)]
    doc = await UniverseDoc.get(uni["id"])
    assert doc.cost["batch_calls"] == 1, "only the entry that succeeded was billed"


async def test_an_abandoned_batch_says_so_and_the_world_ticks_live(client, fake_batch):
    """A batch that outlived its window is written off IN THE JOURNAL, and the
    same sweep falls straight through to the synchronous tick."""
    uni = create_universe(client, founders=2)
    since = (await UniverseDoc.get(uni["id"])).seq
    now = datetime.now(UTC)
    await _sleep(uni["id"], now=now)
    await scheduler.run_scheduler_tick(now=lambda: now)

    doc = await UniverseDoc.get(uni["id"])
    doc.batch_at = datetime.now(UTC) - timedelta(hours=25)
    await doc.save()
    assert await scheduler.run_scheduler_tick(now=lambda: now) == [uni["id"]]

    events = (
        await EventDoc.find(EventDoc.universe_id == uni["id"], EventDoc.seq > since)
        .sort("+seq")
        .to_list()
    )
    said = events[0]
    assert said.kind == "batch" and said.actor == "the clock" and said.cost == 0
    assert said.data["batch_id"] == "b-1"
    assert [e.kind for e in events[1:3]] == ["think", "write"], "and the live tick follows it"

    doc = await UniverseDoc.get(uni["id"])
    assert doc.batch_id is None and doc.batch_at is None
    assert len(fake_batch.submitted) == 1, "the fall-through does not file another"


async def test_a_watched_world_never_batches(client, fake_batch):
    """Somebody is looking at this one, so it keeps its synchronous feed."""
    uni = create_universe(client, founders=2)
    since = (await UniverseDoc.get(uni["id"])).seq
    now = datetime.now(UTC)
    doc = await UniverseDoc.get(uni["id"])
    doc.last_viewed_at = now
    doc.last_tick_at = now - timedelta(seconds=400)  # 3600/12 = 300s per tick
    await doc.save()

    assert await scheduler.run_scheduler_tick(now=lambda: now) == [uni["id"]]
    assert fake_batch.submitted == [], "a watched world files nothing"
    assert (await UniverseDoc.get(uni["id"])).batch_id is None
    assert await _journal(uni["id"], since=since), "and its Journal moved now, not later"


async def test_the_flag_off_means_no_batch_is_ever_filed(client, monkeypatch):
    """Mutation: ignoring TERRARIUM_BATCH_DORMANT. Off is today's behaviour."""
    fake = _FakeBatch()
    monkeypatch.setattr(citizen_llm, "resolve_batch_llm", lambda **_kw: fake, raising=True)
    uni = create_universe(client, founders=2)
    since = (await UniverseDoc.get(uni["id"])).seq
    now = datetime.now(UTC)
    await _sleep(uni["id"], now=now)

    assert await scheduler.run_scheduler_tick(now=lambda: now) == [uni["id"]]
    assert fake.submitted == []
    doc = await UniverseDoc.get(uni["id"])
    assert doc.batch_id is None and doc.tick == 1
    assert not doc.cost.get("batch_calls"), "and nothing was priced at half"
    assert await _journal(uni["id"], since=since), "the dormant world ticked synchronously"


async def test_a_paused_worlds_open_batch_is_left_where_it_is(client, fake_batch):
    """Pause is a kill switch, not a discard: the batch waits for the resume."""
    uni = create_universe(client, founders=2)
    since = (await UniverseDoc.get(uni["id"])).seq
    now = datetime.now(UTC)
    await _sleep(uni["id"], now=now)
    await scheduler.run_scheduler_tick(now=lambda: now)

    doc = await UniverseDoc.get(uni["id"])
    doc.status = "paused"
    await doc.save()

    assert await service.scheduler_sweep(now) == [], "the sweep never selects it"
    assert await service.dormant_batch_step(WS, USER, uni["id"]) == "waiting"
    doc = await UniverseDoc.get(uni["id"])
    assert doc.batch_id == "b-1" and doc.tick == 0
    assert await _journal(uni["id"], since=since) == []


async def test_a_batched_tick_is_priced_at_half_and_a_watched_one_is_not(client, fake_batch):
    """The saving, on the universe's own cost record."""
    uni = create_universe(client, founders=2)
    now = datetime.now(UTC)
    await _sleep(uni["id"], now=now)
    await scheduler.run_scheduler_tick(now=lambda: now)
    await scheduler.run_scheduler_tick(now=lambda: now)

    doc = await UniverseDoc.get(uni["id"])
    assert doc.cost["calls"] == 2 and doc.cost["batch_calls"] == 2
    assert doc.cost["cost_usd"] == pytest.approx(round(2 * _BATCHED_PER_CALL, 6))

    watched = create_universe(client, founders=2)
    client.post(f"/terrarium/universes/{watched['id']}/tick?n=1")
    assert (await UniverseDoc.get(watched["id"])).cost["batch_calls"] == 0


def test_the_half_rate_lands_on_the_batched_call_only():
    """Mutation: discounting the whole meter instead of its batched subset. The
    live figure is asserted against the arithmetic, not against the batched one.
    """
    usage = {"input_tokens": 1000, "output_tokens": 100}

    live = citizen_llm.CostMeter("claude-sonnet-5")
    live.record("", "", dict(usage))
    assert live.cost_usd == pytest.approx((1000 * 2.00 + 100 * 10.00) / 1_000_000)
    assert live.cost_usd == pytest.approx(0.003)
    assert live.summary()["batch_calls"] == 0

    batched = citizen_llm.CostMeter("claude-sonnet-5")
    batched.record("", "", dict(usage), batch=True)
    assert batched.cost_usd == pytest.approx(0.0015)
    assert batched.summary()["batch_calls"] == 1

    both = citizen_llm.CostMeter("claude-sonnet-5")
    both.record("", "", dict(usage))
    both.record("", "", dict(usage), batch=True)
    assert both.cost_usd == pytest.approx(0.0045)
    assert both.drain()["batch_calls"] == 1
    assert both.cost_usd == 0.0, "drain zeroes the batched counters too"
