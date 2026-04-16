# ee/widget/events.py — Canonical event payloads for the widget journal projection.
# Created: 2026-04-16 (feat/widget-journal-projection) — Wave 3 / Org
# Architecture RFC, Phase 3. Carries the intent of held PRs #941 (widget
# graduation engine reading a JSONL interaction log) and #942 (co-occurrence
# detector stacked on that log) onto the org journal. Both shipped as
# side-channel files at ``~/.pocketpaw/widget-interactions.jsonl``; this
# module retires the file and re-lands the same domain as a projection
# over three new action names:
#
#   - ``widget.interaction.recorded`` — one per user touch on a widget
#     (open / edit / click / dismiss / remove). Supersedes #941's
#     WidgetInteraction JSONL append.
#   - ``widget.graduated`` — one per pin / fade / archive decision the
#     policy fires. Supersedes #941's WidgetDecision apply path.
#   - ``widget.cooccurrence.detected`` — one per co-occurring-pair
#     signature that crossed threshold. Supersedes #942's PatternMatch
#     output. Critically: the signature on this payload uses
#     ``sorted(tokens)[:6]`` (sort FIRST, then truncate) — #942 shipped
#     ``sorted(tokens[:6])`` which truncates before sorting and breaks
#     dedup correctness across any query longer than 6 tokens. See
#     cooccurrence_signature below.
#
# Action namespace extensions are allowed per soul-protocol's v0.3.1
# catalog policy (custom namespaces are permitted for domain extensions
# that don't conflict with reserved prefixes). Pinned as constants so the
# projection + policy + store + tests all reach them through one import.
#
# Scope lives on ``EventEntry.scope`` (the journal column), never inside
# the payload. Same rule as ee/fabric/events.py and ee/retrieval/events.py
# — scope is the journal's canonical filter and duplicating it invites
# drift.

from __future__ import annotations

import re
from typing import Any

# ---------------------------------------------------------------------------
# Action names — pocketpaw-specific extensions of soul-protocol's action
# catalog. Kept stable; renaming requires a migration event on every
# existing journal since the projection can no longer replay the old
# events.
# ---------------------------------------------------------------------------

ACTION_WIDGET_INTERACTION_RECORDED = "widget.interaction.recorded"
ACTION_WIDGET_GRADUATED = "widget.graduated"
ACTION_WIDGET_COOCCURRENCE_DETECTED = "widget.cooccurrence.detected"

WIDGET_ACTION_PREFIX = "widget."

ALL_WIDGET_ACTIONS = (
    ACTION_WIDGET_INTERACTION_RECORDED,
    ACTION_WIDGET_GRADUATED,
    ACTION_WIDGET_COOCCURRENCE_DETECTED,
)


# ---------------------------------------------------------------------------
# Token regex — carried verbatim from #942's detector so the signatures
# the projection emits collide with anything a callsite computed out of
# band before the projection takes over the writing.
# ---------------------------------------------------------------------------

_TOKEN_RE = re.compile(r"[a-z0-9]+")

# Max tokens retained inside one signature. #942 used 6; keep the same
# magic number so existing test fixtures and fixtures in downstream
# tooling don't have to be reflowed.
SIGNATURE_MAX_TOKENS = 6


def normalise_signature_tokens(text: str) -> list[str]:
    """Lowercase + alnum-tokenise + sort + cap at SIGNATURE_MAX_TOKENS.

    This is the CORRECT order — sort first, then truncate. #942's
    ``sorted(tokens[:6])`` truncated before sorting, which meant any
    query longer than 6 tokens produced a signature whose sort order
    depended on which 6 raw tokens happened to survive the slice.
    The dedup guarantee the PR advertised ("word-order variants
    collapse") fell apart for longer queries:
    ``"d c b a e f g"`` and ``"a b c d e f g"`` both tokenise to the
    same bag but ``sorted(tokens[:6])`` produced
    ``['a', 'b', 'c', 'd', 'e', 'f']`` and
    ``['b', 'c', 'd', 'e', 'f', 'g']`` — two different signatures for
    the same semantic query.

    Fix: sort the full token list first, THEN slice — the resulting
    prefix is stable across rotations of the input tokens, so dedup
    works as the PR claimed.
    """

    tokens = _TOKEN_RE.findall(text.lower())
    return sorted(tokens)[:SIGNATURE_MAX_TOKENS]


def cooccurrence_signature(text_a: str, text_b: str) -> str:
    """Stable signature for a co-occurring query pair.

    Two queries collapse into one signature when their sorted-token
    bags are equal up to the prefix cap. ``"::"`` is kept as the join
    separator so the resulting string is stable, printable, and easy
    to eyeball in logs.
    """

    a = " ".join(normalise_signature_tokens(text_a))
    b = " ".join(normalise_signature_tokens(text_b))
    if not a or not b or a == b:
        return ""
    # Order the two bags so (A,B) and (B,A) collide.
    lo, hi = sorted((a, b))
    return f"{lo}::{hi}"


# ---------------------------------------------------------------------------
# Payload builders — small module-level functions, same pattern as
# ee/fabric/events.py and ee/retrieval/events.py. Keep them boring so
# migration helpers, external emitters, and the projection all produce
# identical dicts.
# ---------------------------------------------------------------------------


def widget_interaction_payload(
    *,
    widget_name: str,
    surface: str = "dashboard",
    action_type: str = "open",
    pocket_id: str | None = None,
    metadata: dict[str, Any] | None = None,
    query_text: str | None = None,
) -> dict[str, Any]:
    """Payload for ``widget.interaction.recorded`` events.

    ``widget_name`` is the stable identifier the UI emits (``metrics_chart``,
    ``leads_table``, etc.). ``surface`` differentiates which shell the
    interaction happened on (dashboard, telegram, slack). ``action_type``
    mirrors #941's WidgetAction set — open / edit / click / dismiss /
    remove — but stays as a free-form str here because the journal payload
    should survive a vocabulary change without a migration.

    ``query_text`` is optional; when present it lets the co-occurrence
    projection build signatures straight off the interaction payload
    without cross-referencing the retrieval log. Callers wiring widget
    interactions to retrievals should set this from the originating
    query.
    """

    return {
        "widget_name": widget_name,
        "surface": surface,
        "action_type": action_type,
        "pocket_id": pocket_id,
        "metadata": dict(metadata or {}),
        "query_text": query_text,
    }


def widget_graduated_payload(
    *,
    widget_name: str,
    surface: str,
    tier: str,
    confidence: float,
    interactions_in_window: int,
    window_days: int,
    previous_tier: str | None = None,
    pocket_id: str | None = None,
    reason: str = "",
) -> dict[str, Any]:
    """Payload for ``widget.graduated`` events.

    Shape matches #941's WidgetDecision field-for-field so downstream
    consumers (the paw-enterprise SuggestedWidgetsFeed UI in issue #74,
    for instance) don't need a new adapter. ``tier`` carries the verdict
    (``pin`` / ``fade`` / ``archive``) in the same naming scheme as the
    original WidgetVerdict enum. ``previous_tier`` is optional since
    first-time graduations have no prior state.
    """

    return {
        "widget_name": widget_name,
        "surface": surface,
        "tier": tier,
        "confidence": float(confidence),
        "interactions_in_window": int(interactions_in_window),
        "window_days": int(window_days),
        "previous_tier": previous_tier,
        "pocket_id": pocket_id,
        "reason": reason,
    }


def widget_cooccurrence_payload(
    *,
    widget_a: str,
    widget_b: str,
    count: int,
    window_s: int,
    signature: str,
    pocket_id: str | None = None,
    example_queries: list[str] | None = None,
) -> dict[str, Any]:
    """Payload for ``widget.cooccurrence.detected`` events.

    ``signature`` is the output of :func:`cooccurrence_signature` — the
    projection re-derives it on replay from the raw widget pair so a
    caller that computed the signature wrong (or used the #942 bug
    ordering) can't poison the projection state. ``window_s`` is the
    session window in seconds for auditability (#942 used a 15-minute
    gap; keeping it numeric leaves room for per-pocket tuning).
    """

    return {
        "widget_a": widget_a,
        "widget_b": widget_b,
        "count": int(count),
        "window_s": int(window_s),
        "signature": signature,
        "pocket_id": pocket_id,
        "example_queries": list(example_queries or []),
    }
