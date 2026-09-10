# tests/cloud/surface/test_studio_editor_preamble.py — the /studio/editor
# preamble, and specifically the gallery-attach half of it.
#
# Created: 2026-09-10 (feat/studio-editor-gallery-attach). The handler shipped
# 2026-09-08 with no test file of its own; this is it, scoped to the attach
# contract the paw-enterprise composer now emits.
#
# WHAT THE FRONTEND SENDS. The user types `@` in the editor's chat rail, picks a
# /studio gallery item, and the page imports it into the project's media rail
# BEFORE the message is sent. By the time the agent reads the turn it is an
# ordinary asset with a real assetId, so ``SurfaceMeta.timeline`` grows three
# things and no new op:
#
#   timeline["assets"][n]["attached"] -> true on the ones that arrived this turn
#   timeline["attached"]              -> list[str] of those asset ids, in pick order
#   timeline["attach_failed"]         -> list[str], already "<name>: <reason>"
#
# THE ONE THAT MATTERS is ``test_preamble_no_longer_claims_media_cannot_be_
# imported``. The old rule read "You cannot import or generate media from here",
# which is now half false — an agent that believes it would answer "attach the
# beach clip" with a refusal while the affordance sat in the composer. The other
# tests guard the rendering; that one guards the claim.
#
# Absence is asserted against a fixture that produces the block in a sibling test,
# so "no ATTACHED heading" cannot pass because the heading was renamed.

from __future__ import annotations

from typing import Any

from pocketpaw_ee.cloud.surface import SurfaceMeta
from pocketpaw_ee.cloud.surface.handlers import studio_editor

WORKSPACE = "ws-surface-studioeditor"
USER = "u-studioeditor"

# Realistic ids: 24-char ObjectId-shaped, sharing a head, differing in the tail —
# a short fixture id would render ~10 chars narrower than production and would
# also skip ``entity_line``'s tail path entirely (ids at or under 8 chars pass
# through whole).
ASSET_BEACH = "68c0f1a2b3c4d5e6f7a1b201"
ASSET_DRONE = "68c0f1a2b3c4d5e6f7a1b202"
ASSET_OLD = "68c0f1a2b3c4d5e6f7a1b203"


def _asset(asset_id: str, name: str, **extra: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": asset_id,
        "kind": "video",
        "name": name,
        "duration_ms": 4200,
        "in_use": False,
    }
    base.update(extra)
    return base


def _timeline(**over: Any) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "name": "Summer reel",
        "aspect_ratio": "16:9",
        "fps": 30,
        "duration_ms": 12000,
        "tracks": [{"id": "trk-1", "name": "V1", "kind": "video", "role": "main", "clip_count": 0}],
        "assets": [_asset(ASSET_OLD, "old-intro.mp4")],
        "clips": [],
    }
    doc.update(over)
    return doc


async def _render(timeline: dict[str, Any] | None) -> str:
    meta = SurfaceMeta(route_path="/studio/editor", timeline=timeline)
    return (await studio_editor.build_preamble(WORKSPACE, USER, meta)).text


def _has_block(preamble: str, heading: str) -> bool:
    """Is ``heading`` present as a BLOCK, not as prose?

    Both headings are also named inside ``_PROCEDURE`` — the rules tell the agent
    what to do when each block appears — so a plain ``in`` check can never be
    false and would make every absence assertion below vacuous. A block heading
    starts its line; the procedure mentions it mid-sentence."""
    return any(line.startswith(heading) for line in preamble.splitlines())


def _rail_row_for(preamble: str, name: str) -> str:
    """The MEDIA RAIL row naming ``name``.

    Scoped to the rail block on purpose: the same asset is named again under
    ATTACHED THIS TURN, and a bare substring search would not tell the two apart
    — which is exactly the confusion ``test_a_non_attached_asset_gains_no_
    marker`` exists to rule out.
    """
    rail = preamble.split("MEDIA RAIL", 1)[1].split("ATTACHED THIS TURN", 1)[0]
    rows = [line for line in rail.splitlines() if line.startswith(f"- {name} (")]
    assert len(rows) == 1, f"expected one rail row for {name!r}, got {rows}"
    return rows[0]


# --- the attached asset reaches both places ---


async def test_an_attached_asset_is_marked_on_the_rail_and_listed_below() -> None:
    """An asset attached this turn renders twice, doing two different jobs.

    On the RAIL it carries ``attached=yes`` so the agent can pick it out of a
    forty-row list. Under ATTACHED THIS TURN it is named again with its id,
    because "add these" has to resolve to an ordered list of ids and the rail's
    order is the rail's, not the user's."""
    preamble = await _render(
        _timeline(
            assets=[
                _asset(ASSET_OLD, "old-intro.mp4"),
                _asset(ASSET_BEACH, "beach.mp4", attached=True),
            ],
            attached=[ASSET_BEACH],
        )
    )

    row = _rail_row_for(preamble, "beach.mp4")
    assert "attached=yes" in row
    # The pre-existing facts survive — the marker is additive, not a replacement.
    assert "kind=video" in row
    assert "duration=4.2s" in row
    assert "used=no" in row

    assert _has_block(preamble, "ATTACHED THIS TURN")
    block = preamble.split("ATTACHED THIS TURN", 1)[1]
    assert "beach.mp4" in block
    # Addressable: the id renders as the resolvable tail entity_line produces.
    assert ASSET_BEACH[-8:] in block
    lower = block.lower()
    assert "gallery" in lower
    assert "order listed" in lower


async def test_the_attached_block_keeps_the_users_pick_order() -> None:
    """Rows follow ``timeline["attached"]``, not the rail.

    The rail is ordered by import; the pick list is ordered by the user. "Add
    these three" means the second order, so rendering from ``assets`` would
    quietly arrange the clips in the wrong sequence — a failure that produces a
    plausible video nobody asked for."""
    preamble = await _render(
        _timeline(
            assets=[
                _asset(ASSET_BEACH, "beach.mp4", attached=True),
                _asset(ASSET_DRONE, "drone.mp4", attached=True),
            ],
            attached=[ASSET_DRONE, ASSET_BEACH],
        )
    )

    block = preamble.split("ATTACHED THIS TURN", 1)[1]
    assert block.index("drone.mp4") < block.index("beach.mp4")


async def test_an_attached_id_with_no_rail_row_is_still_listed() -> None:
    """A picked id the rail projection did not carry is rendered, not dropped.

    Silently shortening the list would turn "add all three" into an arrangement
    of two, which reads as success. The id is still placeable, so it goes in."""
    preamble = await _render(_timeline(attached=[ASSET_BEACH]))

    block = preamble.split("ATTACHED THIS TURN", 1)[1]
    assert ASSET_BEACH[-8:] in block


# --- the negative: an ordinary asset gains nothing ---


async def test_a_non_attached_asset_gains_no_marker() -> None:
    """Assets already on the rail are untouched.

    If everything renders ``attached``, the marker means nothing and the agent
    picks the wrong clip out of a list where all forty look equally pointed-at."""
    preamble = await _render(
        _timeline(
            assets=[
                _asset(ASSET_OLD, "old-intro.mp4"),
                _asset(ASSET_BEACH, "beach.mp4", attached=True),
            ],
            attached=[ASSET_BEACH],
        )
    )

    assert "attached=" not in _rail_row_for(preamble, "old-intro.mp4")
    # And not smuggled in as a negative fact either — a row saying attached=no
    # spends the characters the marker was meant to save.
    assert "attached=no" not in preamble


# --- failures surface ---


async def test_a_failed_attach_is_reported_rather_than_swallowed() -> None:
    """The honesty floor. An import that failed leaves nothing to place, so the
    agent has to say which file did not arrive — placing the rest quietly looks
    like the whole request went through."""
    preamble = await _render(
        _timeline(
            assets=[_asset(ASSET_BEACH, "beach.mp4", attached=True)],
            attached=[ASSET_BEACH],
            attach_failed=["drone-4k.mp4: network error"],
        )
    )

    assert _has_block(preamble, "ATTACHMENTS THAT FAILED")
    assert "drone-4k.mp4: network error" in preamble
    lower = preamble.lower()
    assert "did not" in lower
    # The procedure has to tell the agent what to DO about it, not just show it.
    assert "name those files" in lower


async def test_failed_attaches_are_capped_like_last_edit_failures() -> None:
    """Twenty failures do not get twenty lines. Prose, not entities — the cap is
    the same ``_MAX_FAILURES`` the last-edit block uses, and the overflow is
    counted so the agent knows it is not seeing all of them."""
    failures = [f"clip-{n:02d}.mp4: upload rejected" for n in range(20)]
    preamble = await _render(_timeline(attach_failed=failures))

    block = preamble.split("ATTACHMENTS THAT FAILED", 1)[1]
    assert "clip-00.mp4" in block
    assert "clip-09.mp4" in block
    assert "clip-10.mp4" not in block
    assert f"…and {20 - studio_editor._MAX_FAILURES} more" in block


# --- absence renders nothing ---


async def test_no_attachment_renders_no_block_and_no_stray_heading() -> None:
    """The ordinary turn — no `@`, nothing attached — is byte-identical to what
    shipped before this feature, so the blocks cost nothing when unused.

    Both the absent key and the explicit empty list are covered: the frontend
    omits them when there is nothing, but an emitter that sends ``[]`` must not
    render a heading over an empty list."""
    for timeline in (
        _timeline(),
        _timeline(attached=[], attach_failed=[]),
    ):
        preamble = await _render(timeline)

        assert not _has_block(preamble, "ATTACHED THIS TURN")
        assert not _has_block(preamble, "ATTACHMENTS THAT FAILED")
        assert "attached=" not in preamble
        # The rest of the timeline still rendered — the assertions above are not
        # passing because the whole block went missing.
        assert "MEDIA RAIL" in preamble
        assert "old-intro.mp4" in preamble


async def test_an_empty_rail_points_at_the_attach_affordance() -> None:
    """With nothing to place, the preamble names the way media gets there.

    It used to name only 'drag files onto the rail' / 'Add files', neither of
    which is the affordance sitting in the composer the user is typing into."""
    preamble = await _render(_timeline(assets=[]))

    assert "MEDIA RAIL: empty" in preamble
    assert "`@`" in preamble
    assert "gallery" in preamble.lower()


# --- the claim that changed ---


async def test_preamble_no_longer_claims_media_cannot_be_imported() -> None:
    """THE regression this task closes.

    The rule read "You cannot import or generate media from here". Attach IS
    import, so half of that sentence became false the day the composer shipped —
    and a false capability claim does not error, it just makes the agent refuse
    a request the product supports. Generation is still out (that is /studio),
    and inventing an assetId is still out; only the import claim goes."""
    preamble = await _render(_timeline())
    lower = preamble.lower()

    assert "cannot import or generate media" not in lower
    assert "cannot import" not in lower
    # The replacement has to tell the user how media actually gets in.
    assert "`@`" in preamble
    assert "attach" in lower
    # Still forbids the two things that were always forbidden.
    assert "never invent one" in lower
    assert "/studio surface" in preamble
    # And the rail is still the only source of an assetId.
    assert "assetid must come from the media rail" in lower


async def test_attach_does_not_add_an_op_to_the_closed_vocabulary() -> None:
    """No verb was added. An attached asset is an ordinary rail asset by the time
    the agent sees it, so ``place_clip`` / ``place_audio`` already place it —
    naming an import op here would advertise a tool the validator rejects."""
    preamble = await _render(_timeline(attached=[ASSET_BEACH]))

    for invented in ("import_media", "attach_media", "import_asset", "add_media"):
        assert invented not in preamble
    assert "this list is closed" in preamble
    assert "place_clip" in preamble


async def test_no_timeline_still_short_circuits() -> None:
    """With no timeline open there is nothing to attach TO, so none of the new
    blocks may appear on the no-timeline path."""
    preamble = await _render(None)

    assert not _has_block(preamble, "ATTACHED THIS TURN")
    assert not _has_block(preamble, "ATTACHMENTS THAT FAILED")
    assert "No timeline is open" in preamble
