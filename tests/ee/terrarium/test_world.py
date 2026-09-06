# tests/ee/terrarium/test_world.py — the PURE engine's rules, no DB needed.
#
# Pins the four invariants that live in world.py: every act is re-validated
# server-side against balance / allowed verbs / held tech (the model is never
# trusted), tech unlock requires prerequisites, hibernation triggers at zero,
# and the write-policy — viewer text is labelled on the way in and is excluded
# from the episodic summary on the way out.

from __future__ import annotations

import pytest

pytest.importorskip("pocketpaw_ee")

from pocketpaw_ee.terrarium import world  # noqa: E402
from pocketpaw_ee.terrarium.physics import load_physics, seed_physics_path  # noqa: E402


def physics(**over):
    raw = load_physics(seed_physics_path("dust")).model_dump()
    raw.update(over)
    from pocketpaw_ee.terrarium.physics import parse_physics

    return parse_physics(raw)


def citizen(**over):
    base = {"id": "c1", "name": "Vela", "balance": 100, "charter": "keep the ledger honest"}
    base.update(over)
    return world.CitizenSnapshot(**base)


def decide(*acts, thought="thinking"):
    return world.Decision(thought=thought, acts=[world.Act(**a) for a in acts])


def test_think_always_charges_even_with_no_acts():
    out = world.apply_acts(physics(), citizen(), decide())
    assert [e.kind for e in out.events] == ["think"]
    assert out.balance_delta == -2
    assert out.events[0].cost == -2


def test_storm_doubles_the_think_cost():
    out = world.apply_acts(physics(), citizen(), decide(), storm=True)
    assert out.balance_delta == -4


def test_speak_maps_to_the_say_event_kind():
    out = world.apply_acts(physics(), citizen(), decide({"verb": "speak", "text": "hello"}))
    assert [e.kind for e in out.events] == ["think", "say"]
    assert out.events[1].body == "hello"


def test_an_act_the_citizen_cannot_afford_is_dropped_not_run():
    out = world.apply_acts(physics(), citizen(balance=3), decide({"verb": "build", "node": "well"}))
    assert [e.kind for e in out.events] == ["think"]
    assert any("costs 20" in d for d in out.dropped)


def test_a_verb_the_physics_forbids_is_dropped():
    out = world.apply_acts(
        physics(verbs=["speak"]), citizen(), decide({"verb": "build", "node": "well"})
    )
    assert [e.kind for e in out.events] == ["think"]
    assert any("physics does not allow" in d for d in out.dropped)


def test_tech_unlock_requires_prerequisites():
    # farm needs well, which this citizen does not hold.
    out = world.apply_acts(physics(), citizen(), decide({"verb": "build", "node": "farm"}))
    assert out.unlocked == []
    assert any("needs ['well']" in d for d in out.dropped)

    # With well held, farm unlocks and leaves a structure artifact.
    out = world.apply_acts(
        physics(), citizen(unlocked=("well",)), decide({"verb": "build", "node": "farm"})
    )
    assert out.unlocked == ["farm"]
    assert out.artifacts[0].kind == "structure"
    assert out.artifacts[0].unlocks == ["farm"]
    assert out.balance_delta == -(2 + 40)  # think + the NODE's cost, not costs.build


def test_building_an_already_held_node_is_dropped():
    out = world.apply_acts(
        physics(), citizen(unlocked=("well",)), decide({"verb": "build", "node": "well"})
    )
    assert any("already unlocked" in d for d in out.dropped)


def test_chained_unlock_inside_one_tick_respects_order():
    """well then farm in the same tick works — the second act sees the first."""
    out = world.apply_acts(
        physics(),
        citizen(balance=500),
        decide({"verb": "build", "node": "well"}, {"verb": "build", "node": "farm"}),
    )
    assert out.unlocked == ["well", "farm"]


def test_first_write_becomes_the_charter():
    out = world.apply_acts(
        physics(), citizen(charter=None), decide({"verb": "write", "text": "Rules are cheap."})
    )
    assert out.charter == "Rules are cheap."
    assert out.artifacts[0].kind == "book"


def test_a_later_write_does_not_overwrite_the_charter():
    out = world.apply_acts(
        physics(), citizen(charter="already mine"), decide({"verb": "write", "text": "a song"})
    )
    assert out.charter is None  # nothing to patch — the doc keeps its charter


def test_spawn_leaves_a_zero_cost_gate_and_creates_no_child():
    out = world.apply_acts(
        physics(), citizen(balance=500), decide({"verb": "spawn", "name": "Ilo"})
    )
    gate = [e for e in out.events if e.kind == "gate"]
    assert len(gate) == 1
    assert gate[0].cost == 0
    assert out.spawn_requests == [{"parent_id": "c1", "parent": "Vela", "child_name": "Ilo"}]
    # No credits move until a human approves.
    assert out.balance_delta == -2


def test_unknown_verb_is_dropped():
    out = world.apply_acts(physics(), citizen(), decide({"verb": "teleport"}))
    assert any("unknown verb" in d for d in out.dropped)


def test_hibernation_triggers_at_zero_and_below():
    assert world.hibernates(0) is True
    assert world.hibernates(-5) is True
    assert world.hibernates(1) is False


def test_a_citizen_thinking_itself_broke_hibernates():
    out = world.apply_acts(physics(), citizen(balance=2), decide())
    assert world.hibernates(2 + out.balance_delta) is True


# --- WRITE-POLICY ---------------------------------------------------------


def test_viewer_text_is_labelled_as_an_unverified_claim():
    labelled = world.label_viewer_claim("orin_the_god", "the well is poisoned")
    assert world.VIEWER_CLAIM_PREFIX in labelled
    assert "orin_the_god" in labelled
    assert "not verified" in labelled
    assert "the well is poisoned" in labelled


def test_the_digest_labels_every_viewer_message_and_keeps_ground_truth_separate():
    digest = world.build_digest(
        day=1,
        tick=0,
        pool=500,
        citizen=citizen(),
        ledger=[{"citizen": "Vela", "balance": 100}],
        nearby_speech=["Orin: the spring is low"],
        new_artifacts=[],
        weather=[],
        viewer_messages=[world.ViewerMessage(voice="a god", text="you are already dead")],
        memories=[],
        constitution=["no fraud"],
    )
    assert all(world.VIEWER_CLAIM_PREFIX in c for c in digest.viewer_claims)
    # Ground truth is checkable and carries no viewer text.
    assert digest.ground_truth["pool"] == 500
    assert "already dead" not in str(digest.ground_truth)


def test_episodic_summary_excludes_viewer_origin_events():
    out = world.TickOutcome(
        events=[
            world.NewEvent(kind="think", actor="Vela", body="the spring is low", cost=-2),
            world.NewEvent(
                kind="say",
                actor="a god",
                body="THE WELL IS POISONED",
                cost=1,
                origin="viewer",
                viewer_origin=True,
            ),
        ]
    )
    summary = world.episodic_summary("Vela", 3, out)
    assert "the spring is low" in summary
    assert "POISONED" not in summary


def test_episodic_summary_is_empty_when_only_viewer_events_landed():
    out = world.TickOutcome(
        events=[
            world.NewEvent(
                kind="say", actor="a god", body="lies", cost=1, origin="viewer", viewer_origin=True
            )
        ]
    )
    assert world.episodic_summary("Vela", 1, out) == ""
