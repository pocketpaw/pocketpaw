# tests/cloud/test_belt_pulley_c4.py — Pulley block manifests as C4 components.
#
# ``orient.block_component`` over the seven real Pulley registry manifests, copied
# verbatim from qbtrix/pulley ``registry/blocks/<name>/manifest.json`` (dev
# 99e5186) into ``fixtures/pulley_blocks/``. Pins: the output is c4-gen's
# Component shape; every surface (provides, routes, endpoints, table prefix,
# emitted and consumed events) lands in the description after the block's own
# first sentence; ``deps`` become sync relationships whose targets are blocks of
# the same registry; and ``c4_lines`` reads a model built from the mapper, showing
# each block's own sentence.

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("pocketpaw_ee")

from pocketpaw_ee.cloud.belt.orient import block_component, c4_lines  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures" / "pulley_blocks"
MANIFESTS = {p.stem: json.loads(p.read_text()) for p in sorted(FIXTURES.glob("*.json"))}


def test_fixtures_are_the_seven_registry_blocks():
    assert sorted(MANIFESTS) == ["audit", "auth", "files", "hello", "notify", "org", "roles"]
    assert all(m["name"] == stem for stem, m in MANIFESTS.items())


def test_auth_maps_to_one_component_and_no_relationships():
    assert block_component(MANIFESTS["auth"]) == (
        {
            "id": "auth",
            "name": "auth",
            "description": (
                "Sessions, email+password sign-in, and one env-selected OAuth provider on "
                "Better Auth. Provides: auth.session, auth.user. Routes: /account, /sign-in, "
                "/sign-up. Endpoints: /api/auth/[...all]. Tables: auth_*. "
                "Emits: auth.user.created, auth.session.revoked."
            ),
            "technology": "Pulley core block 0.1.0",
        },
        [],
    )


def test_roles_depends_on_auth_and_org():
    _, rels = block_component(MANIFESTS["roles"])
    assert rels == [
        {
            "source": "roles",
            "target": "auth",
            "description": "depends on auth ^0.1.0",
            "style": "sync",
        },
        {
            "source": "roles",
            "target": "org",
            "description": "depends on org ^0.1.0",
            "style": "sync",
        },
    ]


@pytest.mark.parametrize("name", sorted(MANIFESTS))
def test_every_surface_lands_in_the_description(name):
    m = MANIFESTS[name]
    component, rels = block_component(m)
    assert set(component) == {"id", "name", "description", "technology"}
    assert component["id"] == component["name"] == name
    assert component["description"].startswith(m["description"] + " ")
    events = m.get("events") or {}
    surfaces = [
        *(m.get("provides") or []),
        *(r["path"] for r in m["routes"]),
        *(e["path"] for e in m.get("endpoints") or []),
        f"{m['tablePrefix']}*",
        *(events.get("emits") or []),
        *(events.get("consumes") or []),
    ]
    for surface in surfaces:
        assert surface in component["description"], surface
    assert [r["target"] for r in rels] == sorted(m.get("deps") or {})
    assert all(r["source"] == name and r["style"] == "sync" for r in rels)
    assert {r["target"] for r in rels} <= set(MANIFESTS)


def test_c4_lines_reads_a_model_built_from_the_blocks(tmp_path):
    pairs = [block_component(m) for m in MANIFESTS.values()]
    model = {
        "scope": "line",
        "model": {
            "people": [],
            "systems": [
                {
                    "id": "line",
                    "name": "Line",
                    "containers": [
                        {"id": "app", "name": "app", "components": [c for c, _ in pairs]}
                    ],
                }
            ],
            "relationships": [r for _, rels in pairs for r in rels],
        },
    }
    (tmp_path / "docs" / "c4").mkdir(parents=True)
    (tmp_path / "docs" / "c4" / "model.json").write_text(json.dumps(model))

    assert c4_lines(tmp_path) == [f"- app / {n}: {m['description']}" for n, m in MANIFESTS.items()]
    assert len(model["model"]["relationships"]) == 7
