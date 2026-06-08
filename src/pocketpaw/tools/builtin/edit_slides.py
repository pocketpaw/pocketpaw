"""edit_slides tool -- AI-powered slide deck editing for reveal.js presentations.

The agent calls ``edit_slides`` with an array of operations. Slide decks are
stored in a per-request ``ContextVar`` (for the REST ai-edit endpoint) or
a module-level dict keyed by file_id (for chat-initiated editing).

Mirrors the ``edit_document`` and ``edit_spreadsheet`` pattern: ContextVar for
request-scoped access, module dict for chat sync, MCP server for agent tooling.
"""

from __future__ import annotations

import json
import logging
import uuid
from contextvars import ContextVar
from typing import Any

logger = logging.getLogger(__name__)

# Per-request slides deck (REST ai-edit endpoint).
_edit_session_slides: ContextVar[dict | None] = ContextVar(
    "edit_session_slides", default=None
)

# Per-request selected slide ID for scoped editing.
_selected_slide_id: ContextVar[str | None] = ContextVar(
    "selected_slide_id", default=None
)

# Module-level store keyed by file_id -- used when decks are synced from
# the frontend slides UI (chat-initiated editing).
_slides_store: dict[str, dict] = {}

SERVER_NAME = "pocketpaw_edit_slides"
EDIT_SLIDES_TOOL_ID = f"mcp__{SERVER_NAME}__edit_slides"

# Valid element types for slide content.
_ELEMENT_TYPES = {"heading", "text", "list", "image", "code", "table", "quote"}

# Valid reveal.js themes.
_THEMES = {
    "black", "white", "league", "beige", "sky", "night",
    "serif", "simple", "solarized", "moon", "dracula",
}

# Valid slide layouts.
_LAYOUTS = {"title-slide", "content", "two-column", "image-full", "blank"}

# Valid transitions.
_TRANSITIONS = {"none", "fade", "slide", "convex", "concave", "zoom"}


# ---------------------------------------------------------------------------
# Session management (ContextVar + module dict -- mirrors edit_document.py)
# ---------------------------------------------------------------------------


def set_edit_session(deck: dict) -> None:
    _edit_session_slides.set(deck)


def clear_edit_session() -> None:
    _edit_session_slides.set(None)


def get_edit_session() -> dict | None:
    return _edit_session_slides.get()


def set_selected_slide_id(slide_id: str | None) -> None:
    _selected_slide_id.set(slide_id)


def get_selected_slide_id() -> str | None:
    return _selected_slide_id.get()


def clear_selected_slide_id() -> None:
    _selected_slide_id.set(None)


def set_slides_data(file_id: str, deck: dict) -> None:
    _slides_store[file_id] = deck


def get_slides_data(file_id: str) -> dict | None:
    return _slides_store.get(file_id)


def get_active_deck() -> dict | None:
    """Get whichever deck is available -- ContextVar first, then store."""
    deck = _edit_session_slides.get()
    if deck is not None:
        return deck
    if _slides_store:
        return next(iter(_slides_store.values()))
    return None


# ---------------------------------------------------------------------------
# Deck helpers
# ---------------------------------------------------------------------------


def _slide_ids(deck: dict) -> list[str]:
    """Return ordered slide IDs from the deck."""
    return [s["id"] for s in deck.get("slides", [])]


def _get_slide(deck: dict, slide_id: str) -> dict | None:
    """Get a slide dict by ID from the deck."""
    for slide in deck.get("slides", []):
        if slide.get("id") == slide_id:
            return slide
    return None


def _get_element(slide: dict, element_id: str) -> dict | None:
    """Get an element dict by ID from a slide."""
    for el in slide.get("elements", []):
        if el.get("id") == element_id:
            return el
    return None


def _ensure_deck(deck: dict) -> None:
    """Ensure the deck has minimal structure."""
    deck.setdefault("version", 1)
    deck.setdefault("theme", "black")
    deck.setdefault("transition", "slide")
    deck.setdefault("slideNumber", True)
    deck.setdefault("slides", [])


def _ensure_slide(deck: dict, slide_id: str | None = None) -> dict:
    """Get a slide by ID or return the first slide. Returns None if no slides."""
    if slide_id:
        slide = _get_slide(deck, slide_id)
        if slide:
            return slide
    slides = deck.get("slides", [])
    return slides[0] if slides else None


def _create_slide(
    layout: str = "content",
    elements: list[dict] | None = None,
    *,
    background: dict | None = None,
    background_transition: str | None = None,
    auto_animate: bool = False,
) -> dict:
    """Create a new slide dict with a UUID."""
    slide: dict = {
        "id": uuid.uuid4().hex[:12],
        "layout": layout if layout in _LAYOUTS else "content",
        "background": background,
        "notes": "",
        "elements": elements or [],
    }
    if background_transition:
        slide["backgroundTransition"] = background_transition
    if auto_animate:
        slide["autoAnimate"] = True
    return slide


def _create_element(
    el_type: str,
    content: Any,
    style: dict | None = None,
    position: dict | None = None,
    *,
    fragment: str | None = None,
) -> dict:
    """Create a new element dict with a UUID."""
    el: dict[str, Any] = {
        "id": uuid.uuid4().hex[:8],
        "type": el_type if el_type in _ELEMENT_TYPES else "text",
        "content": content,
        "style": style or {},
        "position": position or {"x": 0, "y": 0, "width": 12, "height": 2},
    }
    el["style"].setdefault("fontSize", "1em")
    el["style"].setdefault("alignment", "left")
    el["style"].setdefault("color", None)
    if fragment:
        el["fragment"] = fragment
    return el


# ---------------------------------------------------------------------------
# Prompt context builder
# ---------------------------------------------------------------------------


def _summarize_slide(slide: dict, index: int) -> str:
    """Build a one-line summary of a slide for the system prompt."""
    slide_id = slide.get("id", "?")
    layout = slide.get("layout", "content")
    elements = slide.get("elements", [])
    notes = slide.get("notes", "")

    parts = [f"Slide {index + 1} (id={slide_id[:8]}, layout={layout})"]

    for el in elements:
        el_type = el.get("type", "?")
        content = el.get("content", "")
        if el_type == "heading":
            parts.append(f"  heading: \"{_fmt_val(content)}\"")
        elif el_type == "text":
            parts.append(f"  text: \"{_fmt_val(content)}\"")
        elif el_type == "list":
            items = content if isinstance(content, list) else []
            preview = ", ".join(str(i)[:30] for i in items[:3])
            if len(items) > 3:
                preview += f", ... ({len(items)} items)"
            parts.append(f"  list: [{preview}]")
        elif el_type == "image":
            src = content.get("src", "") if isinstance(content, dict) else str(content)
            parts.append(f"  image: {_fmt_val(src)}")
        elif el_type == "code":
            code = content.get("code", "") if isinstance(content, dict) else str(content)
            lang = content.get("language", "") if isinstance(content, dict) else ""
            parts.append(f"  code ({lang or 'no lang'}): {_fmt_val(code)}")
        elif el_type == "table":
            if isinstance(content, dict):
                rows = len(content.get("rows", []))
                cols = len(content.get("headers", []))
                parts.append(f"  table: {rows} rows x {cols} cols")
            else:
                parts.append(f"  table: {_fmt_val(str(content))}")
        elif el_type == "quote":
            parts.append(f"  quote: \"{_fmt_val(str(content))}\"")
        else:
            parts.append(f"  {el_type}: {_fmt_val(str(content))}")

    if notes:
        parts.append(f"  notes: \"{_fmt_val(notes)}\"")

    return "\n".join(parts)


def build_slides_prompt_context(
    deck: dict | None = None,
    *,
    selected_slide_id: str | None = None,
) -> str | None:
    """Build a system-prompt block describing the current slide deck state.

    When *selected_slide_id* is set, per-slide editing mode kicks in: the
    prompt scopes the agent to only that slide and only element operations
    on it are permitted.

    Returns None if no deck is available.
    """
    resolved = deck or get_active_deck()
    if not resolved:
        return None

    _ensure_deck(resolved)
    slides = resolved.get("slides", [])
    theme = resolved.get("theme", "black")
    transition = resolved.get("transition", "slide")

    if not slides:
        return (
            "You are editing a presentation in PocketPaw's slides editor.\n"
            "The deck is currently empty (no slides). "
            "Use the edit_slides tool to create slides.\n\n"
            f"Theme: {theme} | Transition: {transition}"
        )

    parts = [
        "You are editing a presentation in PocketPaw's slides editor.\n",
        f"Theme: {theme} | Transition: {transition}",
        f"Slides: {len(slides)}\n",
        "## Slide overview\n",
    ]

    for i, slide in enumerate(slides):
        parts.append(_summarize_slide(slide, i))
        parts.append("")

    scoping_notice = ""
    if selected_slide_id:
        scoping_notice = (
            "\n"
            "============================================================\n"
            "CRITICAL: PER-SLIDE EDITING MODE\n"
            "============================================================\n"
            "You are in per-slide editing mode. You MUST follow these rules:\n"
            "\n"
            f"1. You are ONLY allowed to edit slide id={selected_slide_id}.\n"
            "2. Do NOT modify, delete, or reorder any other slide.\n"
            "3. Do NOT use 'replace_all' or 'set_theme' -- they would overwrite\n"
            "   the entire deck and destroy content on other slides.\n"
            "4. Do NOT create, delete, or reorder slides -- work within the\n"
            "   selected slide only.\n"
            "5. Use ONLY element operations (update_element, insert_element,\n"
            f"   delete_element) with slide_id={selected_slide_id}.\n"
            "6. If the user's prompt asks you to modify other slides or the\n"
            "   whole deck, politely explain that you can only edit the\n"
            "   selected slide. Suggest they deselect the slide for full-deck\n"
            "   editing.\n"
            "\n"
            f"Target slide: id={selected_slide_id}\n"
            "Permitted operations: update_element, insert_element, delete_element\n"
            f"  (all with slide_id={selected_slide_id})\n"
            "============================================================\n"
        )
        parts.insert(3, f"Selected slide: {selected_slide_id[:8]}\n")

    parts.extend([
        "## How to edit\n",
        "Call the `edit_slides` tool with an array of operations. "
        "You can call it multiple times -- each call returns the updated state.\n",
        "Operations:",
        '- create_slide: {"op":"create_slide", "layout":"content", "position":1, "background":{"type":"gradient","value":"linear-gradient(...)"}, "backgroundTransition":"zoom", "autoAnimate":true, "elements":[...]}',
        '- delete_slide: {"op":"delete_slide", "slide_id":"abc123"}',
        '- reorder_slides: {"op":"reorder_slides", "slide_ids":["id1","id2","id3"]}',
        '- update_element: {"op":"update_element", "slide_id":"abc", "element_id":"def", "content":"New text", "fragment":"fade-in"}',
        '- insert_element: {"op":"insert_element", "slide_id":"abc", "type":"heading", "content":"Title", "fragment":"grow"}',
        '- delete_element: {"op":"delete_element", "slide_id":"abc", "element_id":"def"}',
        '- set_theme: {"op":"set_theme", "theme":"night", "transition":"zoom", "backgroundTransition":"slide"}',
        '- replace_all: {"op":"replace_all", "deck":{...}}',
        "\nElement types: heading, text, list, image, code, table, quote\n",
        "Background types: color, gradient, image, video. E.g.: {\"type\":\"gradient\",\"value\":\"linear-gradient(135deg, #1a1a2e, #16213e)\"}\n",
        "Fragment styles (per element): fade-in, fade-out, fade-up, fade-down, fade-left, fade-right, fade-in-then-out, grow, shrink, highlight-red, highlight-blue, highlight-green\n",
        "After making your edits, briefly tell the user what you changed.",
        scoping_notice,  # appended after the how-to guide so it overrides any conflicting instructions
    ])

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Operations engine
# ---------------------------------------------------------------------------


def apply_slides_operations(
    deck: dict,
    operations: list[dict],
    selected_slide_id: str | None = None,
) -> tuple[dict, str]:
    """Apply slide operations to a deck. Mutates in place.

    When *selected_slide_id* is set, only element operations
    (update_element, insert_element, delete_element) targeting that
    specific slide are permitted.  Deck-level operations (create_slide,
    delete_slide, reorder_slides, set_theme, replace_all) are rejected.

    Returns (deck, summary) -- deck is the same dict reference, mutated.
    """
    _ensure_deck(deck)
    summaries: list[str] = []

    _ELEMENT_OPS = {"update_element", "insert_element", "delete_element"}

    # Pre-flight: reject replace_all in per-slide mode (hard error, stops all).
    if selected_slide_id:
        for op in operations:
            if op.get("op") == "replace_all":
                return deck, (
                    "ERROR: replace_all is not allowed in per-slide editing mode. "
                    "You may only edit elements on the selected slide "
                    f"(id={selected_slide_id}). "
                    "Use update_element, insert_element, or delete_element instead."
                )

    for op in operations:
        op_type = op.get("op")
        try:
            # Per-slide editing enforcement: gate non-element ops and cross-slide ops.
            if selected_slide_id and op_type not in _ELEMENT_OPS:
                summaries.append(
                    f"SKIPPED: '{op_type}' is not allowed in per-slide editing mode. "
                    f"Only update_element, insert_element, delete_element are permitted "
                    f"(target slide id={selected_slide_id})."
                )
                continue

            if selected_slide_id and op.get("slide_id") != selected_slide_id:
                summaries.append(
                    f"SKIPPED: {op_type} on slide '{str(op.get('slide_id', ''))[:8]}' "
                    f"is not allowed in per-slide editing mode. Target slide is "
                    f"id={selected_slide_id}."
                )
                continue

            if op_type == "create_slide":
                _op_create_slide(deck, op, summaries)
            elif op_type == "delete_slide":
                _op_delete_slide(deck, op, summaries)
            elif op_type == "reorder_slides":
                _op_reorder_slides(deck, op, summaries)
            elif op_type == "update_element":
                _op_update_element(deck, op, summaries)
            elif op_type == "insert_element":
                _op_insert_element(deck, op, summaries)
            elif op_type == "delete_element":
                _op_delete_element(deck, op, summaries)
            elif op_type == "set_theme":
                _op_set_theme(deck, op, summaries)
            elif op_type == "replace_all":
                _op_replace_all(deck, op, summaries)
            else:
                summaries.append(f"Unknown operation '{op_type}' -- skipped")
        except ValueError as exc:
            summaries.append(f"ERROR in '{op_type}': {exc}")

    summary = "; ".join(summaries) if summaries else "No operations applied"
    return deck, summary


# ── Individual operation handlers ──


def _op_create_slide(deck: dict, op: dict, summaries: list):
    layout = op.get("layout", "content")
    elements_raw = op.get("elements", [])
    elements: list[dict] = []
    for el in elements_raw:
        if isinstance(el, dict) and "type" in el:
            elements.append(_create_element(
                el_type=el["type"],
                content=el.get("content", ""),
                style=el.get("style"),
                position=el.get("position"),
                fragment=el.get("fragment"),
            ))
        else:
            elements.append(el)

    bg = op.get("background")
    bg_transition = op.get("backgroundTransition")
    auto_animate = op.get("autoAnimate", False)
    slide = _create_slide(
        layout=layout, elements=elements,
        background=bg,
        background_transition=bg_transition,
        auto_animate=auto_animate,
    )
    position = op.get("position")
    slides = deck.setdefault("slides", [])
    if position is not None and 0 <= position < len(slides):
        slides.insert(position, slide)
        actual_pos = position
    else:
        slides.append(slide)
        actual_pos = len(slides) - 1
    summaries.append(f"Created slide {slide['id'][:8]} (layout={layout}) at position {actual_pos}")


def _op_delete_slide(deck: dict, op: dict, summaries: list):
    slide_id = op.get("slide_id", "")
    slides = deck.get("slides", [])
    for i, slide in enumerate(slides):
        if slide.get("id") == slide_id:
            slides.pop(i)
            summaries.append(f"Deleted slide {slide_id[:8]} (was position {i})")
            return
    summaries.append(f"Slide '{slide_id[:8]}' not found -- skipped delete_slide")


def _op_reorder_slides(deck: dict, op: dict, summaries: list):
    new_order = op.get("slide_ids", [])
    slides = deck.get("slides", [])
    if not new_order:
        summaries.append("reorder_slides: slide_ids is empty -- nothing changed")
        return
    slide_map = {s["id"]: s for s in slides}
    reordered = []
    for sid in new_order:
        if sid in slide_map:
            reordered.append(slide_map.pop(sid))
        else:
            summaries.append(f"reorder_slides: slide '{sid[:8]}' not found -- skipping")
    reordered.extend(slide_map.values())
    deck["slides"] = reordered
    summaries.append(f"Reordered {len(new_order)} slides")


def _op_update_element(deck: dict, op: dict, summaries: list):
    slide_id = op.get("slide_id", "")
    element_id = op.get("element_id", "")
    slide = _get_slide(deck, slide_id)
    if slide is None:
        summaries.append(f"update_element: slide '{slide_id[:8]}' not found")
        return
    el = _get_element(slide, element_id)
    if el is None:
        summaries.append(f"update_element: element '{element_id[:8]}' not found in slide '{slide_id[:8]}'")
        return

    changed = []
    if "content" in op:
        el["content"] = op["content"]
        changed.append("content")
    if "style" in op and isinstance(op["style"], dict):
        el.setdefault("style", {}).update(op["style"])
        changed.append("style")
    if "position" in op and isinstance(op["position"], dict):
        el["position"] = op["position"]
        changed.append("position")
    if "fragment" in op:
        el["fragment"] = op["fragment"] if op["fragment"] else None
        changed.append(f"fragment={op['fragment']}")

    summaries.append(
        f"Updated element '{element_id[:8]}' on slide '{slide_id[:8]}': {', '.join(changed) or 'no fields'}"
    )


def _op_insert_element(deck: dict, op: dict, summaries: list):
    slide_id = op.get("slide_id", "")
    el_type = op.get("type", "text")
    content = op.get("content", "")
    style = op.get("style")
    position = op.get("position")
    fragment = op.get("fragment")

    slide = _get_slide(deck, slide_id)
    if slide is None:
        summaries.append(f"insert_element: slide '{slide_id[:8]}' not found")
        return

    el = _create_element(el_type=el_type, content=content, style=style, position=position, fragment=fragment)
    slide.setdefault("elements", []).append(el)
    summaries.append(f"Inserted {el_type} element '{el['id'][:8]}' on slide '{slide_id[:8]}'")


def _op_delete_element(deck: dict, op: dict, summaries: list):
    slide_id = op.get("slide_id", "")
    element_id = op.get("element_id", "")
    slide = _get_slide(deck, slide_id)
    if slide is None:
        summaries.append(f"delete_element: slide '{slide_id[:8]}' not found")
        return
    elements = slide.get("elements", [])
    for i, el in enumerate(elements):
        if el.get("id") == element_id:
            elements.pop(i)
            summaries.append(f"Deleted element '{element_id[:8]}' from slide '{slide_id[:8]}'")
            return
    summaries.append(f"delete_element: element '{element_id[:8]}' not found on slide '{slide_id[:8]}'")


def _op_set_theme(deck: dict, op: dict, summaries: list):
    changed = []
    if "theme" in op and op["theme"] in _THEMES:
        deck["theme"] = op["theme"]
        changed.append(f"theme={op['theme']}")
    elif "theme" in op:
        summaries.append(f"set_theme: unknown theme '{op['theme']}' -- skipping")
    if "transition" in op and op["transition"] in _TRANSITIONS:
        deck["transition"] = op["transition"]
        changed.append(f"transition={op['transition']}")
    elif "transition" in op:
        summaries.append(f"set_theme: unknown transition '{op['transition']}' -- skipping")
    if "backgroundTransition" in op and op["backgroundTransition"] in _TRANSITIONS:
        deck["backgroundTransition"] = op["backgroundTransition"]
        changed.append(f"bgTransition={op['backgroundTransition']}")
    elif "backgroundTransition" in op:
        summaries.append(f"set_theme: unknown backgroundTransition '{op['backgroundTransition']}' -- skipping")
    if "slideNumber" in op:
        deck["slideNumber"] = bool(op["slideNumber"])
        changed.append(f"slideNumber={op['slideNumber']}")
    if changed:
        summaries.append(f"Updated deck settings: {', '.join(changed)}")


def _op_replace_all(deck: dict, op: dict, summaries: list):
    new_deck = op.get("deck", {})
    deck.clear()
    deck.update(new_deck)
    _ensure_deck(deck)
    summaries.append(f"Replaced entire deck ({len(deck.get('slides', []))} slides)")


def _fmt_val(val: Any) -> str:
    s = str(val)
    return s[:60] + "..." if len(s) > 60 else s


# ---------------------------------------------------------------------------
# MCP tool (BaseTool protocol -- used by REST ai-edit endpoint)
# ---------------------------------------------------------------------------


class EditSlidesTool:
    """Tool the agent can call to edit a reveal.js slide deck.

    Operations are applied in order, mutating the deck in place.
    """

    def __init__(self, deck: dict) -> None:
        self._deck = deck

    @property
    def name(self) -> str:
        return "edit_slides"

    @property
    def description(self) -> str:
        return (
            "Edit the slide deck by performing operations on slides and elements. "
            "You can create, delete, and reorder slides, insert and update elements "
            "(heading, text, list, image, code, table, quote), set backgrounds "
            "(color, gradient, image, video), add fragment animations, change the "
            "theme and transitions, or replace the entire deck. Call this tool "
            "multiple times to build up your edit."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "operations": {
                    "type": "array",
                    "description": "Ordered list of slide operations to apply.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "op": {
                                "type": "string",
                                "enum": [
                                    "create_slide", "delete_slide", "reorder_slides",
                                    "update_element", "insert_element", "delete_element",
                                    "set_theme", "replace_all",
                                ],
                            },
                            "slide_id": {"type": "string", "description": "Target slide ID (12-char hex)"},
                            "element_id": {"type": "string", "description": "Target element ID (8-char hex)"},
                            "slide_ids": {"type": "array", "items": {"type": "string"}, "description": "New slide order (all slide IDs)"},
                            "layout": {"type": "string", "enum": list(_LAYOUTS), "description": "Slide layout"},
                            "type": {"type": "string", "enum": list(_ELEMENT_TYPES), "description": "Element type"},
                            "content": {"description": "Element content: string for heading/text/quote, string[] for list, {src,alt} for image, {code,language} for code, {headers,rows} for table"},
                            "elements": {"type": "array", "description": "Elements for a new slide"},
                            "style": {"type": "object", "description": "CSS style overrides (fontSize, alignment, color)"},
                            "position": {
                                "type": "object",
                                "properties": {
                                    "x": {"type": "integer"},
                                    "y": {"type": "integer"},
                                    "width": {"type": "integer"},
                                    "height": {"type": "integer"},
                                },
                                "description": "Grid position (12-column layout)",
                            },
                            "theme": {"type": "string", "enum": list(_THEMES), "description": "reveal.js theme (black, white, league, beige, sky, night, serif, simple, solarized, moon, dracula)"},
                            "transition": {"type": "string", "enum": list(_TRANSITIONS), "description": "Slide transition (none, fade, slide, convex, concave, zoom)"},
                            "backgroundTransition": {"type": "string", "enum": list(_TRANSITIONS), "description": "Background transition for set_theme or create_slide"},
                            "slideNumber": {"type": "boolean", "description": "Show slide numbers"},
                            "background": {
                                "type": "object",
                                "properties": {
                                    "type": {"type": "string", "enum": ["color", "gradient", "image", "video"]},
                                    "value": {"type": "string", "description": "CSS color, gradient string, image URL, or video URL"},
                                },
                                "description": "Slide background. Use type=gradient with value like 'linear-gradient(135deg, #1a1a2e 0%, #16213e 100%)'",
                            },
                            "autoAnimate": {"type": "boolean", "description": "Enable auto-animate for this slide (create_slide)"},
                            "fragment": {"type": "string", "description": "Fragment animation for an element: fade-in, fade-out, fade-up, fade-down, fade-left, fade-right, fade-in-then-out, grow, shrink, highlight-red, highlight-blue, highlight-green"},
                            "deck": {"type": "object", "description": "Complete deck JSON for replace_all"},
                        },
                        "required": ["op"],
                    },
                }
            },
            "required": ["operations"],
        }

    async def execute(self, operations: list[dict]) -> str:
        if not self._deck:
            return _slides_error("No slide deck loaded. Start an editing session first.")
        try:
            selected = get_selected_slide_id()
            updated, summary = apply_slides_operations(
                self._deck, operations, selected_slide_id=selected
            )
            return f"{summary}\n\nUpdated deck:\n{json.dumps(updated, separators=(',', ':'))}"
        except Exception as exc:
            return _slides_error(f"Edit failed: {exc}")


def _slides_error(message: str) -> dict:
    return {
        "content": [{"type": "text", "text": f"Error: {message}"}],
        "is_error": True,
    }


async def _edit_slides_handler(args: dict) -> dict:
    """MCP handler for the edit_slides tool."""
    deck = get_active_deck()
    if deck is None:
        return _slides_error(
            "No slide deck is currently open for editing. "
            "Open a slides file in the editor first."
        )

    operations = args.get("operations")
    if not operations:
        if args.get("operation"):
            return _slides_error(
                "Use `operations` (plural) as the key, not `operation`. "
                'Example: {"operations": [{"op": "create_slide", ...}]}'
            )
        if any(k in args for k in ("create_slide", "slide_id", "type", "content")):
            return _slides_error(
                "Wrap operations inside an `operations` array. "
                'Example: {"operations": [{"op": "create_slide", "layout": "content"}]}'
            )
        return _slides_error(
            "Missing `operations` array. "
            'Format: {"operations": [{"op": "create_slide|delete_slide|...", ...}]}'
        )

    if not isinstance(operations, list):
        return _slides_error("`operations` must be an array (list), not a single object.")

    try:
        selected = get_selected_slide_id()
        updated, summary = apply_slides_operations(deck, operations, selected_slide_id=selected)
        return {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(
                        {
                            "summary": summary,
                            "slideCount": len(updated.get("slides", [])),
                            "deck": updated,
                        },
                        separators=(",", ":"),
                    ),
                }
            ]
        }
    except Exception as exc:
        logger.exception("edit_slides handler failed")
        return _slides_error(str(exc))


def build_edit_slides_mcp_server() -> tuple[str, Any] | None:
    """Build the in-process MCP server for the Claude Agent SDK.

    Returns (server_name, server_config) or None if the SDK is unavailable.
    Always registered -- the handler returns an error when no editing session
    is active, so the agent won't use it outside the slides editing context.
    """
    try:
        from claude_agent_sdk import create_sdk_mcp_server, tool
    except ImportError:
        logger.debug("claude_agent_sdk not installed; edit_slides MCP disabled")
        return None

    @tool(
        "edit_slides",
        (
            "Edit the current slide deck by performing operations on slides and "
            "elements. Use this when the user asks you to edit, create, or "
            "restructure a presentation they have open in the editor.\n\n"
            "You MUST call it with an `operations` array. Each item must have "
            "an `op` field set to one of: create_slide, delete_slide, "
            "reorder_slides, update_element, insert_element, delete_element, "
            "set_theme, replace_all.\n\n"
            "EXACT format (copy this pattern):\n"
            '{"operations": [\n'
            '  {"op": "create_slide", "layout": "title-slide",\n'
            '   "background": {"type": "gradient", "value": "linear-gradient(135deg, #1a1a2e 0%, #16213e 100%)"},\n'
            '   "backgroundTransition": "zoom",\n'
            '   "elements": [\n'
            '     {"type": "heading", "content": "My Title", "style": {"fontSize": "2.5em", "alignment": "center"}, "fragment": "fade-in"},\n'
            '     {"type": "text", "content": "Subtitle", "style": {"fontSize": "1.2em", "alignment": "center"}, "fragment": "fade-up"}\n'
            '  ]},\n'
            '  {"op": "insert_element", "slide_id": "abc123def456", "type": "list", "content": ["Point 1", "Point 2"], "fragment": "grow"},\n'
            '  {"op": "update_element", "slide_id": "abc123def456", "element_id": "a1b2c3d4", "content": "Updated text", "fragment": "highlight-blue"},\n'
            '  {"op": "delete_slide", "slide_id": "abc123def456"},\n'
            '  {"op": "reorder_slides", "slide_ids": ["id2", "id1", "id3"]},\n'
            '  {"op": "set_theme", "theme": "night", "transition": "zoom", "backgroundTransition": "slide"},\n'
            '  {"op": "replace_all", "deck": {...}}\n'
            "]}\n\n"
            "ELEMENT TYPES: heading, text, list, image, code, table, quote\n"
            "CONTENT FORMATS:\n"
            "- heading/text/quote: a string\n"
            "- list: an array of strings\n"
            '- image: {"src": "url", "alt": "description"}\n'
            '- code: {"code": "...", "language": "python"}\n'
            '- table: {"headers": ["Col1","Col2"], "rows": [["a","b"],["c","d"]]}\n\n'
            "STYLE (optional): {\"fontSize\": \"2em\", \"alignment\": \"left|center|right\", \"color\": \"#hex\"}\n"
            "FRAGMENT (optional, per element): fade-in, fade-out, fade-up, fade-down, fade-left, fade-right, fade-in-then-out, fade-in-then-semi-out, grow, shrink, highlight-red, highlight-blue, highlight-green\n"
            "BACKGROUND (optional, per slide): {\"type\":\"color\",\"value\":\"#1a1a2e\"} | {\"type\":\"gradient\",\"value\":\"linear-gradient(135deg, #1a1a2e, #16213e)\"} | {\"type\":\"image\",\"value\":\"https://...\"} | {\"type\":\"video\",\"value\":\"https://...\"}\n"
            "BACKGROUND TRANSITION (optional): none, fade, slide, convex, concave, zoom\n"
            "AUTO-ANIMATE: add \"autoAnimate\":true to create_slide for matching elements across consecutive slides\n"
            "SLIDE LAYOUTS: title-slide, content, two-column, image-full, blank\n"
            "THEMES: black, white, league, beige, sky, night, serif, simple, solarized, moon, dracula\n"
            "TRANSITIONS: none, fade, slide, convex, concave, zoom\n\n"
            "DESIGN TIPS:\n"
            "- For a professional deck, use gradient backgrounds with complementary colors\n"
            "- Dark themes (night, dracula, moon, league) look more polished for tech presentations\n"
            "- Use fragments to reveal content incrementally — headings first, then body text\n"
            "- Pair 'zoom' transition with 'slide' background transition for smooth motion\n"
            "- Code blocks auto-highlight when you specify the language\n\n"
            "IMPORTANT: The field is `op` (not operation or action). "
            "Everything goes inside the `operations` array -- not at top level."
        ),
        {
            "operations": list,
        },
    )
    async def edit_slides(args):
        return await _edit_slides_handler(args)

    server = create_sdk_mcp_server(
        name=SERVER_NAME,
        version="1.0.0",
        tools=[edit_slides],
    )
    return SERVER_NAME, server
