# _helpers.py — Shared helpers for surface handlers.
#
# Created: 2026-05-24 — Keep the per-preamble char cap in one place
# (1500 chars per turn — anything bigger eats too many tokens) and a
# handful of formatting helpers every handler shares (formatting
# Composio tool names for the ``<available-data-tools>`` line, the
# audit-snapshot lines, etc.). Pulling these out keeps each handler
# small (≤80 LOC) per the PR brief.
#
# Changes: 2026-08-02 (PA-2, feat/prompt-assembler-seam) — added the three
# cache-key builders every handler now needs to answer ``SurfacePreamble``'s
# ``cache_key``: ``meta_key`` (identity parts — the surface kind plus whatever
# the handler read off ``meta``), ``content_key`` (a digest of what the handler
# actually rendered, for the handlers that read a LIST and render all of it
# that matters) and ``source_key`` (a digest of the data that went IN, for the
# handlers whose render is capped and so cannot show every change). All three
# are prefixed with the surface kind so two surfaces that both render "nothing
# to report" can never share a key — a collision there would let a navigation
# between them go unnoticed by every backend keying on the digest.
#
# There is deliberately no "key on the document's ``updatedAt``" helper, which
# would otherwise be the obvious strongest option: see ``source_key`` for why
# that field cannot be trusted in this codebase today.

from __future__ import annotations

import hashlib
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


def meta_key(kind: str, *parts: Any) -> str:
    """Build a preamble cache key from identity parts.

    For handlers whose preamble is a function of things they can NAME: the
    surface kind, the ids it read off ``meta``, and — for a handler that loaded
    a document — that document's revision. ``None`` and ``""`` both render as
    ``-`` so "absent" is one value rather than two, and every part is coerced
    through ``str`` so a ``datetime`` revision needs no ceremony at the call
    site. The kind leads so no two surfaces can collide.

    Prefer this over :func:`content_key` whenever a revision is available: it
    moves even when the rendered text cannot, and the rendered text often
    cannot — handlers cap their lists and ``truncate_preamble`` cuts the tail.
    """
    rendered = [kind]
    for part in parts:
        rendered.append("-" if part is None or part == "" else str(part))
    return ":".join(rendered)


def content_key(kind: str, text: str) -> str:
    """Key a preamble on a digest of what the handler actually rendered.

    The answer for handlers that read a LIST — files, agents, pockets, KB
    scopes, work items — where there is no single revision to point at and
    per-item revisions would cost more than the preamble itself. It cannot lie
    in the dangerous direction: the digest cannot hold still while the text
    moves. It CAN move for a cosmetic change, which costs a backend a rebuild
    it did not strictly need, and it cannot see drift the render dropped
    (anything past the list limit or the char cap) — but what the render
    dropped is not in the prompt either, so no cached prompt is stale for it.

    Use :func:`source_key` instead when the handler's render is CAPPED in a way
    that hides real change from the reader — the pocket surface renders 12 of N
    widgets, so its 13th widget can be edited without moving a rendered byte.

    Bounded to 16 hex chars, the same width the assembler's digest uses.
    """
    return f"{kind}:c:{hashlib.sha256(text.encode('utf-8', 'replace')).hexdigest()[:16]}"


def source_key(kind: str, *parts: Any) -> str:
    """Key a preamble on a digest of the SOURCE the handler read.

    The strongest key available, and the one for handlers whose render is
    capped: it digests the data that WENT IN rather than the text that came
    out, so it sees the change a truncated render cannot show.

    It exists because the obvious alternative does not work here. A document's
    ``updatedAt`` would be the natural revision, and both the pocket service's
    comments and ``TimestampedDocument`` say it is bumped on every write — but
    it is NOT, and has not been since the beanie 2 upgrade: beanie's
    ``init_actions`` skips every ``_``-prefixed attribute when it collects
    event hooks, and the hooks are named ``_set_created`` / ``_set_updated``,
    so they are never registered and the timestamps keep their construction
    values forever. A key built on a field that never moves is precisely the
    failure this whole seam exists to prevent, so the handlers digest what they
    read instead. (The timestamp bug is real and wider than this module —
    anything keying on ``updatedAt`` is affected — but fixing it belongs in its
    own change, not in a prompt-layer task.)

    Parts are stringified and separated with ``\\x1f`` so two different splits
    of the same characters cannot collide, the same separator discipline the
    prompt assembler's digest uses.
    """
    h = hashlib.sha256()
    for part in parts:
        h.update(str(part).encode("utf-8", "replace"))
        h.update(b"\x1f")
    return f"{kind}:s:{h.hexdigest()[:16]}"


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
    "content_key",
    "format_widget_line",
    "meta_key",
    "source_key",
    "truncate_preamble",
]
