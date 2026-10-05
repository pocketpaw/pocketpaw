# studio_craft.py — preambles for the three craft studio surfaces:
# /studio/vector, /studio/photo and /studio/design (print work for print-shop
# owners in India).
#
# One frame, three document renderers. Each preamble carries LIVE DATA: the page
# stamps a projection of the open document onto every send
# (``SurfaceMeta.vector`` / ``.photo`` / ``.design``) because the document lives
# in the browser (the craft engines as WebAssembly); the projection IS the read
# path. The procedure block lists the app's exact op names with one valid
# example each (agents guessed wrong names until they were listed). Design's
# rules make text colour (set_text_color; set_paint is the frame's box), overset
# text (set_text fit) and the browser's layout check explicit: its "overset",
# "shrunk", "overlap" and "off page frame <id>:" lines, and that the agent never
# claims text fits or does not overlap, since it replies before the check runs.
#
# Projection shapes the pages send:
#   vector: {title?, colorMode, units?, artboard: {width, height} (pt),
#            layers: [{id, kind, name?, bounds: {x,y,width,height}, fill?, stroke?,
#                      strokeWidth?, text?, children?}], selection: [id], last_edit?}
#   photo:  {name?, width, height (px), ppi, colorMode,
#            layers: [{id, name, kind, visible, opacity (0..1)}] (bottom first),
#            activeLayer: id|null, selection: {bounds: {x,y,width,height}}|null,
#            last_edit?}
#   design: {title?, size_mm: {width, height}, bleed_mm, current_page,
#            pages: [{index, frames: [{id, kind, bounds_mm: {x,y,width,height},
#                     text?, fill?, field?}]}], selection: [id], last_edit?}
#            (field = the template field a Quick mode frame carries: never deleted)
#   last_edit = {failures: [str]} from the last applied batch.
# Bounds in design are mm from the page's trim top-left.
#
# Entity rows render through ``entity_line`` (test_entity_id_contract.py). Ids
# are the engines' small integers, so they render whole.

from __future__ import annotations

from typing import Any

from pocketpaw.prompt.entity import entity_line
from pocketpaw_ee.agent.mcp_servers.craft import TOOL_IDS
from pocketpaw_ee.agent.mcp_servers.craft_ops import op_cheatsheet
from pocketpaw_ee.cloud.surface.domain import SurfaceMeta, SurfacePreamble
from pocketpaw_ee.cloud.surface.handlers._helpers import content_key

_MAX_ROWS = 80  # entity rows; the procedure block must still fit
_MAX_FAILURES = 10
_MAX_TEXT = 60


def _num(value: Any) -> str:
    try:
        return f"{float(value):g}"
    except (TypeError, ValueError):
        return "?"


def _paint(value: Any) -> str:
    if isinstance(value, dict) and set(value) == {"c", "m", "y", "k"}:
        return "cmyk(" + ",".join(_num(value[k]) for k in "cmyk") + ")"
    if isinstance(value, dict) and "gray" in value:
        return f"gray({_num(value['gray'])})"
    if value is None or value == "None":
        return "none"
    return str(value)


def _bounds(b: Any) -> str:
    if not isinstance(b, dict):
        return "?"
    return f"{_num(b.get('x'))},{_num(b.get('y'))} {_num(b.get('width'))}x{_num(b.get('height'))}"


def _clip(text: Any) -> str | None:
    if not isinstance(text, str) or not text:
        return None
    return repr(text if len(text) <= _MAX_TEXT else text[:_MAX_TEXT] + "…")


def _capped(rows: list[str], total: int, noun: str) -> list[str]:
    if total > _MAX_ROWS:
        rows.append(f"  …and {total - _MAX_ROWS} more {noun} (ask the user to select what to edit)")
    return rows


def _tail(doc: dict[str, Any], selection: str) -> list[str]:
    """The SELECTION line and any failures from the last applied batch."""
    lines = [f"SELECTION: {selection}"]
    last = doc.get("last_edit") if isinstance(doc.get("last_edit"), dict) else {}
    failures = [str(f) for f in (last.get("failures") or []) if f]
    if failures:
        lines.append("YOUR LAST BATCH FAILED IN THE BROWSER (fix before building on it):")
        lines += [f"  ! {f}" for f in failures[:_MAX_FAILURES]]
        if len(failures) > _MAX_FAILURES:
            lines.append(f"  …and {len(failures) - _MAX_FAILURES} more")
    return lines


def _id_list(doc: dict[str, Any]) -> str:
    sel = [s for s in (doc.get("selection") or []) if isinstance(s, (int, str))]
    return ", ".join(str(s) for s in sel) if sel else "nothing"


def _list(doc: dict[str, Any], key: str) -> list[Any]:
    value = doc.get(key)
    return value if isinstance(value, list) else []


# ── vector ──────────────────────────────────────────────────────────────────


def _vector_rows(layers: list[Any]) -> list[str]:
    rows: list[str] = []
    total = 0
    stack = [(node, 0) for node in reversed(layers)]
    while stack:
        node, depth = stack.pop()
        if not isinstance(node, dict):
            continue
        total += 1
        if len(rows) < _MAX_ROWS:
            facts: dict[str, Any] = {
                "kind": node.get("kind"),
                "bounds": _bounds(node.get("bounds")),
            }
            if node.get("kind") != "Layer":
                facts["fill"] = _paint(node.get("fill"))
                facts["stroke"] = _paint(node.get("stroke"))
                if node.get("strokeWidth") not in (None, 0, 0.0):
                    facts["strokeWidth"] = _num(node.get("strokeWidth"))
            if text := _clip(node.get("text")):
                facts["text"] = text
            line = entity_line(node.get("name") or node.get("kind"), node.get("id"), **facts)
            rows.append("  " * depth + line)
        children = node.get("children") or []
        stack.extend((child, depth + 1) for child in reversed(children))
    return _capped(rows, total, "layers")


def _vector_block(doc: dict[str, Any]) -> list[str]:
    board = doc.get("artboard") if isinstance(doc.get("artboard"), dict) else {}
    w, h = board.get("width"), board.get("height")
    inches = ""
    try:
        inches = f" ({float(w) / 72:.2f} x {float(h) / 72:.2f} in)"
    except (TypeError, ValueError):
        pass
    lines = [
        f"title: {doc.get('title') or 'Untitled'}",
        f"artboard: {_num(w)} x {_num(h)} pt{inches}",
        f"colour mode: {str(doc.get('colorMode') or '?').lower()}",
        "LAYERS (id, kind, bounds x,y wxh in pt, paint, text):",
    ]
    lines += _vector_rows(_list(doc, "layers")) or ["  (empty document)"]
    return lines + _tail(doc, _id_list(doc))


# ── photo ───────────────────────────────────────────────────────────────────


def _photo_block(doc: dict[str, Any]) -> list[str]:
    w, h, ppi = doc.get("width"), doc.get("height"), doc.get("ppi")
    size = ""
    try:
        size = f" ({float(w) / float(ppi) * 25.4:.1f} x {float(h) / float(ppi) * 25.4:.1f} mm)"
    except (TypeError, ValueError, ZeroDivisionError):
        pass
    lines = [
        f"image: {doc.get('name') or 'Untitled'}, "
        f"{_num(w)} x {_num(h)} px at {_num(ppi)} ppi{size}",
        f"colour mode: {str(doc.get('colorMode') or '?').lower()}",
        "LAYERS (bottom of the stack first; id, kind, visible, opacity 0..1):",
    ]
    layers = [layer for layer in _list(doc, "layers") if isinstance(layer, dict)]
    rows = [
        entity_line(
            layer.get("name") or layer.get("kind"),
            layer.get("id"),
            kind=layer.get("kind"),
            visible="yes" if layer.get("visible", True) else "no",
            opacity=_num(layer.get("opacity", 1)),
        )
        for layer in layers[:_MAX_ROWS]
    ]
    lines += _capped(rows, len(layers), "layers") or ["  (no layers)"]
    active = doc.get("activeLayer")
    lines.append(f"ACTIVE LAYER: {active if active is not None else 'none'}")
    sel = doc.get("selection")
    bounds = sel.get("bounds") if isinstance(sel, dict) else None
    return lines + _tail(doc, f"{_bounds(bounds)} px" if bounds else "nothing")


# ── design ──────────────────────────────────────────────────────────────────


def _design_block(doc: dict[str, Any]) -> list[str]:
    size = doc.get("size_mm") if isinstance(doc.get("size_mm"), dict) else {}
    pages = [p for p in _list(doc, "pages") if isinstance(p, dict)]
    lines = [
        f"title: {doc.get('title') or 'Untitled'}",
        f"page size: {_num(size.get('width'))} x {_num(size.get('height'))} mm (trim), "
        f"bleed {_num(doc.get('bleed_mm'))} mm",
        f"pages: {len(pages)}; page in view: {doc.get('current_page', 0)}",
        "FRAMES per page (id, kind, bounds x,y wxh in mm from the trim top-left, text, fill,"
        " field = the template field it carries):",
    ]
    rows: list[str] = []
    total = 0
    for page in pages:
        frames = [f for f in _list(page, "frames") if isinstance(f, dict)]
        rows.append(f"PAGE {page.get('index')}:" + ("" if frames else " (empty)"))
        for frame in frames:
            total += 1
            if total > _MAX_ROWS:
                continue
            facts: dict[str, Any] = {
                "kind": frame.get("kind"),
                "bounds": _bounds(frame.get("bounds_mm")),
            }
            if text := _clip(frame.get("text")):
                facts["text"] = text
            if frame.get("fill") is not None:
                facts["fill"] = _paint(frame.get("fill"))
            if field := _clip(frame.get("field")):
                facts["field"] = field
            rows.append("  " + entity_line(frame.get("kind"), frame.get("id"), **facts))
    lines += _capped(rows, total, "frames") or ["  (no pages)"]
    return lines + _tail(doc, _id_list(doc))


# ── the frame ───────────────────────────────────────────────────────────────

_SPECS: dict[str, dict[str, Any]] = {
    "vector": {
        "block": _vector_block,
        "purpose": "a VECTOR DESIGN EDITOR making print-ready artwork: visiting cards, "
        "wedding cards,\nflex banners, logos",
        "words": "'the design', 'the artboard', 'layers' and 'objects'",
        "rules": """\
- Units: points, y pointing DOWN, origin at the artboard's top-left; 72 pt = 1 inch
  (a 3.5 x 2 in visiting card is 252 x 144 pt). Keep text 9 pt or more from the
  trim edge.
- Colour: prefer CMYK paints for print, {"c":0,"m":0.6,"y":1,"k":0} with 0..1
  values; "#rrggbb" also works. Pure black text is {"c":0,"m":0,"y":0,"k":1}.
  Fill, stroke and strokeWidth all go through set_paint.
- Ids are the integers in the LAYERS list. Omitting `ids` acts on the selection;
  every new object becomes the selection, so the next op can omit ids to act on it.""",
    },
    "photo": {
        "block": _photo_block,
        "purpose": "a PHOTO EDITOR retouching customers' photos and making passport and ID "
        "photos\nfor print",
        "words": "'the photo', 'layers' and 'the selection'",
        "rules": """\
- Units: pixels, y pointing DOWN, origin at the image's top-left. Opacity is 0..1,
  colours are "#rrggbb".
- Passport photo (India, 35 x 45 mm): crop {"preset":"passport_35x45"} crops the
  largest centred 35:45 box and resamples it to 413 x 531 px at 300 ppi. To place
  the box yourself, crop {x,y,width,height} at a 35:45 ratio, then resize
  {width:413,height:531,resolution:300}. For a white background: select_subject,
  then run_command select.inverse, then fill_selection {"color":"#ffffff"}.
- Retouch gently: small level, curve and sharpen moves read as natural.
- Layer ops name a layer id from the LAYERS list; omit `layer` to act on the
  ACTIVE LAYER. Brush, heal and lasso strokes are the user's hand tools.""",
    },
    "design": {
        "block": _design_block,
        "purpose": "a PAGE LAYOUT EDITOR laying out pages for print: visiting cards, flyers,\n"
        "letterheads, invitations, bill books",
        "words": "'the page', 'frames' and 'pages'",
        "rules": """\
- Units: MILLIMETRES measured from the page's TRIM top-left, y pointing DOWN.
  `page` is 0-based and defaults to the page in view. strokeWidth and text size
  are points. A visiting card is 90 x 54 mm; keep text 4 mm or more inside the
  trim, and run background shapes 3 mm past it (negative x/y) into the bleed.
- Colour: prefer CMYK paints, {"c":0,"m":0.6,"y":1,"k":0} with 0..1 values;
  "#rrggbb" also works.
- TEXT COLOUR is set_text_color (or set_text's color).
  set_paint paints a frame's BOX (its background fill, border and strokeWidth),
  never its text: "make the name red" is set_text_color on the name's frame.
- OVERSET text disappears: a text frame shows only what fits its box, and the rest
  is cut off, invisible on the page and in print. When you raise a text size or
  lengthen a text, pass "fit":true on set_text, or keep the size within the frame
  (a line needs about 1.4 x its size in height; 1 pt = 0.353 mm). fit runs after
  the batch's moves: the frame grows down into free space only (never onto the
  frame below or past the trim), then the text shrinks, at most to half the size
  you asked. To make text really bigger, make room first in the same batch
  (move or shrink the frames under it).
- LAYOUT CHECK: after your batch the editor checks the page and the next message
  shows what it found under the failures, one line each:
  "overset frame <id>: ..." = that text is cut off; "shrunk frame <id>: ..." = fit
  set it smaller than you asked (tell the user the real size);
  "overlap frame <id>: ..." = that frame now covers another one;
  "off page frame <id>: ..." = it runs past the trim and is cut off in print.
  Fix them next (set_text {"id":<id>,"fit":true}, a smaller size, or a move).
- Never state a text size before the editor reports it: fit can set it smaller.
  Say the size you asked for; the real size comes from the shrunk line (or none)
  in the next message, and only then may you tell the user a size.
- TEMPLATE FIELDS: a frame listed with field=... is wired to the user's fill-in
  box. never delete or re-make it, even to make room: move or resize it instead
  (transform), or change its text size. A delete that names one is refused.
- You cannot see the result of the batch you are sending. Never tell the user text
  fits, is fully visible or does not overlap, and never tell the user it worked;
  say what you changed and that the editor will flag anything cut off or overlapping.
  Asked "is anything cut off / overlapping?", answer from those failure lines only
  (none listed after your last batch = the editor found no problem), never from the
  ops you sent.
- Ids are the integers in the FRAMES list. Omitting `ids` acts on the selection;
  every new frame becomes the selection, so the next op can omit ids to act on it.""",
    },
}

_COMMON_RULES = """\
- Batch the whole change in one call. run_command reaches the engine's other
  commands; file, export, document, clipboard, preference, view and app commands
  are blocked there, so saving and exporting are the user's job in the editor.
- The tool returns once the batch is validated and dispatched, not applied. Say
  what you changed; never claim it is done. If the document shows a failed last
  batch, fix that first."""


def _orientation(app: str) -> str:
    spec = _SPECS[app]
    return (
        f"<studio-{app}-orientation>\n"
        f"The user is in {spec['purpose']}. They are print-shop owners in India. Design\n"
        "for print, not the screen. Hindi and other Indic scripts are fully supported:\n"
        "write the text in its own script, never transliterated, unless asked.\n"
        "This is NOT a dashboard: do not build widgets, a pocket or a ui-spec. Talk about\n"
        f"{spec['words']}.\n"
        f"</studio-{app}-orientation>"
    )


def _procedure(app: str) -> str:
    return (
        f"<studio-{app}-procedure>\n"
        f"Edit ONLY through `{TOOL_IDS[app]}` with a batch of ops.\n"
        "These are the ONLY op names, one valid example each. Copy the names and params\n"
        "exactly; the ids in the examples are placeholders, use the ids from the document:\n"
        f"{op_cheatsheet(app)}\n{_SPECS[app]['rules']}\n{_COMMON_RULES}\n"
        f"</studio-{app}-procedure>"
    )


def _no_document(app: str) -> str:
    return (
        f"<studio-{app}-procedure>\n"
        f"No {app} document is open yet, so there is nothing to edit. Ask the user to open\n"
        f"or start one in the editor. Do NOT call edit_{app}; it will refuse.\n"
        f"</studio-{app}-procedure>"
    )


async def _build(app: str, meta: SurfaceMeta) -> SurfacePreamble:
    route = meta.route_path or f"/studio/{app}"
    doc = getattr(meta, app, None)
    if isinstance(doc, dict):
        block = "\n".join(_SPECS[app]["block"](doc))
        body = f"<{app}-document>\n{block}\n</{app}-document>\n{_procedure(app)}"
    else:
        body = _no_document(app)
    text = f'<surface kind="studio_{app}" route="{route}" />\n{_orientation(app)}\n{body}'
    # Content digest: the preamble renders the live document, so a route key
    # would serve a stale document all session.
    return SurfacePreamble(text=text, cache_key=content_key(f"studio_{app}", text))


async def build_vector_preamble(workspace_id: str, user_id: str, meta: SurfaceMeta):
    return await _build("vector", meta)


async def build_photo_preamble(workspace_id: str, user_id: str, meta: SurfaceMeta):
    return await _build("photo", meta)


async def build_design_preamble(workspace_id: str, user_id: str, meta: SurfaceMeta):
    return await _build("design", meta)


__all__ = ["build_design_preamble", "build_photo_preamble", "build_vector_preamble"]
