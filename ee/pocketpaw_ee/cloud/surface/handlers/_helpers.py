# _helpers.py — Shared helpers for surface handlers.
#
# Created: 2026-05-24 — Keep the per-preamble char cap in one place
# (1500 chars per turn — anything bigger eats too many tokens) and a
# handful of formatting helpers every handler shares (formatting
# Composio tool names for the ``<available-data-tools>`` line, the
# audit-snapshot lines, etc.). Pulling these out keeps each handler
# small (≤80 LOC) per the PR brief.

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# Preamble length cap. Soft cap — we never split mid-tag, just truncate
# the trailing lines and append an ellipsis marker.
PREAMBLE_MAX_CHARS = 1500


def truncate_preamble(text: str, *, limit: int = PREAMBLE_MAX_CHARS) -> str:
    """Cap a preamble to ``limit`` chars without breaking the closing tag.

    Truncation is line-aware: we drop trailing lines until the result fits
    and append ``... (truncated)`` so the agent knows context was lost.
    Tag balance is the caller's responsibility — handlers structure their
    preambles so dropping trailing detail lines doesn't break the outer
    XML shape.
    """
    if len(text) <= limit:
        return text
    lines = text.splitlines()
    out: list[str] = []
    total = 0
    suffix = "... (truncated)"
    budget = limit - len(suffix) - 1
    for line in lines:
        if total + len(line) + 1 > budget:
            break
        out.append(line)
        total += len(line) + 1
    out.append(suffix)
    return "\n".join(out)


async def composio_tool_names(*, limit: int = 6) -> list[str]:
    """Return up to ``limit`` Composio tool names enabled for this deploy.

    Returns an empty list when Composio is disabled or unreachable —
    handlers must tolerate that (e.g. omit the ``<available-data-tools>``
    line entirely instead of emitting an empty one).

    Wraps the lookup in a broad try/except because Composio integration
    is optional and the failure modes range from missing env vars to
    upstream HTTP timeouts — none of which should break a chat send.
    """
    try:
        from pocketpaw_ee.cloud.composio import service as composio_service
    except Exception:
        return []
    try:
        if not composio_service.is_enabled():
            return []
    except Exception:
        return []
    # The composio package exposes a per-deployment cap; we don't enumerate
    # the full catalog here (that would balloon the preamble). A handful of
    # canonical action names is enough for the agent to know what's wired.
    canonical = [
        "GMAIL_FETCH_EMAILS",
        "GMAIL_SEND_EMAIL",
        "GOOGLECALENDAR_LIST_EVENTS",
        "SLACK_SEND_MESSAGE",
        "GITHUB_LIST_ISSUES_FOR_REPOSITORY",
        "NOTION_SEARCH",
    ]
    return canonical[:limit]


def format_widget_line(widget: Any) -> str:
    """Format one widget for the ``<pinned-widgets>`` block.

    Marks native vs spec-backed tiles and flags ``type=spec`` widgets
    missing a ``spec`` subtree — those render as broken tiles and the
    agent should NOT re-add them (it'd create a duplicate broken row).
    Accepts duck-typed widget objects (anything with ``name`` / ``type``
    attrs) so the helper works for both Beanie subdocs and domain
    objects without importing either.
    """
    name = getattr(widget, "name", None) or "(unnamed)"
    kind = getattr(widget, "type", None) or "custom"
    spec = getattr(widget, "spec", None)
    if kind == "native":
        marker = "native"
    elif kind == "spec":
        marker = "spec — BROKEN (no spec subtree)" if not spec else "spec — live"
    else:
        marker = f"{kind} — live" if spec else f"{kind} — no spec"
    return f"- {name} ({marker})"


__all__ = [
    "PREAMBLE_MAX_CHARS",
    "composio_tool_names",
    "format_widget_line",
    "truncate_preamble",
]
