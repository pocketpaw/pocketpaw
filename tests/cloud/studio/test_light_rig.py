# tests/cloud/studio/test_light_rig.py — manual lamp placement → lighting prose.
#
# Created 2026-09-03 (studio-light-rig).
#
# The rig dialog lets a user drag three lamps around a stage. A model cannot read
# coordinates, so the whole feature rests on the quantizer in `light_rig`, and
# these tests pin the parts of it that would fail quietly:
#
#   * The compass has to be right. A key dragged upper-left that describes itself
#     as "from the right" produces a plausible image lit from the wrong side, and
#     nothing anywhere would flag it.
#   * The bands have to be WIDE. If a two-pixel drag flips the description, the
#     preview and the prompt disagree and the control feels broken.
#   * Contrast is derived from the key:fill ratio, not asked for. It is the fact
#     that decides whether the picture reads gentle or dramatic.
#   * A rig REPLACES the pick-list lighting. Two lighting sentences in one prompt
#     is the same contradiction the style override exists to prevent.

from __future__ import annotations

import pytest
from pocketpaw_ee.cloud.studio import light_rig, schemas, service


def lamp(**kw: object) -> schemas.RigLamp:
    base: dict[str, object] = {"enabled": True, "strength": 70, "warmth": 50, "quality": "soft"}
    base.update(kw)
    return schemas.RigLamp.model_validate(base)


def rig(**kw: object) -> schemas.LightRig:
    base: dict[str, object] = {"enabled": True, "ambience": "day"}
    base.update(kw)
    return schemas.LightRig.model_validate(base)


# ── Off means silent ────────────────────────────────────────────────────────


def test_a_disabled_rig_says_nothing() -> None:
    assert light_rig.compose_light_rig_phrase(schemas.LightRig()) == ""
    assert light_rig.compose_light_rig_phrase(None) == ""


def test_an_enabled_rig_with_no_lamps_on_says_nothing() -> None:
    """Enabling the rig is not itself a lighting instruction."""
    assert light_rig.compose_light_rig_phrase(rig()) == ""


def test_a_lamp_at_zero_strength_is_the_same_as_off() -> None:
    assert light_rig.compose_light_rig_phrase(rig(key=lamp(x=-0.6, y=0.5, strength=0))) == ""


# ── The compass ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("x", "y", "expected"),
    [
        (0.8, 0.0, "from the right"),
        (0.6, 0.6, "from the upper right"),
        (0.0, 0.8, "from directly above"),
        (-0.6, 0.6, "from the upper left"),
        (-0.8, 0.0, "from the left"),
        (-0.6, -0.6, "from below left"),
        (0.0, -0.8, "from below"),
        (0.6, -0.6, "from below right"),
    ],
)
def test_every_compass_point_reads_correctly(x: float, y: float, expected: str) -> None:
    """+y is UP. Getting this inverted would light every image from the opposite
    side of the frame from the one the user dragged to — a bug that produces
    perfectly plausible images and so would survive a casual look."""
    assert expected in light_rig.compose_light_rig_phrase(rig(key=lamp(x=x, y=y)))


def test_a_lamp_on_top_of_the_subject_is_frontal_not_a_random_direction() -> None:
    """atan2 of a near-zero vector returns a direction, and it would be noise."""
    assert "head-on from the camera position" in light_rig.compose_light_rig_phrase(
        rig(key=lamp(x=0.01, y=-0.01))
    )


def test_direction_bands_are_wide_enough_to_be_draggable() -> None:
    """A small nudge must not flip the direction, or the control reads as jittery.

    Only the DIRECTION is asserted. Every bucketed control has boundaries — these
    two points sit either side of the 0.85 "raking" threshold, so their throw
    clauses legitimately differ — but 45-degree compass bands mean the side the
    light comes from is stable across any drag a hand would call small.
    """
    for x, y in ((-0.60, 0.60), (-0.65, 0.55), (-0.5, 0.7), (-0.7, 0.5)):
        assert "from the upper left" in light_rig.compose_light_rig_phrase(rig(key=lamp(x=x, y=y)))


# ── Distance from the subject ───────────────────────────────────────────────


def test_a_lamp_near_the_subject_reads_as_flat_frontal_light() -> None:
    assert "almost flat" in light_rig.compose_light_rig_phrase(rig(key=lamp(x=0.2, y=0.15)))


def test_a_lamp_at_the_edge_reads_as_raking() -> None:
    """Same angle as a frontal lamp, completely different picture — which is why
    distance earns its own clause instead of being folded into the direction."""
    assert "raking in from the very edge" in light_rig.compose_light_rig_phrase(
        rig(key=lamp(x=0.94, y=0.1))
    )


def test_the_same_angle_at_two_distances_describes_two_different_pictures() -> None:
    near = light_rig.compose_light_rig_phrase(rig(key=lamp(x=0.14, y=0.14)))
    far = light_rig.compose_light_rig_phrase(rig(key=lamp(x=0.7, y=0.7)))
    assert near != far
    assert "from the upper right" in near or "head-on" in near


# ── Contrast is derived, not asked for ──────────────────────────────────────


def test_a_strong_key_against_a_weak_fill_is_high_contrast() -> None:
    out = light_rig.compose_light_rig_phrase(
        rig(key=lamp(x=-0.6, y=0.5, strength=95), fill=lamp(x=0.5, strength=15))
    )
    assert "very high contrast" in out.lower()


def test_a_balanced_key_and_fill_is_low_contrast() -> None:
    out = light_rig.compose_light_rig_phrase(
        rig(key=lamp(x=-0.6, y=0.5, strength=80), fill=lamp(x=0.5, strength=75))
    )
    assert "low contrast" in out.lower()


def test_a_key_with_no_fill_says_the_shadow_side_goes_dark() -> None:
    """ "Fill off" and "fill at zero" are the same picture, and the model has to be
    told what that means rather than left to infer it from an absence."""
    out = light_rig.compose_light_rig_phrase(rig(key=lamp(x=-0.6, y=0.5, strength=85)))
    assert "unfilled" in out.lower()


def test_contrast_is_not_claimed_when_there_is_no_key() -> None:
    out = light_rig.compose_light_rig_phrase(rig(rim=lamp(x=0.9, y=0.3, strength=80)))
    assert "contrast" not in out.lower()
    assert "rim light" in out


# ── Lamp qualities ──────────────────────────────────────────────────────────


def test_warmth_and_quality_reach_the_sentence() -> None:
    out = light_rig.compose_light_rig_phrase(
        rig(key=lamp(x=-0.6, y=0.5, warmth=90, quality="hard"))
    )
    assert "hard-edged" in out
    assert "warm" in out


def test_an_explicit_colour_replaces_the_warmth_word_rather_than_joining_it() -> None:
    """Warmth is the white-balance axis; a gelled lamp has a colour instead. Both
    at once produced "cold blue-toned saturated magenta" — two colours in one
    breath, and the model gets to pick."""
    out = light_rig.compose_light_rig_phrase(
        rig(key=lamp(x=-0.6, y=0.5, warmth=10, colour="#FF3DA6"))
    )
    assert "magenta" in out
    assert "blue-toned" not in out


def test_a_near_white_lamp_is_not_described_as_coloured() -> None:
    """Naming "#FDFDFD" as a colour spends tokens to say nothing."""
    assert light_rig.colour_name("#FDFDFD") == ""
    assert light_rig.colour_name(None) == ""
    assert light_rig.colour_name("not-a-colour") == ""


@pytest.mark.parametrize(
    ("hex_colour", "expected"),
    [
        ("#FF0000", "red"),
        ("#FF8C00", "orange"),
        ("#2BD4FF", "teal"),
        ("#2B5CFF", "blue"),
        ("#B36BE8", "violet"),
    ],
)
def test_colours_are_named_the_way_a_gaffer_would(hex_colour: str, expected: str) -> None:
    assert expected in light_rig.colour_name(hex_colour)


def test_shorthand_hex_is_accepted() -> None:
    assert "red" in light_rig.colour_name("#f00")


# ── Ambience and environment ────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("ambience", "needle"),
    [("day", "daylight"), ("dusk", "dusk"), ("night", "night")],
)
def test_ambience_reaches_the_sentence(ambience: str, needle: str) -> None:
    out = light_rig.compose_light_rig_phrase(rig(ambience=ambience, key=lamp(x=-0.6, y=0.5)))
    assert needle in out


def test_an_hdri_contributes_its_colour_by_name() -> None:
    """The panorama itself never reaches the model. What it can contribute is the
    ambient colour it implies, said in words."""
    out = light_rig.compose_light_rig_phrase(
        rig(
            key=lamp(x=-0.6, y=0.5),
            environment={"name": "sunset_4k.hdr", "dominantTone": "warm amber"},
        )
    )
    assert "warm amber" in out
    assert "sunset_4k.hdr" not in out


# ── Precedence over the pick-lists ──────────────────────────────────────────


def test_an_enabled_rig_replaces_the_lighting_pick_lists() -> None:
    """Both answer "how is this lit". Emitting both would leave the model
    arbitrating between two lighting sentences.

    Mutation that must break this: concatenate the two clauses instead of
    letting the rig win.
    """
    out = service.compose_prompt(
        "A detective",
        None,
        None,
        schemas.LightingSpec(setup="high-key", source="overcast"),
        rig(key=lamp(x=-0.6, y=0.5, strength=90)),
    )
    assert "key light from the upper left" in out
    assert "high-key" not in out
    assert "overcast" not in out
    assert out.count("Lit with") == 1


def test_a_disabled_rig_leaves_the_pick_lists_in_charge() -> None:
    out = service.compose_prompt(
        "A detective", None, None, schemas.LightingSpec(setup="high-key"), schemas.LightRig()
    )
    assert "high-key" in out


def test_an_enabled_but_empty_rig_falls_back_rather_than_going_dark() -> None:
    """A rig toggled on with every lamp off must not silently swallow the picks
    the user made on the other tab."""
    out = service.compose_prompt(
        "A detective", None, None, schemas.LightingSpec(setup="high-key"), rig()
    )
    assert "high-key" in out


def test_a_rig_still_silences_a_curated_style_lighting_sentence() -> None:
    """The rig is an explicit lighting choice, so it wins over Neo-Noir's
    prescribed lighting exactly as a pick-list choice does."""
    out = service.compose_prompt(
        "A detective",
        "neo-noir-thriller",
        None,
        None,
        rig(key=lamp(x=-0.6, y=0.5, strength=90)),
    )
    assert "venetian" not in out
    assert "key light" in out
    assert "Camera: Dutch angles" in out


def test_the_rig_sits_after_the_camera_clause() -> None:
    out = service.compose_prompt(
        "A detective",
        None,
        schemas.CameraSpec(angle="low-angle"),
        None,
        rig(key=lamp(x=-0.6, y=0.5)),
    )
    assert out.index("Framed from a low angle") < out.index("Lit with")


# ── Presets ─────────────────────────────────────────────────────────────────


def test_every_preset_validates_and_produces_a_sentence() -> None:
    for preset in light_rig.LIGHT_RIG_PRESETS:
        model = schemas.LightRigPreset.model_validate(preset)
        rendered = light_rig.compose_light_rig_phrase(model.rig)
        assert rendered.startswith("Lit with "), model.id
        assert rendered.endswith("."), model.id


def test_preset_ids_are_unique() -> None:
    ids = [p["id"] for p in light_rig.LIGHT_RIG_PRESETS]
    assert len(ids) == len(set(ids))


def test_rembrandt_is_placed_high_and_off_axis() -> None:
    """The preset is only worth its name if the geometry makes the cheek
    triangle — up and about 45 degrees to one side."""
    preset = next(p for p in light_rig.LIGHT_RIG_PRESETS if p["id"] == "rembrandt")
    key = preset["rig"]["key"]
    assert key["x"] < -0.3 and key["y"] > 0.3


def test_golden_hour_is_low_and_raking() -> None:
    preset = next(p for p in light_rig.LIGHT_RIG_PRESETS if p["id"] == "golden-hour")
    key = preset["rig"]["key"]
    assert abs(key["x"]) > 0.8, "the sun an hour before it sets is near the horizon"
    assert key["warmth"] >= 80


def test_presets_ship_with_the_camera_catalog() -> None:
    payload = service.list_camera_catalog()
    assert [p.id for p in payload.lightRigPresets] == [p["id"] for p in light_rig.LIGHT_RIG_PRESETS]


# ── Wire contract ───────────────────────────────────────────────────────────


def test_generate_request_accepts_a_rig() -> None:
    req = schemas.GenerateRequest.model_validate(
        {
            "prompt": "A cat",
            "model": "fal-ai/flux/dev",
            "lightRig": {
                "enabled": True,
                "ambience": "night",
                "key": {"enabled": True, "x": -0.6, "y": 0.5, "strength": 90},
            },
        }
    )
    assert req.lightRig is not None
    assert req.lightRig.key.x == -0.6
    # Untouched lamps default to off rather than to a lamp at full strength.
    assert req.lightRig.rim.enabled is False


def test_generation_params_carry_the_rig_back_for_remix() -> None:
    params = schemas.GenerationParams.model_validate(
        {
            "kind": "image",
            "model": "fal-ai/flux/dev",
            "aspectRatio": "1:1",
            "count": 1,
            "lightRig": {"enabled": True, "ambience": "dusk"},
        }
    )
    assert params.lightRig is not None and params.lightRig.ambience == "dusk"
