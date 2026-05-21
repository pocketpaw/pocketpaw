# pocketpaw/ripple/_inline_core.py — Catalog payload for get_inline_widget_help.
#
# The full RIPPLE_DESIGN_RULES text used to ride in every chat-inline
# system prompt. Most replies use 1-3 widgets, so 90%+ of those tokens
# were paid for nothing. This module owns the lookup payload that
# powers the on-demand `get_inline_widget_help` MCP tool.

from __future__ import annotations

from pocketpaw.ripple._design import RIPPLE_DESIGN_RULES


def widget_help(types: list[str] | None = None) -> str:
    """Return Ripple widget reference docs.

    With no args, returns the full RIPPLE_DESIGN_RULES (rare — agent
    only requests this when it needs the catalog overview). With
    specific types, returns sections matching those widget kinds.

    Section matching is naive substring search on the existing rules
    text. It deliberately errs toward returning too much rather than
    too little — the agent already paid the round-trip; better to
    over-deliver than to leave it guessing.
    """
    if not types:
        return RIPPLE_DESIGN_RULES

    wanted = {t.strip().lower() for t in types if isinstance(t, str) and t.strip()}
    if not wanted:
        return RIPPLE_DESIGN_RULES

    # Split RIPPLE_DESIGN_RULES into top-level sections at "# " headings,
    # return any section whose body mentions a wanted type. Always include
    # the section carrying the toolkit / expression-language vocabulary —
    # the agent rarely uses widgets without bindings.
    sections = _split_sections(RIPPLE_DESIGN_RULES)
    keep: list[str] = []
    for sect in sections:
        body = sect.lower()
        # Always keep the section that carries the toolkit /
        # expression-language vocabulary — agents need handler syntax
        # and binding shape for any widget with on_click or {state.x}.
        always_keep = "toolkit" in body or "expression language" in body
        if always_keep or any(t in body for t in wanted):
            keep.append(sect)
    return "\n\n".join(keep) if keep else RIPPLE_DESIGN_RULES


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
