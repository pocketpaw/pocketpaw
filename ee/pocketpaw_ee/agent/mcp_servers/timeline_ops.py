# timeline_ops.py — validator for the closed timeline op vocabulary.
#
# Created: 2026-09-08 (feat/agentic-studio-editor). Python half of the contract
# whose TS half is paw-enterprise/src/lib/core/studio/editor/agent-ops.ts.
# Design: docs/design/drafts/2026-09-08-agentic-studio-editor.md in paw-workspace.
#
# 2026-09-11 (feat/agent-lane-ops): `add_lane`, and `track` resolving by lane
# NAME as well as id. The name path is load-bearing — track ids are minted when
# the batch applies in the browser, so a lane this batch creates has no id to
# quote, and validating names against the summary alone would reject the very
# sequence the verb exists for. Names are therefore checked against the lanes the
# agent was shown PLUS the ones earlier ops in the batch claim, and a duplicate
# is refused: two rows answering to one word makes placement a coin flip decided
# by document order.
#
# Validates VERBS and IDS, never geometry — whether a transition fits or a split
# lands too near an edge depends on document state this process does not have.
# The store decides that and refuses per-op.
#
# Server-side because of where the failure surfaces: the tool call happens here,
# the apply happens in a browser tab. An op that rides through gets declined by
# the client and the user just sees an edit that never happened — the Ripple
# action-verb bug. Failing here gives the agent the same turn to fix it.

from __future__ import annotations

import difflib
from typing import Any

# MUST match ``OP_KINDS`` in agent-ops.ts. Both sides check themselves against
# timeline_ops.contract.json, so a verb added in one place fails the other's suite.
OP_KINDS: frozenset[str] = frozenset(
    {
        "add_caption",
        "add_lane",
        "add_text",
        "move_clip",
        "place_audio",
        "place_clip",
        "remove_clip",
        "set_project",
        "set_transition",
        "set_transform",
        "set_volume",
        "split_clip",
        "style_captions",
        "style_text",
        "trim_clip",
        "add_keyframe",
        "clear_keyframes",
    }
)

# Ops carrying a ``clipId`` that must resolve in the summary.
_CLIP_OPS: frozenset[str] = frozenset(
    {
        "move_clip",
        "trim_clip",
        "split_clip",
        "remove_clip",
        "set_transition",
        "style_text",
        "set_transform",
        "add_keyframe",
        "clear_keyframes",
    }
)

# Ops that bring an asset onto the timeline.
_ASSET_OPS: frozenset[str] = frozenset({"place_clip", "place_audio"})

# Blast radius of one confused turn, not a performance limit.
MAX_OPS_PER_BATCH = 50

_TRANSITION_KINDS: frozenset[str] = frozenset(
    {"none", "crossfade", "dip", "slide", "push", "zoom", "blur"}
)

_ASPECT_RATIOS: frozenset[str] = frozenset(
    {"16:9", "9:16", "1:1", "4:3", "3:4", "3:2", "2:3", "4:5", "1.91:1"}
)

# The nine designed looks in text-presets.ts. Closed for the same reason the
# verbs are: applyTextPreset returns the style UNCHANGED for an unknown id, so
# a typo would report a restyle that never happened.
_TEXT_PRESETS: frozenset[str] = frozenset(
    {
        "clean",
        "pop",
        "boxed",
        "subtitle",
        "neon",
        "typewriter",
        "impact",
        "sticker",
        "editorial",
    }
)

# TextAnimKind in schema.ts — how a title arrives and leaves.
_ANIM_KINDS: frozenset[str] = frozenset({"none", "fade", "pop", "slide", "typewriter", "bounce"})

# CaptionStyleId — the CUE BOX model, a different axis from _TEXT_PRESETS.
_CAPTION_STYLES: frozenset[str] = frozenset({"plain", "boxed", "outlined"})

# AnimatableProp in schema.ts.
_ANIM_PROPS: frozenset[str] = frozenset({"x", "y", "scale", "rotation", "opacity", "volume"})

# TrackKind in schema.ts. 'overlay' is in the enum and nothing authors one yet;
# it stays accepted because the document already allows it and refusing here
# would be this file inventing a narrower contract than the one it validates.
_LANE_KINDS: frozenset[str] = frozenset({"video", "audio", "text", "overlay"})

# TrackRole. One member, and it is the one that matters: a cue lane and a plain
# text lane are different groups, and the store keeps titles out of the cue list.
_LANE_ROLES: frozenset[str] = frozenset({"captions"})

# Long enough to say what a lane carries, short enough to stay a chip on a
# timeline row. Mirrors AddLaneOp's z.string().min(1).max(40).
_MAX_LANE_NAME = 40

# Named easings. The tuple form (cubic bezier) is deliberately not offered: it
# is four numbers with a throwing domain and nothing an agent can reason about.
_EASINGS: frozenset[str] = frozenset({"linear", "easeIn", "easeOut", "easeInOut", "hold"})


class TimelineSummary:
    """The ids the agent was shown, as the tool handler sees them.

    Built from the ``surface_meta`` the editor page stamps on every chat send —
    the same projection rendered into the preamble. That equality is the point:
    validating against anything else would let the tool accept an id the agent
    was never told about, or reject one it was.
    """

    def __init__(
        self,
        *,
        clip_ids: set[str] | None = None,
        asset_ids: set[str] | None = None,
        track_ids: set[str] | None = None,
        track_names: set[str] | None = None,
        has_timeline: bool = True,
    ) -> None:
        self.clip_ids = clip_ids or set()
        self.asset_ids = asset_ids or set()
        self.track_ids = track_ids or set()
        # Lanes are addressable by NAME as well as id, which is what lets one
        # batch create a lane and then put something on it: track ids are minted
        # at apply time, so a lane the batch is adding has no id to quote. Kept
        # case-folded because the agent is quoting a name it wrote in another op,
        # not copying an id out of the preamble.
        self.track_names = {n.strip().casefold() for n in (track_names or set()) if n.strip()}
        self.has_timeline = has_timeline

    @classmethod
    def from_meta(cls, meta: dict[str, Any] | None) -> TimelineSummary:
        """Read the summary out of a ``surface_meta`` dict.

        Absent or malformed meta yields ``has_timeline=False`` rather than an
        empty-but-present summary. The two mean different things to the caller:
        "no editor open" is a thing to tell the user, while "an open timeline
        with no clips" is a thing to start arranging into.
        """
        if not isinstance(meta, dict):
            return cls(has_timeline=False)
        timeline = meta.get("timeline")
        if not isinstance(timeline, dict):
            return cls(has_timeline=False)

        def _ids(key: str) -> set[str]:
            rows = timeline.get(key)
            if not isinstance(rows, list):
                return set()
            out: set[str] = set()
            for row in rows:
                if isinstance(row, dict) and isinstance(row.get("id"), str):
                    out.add(row["id"])
                elif isinstance(row, str):
                    out.add(row)
            return out

        def _names(key: str) -> set[str]:
            rows = timeline.get(key)
            if not isinstance(rows, list):
                return set()
            return {
                row["name"]
                for row in rows
                if isinstance(row, dict) and isinstance(row.get("name"), str)
            }

        return cls(
            clip_ids=_ids("clips"),
            asset_ids=_ids("assets"),
            track_ids=_ids("tracks"),
            track_names=_names("tracks"),
            has_timeline=True,
        )


def _suggest(value: str, known: set[str]) -> str:
    """`… did you mean X?`, or an empty string.

    Same move as ``validate_action_verbs``' nearest-verb hint. An agent that
    mistypes an id retries correctly off this; one that INVENTED the id gets
    nothing appended, which is the honest answer — there is nothing to mean.
    """
    if not known:
        return ""
    close = difflib.get_close_matches(value, sorted(known), n=1, cutoff=0.6)
    return f" Did you mean {close[0]!r}?" if close else ""


def _require_str(raw: dict[str, Any], key: str, index: int) -> tuple[str | None, str | None]:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        return None, f"ops[{index}].{key} must be a non-empty string."
    return value, None


def _resolve(
    raw: Any, known: set[str], *, field: str, noun: str, index: int
) -> tuple[str | None, str | None]:
    """Resolve an id the agent sent back into a FULL id from the summary.

    The preamble renders ids through ``entity_line``, which shortens anything
    over 8 characters to a ``…tail``. Clip and asset ids are 21-char nanoids, so
    what the agent sees is ALWAYS a tail and what it sends back is a tail too.
    Matching those against full ids would reject every well-formed call.

    Reuses ``pockets.id_resolve`` rather than re-deriving the rule: exact match
    wins outright, a tail matching one candidate resolves, a tail matching
    several is an error rather than a guess. Returns the full id, which is what
    the client's store looks up by.
    """
    from pocketpaw_ee.cloud.pockets.id_resolve import AmbiguousId, resolve_id

    try:
        return resolve_id(raw, [{"id": i} for i in known]), None
    except AmbiguousId:
        # "Use more of the id" would be bad advice: the agent only ever saw the
        # tail, so it HAS no more of it. Vanishingly rare (21-char nanoids), but
        # the recourse when it happens is a person, not a retry.
        return None, (
            f"ops[{index}].{field} {raw!r} matches more than one {noun}, so it is "
            "not safe to guess which. Ask the user which one they mean."
        )
    except KeyError:
        hint = _suggest(str(raw), known)
        return None, f"ops[{index}].{field} {raw!r} is not {noun}.{hint}"


def _check_number(
    raw: dict[str, Any], key: str, index: int, *, minimum: float | None = 0.0
) -> str | None:
    """Validate an optional numeric field. Returns an error string or None."""
    if key not in raw or raw[key] is None:
        return None
    value = raw[key]
    # bool is a subclass of int; `{"volume": true}` must not read as 1.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return f"ops[{index}].{key} must be a number."
    if minimum is not None and value < minimum:
        return f"ops[{index}].{key} must be >= {minimum}."
    return None


def validate_ops(
    ops: Any, summary: TimelineSummary
) -> tuple[list[dict[str, Any]] | None, str | None]:
    """Validate an agent's op batch against the closed set and the live ids.

    Returns ``(clean_ops, None)`` or ``(None, error)``. The error is written to
    be read by a MODEL and acted on in the same turn — it names the index, the
    field and the nearest real id — which is why it is a precise string rather
    than an exception type.

    Mirrors ``validate_flow_spec``'s shape so both studio tools fail the same
    way. All-or-nothing on purpose: a half-valid batch that partially applies
    leaves an arrangement nobody asked for and no clean thing to undo to.
    """
    if not summary.has_timeline:
        return None, (
            "No timeline is open. The /studio/editor canvas must be open with a "
            "project loaded before it can be edited. Ask the user to open the "
            "editor, or offer to open it for them."
        )

    if not isinstance(ops, list):
        return None, "`ops` must be a list of operations."
    if not ops:
        return None, "`ops` was empty — nothing to do."
    if len(ops) > MAX_OPS_PER_BATCH:
        return None, (
            f"`ops` had {len(ops)} operations; the maximum is {MAX_OPS_PER_BATCH}. "
            "Split the work across turns, confirming with the user in between."
        )

    clean: list[dict[str, Any]] = []
    # Lane names minted by `add_lane` ops earlier in this batch. A later op may
    # address them even though no id exists yet — see TimelineSummary.track_names.
    batch_lane_names: set[str] = set()

    for i, raw in enumerate(ops):
        if not isinstance(raw, dict):
            return None, f"ops[{i}] must be an object."

        kind = raw.get("op")
        if not isinstance(kind, str):
            return None, f"ops[{i}].op is missing."
        if kind not in OP_KINDS:
            hint = _suggest(kind, set(OP_KINDS))
            return None, (
                f"ops[{i}].op {kind!r} is not a timeline operation.{hint} "
                f"Valid operations: {', '.join(sorted(OP_KINDS))}."
            )

        # ── ids must name something the agent was actually shown ───────────
        # Every id is REWRITTEN to its full form: the agent saw a tail, and the
        # client's store looks up by the whole id.
        op = dict(raw)

        if kind in _CLIP_OPS:
            clip_id, err = _require_str(raw, "clipId", i)
            if err:
                return None, err
            full, err = _resolve(
                clip_id, summary.clip_ids, field="clipId", noun="a clip on this timeline", index=i
            )
            if err:
                return None, err
            op["clipId"] = full

        if kind in _ASSET_OPS:
            asset_id, err = _require_str(raw, "assetId", i)
            if err:
                return None, err
            full, err = _resolve(
                asset_id, summary.asset_ids, field="assetId", noun="on the media rail", index=i
            )
            if err:
                return None, (f"{err} Only media already imported into the project can be placed.")
            op["assetId"] = full

        # `after` and `track` are optional everywhere they appear, but when
        # present they are ids and get the same treatment.
        if raw.get("after") is not None:
            full, err = _resolve(
                raw["after"],
                summary.clip_ids,
                field="after",
                noun="a clip on this timeline",
                index=i,
            )
            if err:
                return None, err
            op["after"] = full

        if raw.get("track") is not None:
            track_ref = raw["track"]
            # A NAME resolves as-is and is passed through untouched — the client
            # matches it against live track names. Checked against the lanes the
            # agent was shown PLUS the ones earlier ops in this batch create, so
            # "open a Score lane, put the music on it" validates as one batch
            # even though the lane has no id until the batch applies.
            if isinstance(track_ref, str) and track_ref.strip().casefold() in (
                summary.track_names | batch_lane_names
            ):
                op["track"] = track_ref
            else:
                full, err = _resolve(
                    track_ref,
                    summary.track_ids,
                    field="track",
                    noun="a track on this timeline",
                    index=i,
                )
                if err:
                    known_names = sorted(summary.track_names | batch_lane_names)
                    extra = (
                        f" Lanes can also be named directly: {', '.join(known_names)}."
                        if known_names
                        else ""
                    )
                    return None, f"{err}{extra}"
                op["track"] = full

        # ── per-verb argument checks ───────────────────────────────────────
        err = _validate_verb(kind, op, i, summary)
        if err:
            return None, err

        if kind == "add_lane":
            err = _check_lane_name(op, i, summary, batch_lane_names)
            if err:
                return None, err
            name = op.get("name")
            if isinstance(name, str) and name.strip():
                batch_lane_names.add(name.strip().casefold())

        clean.append(op)

    return clean, None


def _check_lane_name(
    raw: dict[str, Any], i: int, summary: TimelineSummary, batch_names: set[str]
) -> str | None:
    """A lane name has to be unique, because `track` resolves by it.

    Two rows answering to one word would make placement a coin flip decided by
    document order, and the agent would be told the clip went somewhere it did
    not. Checked against the lanes on the timeline AND the ones earlier ops in
    this batch already claimed.
    """
    name = raw.get("name")
    if name is None:
        return None
    if not isinstance(name, str) or not name.strip():
        return f"ops[{i}].name must be a non-empty string when given."
    if len(name.strip()) > _MAX_LANE_NAME:
        return f"ops[{i}].name is longer than {_MAX_LANE_NAME} characters."
    folded = name.strip().casefold()
    if folded in summary.track_names:
        return (
            f"ops[{i}].name {name!r} is already a lane on this timeline. Pick a "
            "different name, or target the existing lane with `track` instead of "
            "opening a second one."
        )
    if folded in batch_names:
        return f"ops[{i}].name {name!r} was already used by an earlier add_lane in this batch."
    return None


def _validate_verb(kind: str, raw: dict[str, Any], i: int, summary: TimelineSummary) -> str | None:
    """Per-verb argument validation. Returns an error string or None."""

    for key in ("atMs", "inMs", "outMs", "fromMs", "toMs", "durationMs"):
        err = _check_number(raw, key, i)
        if err:
            return err

    if kind == "split_clip":
        if "atMs" not in raw or raw["atMs"] is None:
            return f"ops[{i}].atMs is required for split_clip — where should the cut land?"

    elif kind == "add_lane":
        lane_kind = raw.get("kind")
        if not isinstance(lane_kind, str) or lane_kind not in _LANE_KINDS:
            hint = _suggest(str(lane_kind), set(_LANE_KINDS))
            return (
                f"ops[{i}].kind {lane_kind!r} is not a lane kind.{hint} "
                f"Valid kinds: {', '.join(sorted(_LANE_KINDS))}."
            )
        role = raw.get("role")
        if role is not None and (not isinstance(role, str) or role not in _LANE_ROLES):
            return (
                f"ops[{i}].role {role!r} is not a lane role. The only role is "
                "'captions', which makes a text lane a cue lane; omit it for a "
                "plain lane."
            )
        if role == "captions" and lane_kind != "text":
            return (
                f"ops[{i}] asked for a {lane_kind} lane with role 'captions'. A cue "
                "lane is a TEXT lane wearing that role — pass kind 'text'."
            )

    elif kind == "set_transition":
        tk = raw.get("kind")
        if not isinstance(tk, str) or tk not in _TRANSITION_KINDS:
            hint = _suggest(str(tk), set(_TRANSITION_KINDS))
            return (
                f"ops[{i}].kind {tk!r} is not a transition.{hint} "
                f"Valid kinds: {', '.join(sorted(_TRANSITION_KINDS))}."
            )
        err = _check_number(raw, "softness", i)
        if err:
            return err
        softness = raw.get("softness")
        if isinstance(softness, (int, float)) and not isinstance(softness, bool):
            if not 0 <= softness <= 1:
                return f"ops[{i}].softness must be between 0 and 1."

    elif kind == "add_caption":
        text = raw.get("text")
        if not isinstance(text, str) or not text.strip():
            return f"ops[{i}].text must be a non-empty string."
        for key in ("fromMs", "toMs"):
            if key not in raw or raw[key] is None:
                return f"ops[{i}].{key} is required for {kind}."
        # A zero- or negative-length cue renders as nothing at all and reads to
        # the user as the caption having been dropped.
        if raw["toMs"] <= raw["fromMs"]:
            return f"ops[{i}].toMs must be greater than fromMs."
        cue_style = raw.get("style")
        if cue_style is not None and cue_style not in _CAPTION_STYLES:
            hint = _suggest(str(cue_style), set(_CAPTION_STYLES))
            return (
                f"ops[{i}].style {cue_style!r} is not a caption style.{hint} "
                f"Valid styles: {', '.join(sorted(_CAPTION_STYLES))}."
            )
        if raw.get("anchorClip") is not None:
            full, err = _resolve(
                raw["anchorClip"],
                summary.clip_ids,
                field="anchorClip",
                noun="a clip on this timeline",
                index=i,
            )
            if err:
                return err
            raw["anchorClip"] = full

    elif kind in ("add_text", "style_text"):
        # add_text mints a clip and needs words + a span; style_text patches one
        # that exists, where every field is optional.
        if kind == "add_text":
            for key in ("fromMs", "toMs"):
                if key not in raw or raw[key] is None:
                    return f"ops[{i}].{key} is required for add_text."
            if raw["toMs"] <= raw["fromMs"]:
                return f"ops[{i}].toMs must be greater than fromMs."
        if "text" in raw and raw["text"] is not None:
            if not isinstance(raw["text"], str) or not raw["text"].strip():
                return f"ops[{i}].text must be a non-empty string."
        elif kind == "add_text":
            return f"ops[{i}].text must be a non-empty string."

        preset = raw.get("presetId")
        if preset is not None and preset not in _TEXT_PRESETS:
            hint = _suggest(str(preset), set(_TEXT_PRESETS))
            return (
                f"ops[{i}].presetId {preset!r} is not a text style.{hint} "
                f"Valid styles: {', '.join(sorted(_TEXT_PRESETS))}."
            )
        for key in ("animIn", "animOut"):
            value = raw.get(key)
            if value is not None and value not in _ANIM_KINDS:
                hint = _suggest(str(value), set(_ANIM_KINDS))
                return (
                    f"ops[{i}].{key} {value!r} is not a motion.{hint} "
                    f"Valid motions: {', '.join(sorted(_ANIM_KINDS))}."
                )
        err = _check_number(raw, "fontSize", i)
        if err:
            return err
        err = _check_number(raw, "animDurationMs", i)
        if err:
            return err
        align = raw.get("align")
        if align is not None and align not in ("left", "center", "right"):
            return f"ops[{i}].align must be 'left', 'center' or 'right'."

    elif kind == "style_captions":
        style = raw.get("style")
        if style is not None and style not in _CAPTION_STYLES:
            hint = _suggest(str(style), set(_CAPTION_STYLES))
            return (
                f"ops[{i}].style {style!r} is not a caption style.{hint} "
                f"Valid styles: {', '.join(sorted(_CAPTION_STYLES))}. "
                "(The nine named looks are for titles, not captions.)"
            )
        err = _check_number(raw, "fontSize", i)
        if err:
            return err
        if not any(raw.get(k) is not None for k in ("style", "fontSize", "y")):
            return f"ops[{i}] changes nothing — give style, fontSize or y."

    elif kind == "set_transform":
        for key in ("x", "y", "rotation"):
            err = _check_number(raw, key, i, minimum=None)
            if err:
                return err
        err = _check_number(raw, "scale", i, minimum=None)
        if err:
            return err
        scale = raw.get("scale")
        if scale is not None and scale <= 0:
            return f"ops[{i}].scale must be greater than 0."
        err = _check_number(raw, "opacity", i)
        if err:
            return err
        opacity = raw.get("opacity")
        if opacity is not None and not 0 <= opacity <= 1:
            return f"ops[{i}].opacity must be between 0 and 1."
        if not any(raw.get(k) is not None for k in ("x", "y", "scale", "rotation", "opacity")):
            return f"ops[{i}] changes nothing — give x, y, scale, rotation or opacity."

    elif kind in ("add_keyframe", "clear_keyframes"):
        prop = raw.get("prop")
        if prop not in _ANIM_PROPS:
            hint = _suggest(str(prop), set(_ANIM_PROPS))
            return (
                f"ops[{i}].prop {prop!r} cannot be animated.{hint} "
                f"Animatable: {', '.join(sorted(_ANIM_PROPS))}."
            )
        if kind == "add_keyframe":
            if "atMs" not in raw or raw["atMs"] is None:
                return f"ops[{i}].atMs is required for add_keyframe."
            err = _check_number(raw, "value", i, minimum=None)
            if err:
                return err
            ease = raw.get("ease")
            if ease is not None and ease not in _EASINGS:
                hint = _suggest(str(ease), set(_EASINGS))
                return (
                    f"ops[{i}].ease {ease!r} is not an easing.{hint} "
                    f"Valid easings: {', '.join(sorted(_EASINGS))}."
                )

    elif kind == "set_volume":
        target, err = _require_str(raw, "target", i)
        if err:
            return err
        # 'master' is the one non-id value this field takes.
        if target != "master":
            full, err = _resolve(
                target, summary.clip_ids, field="target", noun="a clip on this timeline", index=i
            )
            if err:
                return f"{err} (Use 'master' for the whole timeline.)"
            raw["target"] = full
        err = _check_number(raw, "volume", i)
        if err:
            return err
        volume = raw.get("volume")
        if volume is None:
            return f"ops[{i}].volume is required for set_volume."
        if not 0 <= volume <= 2:
            return f"ops[{i}].volume must be between 0 and 2."

    elif kind == "set_project":
        aspect = raw.get("aspectRatio")
        if aspect is not None and aspect not in _ASPECT_RATIOS:
            hint = _suggest(str(aspect), set(_ASPECT_RATIOS))
            return (
                f"ops[{i}].aspectRatio {aspect!r} is not a known frame shape.{hint} "
                f"Valid: {', '.join(sorted(_ASPECT_RATIOS))}."
            )
        fps = raw.get("fps")
        if fps is not None:
            if isinstance(fps, bool) or not isinstance(fps, (int, float)) or fps <= 0:
                return f"ops[{i}].fps must be a positive number."
        fit = raw.get("fit")
        if fit is not None and fit not in ("contain", "cover"):
            return f"ops[{i}].fit must be 'contain' or 'cover'."

    return None


__all__ = [
    "MAX_OPS_PER_BATCH",
    "OP_KINDS",
    "TimelineSummary",
    "validate_ops",
]
