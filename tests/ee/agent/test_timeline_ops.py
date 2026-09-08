# test_timeline_ops.py — the timeline op vocabulary and its validator.
#
# Created: 2026-09-08 (feat/agentic-studio-editor, chunk 1).
#
# Two jobs, deliberately in one file because they fail for related reasons:
#
#   1. CONTRACT. `OP_KINDS` must match the committed manifest, which is
#      byte-identical to the copy paw-enterprise's vitest checks itself against.
#      Neither repo checks the other out, so a test reading across them would be
#      skipped in the CI that matters. The manifest is the seam instead.
#
#   2. BEHAVIOUR. The validator is a GATE — the only thing standing between an
#      agent's invented clip id and a silent no-op in the user's browser. Per
#      pocketpaw/CLAUDE.md, "a gate is not a gate until a mutation has been
#      observed to break it", so every assertion here has a matching entry in
#      tests/mutations/timeline_ops.json and that plan has been run.

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pocketpaw_ee.agent.mcp_servers.timeline_ops import (
    MAX_OPS_PER_BATCH,
    OP_KINDS,
    TimelineSummary,
    validate_ops,
)

CONTRACT_PATH = (
    Path(__file__).resolve().parents[3]
    / "ee"
    / "pocketpaw_ee"
    / "agent"
    / "mcp_servers"
    / "timeline_ops.contract.json"
)


@pytest.fixture
def summary() -> TimelineSummary:
    """A timeline with three video clips, an audio bed and two tracks.

    Shaped like the motivating request — "these are the three clips, arrange
    them, caption this, put the audio in the lanes" — so the tests exercise the
    ids an agent would actually be handed.
    """
    return TimelineSummary(
        clip_ids={"clip_aaa", "clip_bbb", "clip_ccc"},
        asset_ids={"asset_one", "asset_two", "asset_three", "asset_music"},
        track_ids={"track_video", "track_audio"},
    )


# ── 1. Contract ────────────────────────────────────────────────────────────


def test_op_kinds_matches_the_committed_contract() -> None:
    """Mutation: drop a verb from OP_KINDS, or add one not in the manifest."""
    contract = json.loads(CONTRACT_PATH.read_text())
    assert OP_KINDS == frozenset(contract["ops"])


def test_batch_cap_matches_the_committed_contract() -> None:
    """Mutation: change MAX_OPS_PER_BATCH without touching the manifest."""
    contract = json.loads(CONTRACT_PATH.read_text())
    assert MAX_OPS_PER_BATCH == contract["maxOpsPerBatch"]


def test_contract_ops_are_sorted_and_unique() -> None:
    """The manifest's ops array is the review signal for a wire-format change.
    Unsorted or duplicated entries make that diff unreadable."""
    ops = json.loads(CONTRACT_PATH.read_text())["ops"]
    assert ops == sorted(ops)
    assert len(ops) == len(set(ops))


# ── 2. The closed set ──────────────────────────────────────────────────────


def test_unknown_verb_is_rejected_with_the_valid_set(summary: TimelineSummary) -> None:
    """The Ripple action-verb bug in one assertion: an invented verb must not
    reach the client, where it would hit a default case and no-op silently.

    Mutation: let an unknown `op` fall through instead of returning an error."""
    clean, error = validate_ops([{"op": "invoke_endpoint", "clipId": "clip_aaa"}], summary)
    assert clean is None
    assert error is not None
    assert "invoke_endpoint" in error
    # The agent must be able to fix it in the same turn without guessing.
    assert "place_clip" in error


def test_near_miss_verb_gets_a_suggestion(summary: TimelineSummary) -> None:
    """Mutation: return the error without the difflib hint."""
    _, error = validate_ops([{"op": "place_clips", "assetId": "asset_one"}], summary)
    assert error is not None
    assert "Did you mean 'place_clip'?" in error


def test_every_contract_op_is_reachable(summary: TimelineSummary) -> None:
    """Guards the opposite drift from the contract test: a verb listed in the
    manifest that the validator rejects as unknown would pass the contract
    assertion and still be unusable.

    Mutation: remove a verb from the validator's dispatch while leaving it in
    OP_KINDS."""
    minimal: dict[str, dict] = {
        "place_clip": {"assetId": "asset_one"},
        "move_clip": {"clipId": "clip_aaa", "atMs": 0},
        "trim_clip": {"clipId": "clip_aaa", "inMs": 0, "outMs": 1000},
        "split_clip": {"clipId": "clip_aaa", "atMs": 500},
        "remove_clip": {"clipId": "clip_aaa"},
        "set_transition": {"clipId": "clip_bbb", "kind": "crossfade"},
        "add_text": {"text": "hi", "fromMs": 0, "toMs": 1000},
        "add_caption": {"text": "hi", "fromMs": 0, "toMs": 1000},
        "place_audio": {"assetId": "asset_music"},
        "set_volume": {"target": "master", "volume": 1},
        "set_project": {"aspectRatio": "9:16"},
    }
    assert set(minimal) == set(OP_KINDS), "a verb was added without a case here"
    for kind, args in minimal.items():
        clean, error = validate_ops([{"op": kind, **args}], summary)
        assert error is None, f"{kind} was rejected: {error}"
        assert clean is not None


# ── 3. Ids must name something the agent was shown ─────────────────────────


def test_invented_clip_id_is_rejected(summary: TimelineSummary) -> None:
    """Mutation: skip the clip_ids membership check."""
    _, error = validate_ops([{"op": "remove_clip", "clipId": "clip_zzz"}], summary)
    assert error is not None
    assert "clip_zzz" in error


def test_invented_asset_id_is_rejected(summary: TimelineSummary) -> None:
    """Mutation: skip the asset_ids membership check."""
    _, error = validate_ops([{"op": "place_clip", "assetId": "asset_nope"}], summary)
    assert error is not None
    assert "asset_nope" in error
    assert "media rail" in error


def test_mistyped_clip_id_gets_a_suggestion(summary: TimelineSummary) -> None:
    _, error = validate_ops([{"op": "remove_clip", "clipId": "clip_aab"}], summary)
    assert error is not None
    assert "Did you mean 'clip_aaa'?" in error


def test_an_id_tail_resolves_to_the_full_id() -> None:
    """The preamble renders ids through `entity_line`, which shortens anything
    over 8 chars to a `…tail`. Clip ids are 21-char nanoids, so a tail is ALL
    the agent ever sees — matching against full ids would reject every
    well-formed call. The cleaned op must carry the full id back, because the
    client's store looks up by that.

    Mutation: compare against summary.clip_ids directly instead of resolving."""
    full = "V1StGXR8_Z5jdHi6B-myT"
    s = TimelineSummary(clip_ids={full}, asset_ids=set(), track_ids=set())
    clean, error = validate_ops([{"op": "remove_clip", "clipId": "…6B-myT"}], s)
    assert error is None, error
    assert clean is not None
    assert clean[0]["clipId"] == full


def test_an_ambiguous_tail_errors_rather_than_guessing(summary: TimelineSummary) -> None:
    """Picking one of two matches would edit the wrong clip silently."""
    s = TimelineSummary(
        clip_ids={"aaaaaaaaaaaaaSHARED01", "bbbbbbbbbbbbbSHARED01"},
        asset_ids=set(),
        track_ids=set(),
    )
    _, error = validate_ops([{"op": "remove_clip", "clipId": "SHARED01"}], s)
    assert error is not None
    assert "more than one" in error
    assert "Ask the user" in error


def test_unknown_track_is_rejected(summary: TimelineSummary) -> None:
    _, error = validate_ops(
        [{"op": "place_clip", "assetId": "asset_one", "track": "track_captions"}], summary
    )
    assert error is not None
    assert "track_captions" in error


def test_after_must_name_a_real_clip(summary: TimelineSummary) -> None:
    """`after` drives the whole arrange-in-sequence flow, so an unresolvable one
    would land the clip at a silently wrong position rather than failing."""
    _, error = validate_ops(
        [{"op": "place_clip", "assetId": "asset_one", "after": "clip_ghost"}], summary
    )
    assert error is not None
    assert "clip_ghost" in error


def test_anchor_clip_must_name_a_real_clip(summary: TimelineSummary) -> None:
    _, error = validate_ops(
        [
            {
                "op": "add_caption",
                "text": "hello",
                "fromMs": 0,
                "toMs": 900,
                "anchorClip": "clip_ghost",
            }
        ],
        summary,
    )
    assert error is not None
    assert "clip_ghost" in error


# ── 4. No timeline open ────────────────────────────────────────────────────


def test_no_timeline_open_is_a_clear_refusal() -> None:
    """The editor tab holds the document, so with none open there is nothing to
    edit. The agent must be told to open it rather than silently succeeding.

    Mutation: treat a missing summary as an empty-but-present one."""
    _, error = validate_ops(
        [{"op": "place_clip", "assetId": "asset_one"}],
        TimelineSummary(has_timeline=False),
    )
    assert error is not None
    assert "No timeline is open" in error


def test_from_meta_reads_ids_out_of_surface_meta() -> None:
    parsed = TimelineSummary.from_meta(
        {
            "timeline": {
                "clips": [{"id": "clip_aaa", "name": "intro"}],
                "assets": [{"id": "asset_one"}],
                "tracks": [{"id": "track_video"}],
            }
        }
    )
    assert parsed.has_timeline
    assert parsed.clip_ids == {"clip_aaa"}
    assert parsed.asset_ids == {"asset_one"}


@pytest.mark.parametrize("meta", [None, {}, {"timeline": None}, {"timeline": "nope"}, "junk"])
def test_from_meta_treats_malformed_meta_as_no_timeline(meta: object) -> None:
    """Absent and malformed must both mean "no timeline", never "empty
    timeline" — the two lead the agent to opposite, and one wrong, behaviour."""
    assert TimelineSummary.from_meta(meta).has_timeline is False  # type: ignore[arg-type]


# ── 5. Argument validation ─────────────────────────────────────────────────


def test_zero_length_caption_is_rejected(summary: TimelineSummary) -> None:
    """A cue with toMs <= fromMs renders as nothing, which the user reads as the
    caption having been dropped.

    Mutation: flip the comparison, or drop the check."""
    _, error = validate_ops(
        [{"op": "add_caption", "text": "hi", "fromMs": 1000, "toMs": 1000}], summary
    )
    assert error is not None
    assert "toMs" in error


def test_unknown_transition_kind_is_rejected(summary: TimelineSummary) -> None:
    _, error = validate_ops(
        [{"op": "set_transition", "clipId": "clip_bbb", "kind": "dissolve"}], summary
    )
    assert error is not None
    assert "Did you mean 'crossfade'?" in error or "dissolve" in error


def test_volume_out_of_range_is_rejected(summary: TimelineSummary) -> None:
    _, error = validate_ops([{"op": "set_volume", "target": "master", "volume": 9}], summary)
    assert error is not None
    assert "between 0 and 2" in error


def test_master_is_a_valid_volume_target(summary: TimelineSummary) -> None:
    """'master' is the one non-id value `target` takes; rejecting it would make
    "turn the whole thing down" impossible."""
    clean, error = validate_ops([{"op": "set_volume", "target": "master", "volume": 0.4}], summary)
    assert error is None
    assert clean is not None


def test_boolean_is_not_accepted_as_a_number(summary: TimelineSummary) -> None:
    """bool subclasses int in Python, so a naive isinstance check reads
    `{"volume": true}` as 1.0 and silently sets full volume.

    Mutation: drop the `isinstance(value, bool)` guard in _check_number."""
    _, error = validate_ops([{"op": "set_volume", "target": "master", "volume": True}], summary)
    assert error is not None


def test_unknown_aspect_ratio_is_rejected(summary: TimelineSummary) -> None:
    """4:5 and 9:16 get conflated constantly — the confusion platform-presets.ts
    exists to stop — so the frame shape is a closed set, never raw pixels."""
    _, error = validate_ops([{"op": "set_project", "aspectRatio": "1080x1920"}], summary)
    assert error is not None
    assert "1080x1920" in error


def test_split_without_a_cut_point_is_rejected(summary: TimelineSummary) -> None:
    _, error = validate_ops([{"op": "split_clip", "clipId": "clip_aaa"}], summary)
    assert error is not None
    assert "atMs" in error


def test_negative_time_is_rejected(summary: TimelineSummary) -> None:
    _, error = validate_ops([{"op": "move_clip", "clipId": "clip_aaa", "atMs": -500}], summary)
    assert error is not None


# ── 6. Batch shape ─────────────────────────────────────────────────────────


def test_empty_batch_is_rejected(summary: TimelineSummary) -> None:
    _, error = validate_ops([], summary)
    assert error is not None
    assert "empty" in error


def test_oversized_batch_is_rejected(summary: TimelineSummary) -> None:
    """Mutation: raise or remove the cap."""
    ops = [{"op": "remove_clip", "clipId": "clip_aaa"}] * (MAX_OPS_PER_BATCH + 1)
    _, error = validate_ops(ops, summary)
    assert error is not None
    assert str(MAX_OPS_PER_BATCH) in error


def test_one_bad_op_rejects_the_whole_batch(summary: TimelineSummary) -> None:
    """All-or-nothing. A half-valid batch that partially applies leaves an
    arrangement nobody asked for and no clean state to undo to.

    Mutation: collect valid ops and return them alongside the error."""
    clean, error = validate_ops(
        [
            {"op": "place_clip", "assetId": "asset_one"},
            {"op": "place_clip", "assetId": "asset_ghost"},
        ],
        summary,
    )
    assert clean is None
    assert error is not None


def test_the_motivating_request_validates_as_one_batch(summary: TimelineSummary) -> None:
    """The captain's sentence, end to end: three clips arranged in sequence, a
    caption anchored to the first, and a music bed in the audio lane.

    This is the acceptance test for the whole vocabulary — if arranging three
    clips takes more than one batch, the design failed its own goal."""
    clean, error = validate_ops(
        [
            {"op": "place_clip", "assetId": "asset_one", "atMs": 0},
            {"op": "place_clip", "assetId": "asset_two", "after": "clip_aaa"},
            {"op": "place_clip", "assetId": "asset_three", "after": "clip_bbb"},
            {
                "op": "add_caption",
                "text": "Welcome back",
                "fromMs": 400,
                "toMs": 2200,
                "anchorClip": "clip_aaa",
            },
            {"op": "place_audio", "assetId": "asset_music", "atMs": 0, "volume": 0.3},
            {"op": "set_transition", "clipId": "clip_bbb", "kind": "crossfade", "durationMs": 400},
        ],
        summary,
    )
    assert error is None
    assert clean is not None
    assert len(clean) == 6
