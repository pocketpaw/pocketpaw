# ee/pocketpaw_ee/sites/site_data/dashboard_spec.py — compiles one dashboard widget into
# the Ripple spec PE renders for its tile.
#
# A widget is never stored as Ripple: neither the model nor the owner writes a node. The
# server builds it here from a fixed template per viz, so a widget can only ever be one
# of seven allowlisted data components (stat, metric, chart, table, gauge, funnel, or a
# sparkline-type chart). There is no HTML, embed, link or action component to reach.
#
# Shape: ``{"state": {"data": <empty>}, "ui": <node>}``. The node's data props read
# ``{state.data}`` (``{state.data.value}`` for single values); PE sets ``state.data`` to
# the widget's ``WidgetResult.data`` and renders. Every literal string that comes from
# the widget (title, column headers) has ``{`` / ``}`` replaced so it can never be read
# as a Ripple expression, and a table drops any column whose NAME holds a brace (its
# accessor key would be an expression).
from __future__ import annotations

from typing import Any

from pocketpaw_ee.sites.site_data.dashboard_dto import WidgetMapping

_STAT_FORMATS = {"number": "number", "currency": "currency", "percent": "percent"}


def literal(text: Any, limit: int = 120) -> str:
    """``text`` as a plain string Ripple won't evaluate."""
    return str(text or "").replace("{", "(").replace("}", ")")[:limit]


def _empty(viz: str) -> Any:
    return {"value": None} if viz in ("metric", "stat", "gauge") else []


def compile_widget(
    viz: str,
    chart_type: str | None,
    title: str,
    mapping: WidgetMapping | None,
    columns: list[str] | None = None,
) -> dict[str, Any]:
    """The tile's spec. ``columns`` (the result's, when known) fill a table's headers
    when the mapping doesn't pick them."""
    m = mapping or WidgetMapping()
    label = literal(title)
    node: dict[str, Any]
    if viz == "stat":
        props: dict[str, Any] = {
            "label": label,
            "value": "{state.data.value}",
            "delta": "{state.data.delta}",
        }
        if m.format in _STAT_FORMATS:
            props["format"] = _STAT_FORMATS[m.format]
        if m.format == "currency" and m.currency:
            props["currency"] = m.currency
        node = {"type": "stat", "props": props}
    elif viz == "metric":
        node = {"type": "metric", "props": {"label": label, "value": "{state.data.value}"}}
    elif viz == "gauge":
        props = {"label": label, "value": "{state.data.value}", "max": "{state.data.max}"}
        if m.format == "currency" and m.currency:
            props["currency"] = m.currency
        node = {"type": "gauge", "props": props}
    elif viz == "funnel":
        node = {"type": "funnel", "props": {"title": label, "data": "{state.data}"}}
    elif viz == "table":
        names = [c for c in (m.columns or columns or []) if "{" not in c and "}" not in c]
        node = {
            "type": "table",
            "props": {
                "rows": "{state.data}",
                "columns": [{"header": literal(c, 80), "accessorKey": c} for c in names],
                "variant": "compact",
            },
        }
    else:  # chart, sparkline
        kind = "sparkline" if viz == "sparkline" else (chart_type or "bar")
        node = {"type": "chart", "props": {"type": kind, "title": label, "data": "{state.data}"}}
    return {"state": {"data": _empty(viz)}, "ui": node}
