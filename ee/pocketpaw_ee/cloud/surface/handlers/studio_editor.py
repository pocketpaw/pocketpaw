# studio_editor.py — /studio/editor surface preamble.
#
# Created: 2026-09-08 (feat/agentic-studio-editor). Orients the chat agent when
# the user is looking at the timeline editor. Until this existed the route
# mapped to NOTHING (surfaces.ts declared `studio` with no routeVariants), so
# the client stamped GENERIC and the agent answered "arrange these clips" by
# building a dashboard about them.
#
# Unlike handlers/studio.py this preamble carries LIVE DATA: the open timeline,
# projected to ids and times. It has to — the document lives in the browser
# (IndexedDB + OPFS) and there is no server copy for a handler to fetch, which
# is also why there is no read tool. The summary IS the read path.
#
# Rows render through ``entity_line`` (mandatory: ``edit_timeline`` declares
# required clipId / assetId, and tests/cloud/surface/test_entity_id_contract.py
# derives addressable kinds from the MCP tool schemas). Ids therefore render as
# ``…tail``, and the tool's validator resolves tails back through
# ``pockets.id_resolve``.

from __future__ import annotations

from typing import Any

from pocketpaw.prompt.entity import entity_line
from pocketpaw_ee.cloud.surface.domain import SurfaceMeta, SurfacePreamble
from pocketpaw_ee.cloud.surface.handlers._helpers import content_key

# Rows are capped rather than truncated silently: a 200-clip timeline would
# otherwise crowd out the procedure block that tells the agent what to do with
# it. The tail line keeps the agent honest about what it cannot see.
_MAX_ROWS = 40


def _fmt_ms(value: Any) -> str:
    """Milliseconds as seconds, one decimal — the unit a person edits in."""
    try:
        return f"{float(value) / 1000:.1f}s"
    except (TypeError, ValueError):
        return "?"


def _rows(items: list[dict[str, Any]], render) -> list[str]:
    out = [render(item) for item in items[:_MAX_ROWS]]
    if len(items) > _MAX_ROWS:
        # Not an entity row and deliberately not shaped like one: it names no
        # entity and carries no id, so it must not read as something the
        # agent can address.
        out.append(f"  …and {len(items) - _MAX_ROWS} more (ask the user to narrow the request)")
    return out


def _timeline_block(timeline: dict[str, Any]) -> str:
    """Render the open timeline as ids + times. Never the whole document."""
    lines: list[str] = []

    name = timeline.get("name") or "Untitled timeline"
    lines.append(
        f"Project: {name} — {timeline.get('aspect_ratio') or '?'}, "
        f"{timeline.get('fps') or '?'}fps, {_fmt_ms(timeline.get('duration_ms'))} long."
    )

    tracks = [t for t in (timeline.get("tracks") or []) if isinstance(t, dict)]
    if tracks:
        lines.append("")
        lines.append("TRACKS (lanes clips sit on):")
        lines.extend(
            _rows(
                tracks,
                lambda t: entity_line(
                    t.get("name"),
                    t.get("id"),
                    kind=t.get("kind"),
                    role=t.get("role") or "-",
                    clips=t.get("clip_count"),
                ),
            )
        )

    assets = [a for a in (timeline.get("assets") or []) if isinstance(a, dict)]
    lines.append("")
    if assets:
        lines.append("MEDIA RAIL (what can be placed — nothing else exists):")
        lines.extend(
            _rows(
                assets,
                lambda a: entity_line(
                    a.get("name"),
                    a.get("id"),
                    kind=a.get("kind"),
                    duration=_fmt_ms(a.get("duration_ms")),
                    used=("yes" if a.get("in_use") else "no"),
                ),
            )
        )
    else:
        lines.append(
            "MEDIA RAIL: empty. Nothing can be placed until the user imports "
            "media (drag files onto the rail, or 'Add files')."
        )

    clips = [c for c in (timeline.get("clips") or []) if isinstance(c, dict)]
    lines.append("")
    if clips:
        lines.append("CLIPS ON THE TIMELINE (in timeline order):")
        lines.extend(
            _rows(
                clips,
                lambda c: entity_line(
                    c.get("label"),
                    c.get("id"),
                    track=c.get("track_name"),
                    start=_fmt_ms(c.get("start_ms")),
                    end=_fmt_ms(c.get("end_ms")),
                    kind=c.get("kind"),
                ),
            )
        )
    else:
        lines.append("CLIPS ON THE TIMELINE: none yet — the timeline is empty.")

    last = timeline.get("last_edit")
    if isinstance(last, dict) and last.get("failures"):
        # The result of the PREVIOUS turn's edit. This is the only confirmation
        # channel: edit_timeline returns once the batch is validated and
        # dispatched, not once the browser has applied it.
        lines.append("")
        lines.append("YOUR LAST EDIT DID NOT FULLY APPLY:")
        lines.extend(f"  • {f}" for f in list(last["failures"])[:10])

    return "\n".join(lines)


_PROCEDURE = """\
<studio-editor-procedure>
To change the timeline, call `mcp__pocketpaw_timeline__edit_timeline` with an
`ops` list. ONE call carries the whole change — "arrange these three clips, put
a caption here, drop the music underneath" is a SINGLE batch, not six calls. The
batch is applied atomically and lands as ONE undo step, so a user who dislikes
the result presses Ctrl+Z once.

Operations (this list is closed — an invented verb is rejected):
- place_clip    {assetId, atMs|after, track?, inMs?, outMs?}
- move_clip     {clipId, atMs|after, track?}
- trim_clip     {clipId, inMs?, outMs?}     absolute SOURCE points, not a delta
- split_clip    {clipId, atMs}
- remove_clip   {clipId}
- set_transition{clipId, kind, durationMs?, direction?, color?, softness?}
- add_text      {text, fromMs, toMs, + any style field below}
- style_text    {clipId, text?, + any style field below}   restyle what exists
- add_caption   {text, fromMs, toMs, anchorClip?, style?}
- style_captions{style?, fontSize?, y?}                    the whole cue set
- place_audio   {assetId, atMs|after, track?, volume?, muted?}
- set_volume    {target: clipId|'master', volume}
- set_transform {clipId, x?, y?, scale?, rotation?, opacity?}
- add_keyframe  {clipId, prop, atMs, value?, ease?}
- clear_keyframes {clipId, prop, atMs?}
- set_project   {name?, aspectRatio?, fps?, fit?, background?}

Style fields (add_text and style_text both take these):
  presetId   clean | pop | boxed | subtitle | neon | typewriter | impact |
             sticker | editorial — a whole designed look
  fontSize   pixels          color   any CSS colour     align  left|center|right
  animIn / animOut   none | fade | pop | slide | typewriter | bounce
  animDurationMs     how long each end runs, capped at half the clip
  animDirection      slide only: the edge the text travels FROM

Rules that matter:
- TO ARRANGE NEW CLIPS END TO END, PLACE THEM WITH NO POSITION. Omit both atMs
  and after, and each clip lands after the last one on its lane. "Arrange these
  three clips" is three bare place_clip ops, in the order you want them. Do NOT
  add up durations to compute atMs yourself.
  You CANNOT use `after` for a clip created in the same batch — clip ids are
  minted when the batch is applied, so a clip you are creating right now has no
  id yet. `after: <clipId>` is for anchoring to a clip ALREADY on the timeline
  (one listed above); it is resolved against the live document, so it stays right
  even when an earlier op in the same batch changed a length.
- ANCHOR CAPTIONS. Caption times are timeline-absolute and do NOT move when a
  clip moves. If a caption belongs to a clip's dialogue, pass
  `anchorClip: <clipId>` and give fromMs/toMs as offsets from that clip's start.
  Absolute times are only right for captions that belong to the timeline itself.
- ONLY PLACE WHAT EXISTS. assetId must come from the MEDIA RAIL above. You
  cannot import or generate media from here; if the rail lacks what the user
  described, say so and ask them to add it.
- A TRANSITION IS EXPLICIT. Clips touching or overlapping does not create one.
  Use set_transition, and only when the user asked for one.
- COPY IDS EXACTLY as they appear above (the `…` prefix and all).
- RESTYLE, DO NOT RE-ADD. "make that title bigger" is style_text on the clip
  that exists. add_text would leave the original in place and put a second one
  on top of it.
- A PRESET SETS THE WHOLE LOOK, and any style field you pass alongside wins over
  it — "Neon but 90px" is ONE style_text with presetId and fontSize together.
- TWO DIFFERENT STYLE VOCABULARIES, and 'boxed' is in both. The nine named looks
  above are for TITLES (add_text / style_text). Captions take plain | boxed |
  outlined, and are styled as a SET with style_captions — never one cue at a
  time.
- TRANSFORM IS ABSOLUTE, not a nudge. x and y are pixel offsets from the frame
  CENTRE, so negative is left and up; scale 1 is original size; opacity 0-1.
- TO ANIMATE, PIN TWO VALUES. add_keyframe at the start time and again at the
  end time, and the property moves between them. atMs is TIMELINE time and must
  fall inside the clip. Animatable: x, y, scale, rotation, opacity, volume —
  nothing else (font size and colour cannot be animated).

Honesty (this surface has burned people before):
- edit_timeline returns when the batch is VALIDATED AND DISPATCHED — not when
  the browser has applied it. Say what you arranged, never "done" or "applied".
- If the tool returns an error, relay it plainly and fix the ops. NEVER claim an
  edit that did not go through, and never invent a clip, asset or timing.
- If "YOUR LAST EDIT DID NOT FULLY APPLY" appears above, tell the user which
  parts the editor declined before doing anything else.

To render the finished video, call `mcp__pocketpaw_timeline__export_timeline`.
Never batch an export with edits — it would render a half-built timeline.
</studio-editor-procedure>"""

_NO_TIMELINE = """\
<studio-editor-procedure>
No timeline is open, so there is nothing to edit yet. Ask the user to open a
project in the editor (or start a new one) and to add media to the rail. Do NOT
call the timeline tools — they will refuse. Do not offer to generate media here
either; that is the /studio surface.
</studio-editor-procedure>"""


async def build_preamble(workspace_id: str, user_id: str, meta: SurfaceMeta) -> SurfacePreamble:
    """Render the /studio/editor preamble — arrange what is on the timeline."""
    route = meta.route_path or "/studio/editor"
    timeline = meta.timeline if isinstance(meta.timeline, dict) else None

    orientation = (
        "<studio-editor-orientation>\n"
        "The user is looking at a VIDEO TIMELINE EDITOR — tracks, clips, "
        "captions, transitions. Your job here is to ARRANGE what is already on "
        "the timeline: order clips, trim them, place titles and captions, and "
        "lay audio into lanes. This is NOT a dashboard: do not build widgets, "
        "charts, a pocket or a ui-spec. It is also NOT the generation surface: "
        "you cannot make new footage here. Talk about 'clips', 'tracks', "
        "'captions' and 'the timeline'.\n"
        "</studio-editor-orientation>"
    )

    if timeline is None:
        body = _NO_TIMELINE
    else:
        body = f"<timeline>\n{_timeline_block(timeline)}\n</timeline>\n{_PROCEDURE}"

    text = f'<surface kind="studio_editor" route="{route}" />\n{orientation}\n{body}'
    # Digest, not meta_key. handlers/studio.py can key on the route because its
    # preamble is static; this one renders the live clip list, so a route key
    # would serve a stale timeline all session. Keying on `updated_at` would go
    # the other way and reconnect (~12s on the SDK backend) after every edit,
    # including ones that change nothing the agent can see. The digest moves
    # exactly when the agent's view does.
    return SurfacePreamble(text=text, cache_key=content_key("studio_editor", text))


__all__ = ["build_preamble"]
