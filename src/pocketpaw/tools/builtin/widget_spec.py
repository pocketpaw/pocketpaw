# pocketpaw/tools/builtin/widget_spec.py — `get_widget_spec` +
# `get_inline_widget_help` as RUNTIME builtin tools.
#
# Created: 2026-08-04 (fix/prompt-tells-the-truth).
#
# WHY THIS FILE EXISTS. Both tools already shipped — on the in-process MCP
# server `pocketpaw_widgets` (`agents/sdk_mcp_widgets.py`), which is built in
# exactly one place: `agents/claude_sdk.py`. Every other backend runs without
# them.
#
# That would be an ordinary gap except the chat-inline system prompt makes
# calling one of them MANDATORY:
#
#     "Before the FIRST node of any non-core type lands in your spec, you MUST
#      call `get_inline_widget_help(types=[...])` … there is no excuse to skip
#      it."
#
# followed by "If the tool returns an error, OMIT the widget rather than
# guess." On a backend where the tool is absent rather than erroring, the
# agent's only consistent reading is to omit — so the deployment silently
# loses every widget outside the core six. The instruction was right; the
# tool was unreachable. `start_flow` had the identical problem and was fixed
# the identical way (`flow_tool.py`), which is the precedent this follows.
#
# WHICH TOOL ANSWERS WHICH QUESTION. They are not interchangeable, and the
# prompt block above named the weaker one:
#
#   get_widget_spec          reads the Ripple MANIFEST. Returns the canonical
#                            prop schema for any widget in it. 664–1,425 chars
#                            measured across definition-list / sparkline /
#                            gauge / kanban / timeline / navbar.
#   get_inline_widget_help   reads a hand-written design-guidance catalog
#                            covering 16 widgets. Returns layout and
#                            composition advice, not a schema.
#
# For `definition-list` — the widget the prompt itself cites as having shipped
# broken, with `description` where the schema says `definition` —
# get_widget_spec returns the 759-char schema that names the field, while
# get_inline_widget_help returned 18,623 characters that never did. Prop names
# come from the manifest; design guidance comes from the catalog.

from __future__ import annotations

from typing import Any

from pocketpaw.tools.protocol import BaseTool


class WidgetSpecTool(BaseTool):
    """Return the manifest prop schema for one or more Ripple widgets.

    The authoritative answer to "what props does this widget take" — it reads
    the same manifest the renderer does, so a schema it returns is a schema
    that renders, and a type it rejects is a type this deployment cannot draw.
    """

    @property
    def name(self) -> str:
        return "get_widget_spec"

    @property
    def description(self) -> str:
        return (
            "Get the canonical prop schema, types, and a runnable example for "
            "one or more Ripple widgets, straight from the widget manifest. "
            "Pass `types` as an array of widget type names (e.g. "
            "['definition-list', 'timeline', 'sparkline']) — batch them in a "
            "single call. MANDATORY before you emit any widget whose props you "
            "have not been shown: the widget's NAME is not a contract, the "
            "manifest is, and guessed prop names render as empty rows. If this "
            "tool reports a type as unknown, this deployment's manifest does "
            "not carry that widget — do not emit it, pick one the manifest has."
        )

    @property
    def trust_level(self) -> str:
        return "standard"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "types": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Widget type names to look up, e.g. "
                        "['chart', 'definition-list']. Required and non-empty."
                    ),
                },
            },
            "required": ["types"],
        }

    async def execute(self, types: list[str] | str | None = None, **kwargs: Any) -> str:
        """Look the requested types up in the manifest.

        Mirrors ``sdk_mcp_widgets._get_widget_spec_handler`` so the SDK backend
        and every runtime backend answer the same question identically. Reuses
        the shared JSON-string coercion for the same reason it exists there:
        a model that cannot pass an array through a flat signature sends
        ``'["chart"]'``, and treating that as a single type name named a widget
        nobody has.
        """
        from pocketpaw.agents.mcp_arg_coercion import coerce_json_object_args
        from pocketpaw.config import get_settings
        from pocketpaw.ripple.manifest import format_for_prompt, get_manifest

        coerced = coerce_json_object_args({"types": types}, ("types",))
        raw = coerced.get("types") or []
        if isinstance(raw, str):
            raw = [raw]
        requested = [t for t in raw if isinstance(t, str) and t.strip()]
        if not requested:
            return self._error("Pass `types` as a non-empty array of widget type names.")

        settings = get_settings()
        manifest = await get_manifest(
            settings.ripple_manifest_url,
            ttl_seconds=settings.ripple_manifest_ttl_seconds,
        )
        if manifest is None:
            return self._error(
                "Ripple manifest unavailable — cannot verify prop schemas. "
                "Stick to core widgets (text, heading, stat, button, table, flex) "
                "rather than guessing props for anything else."
            )

        by_type = {w.get("type"): w for w in (manifest.get("widgets") or []) if w.get("type")}
        matched = [by_type[t] for t in requested if t in by_type]
        missing = [t for t in requested if t not in by_type]

        if not matched:
            # Naming the misses is the whole value here: it is how a stale
            # manifest pin becomes visible instead of producing a widget that
            # renders as nothing.
            return self._error(
                f"No such widget(s) in this deployment's manifest: {', '.join(missing)}. "
                "Do not emit them — they would render as nothing. Pick a widget the "
                "manifest carries."
            )

        block = format_for_prompt({"widgets": matched})
        if missing:
            block += f"\n\n_Not in this deployment's manifest, do not emit: {', '.join(missing)}_"
        return self._success(block)


class InlineWidgetHelpTool(BaseTool):
    """Return hand-written design guidance for the widgets that have it.

    Distinct from :class:`WidgetSpecTool`: this is composition and layout
    advice for 16 widgets, not a prop schema. A miss returns a short note
    pointing at ``get_widget_spec`` rather than the whole catalog.
    """

    @property
    def name(self) -> str:
        return "get_inline_widget_help"

    @property
    def description(self) -> str:
        return (
            "Get hand-written DESIGN guidance (layout, composition, visual "
            "variation) for chat-inline Ripple widgets. Covers a small set of "
            "high-traffic widgets; for a widget it does not cover it will say "
            "so and send you to `get_widget_spec`. Use this for 'how should "
            "this look' — use `get_widget_spec` for 'what props does this "
            "take'. Called with no `types` it returns the entire design "
            "rulebook, which is large: pass the types you actually need."
        )

    @property
    def trust_level(self) -> str:
        return "standard"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "types": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Widget kinds you plan to use, e.g. ['chart', 'kanban']. "
                        "Omit to get the full design rulebook (large — prefer "
                        "naming types)."
                    ),
                },
            },
        }

    async def execute(self, types: list[str] | str | None = None, **kwargs: Any) -> str:
        from pocketpaw.agents.mcp_arg_coercion import coerce_json_object_args
        from pocketpaw.ripple._inline_core import widget_help
        from pocketpaw.tools.output_budget import cap_tool_output

        coerced = coerce_json_object_args({"types": types}, ("types",))
        raw = coerced.get("types") or []
        if isinstance(raw, str):
            raw = [raw]
        if not isinstance(raw, list):
            raw = []
        body = widget_help([str(t) for t in raw])
        # NOT ``_success``. Its default 12,000-char cap sits BELOW a legitimate
        # single-widget payload — `chart` guidance is 14,699 chars — and a
        # head+tail slice of a prop schema is the guessed-prop-name failure this
        # tool exists to prevent, arriving by a different route. 20,000 clears
        # every measured per-type lookup untouched (largest: 14,699) while still
        # bounding the one call that can run away: `types=[]`, which is
        # documented as rare and returns the entire 58,765-char rulebook.
        return cap_tool_output(body, cap=20_000, tool_name=self.name)
