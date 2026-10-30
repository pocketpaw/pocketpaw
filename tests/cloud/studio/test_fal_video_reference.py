# tests/cloud/studio/test_fal_video_reference.py — Seedance 2.0 reference-to-video.
#
# Created 2026-09-04 (studio-movie-pipeline).
#
# This is the only endpoint in the catalog that accepts AUDIO as an input, which
# is what lets the movie flow hand a character still and a generated music bed to
# ONE video call instead of generating and muxing afterwards. Its contract
# differs from Seedance 2.5's in ways that fail as a 422 rather than obviously:
#
#   * duration is 4..30 on 2.5 and 4..15 on the tighter 2.0 — SAME arguments,
#     different caps, so the limits are resolved per endpoint rather than assumed.
#   * `seed` is an OUTPUT field on 2.5, never an input.
#   * references are USED by being CITED in the prompt (@Image1 / @Audio1). An
#     uncited reference is attached for nothing — the call succeeds and the model
#     ignores it, so nothing surfaces the mistake.
#   * audio requires an image or video to anchor it; audio alone is invalid.
#   * 9 images / 3 videos / 3 audio, and 12 files across all three.

from __future__ import annotations

import pytest
from pocketpaw_ee.cloud.studio import fal_video

# ── Endpoint detection ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "endpoint",
    [
        "bytedance/seedance-2.0/reference-to-video",
        "bytedance/seedance-2.0/fast/reference-to-video",
    ],
)
def test_reference_endpoints_are_recognised(endpoint: str) -> None:
    assert fal_video.is_seedance_reference_endpoint(endpoint) is True


@pytest.mark.parametrize(
    "endpoint",
    [
        "bytedance/seedance-2.5/image-to-video",
        "bytedance/seedance-2.5/text-to-video",
        "fal-ai/kling-video/v1/standard/text-to-video",
        "",
        None,
    ],
)
def test_other_endpoints_are_not_mistaken_for_it(endpoint: str | None) -> None:
    """Seedance 2.5 shares the family name but NOT this contract — routing 2.5
    through the reference builder would send it arrays it does not accept."""
    assert fal_video.is_seedance_reference_endpoint(endpoint) is False


# ── Reference citation ──────────────────────────────────────────────────────


def test_tokens_are_one_indexed() -> None:
    """Off-by-one here points the model at the wrong asset and the call still
    succeeds — the video is simply built from something else."""
    assert fal_video.reference_token("Image", 0) == "@Image1"
    assert fal_video.reference_token("Audio", 2) == "@Audio3"


def test_uncited_references_are_appended_to_the_prompt() -> None:
    """fal's contract is that an asset is used by being cited. Attaching a still
    and a music bed and never naming them means the model ignores both."""
    out = fal_video.annotate_reference_prompt("A detective walks in", image_count=1, audio_count=1)
    assert "@Image1" in out
    assert "@Audio1" in out


def test_a_prompt_that_already_cites_is_left_alone() -> None:
    prompt = "Recreate @Image1 with the rhythm of @Audio1."
    assert fal_video.annotate_reference_prompt(prompt, image_count=1, audio_count=1) == prompt


def test_only_the_uncited_ones_are_added() -> None:
    out = fal_video.annotate_reference_prompt(
        "Keep the character from @Image1.", image_count=1, audio_count=1
    )
    assert out.count("@Image1") == 1
    assert "@Audio1" in out


def test_the_citation_clause_does_not_run_on() -> None:
    """ "a detective walks in Use @Image1 as reference." reads as one broken
    sentence and the model has to guess where the instruction starts."""
    out = fal_video.annotate_reference_prompt("A detective walks in", image_count=1)
    assert "in. Use @Image1" in out


def test_an_empty_prompt_becomes_just_the_citation() -> None:
    out = fal_video.annotate_reference_prompt("", image_count=1)
    assert out == "Use @Image1 as reference."


# ── Argument building ───────────────────────────────────────────────────────


def test_references_are_flat_url_arrays_not_objects() -> None:
    args = fal_video.build_reference_arguments(
        prompt="@Image1 dances to @Audio1",
        image_urls=["https://x/i.png"],
        audio_urls=["https://x/a.mp3"],
    )
    assert args["image_urls"] == ["https://x/i.png"]
    assert args["audio_urls"] == ["https://x/a.mp3"]


def test_thirty_seconds_is_allowed_on_2_5() -> None:
    """2.5 accepts 4..30. Clamping to 15 here (the 2.0 limit) would silently cut
    every clip in half — the generation succeeds, just short, so nothing reports
    it."""
    args = fal_video.build_reference_arguments(prompt="x", image_urls=["i"], duration_sec=30)
    assert args["duration"] == "30"


def test_duration_clamps_to_fifteen_on_the_tighter_2_0_endpoint() -> None:
    """Same arguments, different cap. Sending 30 to 2.0 is a 422.

    Mutation that must break this: use one set of limits for both endpoints.
    """
    args = fal_video.build_reference_arguments(
        prompt="x",
        endpoint=fal_video.SEEDANCE_REF_TO_VIDEO_LEGACY_MODEL,
        image_urls=["i"],
        duration_sec=30,
    )
    assert args["duration"] == "15"


def test_over_thirty_is_still_clamped_on_2_5() -> None:
    args = fal_video.build_reference_arguments(prompt="x", image_urls=["i"], duration_sec=45)
    assert args["duration"] == "30"


def test_duration_is_a_string_not_an_int() -> None:
    args = fal_video.build_reference_arguments(prompt="x", image_urls=["i"], duration_sec=8)
    assert args["duration"] == "8"
    assert isinstance(args["duration"], str)


def test_a_short_duration_is_lifted_to_the_minimum() -> None:
    args = fal_video.build_reference_arguments(prompt="x", image_urls=["i"], duration_sec=2)
    assert args["duration"] == "4"


def test_audio_without_an_anchor_is_refused_here_not_at_fal() -> None:
    """fal rejects audio with no image or video. Failing locally turns a 502 from
    a remote 422 into a message that says what to fix."""
    with pytest.raises(ValueError, match="image or video"):
        fal_video.build_reference_arguments(prompt="x", audio_urls=["a"])


def test_audio_with_a_video_anchor_is_fine() -> None:
    args = fal_video.build_reference_arguments(prompt="x", video_urls=["v"], audio_urls=["a"])
    assert args["audio_urls"] == ["a"]


def test_over_long_arrays_are_truncated_to_fals_caps() -> None:
    args = fal_video.build_reference_arguments(
        prompt="x",
        image_urls=[f"i{n}" for n in range(40)],
        video_urls=[f"v{n}" for n in range(20)],
        audio_urls=[f"a{n}" for n in range(20)],
    )
    assert len(args["image_urls"]) <= fal_video.REF_MAX_IMAGES
    assert len(args["video_urls"]) == fal_video.REF_MAX_VIDEOS
    assert len(args["audio_urls"]) == fal_video.REF_MAX_AUDIO


def test_the_2_0_endpoint_gets_its_own_much_tighter_caps() -> None:
    limits = fal_video.reference_limits(fal_video.SEEDANCE_REF_TO_VIDEO_LEGACY_MODEL)
    assert (limits.images, limits.videos, limits.audio, limits.files) == (9, 3, 3, 12)
    assert fal_video.reference_limits(fal_video.SEEDANCE_REF_TO_VIDEO_MODEL).duration_max == 30


def test_the_total_file_cap_trims_images_first() -> None:
    """On 2.0: 9 + 3 + 3 is 15, over its 12-file cap. Images are the most
    replaceable — dropping the audio or the only video changes what the shot IS."""
    args = fal_video.build_reference_arguments(
        prompt="x",
        endpoint=fal_video.SEEDANCE_REF_TO_VIDEO_LEGACY_MODEL,
        image_urls=[f"i{n}" for n in range(9)],
        video_urls=["v1", "v2", "v3"],
        audio_urls=["a1", "a2", "a3"],
    )
    total = len(args["image_urls"]) + len(args["video_urls"]) + len(args["audio_urls"])
    assert total <= fal_video.reference_limits(fal_video.SEEDANCE_REF_TO_VIDEO_LEGACY_MODEL).files
    assert len(args["video_urls"]) == 3
    assert len(args["audio_urls"]) == 3


def test_blank_urls_are_dropped_rather_than_sent() -> None:
    args = fal_video.build_reference_arguments(
        prompt="x", image_urls=["  ", "i1", ""], audio_urls=None
    )
    assert args["image_urls"] == ["i1"]


def test_absent_options_are_omitted_so_fal_uses_its_own_defaults() -> None:
    args = fal_video.build_reference_arguments(prompt="x", image_urls=["i"])
    for key in ("resolution", "duration", "aspect_ratio", "generate_audio", "video_urls"):
        assert key not in args


def test_every_option_lands_when_given() -> None:
    args = fal_video.build_reference_arguments(
        prompt="x",
        image_urls=["i"],
        resolution="1080p",
        aspect_ratio="21:9",
        generate_audio=False,
        bitrate_mode="high",
    )
    assert args["resolution"] == "1080p"
    assert args["aspect_ratio"] == "21:9"
    assert args["generate_audio"] is False
    assert args["bitrate_mode"] == "high"


def test_seed_is_never_sent_because_2_5_has_no_such_input() -> None:
    """It is an OUTPUT field on 2.5. Sending it is an unrecognised argument."""
    args = fal_video.build_reference_arguments(prompt="x", image_urls=["i"])
    assert "seed" not in args


# ── Dispatch ────────────────────────────────────────────────────────────────


async def _fake_download(url):
    return b"mp4", "video/mp4"


async def test_audio_routes_to_the_reference_endpoint_whatever_model_was_named(
    monkeypatch,
) -> None:
    """Audio claims the call. Every other endpoint would drop the track silently
    and hand back a video with none of the music that was asked for — so honouring
    a caller's Kling selection here would be honouring it into a wrong result.

    Mutation that must break this: dispatch on the model id alone.
    """
    seen: dict = {}

    async def _fake_run(endpoint, arguments, *, key):
        seen["endpoint"] = endpoint
        seen["arguments"] = arguments
        return {"video": {"url": "https://x/out.mp4"}}

    monkeypatch.setattr(fal_video, "_run_fal", _fake_run)
    monkeypatch.setattr(fal_video, "_download", _fake_download)

    await fal_video.run_fal_video(
        prompt="a detective",
        model="fal-ai/kling-video/v1/standard/text-to-video",
        image_urls=["https://x/i.png"],
        audio_urls=["https://x/a.mp3"],
        key="k",
    )
    assert seen["endpoint"] == fal_video.SEEDANCE_REF_TO_VIDEO_MODEL
    assert seen["arguments"]["audio_urls"] == ["https://x/a.mp3"]


async def test_the_fast_tier_is_honoured_when_explicitly_selected(monkeypatch) -> None:
    seen: dict = {}

    async def _fake_run(endpoint, arguments, *, key):
        seen["endpoint"] = endpoint
        return {"video": {"url": "https://x/out.mp4"}}

    monkeypatch.setattr(fal_video, "_run_fal", _fake_run)
    monkeypatch.setattr(fal_video, "_download", _fake_download)

    await fal_video.run_fal_video(
        prompt="x",
        model=fal_video.SEEDANCE_REF_TO_VIDEO_FAST_MODEL,
        image_urls=["i"],
        audio_urls=["a"],
        key="k",
    )
    assert seen["endpoint"] == fal_video.SEEDANCE_REF_TO_VIDEO_FAST_MODEL


async def test_no_audio_leaves_the_existing_paths_alone(monkeypatch) -> None:
    """A plain text-to-video run must be untouched by any of this."""
    seen: dict = {}

    async def _fake_run(endpoint, arguments, *, key):
        seen["endpoint"] = endpoint
        return {"video": {"url": "https://x/out.mp4"}}

    monkeypatch.setattr(fal_video, "_run_fal", _fake_run)
    monkeypatch.setattr(fal_video, "_download", _fake_download)

    await fal_video.run_fal_video(prompt="a cat", model=fal_video.SEEDANCE_T2V_MODEL, key="k")
    assert seen["endpoint"] == fal_video.SEEDANCE_T2V_MODEL


# ── Catalog visibility ──────────────────────────────────────────────────────


def test_the_reference_model_is_in_the_curated_catalog() -> None:
    """It shipped routable but unlisted, so the composer's video picker never
    offered it — the endpoint worked and nobody could select it.

    Mutation that must break this: remove the seedance_2_5_ref entry.
    """
    ids = {cfg["id"] for cfg in fal_video.CURATED_VIDEO_MODELS.values()}
    assert fal_video.SEEDANCE_REF_TO_VIDEO_MODEL in ids


def test_it_advertises_thirty_seconds_and_the_wide_ratios() -> None:
    """Its enum is wider than its siblings'. Advertising the narrower set would
    hide options the endpoint accepts."""
    cfg = fal_video.CURATED_VIDEO_MODELS["seedance_2_5_ref"]
    assert 30 in cfg["durations"]
    assert "21:9" in cfg["aspect_ratios"]
    # Duration is a string enum on this family; sending an int is a 422.
    assert cfg["duration_as_string"] is True


def test_selecting_it_with_nothing_wired_says_what_is_missing() -> None:
    """fal requires at least one image or video. Now that the model is
    selectable, a user can reach it with an empty graph."""
    with pytest.raises(ValueError, match="reference image or video"):
        fal_video.build_reference_arguments(prompt="a detective", image_urls=[])
