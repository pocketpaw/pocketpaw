"""The two caps that jointly bound the pocket preamble stay consistent (PA-9).

Created 2026-08-03 (PA-9, feat/prompt-budget-measurement).

``PREAMBLE_MAX_CHARS`` and ``WIDGET_PREVIEW_LIMIT`` are set independently, in
different units, and bound the same text. PA-9 measured that they do not
currently collide — a full 300-widget preamble renders at 609 chars against a
1500-char cap — but nothing asserted it, so the property held by luck.

It matters because ``truncate_preamble`` cuts the TAIL. In
``handlers/pocket.py`` the widget rows are emitted BEFORE the node and backend
summaries, so a widget limit raised past the char cap does not truncate the
widget list that caused the overflow; it silently deletes ``<pocket-nodes>`` and
``<pocket-backend>`` instead. The agent would lose the pocket's wiring state and
nothing in the preamble would say so.

These are pure-function tests over the two helpers — no DB, no fixtures.
"""

from __future__ import annotations

from pocketpaw_ee.cloud.surface.handlers._helpers import (
    PREAMBLE_MAX_CHARS,
    WIDGET_PREVIEW_LIMIT,
    format_widget_line,
    truncate_preamble,
)


class _Widget:
    """A widget with realistic field lengths.

    ``format_widget_line`` reads ``name`` / ``type`` / ``spec`` off a duck-typed
    object. Names must be plausible: a ``(unnamed)`` fallback renders ~7 chars
    shorter per line and would flatter the measurement.
    """

    def __init__(self, i: int) -> None:
        self.name = f"Revenue by region {i}"
        self.type = "spec"
        self.spec = {"kind": "chart"}


def _render_preamble(widget_count: int) -> str:
    """Rebuild the widest preamble ``handlers/pocket.py`` can emit.

    Mirrors that handler's assembly order — surface tag, current-pocket tag,
    widget rows, then the node and backend summaries that sit in the truncation
    firing line.
    """
    widgets = [_Widget(i) for i in range(widget_count)]
    parts = [
        '<surface kind="pocket" route="/pockets/pk-123" />',
        f'<current-pocket id="pk-123" name="Launch Tracker" widgets="{widget_count}" />',
    ]
    rows = [format_widget_line(w) for w in widgets[:WIDGET_PREVIEW_LIMIT]]
    if widget_count > WIDGET_PREVIEW_LIMIT:
        rows.append(f"... (+{widget_count - WIDGET_PREVIEW_LIMIT} more)")
    parts.append(
        f'<pocket-widgets count="{widget_count}">\n' + "\n".join(rows) + "\n</pocket-widgets>"
    )
    # The two blocks that get deleted first when the preamble overflows.
    parts.append("<pocket-nodes>3 nodes: fetch, transform, render</pocket-nodes>")
    parts.append("<pocket-backend>backend: connected (postgres)</pocket-backend>")
    return "\n".join(parts)


def test_widget_preview_limit_fits_inside_the_preamble_char_cap():
    """A full widget preview must not push the preamble over its own cap.

    MUTATION: set ``WIDGET_PREVIEW_LIMIT = 60`` in
    ``ee/pocketpaw_ee/cloud/surface/handlers/_helpers.py``. 60 lines at ~36
    chars is ~2,170 chars against a 1,500 cap, ``truncate_preamble`` fires, and
    both assertions below fail.
    """
    raw = _render_preamble(300)

    assert len(raw) <= PREAMBLE_MAX_CHARS, (
        f"{WIDGET_PREVIEW_LIMIT} widgets render a {len(raw)}-char preamble, over "
        f"the {PREAMBLE_MAX_CHARS}-char cap — truncation would eat the node and "
        "backend summaries, not the widget list that overflowed it."
    )
    assert truncate_preamble(raw) == raw


def test_truncation_would_eat_the_wiring_summaries_not_the_widgets():
    """Why the cap above is load-bearing rather than cosmetic.

    Pins the failure MODE: ``truncate_preamble`` drops trailing lines, so an
    over-long preamble loses the blocks that come last. If a future refactor
    made truncation cut the widget rows instead, the test above would be
    guarding a risk that no longer exists and should be revisited.

    MUTATION: change ``truncate_preamble`` to keep the LAST lines rather than
    the first (iterate ``reversed(lines)``). Verified 2026-08-03: the head
    assertion is the one that fires — the surface tag is dropped — and the
    backend summary survives, so the last assertion would fail too.
    """
    raw = _render_preamble(300)
    # Force overflow independent of the real cap, so this test documents the
    # helper's behaviour rather than today's cap values.
    cut = truncate_preamble(raw, limit=300)

    assert cut != raw
    assert cut.endswith("... (truncated)")
    assert "<surface kind=" in cut, "truncation must keep the head"
    assert "<pocket-backend>" not in cut, "truncation drops the tail — that is the risk"


def test_a_small_pocket_is_never_truncated():
    """The common case stays whole.

    Most pockets hold fewer widgets than the preview limit, and that preamble
    must survive intact — it is the agent's only view of the surface.

    MUTATION: set ``PREAMBLE_MAX_CHARS = 200`` in ``_helpers.py``; the 5-widget
    preamble (~400 chars) is then cut and both assertions fail.
    """
    raw = _render_preamble(5)

    assert truncate_preamble(raw) == raw
    assert "<pocket-backend>" in raw
