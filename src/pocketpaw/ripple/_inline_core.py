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
# Modified: 2026-08-04 (fix/prompt-tells-the-truth) — A LOOKUP MISS NO
# LONGER RETURNS THE WHOLE CATALOG. This module knows 16 widgets; the
# manifest carries 150+. Asking about any of the other 130-odd fell
# through both tiers and returned RIPPLE_DESIGN_RULES entire — 58,765
# characters, larger than the whole system prompt, and never once
# mentioning the widget that was asked about. The inline prompt drives
# the agent here for exactly the widgets this catalog does not cover, so
# the miss path WAS the common path. It now returns a short note that
# names the miss and routes to `get_widget_spec`, which reads the
# manifest and answers the same question in ~700-1,400 chars.
#
# Lookup is two-tier:
#  1. Per-widget canonical shapes via WIDGET_SHAPES — preferred.
#     Asking for ["chart"] returns just the chart shape (~2k chars),
#     not the whole CANONICAL_SHAPES blob (~10k).
#  2. Section search across the rest of RIPPLE_DESIGN_RULES — for
#     niche rules (TABULAR_PICKER, ACTIVITY_PICKER, etc.) or widgets
#     not in WIDGET_SHAPES, we still split-by-heading and fuzzy-match.
#  3. Anything still unresolved gets `_unknown_types_note` — never the
#     full blob.

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
      3. Section search across RIPPLE_DESIGN_RULES.
      4. Still nothing — say so, and point at ``get_widget_spec``.

    Step 4 is the important one. This catalog is hand-written design
    guidance for a *small* set of widgets; the manifest is the source of
    truth for prop schemas and covers an order of magnitude more. A type
    this module has never heard of is not a reason to hand back every
    word it knows — that buries the answer in ~59k characters and still
    doesn't contain it. An honest miss is shorter AND more useful,
    because it names the tool that can answer.
    """
    if not types:
        return RIPPLE_DESIGN_RULES

    wanted = {t.strip().lower() for t in types if isinstance(t, str) and t.strip()}
    if not wanted:
        return RIPPLE_DESIGN_RULES

    parts: list[str] = []
    unresolved: set[str] = set()
    for t in sorted(wanted):
        if t in WIDGET_SHAPES:
            parts.append(WIDGET_SHAPES[t])
        elif t in OPTIONAL_DESIGN_SECTIONS:
            parts.append(OPTIONAL_DESIGN_SECTIONS[t])
        else:
            unresolved.add(t)

    if unresolved:
        # Section search across RIPPLE_DESIGN_RULES for any type we
        # couldn't resolve directly. Two changes from the original, both
        # for the same reason — the old search answered the wrong
        # question:
        #
        #  * It matched the type ANYWHERE in a section body, so every
        #    lookup hit `# WIDGET CATALOG` (a bare list of names) and
        #    `# CANONICAL SHAPES` (13,479 chars covering every widget).
        #    Asking about `sparkline` returned 40,177 characters, none of
        #    which was the sparkline prop schema. That is the documented
        #    failure this catalog's caller was built to prevent — the
        #    inline prompt cites `definition-list` shipping with
        #    `description` instead of `definition`, and a lookup for
        #    `definition-list` returned exactly two sections that merely
        #    NAME it. The agent paid the round-trip and still guessed.
        #    A section now has to name the type in its HEADING to count
        #    as being about it.
        #  * It could not tell "a section matched" from "nothing
        #    matched", so a miss was indistinguishable from a hit.
        for sect in _split_sections(RIPPLE_DESIGN_RULES):
            heading = sect.split("\n", 1)[0].lower().replace("_", "-")
            hits = {t for t in unresolved if t in heading}
            if hits:
                parts.append(sect)
                unresolved -= hits

    # Always tack the interactive-state rule on the end. Bindings,
    # {state.x} expressions, and action-chain vocabulary apply to every
    # widget — the agent rarely uses a widget without them, so shipping
    # the toolkit alongside any retrieval avoids a follow-up round-trip.
    if parts:
        parts.append(INTERACTIVE_STATE_RULE)
        if unresolved:
            # A partial hit still owes the caller the truth about the
            # half that missed, or it reads as full coverage.
            parts.append(_unknown_types_note(unresolved))
        return "\n\n".join(parts)
    return _unknown_types_note(wanted)


def _unknown_types_note(types: set[str]) -> str:
    """Report a lookup miss and route to the tool that can answer it.

    Deliberately short. The agent asked a question this catalog cannot
    answer; the useful reply is "not here, ask over there", not a
    document dump. The closing line makes a stale manifest pin
    self-diagnosing: if ``get_widget_spec`` also reports the type
    unknown, the deployment's manifest genuinely lacks that widget and
    emitting it would render nothing.
    """
    asked = ", ".join(sorted(types))
    known = ", ".join(sorted(set(WIDGET_SHAPES) | set(OPTIONAL_DESIGN_SECTIONS)))
    return (
        f"# NO INLINE DESIGN GUIDANCE FOR: {asked}\n"
        "\n"
        "This catalog carries hand-written *design* guidance for a small set of "
        f"widgets only:\n{known}\n"
        "\n"
        "That is not the full widget list — it is the subset with extra design "
        "notes. For every other widget, call `get_widget_spec(types=[...])`: it "
        "reads the Ripple manifest and returns the canonical prop schema, which "
        "is what you need to author a spec.\n"
        "\n"
        "If `get_widget_spec` ALSO reports the type as unknown, this deployment's "
        "manifest does not carry that widget — it would render as nothing. Pick a "
        "widget the manifest does have."
    )


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
