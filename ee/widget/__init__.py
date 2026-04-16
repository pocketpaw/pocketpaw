# ee/widget/__init__.py — Widget graduation + co-occurrence detection as
# a journal projection.
# Created: 2026-04-16 (feat/widget-journal-projection) — Wave 3 / Org
# Architecture RFC, Phase 3. Supersedes the side-channel designs in held
# PRs #941 (widget graduation engine reading
# ``~/.pocketpaw/widget-interactions.jsonl``) and #942 (co-occurrence
# detector stacked on that same JSONL, shipped with a
# ``sorted(tokens[:6])`` bug that broke dedup for any query longer than
# six tokens). The org journal becomes the source of truth; the JSONL
# file is retired; and the ``sorted(tokens)[:6]`` fix ships with the
# projection itself — the signature is re-derived from widget pair on
# replay so out-of-band emitters with the old bug can't poison state.
#
# What we re-export: the store (write path), the projection (read
# path), the policy (graduation + co-occurrence decisions), plus the
# canonical action names and payload builders for out-of-band emitters.

from ee.widget.events import (
    ACTION_WIDGET_COOCCURRENCE_DETECTED,
    ACTION_WIDGET_GRADUATED,
    ACTION_WIDGET_INTERACTION_RECORDED,
    ALL_WIDGET_ACTIONS,
    SIGNATURE_MAX_TOKENS,
    cooccurrence_signature,
    normalise_signature_tokens,
    widget_cooccurrence_payload,
    widget_graduated_payload,
    widget_interaction_payload,
)
from ee.widget.policy import (
    DEFAULT_ARCHIVE_DAYS,
    DEFAULT_COOCCURRENCE_THRESHOLD,
    DEFAULT_COOCCURRENCE_WINDOW_DAYS,
    DEFAULT_PIN_THRESHOLD,
    DEFAULT_SESSION_GAP_SECONDS,
    DEFAULT_WINDOW_DAYS,
    CooccurrenceCandidate,
    CooccurrenceReport,
    WidgetGraduationDecision,
    WidgetGraduationReport,
    WidgetTier,
    apply_cooccurrences,
    apply_widget_graduations,
    scan_for_cooccurrences,
    scan_for_widget_graduations,
)
from ee.widget.projection import (
    CooccurrenceProjection,
    CooccurrenceRow,
    GraduationStateProjection,
    GraduationStateRow,
    WidgetInteractionView,
    WidgetProjection,
    WidgetUsageProjection,
    WidgetUsageRow,
)
from ee.widget.store import WidgetJournalStore

__all__ = [
    # Actions + payload builders.
    "ACTION_WIDGET_INTERACTION_RECORDED",
    "ACTION_WIDGET_GRADUATED",
    "ACTION_WIDGET_COOCCURRENCE_DETECTED",
    "ALL_WIDGET_ACTIONS",
    "SIGNATURE_MAX_TOKENS",
    "cooccurrence_signature",
    "normalise_signature_tokens",
    "widget_interaction_payload",
    "widget_graduated_payload",
    "widget_cooccurrence_payload",
    # Write path.
    "WidgetJournalStore",
    # Read path.
    "WidgetProjection",
    "WidgetUsageProjection",
    "CooccurrenceProjection",
    "GraduationStateProjection",
    "WidgetInteractionView",
    "WidgetUsageRow",
    "CooccurrenceRow",
    "GraduationStateRow",
    # Policy.
    "WidgetTier",
    "WidgetGraduationDecision",
    "WidgetGraduationReport",
    "CooccurrenceCandidate",
    "CooccurrenceReport",
    "scan_for_widget_graduations",
    "scan_for_cooccurrences",
    "apply_widget_graduations",
    "apply_cooccurrences",
    "DEFAULT_WINDOW_DAYS",
    "DEFAULT_PIN_THRESHOLD",
    "DEFAULT_ARCHIVE_DAYS",
    "DEFAULT_COOCCURRENCE_THRESHOLD",
    "DEFAULT_COOCCURRENCE_WINDOW_DAYS",
    "DEFAULT_SESSION_GAP_SECONDS",
]
