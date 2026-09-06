# tests/ee/terrarium/test_soul_seeding.py — citizens must be able to have
# children who differ from them.
#
# EvolutionConfig.immutable_traits defaults to ["personality", "core_values"],
# and the "personality" category gates all five OCEAN traits, so a soul born
# with the defaults forks into an EXACT clone however much drift is requested —
# silently. A universe seeded that way looks healthy and evolves never, which
# would quietly invalidate the whole lineage experiment. These tests pin the
# seeding path, not soul-protocol's behaviour.

from __future__ import annotations

import pytest

soul_protocol = pytest.importorskip("soul_protocol")

from pocketpaw_ee.terrarium import soul_link  # noqa: E402


@pytest.mark.skipif(
    not hasattr(soul_protocol.Soul, "fork"),
    reason=(
        "Soul.fork() ships in soul-protocol after the pinned published floor "
        "(>=0.3.1). This runs end to end once that release lands; until then the "
        "repair itself is pinned by the tests below."
    ),
)
@pytest.mark.asyncio
async def test_a_seeded_citizen_can_drift_when_it_forks(tmp_path):
    did = await soul_link.birth_soul(
        tmp_path / "vela.soul",
        name="Vela",
        role="the lawgiver",
        ocean={"O": 0.6, "C": 0.9, "E": 0.5, "A": 0.55, "N": 0.35},
        values=["fairness", "survival"],
        world_brief="You woke by a spring.",
    )
    assert did, "seeding a citizen must mint a soul"

    soul = await soul_protocol.Soul.awaken(str(tmp_path / "vela.soul"))
    assert "personality" not in soul._evolution.config.immutable_traits

    child = await soul.fork("Nim", drift=0.08)

    parent_ocean = soul._dna.personality
    child_ocean = child._dna.personality
    moved = any(
        getattr(child_ocean, trait) != getattr(parent_ocean, trait)
        for trait in (
            "openness",
            "conscientiousness",
            "extraversion",
            "agreeableness",
            "neuroticism",
        )
    )
    assert moved, "a citizen's child must be able to differ from it"


@pytest.mark.asyncio
async def test_unfreeze_is_repaired_even_when_birth_ignores_the_kwarg(tmp_path):
    """Older published soul-protocol WARNS on an unknown kwarg instead of raising,
    so passing ``evolution=`` is not enough on its own. The repair must stand alone."""

    class _Config:
        immutable_traits = ["personality", "core_values"]

    class _Evolution:
        config = _Config()

    class _Soul:
        _evolution = _Evolution()

    soul_link._unfreeze_personality(_Soul())

    assert _Soul._evolution.config.immutable_traits == ["core_values"]


def test_unfreeze_never_raises_on_an_unexpected_soul_shape():
    soul_link._unfreeze_personality(object())
