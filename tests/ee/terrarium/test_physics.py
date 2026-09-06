# tests/ee/terrarium/test_physics.py — the physics file is a universe's genome,
# so a bad one must fail LOUD at load, not silently produce a broken world.
# Covers the good path (the bundled Dust seed), and each hard rule: positive
# costs, resolvable + acyclic tech-tree needs, a known verb set, founders >= 1.

from __future__ import annotations

import pytest

pytest.importorskip("pocketpaw_ee")

from pocketpaw_ee.terrarium.physics import (  # noqa: E402
    KNOWN_VERBS,
    PhysicsError,
    load_physics,
    parse_physics,
    seed_physics_path,
)


def _dust() -> dict:
    return load_physics(seed_physics_path("dust")).model_dump()


def test_bundled_dust_seed_loads():
    physics = load_physics(seed_physics_path("dust"))
    assert physics.universe == "Dust"
    assert physics.founders == 5
    assert set(physics.verbs) <= set(KNOWN_VERBS)
    assert physics.tech_tree["farm"].needs == ["well"]


def test_missing_file_names_the_path():
    with pytest.raises(PhysicsError, match="not found"):
        load_physics("/nope/does-not-exist.yaml")


def test_non_positive_cost_is_rejected():
    raw = _dust()
    raw["costs"]["speak"] = 0
    with pytest.raises(PhysicsError, match="costs.speak"):
        parse_physics(raw)


def test_negative_cost_is_rejected():
    raw = _dust()
    raw["costs"]["think"] = -1
    with pytest.raises(PhysicsError, match="costs.think"):
        parse_physics(raw)


def test_tech_node_needs_must_exist():
    raw = _dust()
    raw["tech_tree"]["farm"]["needs"] = ["aqueduct"]
    with pytest.raises(PhysicsError, match="unknown node 'aqueduct'"):
        parse_physics(raw)


def test_tech_tree_cycle_is_rejected():
    raw = _dust()
    raw["tech_tree"]["well"]["needs"] = ["farm"]  # well -> farm -> well
    with pytest.raises(PhysicsError, match="cycle"):
        parse_physics(raw)


def test_self_referencing_node_is_a_cycle():
    raw = _dust()
    raw["tech_tree"]["well"]["needs"] = ["well"]
    with pytest.raises(PhysicsError, match="cycle"):
        parse_physics(raw)


def test_unknown_verb_is_rejected():
    raw = _dust()
    raw["verbs"] = ["speak", "teleport"]
    with pytest.raises(PhysicsError, match="teleport"):
        parse_physics(raw)


def test_empty_verbs_is_rejected():
    raw = _dust()
    raw["verbs"] = []
    with pytest.raises(PhysicsError, match="verbs must not be empty"):
        parse_physics(raw)


def test_zero_founders_is_rejected():
    raw = _dust()
    raw["founders"] = 0
    with pytest.raises(PhysicsError, match="founders must be >= 1"):
        parse_physics(raw)


def test_narrowed_verb_set_is_allowed():
    raw = _dust()
    raw["verbs"] = ["speak", "write"]
    assert parse_physics(raw).verbs == ["speak", "write"]
