# _helpers.py — Shared helpers for surface handlers.
#
# Created: 2026-05-24 — Keep the per-preamble char cap in one place
# (1500 chars per turn — anything bigger eats too many tokens) and a
# handful of formatting helpers every handler shares (formatting
# Composio tool names for the ``<available-data-tools>`` line, the
# audit-snapshot lines, etc.). Pulling these out keeps each handler
# small (≤80 LOC) per the PR brief.
#
# Changes: 2026-08-02 (PA-2, feat/prompt-assembler-seam) — added the two
# cache-key builders every handler now needs to answer ``SurfacePreamble``'s
# ``cache_key``. The split is by what the handler READS, and there are only two
# kinds: ``content_key`` (a digest of what was rendered) for every handler that
# reads mutable state, and ``meta_key`` (name the inputs outright) for the ones
# whose preamble is a pure function of ``meta``. Both are prefixed with the
# surface kind so two surfaces that both render "nothing to report" can never
# share a key — a collision there would let a navigation between them go
# unnoticed by every backend keying on the digest.
#
# There is deliberately no "key on the document's ``updatedAt``" helper, which
# would otherwise be the obvious strongest option, and no "digest the source
# data" one either: ``content_key`` documents why each was rejected.

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

    Use this ONLY where the preamble is a pure function of the parts named —
    no I/O, nothing live read. Where anything mutable was read, the rendered
    digest (:func:`content_key`) is the honest key, because ``meta`` cannot see
    the data change underneath it.
    """
    rendered = [kind]
    for part in parts:
        rendered.append("-" if part is None or part == "" else str(part))
    return ":".join(rendered)


def content_key(kind: str, text: str) -> str:
    """Key a preamble on a digest of what the handler actually rendered.

    The answer for every handler that reads mutable state. It invalidates
    exactly when the agent's view of that state changes and never otherwise,
    which is the property that matters: an invalidation is not free — on the
    Claude SDK backend the system prompt is bound at ``connect``, so a moved
    key costs a ~12s reconnect — and re-rendering after an invisible change
    would hand the agent the bytes it already had.

    It cannot fail in the dangerous direction. The digest cannot hold still
    while the text moves, so a stale prompt cannot go unnoticed. What it does
    not see is change the render dropped (past a list limit, past the 1500-char
    cap), and that is correct rather than a gap: what the render dropped is not
    in the prompt, so no cached prompt is stale for it.

    Hashing the PREAMBLE is safe in a way that hashing the whole prompt is not,
    and the difference is worth stating because it looks like the thing this
    package refuses to do. The prompt carries the per-message soul recall,
    which changes every turn by construction, so a digest over it would churn
    for reasons unrelated to meaning. The surface preamble is a function of
    surface state alone — nothing in it is keyed on the user's message — so its
    digest moves only on real change.

    Two alternatives were tried and rejected while writing PA-2:

    * a document's ``updatedAt``. The natural revision, claimed by
      ``TimestampedDocument`` and by several services' comments to be bumped on
      every write. It is NOT: beanie 2's ``init_actions`` skips ``_``-prefixed
      attributes when it collects event hooks, and the hooks are named
      ``_set_created`` / ``_set_updated``, so they have never been registered
      and every cloud document's timestamps keep their construction values. A
      key on it reviews clean and reports every edit as "unchanged". (That bug
      is real and wider than this module — anything reading ``updatedAt`` is
      affected — and belongs in its own fix.)
    * a fingerprint of the source data. It fixes the above and over-corrects:
      it invalidates on changes the preamble cannot show, paying a reconnect
      for an identical prompt.

    Bounded to 16 hex chars, the same width the assembler's digest uses.
    """
    return f"{kind}:c:{hashlib.sha256(text.encode('utf-8', 'replace')).hexdigest()[:16]}"


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
    "truncate_preamble",
]
