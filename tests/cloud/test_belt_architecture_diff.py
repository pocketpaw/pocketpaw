# tests/cloud/test_belt_architecture_diff.py — ``architecture_diff``: what changed
# between two C4 models, for the viewer's Compare and the history's counts.
#
# Pure, on the real model shape (``source`` / ``target`` relationships, nested
# ``containers`` / ``components``). Pins added / removed / changed by id across
# every level, that nested lists are not "fields", a move reported as
# ``parent``, a relationship matched by its ends (and ``from`` / ``to``
# accepted, a second one between the same ends counted on its own), the
# topmost removed element carried with its parent and pruned subtree, and the
# counts.

from __future__ import annotations

import pytest

pytest.importorskip("pocketpaw_ee")

from pocketpaw_ee.cloud.belt.architecture_diff import diff_counts, diff_models  # noqa: E402


def model(**over) -> dict:
    base = {
        "people": [{"id": "dev", "name": "Dev"}],
        "systems": [
            {
                "id": "app",
                "name": "App",
                "containers": [
                    {
                        "id": "web",
                        "name": "Web",
                        "technology": "Svelte",
                        "components": [{"id": "ui", "name": "UI"}, {"id": "auth", "name": "Auth"}],
                    },
                    {
                        "id": "api",
                        "name": "API",
                        "components": [{"id": "routes", "name": "Routes"}],
                    },
                ],
            }
        ],
        "relationships": [
            {"source": "dev", "target": "web", "description": "uses"},
            {"source": "web", "target": "api", "description": "calls"},
        ],
    }
    return {**base, **over}


def test_identical_models_have_an_empty_diff():
    d = diff_models(model(), model())
    assert (d["added"], d["removed"], d["changed"], d["removed_elements"]) == ([], [], [], [])
    assert d["relationships"] == {"added": [], "removed": []}
    assert diff_counts(d) == {"added": 0, "removed": 0, "changed": 0}


def test_added_removed_and_changed_by_id_at_every_level():
    new = model()
    web, api = new["systems"][0]["containers"]
    web["description"], web["technology"] = "The site", "SvelteKit"
    web["components"] = [{"id": "ui", "name": "UI"}, {"id": "cart", "name": "Cart"}]
    new["people"] = [{"id": "dev", "name": "Dev"}, {"id": "ops", "name": "Ops"}]
    d = diff_models(model(), new)
    assert sorted(d["added"]) == ["cart", "ops"]
    assert d["removed"] == ["auth"]
    assert d["changed"] == [{"id": "web", "fields": ["description", "technology"]}]


def test_a_child_list_change_is_not_a_field_change_but_a_move_is_parent():
    new = model()
    web, api = new["systems"][0]["containers"]
    auth = web["components"].pop()
    api["components"].append(auth)  # auth moves from web to api
    d = diff_models(model(), new)
    assert d["changed"] == [{"id": "auth", "fields": ["parent"]}]
    assert (d["added"], d["removed"]) == ([], [])


def test_removed_elements_carry_the_topmost_one_with_its_parent_and_pruned_subtree():
    new = model()
    web, api = new["systems"][0]["containers"]
    keep = web["components"][0]
    new["systems"][0]["containers"] = [api]
    api["components"].append(keep)  # ui survives under api; web and auth are gone
    d = diff_models(model(), new)
    assert sorted(d["removed"]) == ["auth", "web"]
    assert d["removed_elements"] == [
        {
            "parent": "app",
            "in": "containers",
            "element": {
                "id": "web",
                "name": "Web",
                "technology": "Svelte",
                "components": [{"id": "auth", "name": "Auth"}],
            },
        }
    ]


def test_a_removed_top_level_element_has_no_parent():
    d = diff_models(model(), model(people=[]))
    assert d["removed_elements"] == [
        {"parent": None, "in": "people", "element": {"id": "dev", "name": "Dev"}}
    ]


def test_relationships_match_by_their_ends_and_accept_from_to():
    new = model()
    new["relationships"] = [
        {"from": "dev", "to": "web", "label": "relabelled"},  # same ends: not added or removed
        {"source": "api", "target": "routes", "description": "mounts"},
    ]
    d = diff_models(model(), new)
    assert d["relationships"] == {
        "added": [{"source": "api", "target": "routes", "description": "mounts"}],
        "removed": [{"source": "web", "target": "api", "description": "calls"}],
    }


def test_a_second_relationship_between_the_same_ends_is_its_own():
    twice = [
        {"source": "web", "target": "api", "description": "calls"},
        {"source": "web", "target": "api", "description": "streams"},
    ]
    added = diff_models(model(), model(relationships=model()["relationships"] + twice[1:]))
    assert added["relationships"] == {"added": [twice[1]], "removed": []}
    gone = diff_models(model(relationships=model()["relationships"] + twice[1:]), model())
    assert gone["relationships"] == {"added": [], "removed": [twice[1]]}
    relabel = model(relationships=[model()["relationships"][0], {**twice[0], "description": "x"}])
    assert diff_models(model(), relabel)["relationships"] == {"added": [], "removed": []}


def test_counts_add_relationships_to_elements():
    new = model(people=[])
    new["relationships"] = [{"source": "web", "target": "api"}, {"source": "api", "target": "web"}]
    d = diff_models(model(), new)
    assert diff_counts(d) == {"added": 1, "removed": 2, "changed": 0}  # +api->web; -dev, -dev->web


def test_an_empty_old_model_counts_everything_as_added():
    d = diff_models({}, model())
    assert sorted(d["added"]) == ["api", "app", "auth", "dev", "routes", "ui", "web"]
    assert diff_counts(d) == {"added": 9, "removed": 0, "changed": 0}
