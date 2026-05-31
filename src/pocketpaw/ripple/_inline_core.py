# pocketpaw/ripple/_inline_core.py — Catalog payload for get_inline_widget_help.
#
# The full RIPPLE_DESIGN_RULES text used to ride in every chat-inline
# system prompt. Most replies use 1-3 widgets, so 90%+ of those tokens
# were paid for nothing. This module owns the lookup payload that
# powers the on-demand `get_inline_widget_help` MCP tool.
#
# Modified: 2026-05-21 — widget_help is now a two-tier lookup
# (per-widget WIDGET_SHAPES first, section search second). Reworked
# from PR #1106.
#
# Lookup is two-tier:
#  1. Per-widget canonical shapes via WIDGET_SHAPES — preferred.
#     Asking for ["chart"] returns just the chart shape (~2k chars),
#     not the whole CANONICAL_SHAPES blob (~10k).
#  2. Section search across the rest of RIPPLE_DESIGN_RULES — for
#     niche rules (TABULAR_PICKER, ACTIVITY_PICKER, etc.) or widgets
#     not in WIDGET_SHAPES, we still split-by-heading and fuzzy-match.

from __future__ import annotations

from pocketpaw.ripple._design import (
    INTERACTIVE_STATE_RULE,
    OPTIONAL_DESIGN_SECTIONS,
    RIPPLE_DESIGN_RULES,
    WIDGET_SHAPES,
)


def widget_help(types: list[str] | None = None) -> str:
    """Return Ripple widget reference docs.

    With no args, returns the full RIPPLE_DESIGN_RULES (rare — agent
    only requests this when it needs the catalog overview). With
    specific types, returns sections matching those widget kinds.

    Resolution order for each requested type:
      1. WIDGET_SHAPES[type] — exact canonical shape, ~600–2400 chars.
      2. OPTIONAL_DESIGN_SECTIONS[type] — niche layout sections that
         aren't eagerly in RIPPLE_DESIGN_RULES (tabular-picker,
         activity-picker, visual-variation, etc.).
      3. Fall back to section search across RIPPLE_DESIGN_RULES.

    Errs toward returning too much rather than too little — the agent
    already paid the round-trip; better to over-deliver than leave it
    guessing.
    """
    if not types:
        return RIPPLE_DESIGN_RULES

    wanted = {t.strip().lower() for t in types if isinstance(t, str) and t.strip()}
    if not wanted:
        return RIPPLE_DESIGN_RULES

    parts: list[str] = []
    unresolved: set[str] = set()
    for t in wanted:
        if t in WIDGET_SHAPES:
            parts.append(WIDGET_SHAPES[t])
        elif t in OPTIONAL_DESIGN_SECTIONS:
            parts.append(OPTIONAL_DESIGN_SECTIONS[t])
        else:
            unresolved.add(t)

    if unresolved:
        # Section search across RIPPLE_DESIGN_RULES for any type we
        # couldn't resolve directly.
        sections = _split_sections(RIPPLE_DESIGN_RULES)
        for sect in sections:
            body = sect.lower()
            if any(t in body for t in unresolved):
                parts.append(sect)

    # Always tack the interactive-state rule on the end. Bindings,
    # {state.x} expressions, and action-chain vocabulary apply to every
    # widget — the agent rarely uses a widget without them, so shipping
    # the toolkit alongside any retrieval avoids a follow-up round-trip.
    if parts:
        parts.append(INTERACTIVE_STATE_RULE)
        return "\n\n".join(parts)
    return RIPPLE_DESIGN_RULES


def _split_sections(text: str) -> list[str]:
    """Split on top-level ('# ') headings only. '## ' subheadings are
    part of the section body and not split points — splitting on them
    fragments coherent sections like INTERACTIVE_STATE_RULE (which
    uses '##' subheadings for Toolkit, action vocabulary, etc.) into
    disconnected pieces.
    """
    lines = text.split("\n")
    sections: list[list[str]] = []
    current: list[str] = []
    for line in lines:
        # '# Foo' opens a new section; '## Foo' stays in the current.
        if line.startswith("# ") and not line.startswith("## "):
            if current:
                sections.append(current)
            current = [line]
        else:
            current.append(line)
    if current:
        sections.append(current)
    return ["\n".join(s) for s in sections if s]


__all__ = ["widget_help"]
