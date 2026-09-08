# tests/ee/terrarium/test_concurrent_tick.py — the tick fans its judgment calls
# out through Foresight, and that must change nothing a viewer can see.
#
# Pins: a citizen whose transport dies costs the world one think charge and
# nothing else, while its neighbours act normally; the Journal a concurrent tick
# writes is byte-for-byte the list the serial ``for doc in citizens`` loop wrote
# for the same seed; a descendant's prompt says how it differs from its parent
# and a founder's does not; and ``has_fidelity`` reports whether a real .soul
# stands behind a citizen.

from __future__ import annotations

import pytest

pytest.importorskip("pocketpaw_ee")
pytest.importorskip("mongomock_motor")

from types import SimpleNamespace  # noqa: E402

from pocketpaw_ee.terrarium import llm as citizen_llm  # noqa: E402
from pocketpaw_ee.terrarium import service, world  # noqa: E402
from pocketpaw_ee.terrarium.persona import CitizenPersona  # noqa: E402
from pocketpaw_ee.terrarium.physics import load_physics, seed_physics_path  # noqa: E402

from .conftest import create_universe  # noqa: E402


class _OneCitizenIsDown:
    """A transport that fails for exactly one citizen and works for the rest."""

    def __init__(self, broken: str) -> None:
        self.broken = broken
        self.inner = citizen_llm.MockLlm()

    async def decide(self, *, prompt, physics, citizen, digest) -> str:
        if citizen.name == self.broken:
            raise RuntimeError("this citizen's transport is down")
        return await self.inner.decide(
            prompt=prompt, physics=physics, citizen=citizen, digest=digest
        )


async def test_one_broken_citizen_still_pays_and_leaves_the_others_alone(client, monkeypatch):
    """The degrade the serial loop gave: the citizen thinks, does nothing, and is
    still charged. Concurrency must not widen one failure into the tick's."""
    monkeypatch.setattr(
        citizen_llm, "resolve_llm", lambda **_kw: _OneCitizenIsDown("Vela"), raising=True
    )
    think = load_physics(seed_physics_path("dust")).costs.think

    uni = create_universe(client, founders=3)
    events = client.post(f"/terrarium/universes/{uni['id']}/tick?n=1").json()["events"]

    vela = [(e["kind"], e["cost"]) for e in events if e["actor"] == "Vela"]
    assert vela == [("think", -think)], "a dead transport must cost one think and no more"

    wrote = {e["actor"] for e in events if e["kind"] == "write"}
    assert wrote == {"Orin", "Sabe"}, "the other citizens' acts must land untouched"

    ledger = {
        r["citizen"]: r for r in client.get(f"/terrarium/universes/{uni['id']}").json()["ledger"]
    }
    assert ledger["Vela"]["balance"] == 120 - think
    assert ledger["Orin"]["balance"] == 120 - think - 4  # think + the charter write


# The Journal the SERIAL ``for doc in citizens`` loop wrote for this seed,
# captured by running this same case against service.py at 36d93edbc (the commit
# before the fan-out landed). Kinds, costs, count and order, which is everything
# a viewer pages through.
_SERIAL_JOURNAL = [
    ("think", "Vela", -2),
    ("write", "Vela", -4),
    ("think", "Orin", -2),
    ("write", "Orin", -4),
    ("think", "Sabe", -2),
    ("write", "Sabe", -4),
    ("think", "Vela", -2),
    ("say", "Vela", -1),
    ("build", "Vela", -20),
    ("think", "Orin", -2),
    ("say", "Orin", -1),
    ("build", "Orin", -20),
    ("think", "Sabe", -2),
    ("say", "Sabe", -1),
    ("build", "Sabe", -20),
]


async def test_a_concurrent_tick_writes_the_journal_the_serial_loop_wrote(client):
    uni = create_universe(client, founders=3)
    res = client.post(f"/terrarium/universes/{uni['id']}/tick?n=2")
    events = res.json()["events"]
    rows = [(e["kind"], e["actor"], e["cost"]) for e in events]
    assert rows == _SERIAL_JOURNAL

    # The actor comes off the doc and the body off the decision, so this is the
    # one row where a misaligned zip between ``rows`` and ``last_tick_actions``
    # shows: a charter reading "I am Sabe" filed under Vela.
    charters = [e for e in events if e["kind"] == "write"]
    assert charters and all(e["actor"] in e["body"] for e in charters)


async def test_a_descendants_prompt_says_how_it_differs_from_its_parent():
    physics = load_physics(seed_physics_path("dust"))
    snap = world.CitizenSnapshot(id="c1", name="Nim", role="", balance=100, state="alive")
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

    founder = citizen_llm.build_prompt(physics, snap, digest)
    assert "Compared with the parent" not in founder, "a founder has nobody to differ from"

    child = citizen_llm.build_prompt(
        physics, snap, digest, drift_line="slightly more open; noticeably less agreeable"
    )
    assert "Compared with the parent you came from, you are slightly more open" in child


async def test_lineage_drift_is_measured_in_drift_widths(client, beanie_test_db):
    """The prompt line's arithmetic: (child - parent) / DRIFT_WIDTH per trait,
    and no entry at all when the parent document is gone."""
    from pocketpaw_ee.terrarium.domain import CitizenDoc
    from pocketpaw_ee.terrarium.executor import DRIFT_WIDTH

    base = {"O": 0.5, "C": 0.5, "E": 0.5, "A": 0.5, "N": 0.5}
    parent = CitizenDoc(
        workspace="ws", universe_id="u1", name="Vela", did="did:soul:vela", ocean=dict(base)
    )
    await parent.insert()
    child = CitizenDoc(
        workspace="ws",
        universe_id="u1",
        name="Nim",
        did="did:soul:nim",
        parent_did="did:soul:vela",
        generation=2,
        ocean={**base, "O": 0.5 + DRIFT_WIDTH, "A": 0.5 - DRIFT_WIDTH / 2},
    )
    await child.insert()
    orphan = CitizenDoc(
        workspace="ws",
        universe_id="u1",
        name="Kell",
        did="did:soul:kell",
        parent_did="did:soul:nobody",
        ocean=dict(base),
    )
    await orphan.insert()

    drifts = await service._lineage_drifts("u1", [parent, child, orphan])

    assert str(parent.id) not in drifts, "a founder gets no line"
    assert str(orphan.id) not in drifts, "a lost parent gets no line"
    drift = drifts[str(child.id)]
    assert drift.openness == pytest.approx(1.0)
    assert drift.agreeableness == pytest.approx(-0.5)
    assert drift.conscientiousness == pytest.approx(0.0)
    assert "more open" in drift.as_prompt_block()


def test_has_fidelity_is_false_without_a_soul():
    def persona(soul_path):
        return CitizenPersona(SimpleNamespace(soul_path=soul_path), None, None, None, None)

    assert persona(None).has_fidelity is False
    assert persona("").has_fidelity is False
    assert persona("/souls/vela.soul").has_fidelity is True
