# tests/cloud/studio/test_camera_lighting.py — camera & lighting prompt injection.
#
# Image models expose no camera or lighting parameters, so the studio's "Camera &
# lighting" dialog works by writing words into the prompt. That makes prompt
# ASSEMBLY the feature, and these tests pin the four things that can silently
# ruin it:
#
#   * Auto must render to SILENCE. An unset slot that emits "auto focal length"
#     degrades the image, and nothing downstream would flag it.
#   * A curated style must actually reach the prompt (the regression below).
#   * An explicit lighting pick must SILENCE the style's own lighting sentence,
#     or the model gets two contradictory instructions and tends to follow the
#     longer one.
#   * A catalog group must write a field the spec actually has, or the pick is
#     accepted by the UI and dropped on the floor by the renderer.
#
# Created 2026-09-03 (studio-camera-lighting).

from __future__ import annotations

import pytest
from pocketpaw_ee.cloud.studio import camera_catalog, schemas, service

# ── Auto means silence ──────────────────────────────────────────────────────


def test_absent_specs_render_to_nothing() -> None:
    assert camera_catalog.compose_camera_phrase(None) == ""
    assert camera_catalog.compose_lighting_phrase(None) == ""


def test_empty_specs_render_to_nothing() -> None:
    """Every field on Auto contributes no words — not the word "auto".

    Mutation that must break this: give any catalog option a phrase that renders
    for a None value.
    """
    assert camera_catalog.compose_camera_phrase(schemas.CameraSpec()) == ""
    assert camera_catalog.compose_lighting_phrase(schemas.LightingSpec()) == ""


def test_all_auto_leaves_the_prompt_completely_untouched() -> None:
    out = service.compose_prompt("A cat", None, schemas.CameraSpec(), schemas.LightingSpec())
    assert out == "A cat"


def test_unknown_ids_are_ignored_rather_than_raising() -> None:
    """A stale client sending a retired id loses that one control, not the run."""
    spec = schemas.CameraSpec(angle="no-such-angle", shotSize="wide")
    rendered = camera_catalog.compose_camera_phrase(spec)
    assert "no-such-angle" not in rendered
    assert "wide shot" in rendered


# ── Rendering ───────────────────────────────────────────────────────────────


def test_camera_renders_gear_and_framing_as_separate_sentences() -> None:
    spec = schemas.CameraSpec(body="arri-alexa-35", focalLengthMm=24, angle="low-angle")
    out = camera_catalog.compose_camera_phrase(spec)
    assert out.startswith("Shot on ")
    assert "Framed from a low angle" in out
    assert "24mm wide-angle lens" in out


def test_phrases_never_leak_the_bare_token() -> None:
    """The id is a UI handle; what reaches the model is prose. "24mm" alone in a
    prompt reads as a stray number."""
    out = camera_catalog.compose_camera_phrase(schemas.CameraSpec(focalLengthMm=24))
    assert "lens" in out


def test_custom_focal_length_is_used_verbatim() -> None:
    spec = schemas.CameraSpec(focalLengthMm=None, customFocalLength="18mm shift")
    assert "a 18mm shift lens" in camera_catalog.compose_camera_phrase(spec)


def test_custom_focal_length_wins_over_a_preset() -> None:
    spec = schemas.CameraSpec(focalLengthMm=50, customFocalLength="anamorphic 40mm")
    out = camera_catalog.compose_camera_phrase(spec)
    assert "anamorphic 40mm" in out
    assert "50mm" not in out


def test_lighting_renders_one_sentence() -> None:
    spec = schemas.LightingSpec(setup="low-key", source="candlelight", quality="soft")
    out = camera_catalog.compose_lighting_phrase(spec)
    assert out.startswith("Lit with ")
    assert out.endswith(".")
    assert out.count("Lit with") == 1


def test_clauses_land_between_the_subject_and_the_style() -> None:
    """Order is subject → camera → lighting → style. The technical direction has
    to precede a curated style's thousand-character block, not trail it."""
    out = service.compose_prompt(
        "A detective",
        "neo-noir-thriller",
        schemas.CameraSpec(angle="dutch"),
        schemas.LightingSpec(source="night-neon"),
    )
    assert out.index("A detective") < out.index("Framed with a tilted dutch angle")
    assert out.index("Framed with a tilted dutch angle") < out.index("Lit with")
    assert out.index("Lit with") < out.index("Art style:")


def test_no_run_on_sentences_at_the_subject_junction() -> None:
    out = service.compose_prompt("A cat", None, schemas.CameraSpec(angle="pov"), None)
    assert out == "A cat. Framed from a first-person point of view."


def test_a_subject_that_already_ends_in_punctuation_is_not_double_stopped() -> None:
    out = service.compose_prompt("A cat!", None, schemas.CameraSpec(angle="pov"), None)
    assert out.startswith("A cat! Framed")


# ── Regression: curated styles never reached the prompt ─────────────────────


def test_curated_style_actually_applies() -> None:
    """`_apply_style` searched only STYLES (the 7 quick ones) while `list_styles`
    served quick + ~20 CURATED_STYLES. Every curated pick therefore matched
    nothing and generated with NO style — silently, with the styleId echoed back
    in the response as though it had worked.

    Mutation that must break this: restrict `_find_style` to STYLES again.
    """
    out = service.compose_prompt("A detective", "neo-noir-thriller")
    assert out != "A detective"
    assert "Neo-noir" in out


@pytest.mark.parametrize("style_id", ["product-ad", "real-estate", "documentary", "pastel"])
def test_every_offered_style_changes_the_prompt(style_id: str) -> None:
    """Whatever the picker offers must have an effect. A style that is listed but
    inert is worse than one that is absent."""
    assert service.compose_prompt("A subject", style_id) != "A subject"


def test_quick_style_output_is_unchanged_when_nothing_is_picked() -> None:
    """The comma-continuation concatenation is preserved byte for byte so this
    change is invisible to anyone not using the new controls."""
    assert service.compose_prompt("A cat", "cinematic") == (
        "A cat, cinematic lighting, shallow depth of field, film grain, dramatic composition"
    )


def test_none_and_unknown_styles_leave_the_prompt_alone() -> None:
    assert service.compose_prompt("A cat", "none") == "A cat"
    assert service.compose_prompt("A cat", None) == "A cat"
    assert service.compose_prompt("A cat", "not-a-style") == "A cat"


# ── Style collision: the explicit pick wins ─────────────────────────────────


def test_lighting_pick_silences_the_style_lighting_but_keeps_its_camera() -> None:
    """Neo-Noir prescribes "venetian blind shadows"; asking for high-key light on
    top of it would hand the model a contradiction. The explicit pick wins and
    only the style's LIGHTING sentence is dropped — art style, camera, grading and
    references all survive.
    """
    out = service.compose_prompt(
        "A detective", "neo-noir-thriller", None, schemas.LightingSpec(setup="high-key")
    )
    assert "venetian" not in out
    assert "high-key lighting" in out
    assert "Camera: Dutch angles" in out
    assert "Art style:" in out
    assert "Inspired by:" in out


def test_camera_pick_silences_the_style_camera_but_keeps_its_lighting() -> None:
    out = service.compose_prompt(
        "A detective", "neo-noir-thriller", schemas.CameraSpec(angle="overhead"), None
    )
    assert "Camera: Dutch angles" not in out
    assert "Lighting: High contrast" in out
    assert "bird's-eye" in out


def test_picking_both_drops_both_style_sentences() -> None:
    out = service.compose_prompt(
        "A detective",
        "neo-noir-thriller",
        schemas.CameraSpec(angle="overhead"),
        schemas.LightingSpec(setup="high-key"),
    )
    assert "Camera: Dutch angles" not in out
    assert "Lighting: High contrast" not in out
    assert "Art style:" in out


def test_a_quick_style_survives_a_pick_since_it_has_no_parts_to_drop() -> None:
    """Quick styles carry only a flat suffix with no structured lighting/camera to
    subtract, so they append whole. Mild redundancy beats dropping the style."""
    out = service.compose_prompt("A cat", "cinematic", None, schemas.LightingSpec(setup="high-key"))
    assert "high-key lighting" in out
    assert "Cinematic lighting" in out


# ── Catalog integrity ───────────────────────────────────────────────────────


ALL_GROUPS = camera_catalog.CAMERA_GROUPS + camera_catalog.LIGHTING_GROUPS


def test_every_group_writes_a_field_the_spec_actually_has() -> None:
    """The contract that keeps the dialog honest: a group whose `field` is not a
    real spec attribute would render a control whose picks are silently dropped
    by the renderer. Fails the moment someone renames a spec field.
    """
    camera_fields = set(schemas.CameraSpec.model_fields)
    lighting_fields = set(schemas.LightingSpec.model_fields)
    for group in camera_catalog.CAMERA_GROUPS:
        assert group["field"] in camera_fields, group["id"]
    for group in camera_catalog.LIGHTING_GROUPS:
        assert group["field"] in lighting_fields, group["id"]


def test_every_option_carries_a_phrase() -> None:
    """A phrase-less option is a control that does nothing when picked — the exact
    failure this feature cannot afford. Custom is the one exemption: its words
    come from the user."""
    for group in ALL_GROUPS:
        for opt in group["options"]:
            if opt.get("custom"):
                continue
            assert opt.get("phrase"), f"{group['id']}/{opt['id']} has no phrase"


def test_option_ids_are_unique_within_a_group() -> None:
    for group in ALL_GROUPS:
        ids = [o["id"] for o in group["options"]]
        assert len(ids) == len(set(ids)), group["id"]


# Proper nouns keep their capital mid-sentence ("Lit with Rembrandt lighting…").
# Everything else starting with one would read as a sentence break.
_PROPER_NOUN_STARTS = ("Rembrandt",)


def test_no_option_phrase_starts_a_new_sentence() -> None:
    """Phrases are clause fragments spliced mid-sentence after "Shot on" /
    "Framed" / "Lit with", so a stray capital reads as a run-on."""
    for group in ALL_GROUPS:
        for opt in group["options"]:
            phrase = opt.get("phrase") or ""
            if not phrase or phrase.startswith(_PROPER_NOUN_STARTS):
                continue
            assert not phrase[0].isupper(), f"{group['id']}/{opt['id']}: {phrase!r}"


def test_aperture_phrases_do_not_restate_the_verb() -> None:
    """They follow "Shot on …, " so "shot at f/1.4" would read "Shot on … shot at"."""
    for opt in camera_catalog.APERTURES:
        assert not opt["phrase"].startswith("shot ")


def test_catalog_endpoint_payload_validates() -> None:
    payload = service.list_camera_catalog()
    assert [g.id for g in payload.camera] == [g["id"] for g in camera_catalog.CAMERA_GROUPS]
    assert [g.id for g in payload.lighting] == [g["id"] for g in camera_catalog.LIGHTING_GROUPS]
    focal = next(g for g in payload.camera if g.id == "focalLength")
    assert next(o for o in focal.options if o.id == "24").mm == 24
    assert next(o for o in focal.options if o.id == "custom").custom is True


# ── Wire contract ───────────────────────────────────────────────────────────


def test_generate_request_accepts_and_round_trips_the_specs() -> None:
    req = schemas.GenerateRequest.model_validate(
        {
            "prompt": "A cat",
            "model": "fal-ai/flux/dev",
            "camera": {"angle": "low-angle", "focalLengthMm": 35},
            "lighting": {"source": "golden-hour"},
        }
    )
    assert req.camera is not None and req.camera.angle == "low-angle"
    assert req.lighting is not None and req.lighting.source == "golden-hour"


def test_generation_params_can_carry_the_specs_back_for_remix() -> None:
    """Storing the structured picks rather than the rendered sentence is what lets
    one-tap remix reopen the dialog with the same chips lit."""
    params = schemas.GenerationParams.model_validate(
        {
            "kind": "image",
            "model": "fal-ai/flux/dev",
            "aspectRatio": "1:1",
            "count": 1,
            "camera": {"angle": "dutch"},
            "lighting": {"setup": "low-key"},
        }
    )
    assert params.camera is not None and params.camera.angle == "dutch"
    assert params.lighting is not None and params.lighting.setup == "low-key"


def test_generate_request_without_the_specs_still_validates() -> None:
    req = schemas.GenerateRequest.model_validate({"prompt": "A cat", "model": "fal-ai/flux/dev"})
    assert req.camera is None
    assert req.lighting is None
